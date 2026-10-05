"""Tests for scope report in sprints preflight and close."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from plane_proj import config as config_module
from plane_proj import sprints
from plane_proj.cli import Context, cli
from plane_proj.delivery_plan import DeliveryPlan, TouchedPath, render_section
from plane_proj.guards import ConfigError
from tests.conftest import Card


def init_git_repo(path: Path, branch: str = "main") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", branch], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=path, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path, check=True, capture_output=True,
    )
    return path


def commit_files(
    repo: Path,
    files: dict[str, str],
    message: str,
    *,
    trailers: list[tuple[str, str]] | None = None,
    committer_date: str | None = None,
) -> str:
    for rel_path, content in files.items():
        file_path = repo / rel_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        subprocess.run(["git", "add", rel_path], cwd=repo, check=True, capture_output=True)
    full_message = message
    if trailers:
        full_message += "\n\n" + "\n".join(f"{k}: {v}" for k, v in trailers)
    env = dict(os.environ)
    if committer_date:
        env["GIT_COMMITTER_DATE"] = committer_date
        env["GIT_AUTHOR_DATE"] = committer_date
    subprocess.run(
        ["git", "commit", "-m", full_message],
        cwd=repo, check=True, capture_output=True, env=env,
    )
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True,
    )
    return proc.stdout.strip()


def test_config_accepts_and_validates_integration_branch(tmp_path: Path):
    """pytest shows the config accepts defaults.integration_branch and rejects

    a non-string value.
    """
    conf_file = tmp_path / "plane-proj.json"

    # Valid string integration_branch
    conf_file.write_text(
        json.dumps({
            "defaults": {
                "workspace": "w", "project": "DEMO", "integration_branch": "develop",
            },
        }),
        encoding="utf-8",
    )
    cfg = config_module.load_config(conf_file)
    assert cfg.integration_branch == "develop"

    # Non-string value rejected
    conf_file.write_text(
        json.dumps({
            "defaults": {
                "workspace": "w", "project": "DEMO", "integration_branch": 123,
            },
        }),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as exc_info:
        config_module.load_config(conf_file)
    assert "defaults.integration_branch must be a nonempty string" in str(exc_info.value)

    # Empty string rejected
    conf_file.write_text(
        json.dumps({
            "defaults": {
                "workspace": "w", "project": "DEMO", "integration_branch": "  ",
            },
        }),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as exc_info:
        config_module.load_config(conf_file)
    assert "defaults.integration_branch must be a nonempty string" in str(exc_info.value)


class MockSprintBoard:
    def __init__(
        self,
        cards: list[Card],
        integration_branch: str | None = "main",
        path: Path | None = None,
    ):
        self.project = SimpleNamespace(
            key="DEMO",
            states={"In Progress": "state-progress", "Done": "state-done"},
            estimates_enabled=True,
        )
        self.config = SimpleNamespace(integration_branch=integration_branch, path=path)
        self.cards = cards
        self.closed: list[tuple[str, str]] = []

    def cycle_cards(self, cycle_id: str):
        return self.cards

    def sprint_cycle_metrics(self, cycle_id: str):
        return {
            "cards_current": len(self.cards),
            "points_current": len(self.cards) * 2,
            "cards_done": len(self.cards),
            "points_done": len(self.cards) * 2,
            "cards_cancelled": 0,
            "points_cancelled": 0,
        }

    def close_sprint_cycle(self, cycle_id: str, ended: str):
        self.closed.append((cycle_id, ended))
        return f"Sprint {cycle_id}"


def setup_sprint_repo_and_database(
    tmp_path: Path, monkeypatch,
) -> tuple[Path, Path, str, str, str]:
    repo = init_git_repo(tmp_path / "repo", branch="main")
    monkeypatch.chdir(repo)
    conf_file = repo / "plane" / "plane-proj.json"
    conf_file.parent.mkdir(parents=True, exist_ok=True)
    conf_file.write_text(
        json.dumps({
            "defaults": {
                "workspace": "test", "project": "DEMO", "integration_branch": "main",
            },
            "estimate_points": {"1": "uuid-1"},
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("PLANE_PROJ_CONFIG", str(conf_file))

    database = repo / "SPRINTS.sqlite"
    sprints.create_database(database, ("https://plane.test", "test", "DEMO"))

    # Plan and start sprint 3
    sprint_start = "2026-01-02T03:00:00+00:00"
    with sprints.connect_database(database, writable=True) as connection:
        sprints.plan_sprint(connection, 3, "Sprint 3", 1, "Goal", "Exec", ("Accept",))
        sprints.start_sprint(connection, 3, sprint_start, "3", 2, 4)

    # Card 12 has commit on main touching src/allowed.py and out-of-scope src/extra.py
    rev_12 = commit_files(
        repo,
        {"src/allowed.py": "x = 1\n", "src/extra.py": "out = 1\n"},
        "Implement DEMO-12",
        trailers=[("Card", "DEMO-12")],
        committer_date="2026-01-02T03:15:00+00:00",
    )

    # An unnamed commit on main made while sprint ran (no Card: trailer)
    unnamed_rev = commit_files(
        repo,
        {"docs/notes.md": "notes\n"},
        "Chore docs",
        committer_date="2026-01-02T03:30:00+00:00",
    )

    # Card 13 commit on a side feature branch not merged into main
    subprocess.run(
        ["git", "checkout", "-b", "feature-13"], cwd=repo, check=True, capture_output=True,
    )
    rev_13 = commit_files(
        repo,
        {"src/feat.py": "feat = 1\n"},
        "Implement DEMO-13",
        trailers=[("Card", "DEMO-13")],
        committer_date="2026-01-02T03:45:00+00:00",
    )
    subprocess.run(["git", "checkout", "main"], cwd=repo, check=True, capture_output=True)

    return repo, conf_file, database, rev_12, unnamed_rev, rev_13


def test_scope_report_lists_findings_in_text_and_json_and_preflight_exits_as_before(
    tmp_path: Path, monkeypatch,
):
    """pytest against a temporary git repository shows the report lists an

    out-of-scope path, an unmerged card and an unnamed commit, in text and
    --json, and preflight exits as before when the report has findings.
    """
    repo, conf_file, database, rev_12, unnamed_rev, rev_13 = setup_sprint_repo_and_database(
        tmp_path, monkeypatch,
    )

    card_12_plan = DeliveryPlan(
        touches=(TouchedPath("src/allowed.py"),),
        dependencies_assessed=True,
    )
    card_13_plan = DeliveryPlan(
        touches=(TouchedPath("src/feat.py"),),
        dependencies_assessed=True,
    )

    card_12 = Card(
        id="card-12",
        sequence_id=12,
        state="state-done",
        description_html=render_section(card_12_plan),
    )
    card_13 = Card(
        id="card-13",
        sequence_id=13,
        state="state-done",
        description_html=render_section(card_13_plan),
    )

    board = MockSprintBoard(
        cards=[card_12, card_13], integration_branch="main", path=conf_file,
    )
    monkeypatch.setattr(Context, "board", property(lambda self: board))

    # Add execution snapshots so preflight would otherwise be ready/clean
    as_of = datetime.now(UTC).isoformat(timespec="seconds")
    with sprints.connect_database(database, writable=True) as connection:
        sprints.record_execution_snapshot(
            connection,
            sprint_id=3,
            work_item_id="card-12",
            card_reference="DEMO-12",
            captured_at=as_of,
            is_final=True,
            stats={"open_timer": None},
        )
        sprints.record_execution_snapshot(
            connection,
            sprint_id=3,
            work_item_id="card-13",
            card_reference="DEMO-13",
            captured_at=as_of,
            is_final=True,
            stats={"open_timer": None},
        )

    # 1. Text output check
    text_result = CliRunner().invoke(
        cli, ["sprints", "--database", str(database), "preflight", "3"],
    )
    assert text_result.exit_code == 0, text_result.output
    # Preflight exits as before: READY
    assert "READY" in text_result.output
    # Scope report findings in text
    assert "out-of-scope path: DEMO-12 touches src/extra.py" in text_result.output
    assert "unmerged card: DEMO-13" in text_result.output
    assert f"unnamed commit: {unnamed_rev}" in text_result.output

    # 2. JSON output check
    json_result = CliRunner().invoke(
        cli, ["--json", "sprints", "--database", str(database), "preflight", "3"],
    )
    assert json_result.exit_code == 0, json_result.output
    payload = json.loads(json_result.output)

    assert payload["ready"] is True
    assert "scope_report" in payload
    report = payload["scope_report"]
    assert report["configured"] is True
    assert report["branch"] == "main"
    assert report["unmerged_cards"] == ["DEMO-13"]
    assert report["out_of_scope"] == [{"card": "DEMO-12", "paths": ["src/extra.py"]}]
    assert report["out_of_scope_paths"] == ["src/extra.py"]
    assert report["unnamed_commits"] == [unnamed_rev]

    # 3. Sprints close prints scope findings and never refuses closure
    close_result = CliRunner().invoke(
        cli,
        [
            "sprints", "--database", str(database), "close", "3",
            "--ended", "2026-01-02T04:00:00+00:00",
            "--delivered", "Delivered scope work",
        ],
    )
    assert close_result.exit_code == 0, close_result.output
    assert "Closed sprint 3" in close_result.output
    assert "out-of-scope path: DEMO-12 touches src/extra.py" in close_result.output
    assert "unmerged card: DEMO-13" in close_result.output
    assert f"unnamed commit: {unnamed_rev}" in close_result.output


def test_scope_report_missing_key_or_branch(tmp_path: Path, monkeypatch):
    repo = init_git_repo(tmp_path / "repo", branch="main")
    monkeypatch.chdir(repo)
    conf_file = repo / "plane" / "plane-proj.json"
    conf_file.parent.mkdir(parents=True, exist_ok=True)
    conf_file.write_text(
        json.dumps({
            "defaults": {"workspace": "test", "project": "DEMO"},
            "estimate_points": {"1": "uuid-1"},
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("PLANE_PROJ_CONFIG", str(conf_file))

    database = repo / "SPRINTS.sqlite"
    sprints.create_database(database, ("https://plane.test", "test", "DEMO"))
    with sprints.connect_database(database, writable=True) as connection:
        sprints.plan_sprint(connection, 3, "Sprint 3", 1, "Goal", "Exec", ("Accept",))
        sprints.start_sprint(connection, 3, "2026-01-02T03:00:00+00:00", "3", 0, 0)

    # Missing config key: integration_branch is None
    board_no_key = MockSprintBoard(cards=[], integration_branch=None, path=conf_file)
    monkeypatch.setattr(Context, "board", property(lambda self: board_no_key))

    res_no_key = CliRunner().invoke(
        cli, ["--json", "sprints", "--database", str(database), "preflight", "3"],
    )
    assert res_no_key.exit_code == 0
    payload_no_key = json.loads(res_no_key.output)["scope_report"]
    assert payload_no_key["configured"] is False
    assert "not configured" in payload_no_key["message"]

    # Branch does not exist in git
    board_bad_branch = MockSprintBoard(
        cards=[], integration_branch="nonexistent-branch", path=conf_file,
    )
    monkeypatch.setattr(Context, "board", property(lambda self: board_bad_branch))

    res_bad_branch = CliRunner().invoke(
        cli, ["--json", "sprints", "--database", str(database), "preflight", "3"],
    )
    assert res_bad_branch.exit_code == 0
    payload_bad_branch = json.loads(res_bad_branch.output)["scope_report"]
    assert payload_bad_branch["configured"] is False
    assert "does not exist" in payload_bad_branch["message"]
