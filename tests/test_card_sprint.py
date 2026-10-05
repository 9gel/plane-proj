"""Tests for card list --sprint ID (number or alias)."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner

from plane_proj import sprints as sprints_module
from plane_proj.cli import Context, cli
from tests.conftest import Card, FakeClient, _page


@pytest.fixture
def sprint_db(tmp_path: Path) -> Path:
    db = tmp_path / "SPRINTS.sqlite"
    sprints_module.create_database(db, ("https://plane.test", "test", "DEMO"))
    with sprints_module.connect_database(db, writable=True) as conn:
        sprints_module.plan_sprint(
            conn, 1, "Sprint 1", 1, "completed goal", "", ("criterion",),
        )
        sprints_module.set_alias(conn, 1, "COMP-1")
        sprints_module.start_sprint(
            conn, 1, "2026-09-01T00:00:00+00:00", "cycle-comp", 2, 5,
        )
        sprints_module.close_sprint(
            conn, 1, "2026-09-02T00:00:00+00:00", 24.0, 2, 2, 5, 5, 0.2, "delivered", "",
        )

        sprints_module.plan_sprint(
            conn, 2, "Sprint 2", 1, "planned goal", "", ("criterion",),
        )
        sprints_module.set_alias(conn, 2, "PLAN-2")
    return db


def test_card_list_planned_sprint_backlog_cards(
    board, client: FakeClient, sprint_db: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    board.config = replace(board.config, state_file=sprint_db)

    planned_cards = [
        Card(
            id="plan-1",
            sequence_id=10,
            name="Planned backlog work",
            state="state-backlog",
            estimate_point="uuid-2",
        ),
    ]
    cycles = [SimpleNamespace(id="cycle-plan", name="Sprint 2")]
    monkeypatch.setattr(board, "groupings", lambda kind: cycles)

    def cycle_items(slug: str, project_id: str, cycle_id: str, params: Any = None) -> Any:
        assert cycle_id == "cycle-plan"
        return _page(planned_cards)

    monkeypatch.setattr(client.cycles, "list_work_items", cycle_items)
    monkeypatch.setattr(Context, "board", property(lambda self: board))

    runner = CliRunner()

    # By number
    res_num = runner.invoke(cli, ["--json", "card", "list", "--sprint", "2"])
    assert res_num.exit_code == 0, res_num.output
    data_num = json.loads(res_num.output)
    assert len(data_num) == 1
    assert data_num[0]["work_item_id"] == "plan-1"
    assert data_num[0]["state"] == "Backlog"
    assert data_num[0]["cycles"] == ["Sprint 2"]

    # By alias
    res_alias = runner.invoke(cli, ["--json", "card", "list", "--sprint", "PLAN-2"])
    assert res_alias.exit_code == 0, res_alias.output
    data_alias = json.loads(res_alias.output)
    assert len(data_alias) == 1
    assert data_alias[0]["work_item_id"] == "plan-1"
    assert data_alias[0]["state"] == "Backlog"


def test_card_list_completed_sprint_done_and_cancelled_cards(
    board, client: FakeClient, sprint_db: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    board.config = replace(board.config, state_file=sprint_db)

    comp_cards = [
        Card(
            id="done-1", sequence_id=20, name="Completed card",
            state="state-done", estimate_point="uuid-3",
        ),
        Card(
            id="canc-1", sequence_id=21, name="Cancelled card",
            state="state-cancelled", estimate_point="uuid-2",
        ),
    ]

    def cycle_items(slug: str, project_id: str, cycle_id: str, params: Any = None) -> Any:
        assert cycle_id == "cycle-comp"
        return _page(comp_cards)

    monkeypatch.setattr(client.cycles, "list_work_items", cycle_items)
    monkeypatch.setattr(Context, "board", property(lambda self: board))

    runner = CliRunner()

    # By number
    res_num = runner.invoke(cli, ["--json", "card", "list", "--sprint", "1"])
    assert res_num.exit_code == 0, res_num.output
    data_num = json.loads(res_num.output)
    assert len(data_num) == 2
    states_num = {r["work_item_id"]: r["state"] for r in data_num}
    assert states_num == {"done-1": "Done", "canc-1": "Cancelled"}
    assert all(r["cycles"] == ["Sprint 1"] for r in data_num)

    # By alias
    res_alias = runner.invoke(cli, ["--json", "card", "list", "--sprint", "COMP-1"])
    assert res_alias.exit_code == 0, res_alias.output
    data_alias = json.loads(res_alias.output)
    assert len(data_alias) == 2
    states_alias = {r["work_item_id"]: r["state"] for r in data_alias}
    assert states_alias == {"done-1": "Done", "canc-1": "Cancelled"}


def test_card_list_unknown_sprint_refused_before_any_request(
    board, client: FakeClient, sprint_db: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    board.config = replace(board.config, state_file=sprint_db)
    monkeypatch.setattr(Context, "board", property(lambda self: board))
    runner = CliRunner()

    # Unknown number
    client.calls.clear()
    res_num = runner.invoke(cli, ["card", "list", "--sprint", "99"])
    assert res_num.exit_code != 0
    assert "Sprint reference rule: unknown ID or alias '99'" in res_num.output
    assert len(client.calls) == 0

    # Unknown alias
    client.calls.clear()
    res_alias = runner.invoke(cli, ["card", "list", "--sprint", "NONEXISTENT"])
    assert res_alias.exit_code != 0
    assert "Sprint reference rule: unknown ID or alias 'NONEXISTENT'" in res_alias.output
    assert len(client.calls) == 0


def test_card_list_sprint_combines_with_filters(
    board, client: FakeClient, sprint_db: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    board.config = replace(board.config, state_file=sprint_db)

    comp_cards = [
        Card(
            id="done-1", sequence_id=20, name="Completed card",
            state="state-done", estimate_point="uuid-3",
        ),
        Card(
            id="canc-1", sequence_id=21, name="Cancelled card",
            state="state-cancelled", estimate_point="uuid-2",
        ),
    ]

    monkeypatch.setattr(client.cycles, "list_work_items", lambda *a, **k: _page(comp_cards))
    monkeypatch.setattr(Context, "board", property(lambda self: board))

    runner = CliRunner()

    # Filter --state Done
    res_done = runner.invoke(
        cli, ["--json", "card", "list", "--sprint", "1", "--state", "Done"],
    )
    assert res_done.exit_code == 0, res_done.output
    data_done = json.loads(res_done.output)
    assert len(data_done) == 1
    assert data_done[0]["work_item_id"] == "done-1"

    # Filter --state Cancelled
    res_canc = runner.invoke(
        cli, ["--json", "card", "list", "--sprint", "1", "--state", "Cancelled"],
    )
    assert res_canc.exit_code == 0, res_canc.output
    data_canc = json.loads(res_canc.output)
    assert len(data_canc) == 1
    assert data_canc[0]["work_item_id"] == "canc-1"

    # Filter --state Backlog (none in completed sprint)
    res_backlog = runner.invoke(
        cli, ["--json", "card", "list", "--sprint", "1", "--state", "Backlog"],
    )
    assert res_backlog.exit_code == 0, res_backlog.output
    assert json.loads(res_backlog.output) == []


def test_card_list_planned_sprint_missing_plane_cycle(
    board, client: FakeClient, sprint_db: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    board.config = replace(board.config, state_file=sprint_db)
    monkeypatch.setattr(board, "groupings", lambda kind: [])
    monkeypatch.setattr(Context, "board", property(lambda self: board))

    runner = CliRunner()
    res = runner.invoke(cli, ["card", "list", "--sprint", "2"])
    assert res.exit_code != 0
    assert "sprint 2 has no matching Plane cycle" in res.output


def test_card_list_sprint_human_table_output(
    board, client: FakeClient, sprint_db: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    board.config = replace(board.config, state_file=sprint_db)
    cards = [
        Card(
            id="done-1", sequence_id=20, name="Completed card",
            state="state-done", estimate_point="uuid-3",
        ),
    ]
    monkeypatch.setattr(client.cycles, "list_work_items", lambda *a, **k: _page(cards))
    monkeypatch.setattr(Context, "board", property(lambda self: board))

    runner = CliRunner()
    res = runner.invoke(cli, ["card", "list", "--sprint", "1"])
    assert res.exit_code == 0, res.output
    assert "CARD" in res.output
    assert "STATE" in res.output
    assert "CYCLE" in res.output
    assert "Sprint 1" in res.output
    assert "DEMO-20" in res.output
