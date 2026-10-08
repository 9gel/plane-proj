"""Tests for git commit trailers and declared card scope checks."""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from plane_proj.board import Board
from plane_proj.cli import Context, cli
from plane_proj.delivery_plan import DeliveryPlan, TouchedPath, render_section
from plane_proj.guards import ScopeRule
from plane_proj.scope import card_change_set, check_verdict_scope, path_covered
from tests.conftest import Card

REV = "1111111111111111111111111111111111111111"
OP = "00000000-0000-0000-0000-000000000001"


def init_git_repo(path: Path) -> Path:
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
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
    trailers: list[tuple[str, str]] | None = None,
) -> str:
    for rel_path, content in files.items():
        file_path = repo / rel_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        subprocess.run(["git", "add", rel_path], cwd=repo, check=True, capture_output=True)
    full_message = message
    if trailers:
        full_message += "\n\n" + "\n".join(f"{k}: {v}" for k, v in trailers)
    subprocess.run(["git", "commit", "-m", full_message], cwd=repo, check=True, capture_output=True)
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True,
    )
    return proc.stdout.strip()


def with_rules(board: Board, **rules: bool) -> Board:
    board.project = replace(
        board.project, rules=replace(board.project.rules, **rules)
    )
    return board


def invoke(config_path, board, monkeypatch, *args: str):
    monkeypatch.setattr(Context, "board", property(lambda self: board))
    return CliRunner().invoke(cli, ["--conf", str(config_path), *args])


def test_card_change_set_multi_branch_and_merge(tmp_path):
    """pytest builds a temporary git repository with trailer commits on two

    branches and a merge, and shows the change set is the same whichever
    branch the revision is on.
    """
    repo = init_git_repo(tmp_path)

    # Initial commit on main
    commit_files(repo, {"README.md": "# Readme\n"}, "Initial commit")

    # Branch A: commit touching src/a.py for DEMO-12
    subprocess.run(
        ["git", "checkout", "-b", "branch-a"], cwd=repo, check=True, capture_output=True,
    )
    commit_files(
        repo,
        {"src/a.py": "x = 1\n"},
        "Feature A",
        trailers=[("Card", "DEMO-12")],
    )

    # Branch B: branched from main, commit touching src/b.py for DEMO-12
    subprocess.run(["git", "checkout", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "checkout", "-b", "branch-b"], cwd=repo, check=True, capture_output=True,
    )
    commit_files(
        repo,
        {"src/b.py": "y = 2\n"},
        "Feature B",
        trailers=[("Card", "DEMO-12")],
    )

    # Merge branch-a into branch-b
    subprocess.run(
        ["git", "merge", "--no-ff", "-m", "Merge branch-a into branch-b", "branch-a"],
        cwd=repo, check=True, capture_output=True,
    )

    # Fast-forward branch-a so both branches reach both commits
    subprocess.run(["git", "checkout", "branch-a"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "merge", "--ff-only", "branch-b"], cwd=repo, check=True, capture_output=True,
    )

    # Check change sets on both branches
    set_a = card_change_set("DEMO-12", "branch-a", cwd=repo)
    set_b = card_change_set("DEMO-12", "branch-b", cwd=repo)

    assert set_a == frozenset({"src/a.py", "src/b.py"})
    assert set_b == frozenset({"src/a.py", "src/b.py"})
    assert set_a == set_b


def test_verdict_refused_for_undeclared_path_and_writes_no_comment(
    config_path, board, client, monkeypatch, tmp_path,
):
    """pytest shows the verdict is refused with ScopeRule for an undeclared

    path, accepted after the declaration covers it, and that no verdict
    comment is written when refused.
    """
    repo = init_git_repo(tmp_path)
    monkeypatch.chdir(repo)

    rev = commit_files(
        repo,
        {"src/allowed.py": "pass\n", "src/extra.py": "extra\n"},
        "Implement card",
        trailers=[("Card", "DEMO-12")],
    )

    board = with_rules(board, require_declared_scope=True)
    plan_undeclared_extra = DeliveryPlan(
        touches=(TouchedPath("src/allowed.py"),),
        dependencies_assessed=True,
    )
    desc = f"<p>Description</p>\n{render_section(plan_undeclared_extra)}"
    card = Card(sequence_id=12, state="state-verifying", description_html=desc)
    client.cards = [card]

    # Verdict attempt with undeclared src/extra.py
    result = invoke(
        config_path,
        board,
        monkeypatch,
        "card",
        "verdict",
        "DEMO-12",
        "--role",
        "qa",
        "--result",
        "pass",
        "--revision",
        rev,
        "--author",
        "qa-agent",
        "--operation-id",
        OP,
    )

    assert result.exit_code != 0
    assert isinstance(result.exception, ScopeRule)
    assert "src/extra.py" in str(result.exception)
    # Zero verdict comments written to board
    assert len(client.named("comments.create")) == 0

    # Now update declaration to cover src/extra.py
    plan_covered = DeliveryPlan(
        touches=(TouchedPath("src/allowed.py"), TouchedPath("src/extra.py")),
        dependencies_assessed=True,
    )
    card.description_html = f"<p>Description</p>\n{render_section(plan_covered)}"

    # Verdict retry
    result2 = invoke(
        config_path,
        board,
        monkeypatch,
        "card",
        "verdict",
        "DEMO-12",
        "--role",
        "qa",
        "--result",
        "pass",
        "--revision",
        rev,
        "--author",
        "qa-agent",
        "--operation-id",
        OP,
    )

    assert result2.exit_code == 0, result2.output
    assert len(client.named("comments.create")) == 1


def test_directory_declaration_and_new_path_matching(tmp_path):
    """pytest shows a directory declaration covers files below it and a

    (new) path is matched like any other.
    """
    repo = init_git_repo(tmp_path)
    rev = commit_files(
        repo,
        {
            "src/sub/deep/module.py": "x = 1\n",
            "src/created.py": "y = 2\n",
        },
        "Deep commits",
        trailers=[("Card", "DEMO-12")],
    )

    plan = DeliveryPlan(
        touches=(
            TouchedPath("src/sub/"),
            TouchedPath("src/created.py", is_new=True),
        ),
        dependencies_assessed=True,
    )
    card = SimpleNamespace(description_html=render_section(plan))

    # Should pass without raising
    check_verdict_scope(card, "DEMO-12", rev, cwd=repo)

    # Adding an uncovered path causes ScopeRule
    rev2 = commit_files(
        repo,
        {"uncovered.txt": "oops\n"},
        "Extra commit",
        trailers=[("Card", "DEMO-12")],
    )
    with pytest.raises(ScopeRule) as exc_info:
        check_verdict_scope(card, "DEMO-12", rev2, cwd=repo)
    assert "uncovered.txt" in str(exc_info.value)


def test_touches_none_refuses_when_changes_exist_and_passes_when_clean(tmp_path):
    repo = init_git_repo(tmp_path)
    commit_files(repo, {"README.md": "base\n"}, "Base")

    plan_none = DeliveryPlan(touches=(), dependencies_assessed=True)
    card = SimpleNamespace(description_html=render_section(plan_none))

    # Clean: no changes for DEMO-12
    check_verdict_scope(card, "DEMO-12", "HEAD", cwd=repo)

    # Changes made naming DEMO-12
    rev = commit_files(
        repo,
        {"docs/guide.md": "docs\n"},
        "Docs update",
        trailers=[("Card", "DEMO-12")],
    )
    with pytest.raises(ScopeRule) as exc_info:
        check_verdict_scope(card, "DEMO-12", rev, cwd=repo)
    assert "declares File scope: none" in str(exc_info.value)
    assert "docs/guide.md" in str(exc_info.value)


def test_scope_rule_refuses_outside_git_repository(tmp_path):
    plan = DeliveryPlan(touches=(TouchedPath("src/foo.py"),), dependencies_assessed=True)
    card = SimpleNamespace(description_html=render_section(plan))

    with pytest.raises(ScopeRule) as exc_info:
        check_verdict_scope(card, "DEMO-12", "HEAD", cwd=tmp_path)
    assert "project directory must be a git repository" in str(exc_info.value)


def test_undeclared_card_skips_scope_check(tmp_path):
    card = SimpleNamespace(description_html="<p>No delivery plan</p>")
    # Outside git repo, still passes because undeclared cards are not checked
    check_verdict_scope(card, "DEMO-12", "HEAD", cwd=tmp_path)


def test_path_covered_helpers():
    assert path_covered("src/", "src/a.py")
    assert path_covered("src/", "src/sub/b.py")
    assert path_covered("src/a.py (new)", "src/a.py")
    assert path_covered(TouchedPath("src/a.py", is_new=True), "src/a.py")
    assert path_covered("./src/a.py", "src/a.py")
    assert not path_covered("src/a.py", "src/b.py")
    assert not path_covered("src/", "tests/test_a.py")


def test_verdict_accepts_undeclared_shared_path_and_refuses_undeclared_non_shared(tmp_path):
    """pytest against a temporary git repository: a verdict is accepted when the
    change set adds an undeclared shared path, and refused for an undeclared
    non-shared path.
    """
    repo = init_git_repo(tmp_path)
    commit_files(repo, {"README.md": "base\n"}, "Base commit")

    plan = DeliveryPlan(
        touches=(TouchedPath("src/allowed.py"),),
        dependencies_assessed=True,
    )
    card = SimpleNamespace(description_html=render_section(plan))
    shared = ("pyproject.toml", "uv.lock")

    rev_ok = commit_files(
        repo,
        {"src/allowed.py": "print('ok')\n", "pyproject.toml": "version = '1.0'\n"},
        "Implement feature and bump version",
        trailers=[("Card", "DEMO-12")],
    )
    check_verdict_scope(card, "DEMO-12", rev_ok, cwd=repo, shared_paths=shared)

    rev_bad = commit_files(
        repo,
        {"uncovered.py": "print('bad')\n"},
        "Add unexpected file",
        trailers=[("Card", "DEMO-12")],
    )
    with pytest.raises(ScopeRule) as exc_info:
        check_verdict_scope(card, "DEMO-12", rev_bad, cwd=repo, shared_paths=shared)
    assert "uncovered.py" in str(exc_info.value)
    assert "pyproject.toml" not in str(exc_info.value)

