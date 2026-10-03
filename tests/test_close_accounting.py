"""Closure accounting is derived from recorded facts, never hand-typed."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from plane_proj import sprints
from plane_proj.board import Board
from plane_proj.guards import GuardViolation
from tests.conftest import Card, FakeClient

METRICS = {
    "cards_current": 3, "points_current": 8,
    "cards_done": 1, "points_done": 3,
    "cards_cancelled": 1, "points_cancelled": 2,
}

CURRENT = sprints.Sprint(
    3, "Sprint 3", sprints.STATUS_CURRENT, cycle_id="cycle-3",
    started="2026-01-02T03:04:05+08:00", cards_start=3, points_start=8,
)


def test_derivation_uses_stored_opening_and_live_closing_totals():
    derived = sprints.derive_close_accounting(
        CURRENT, "2026-01-02T04:04:05+08:00", METRICS
    )

    assert derived["hours"] == pytest.approx(1.0)
    assert (derived["cards_start"], derived["points_start"]) == (3, 8)
    # Cancelled members (1 card, 2 points) never count in closing totals.
    assert (derived["cards_end"], derived["points_end"]) == (2, 6)
    # Velocity counts Done points only; cancelled points are excluded.
    assert derived["velocity"] == pytest.approx(3.0)


def test_changed_estimates_flow_from_live_metrics():
    grown = METRICS | {"points_current": 13, "points_done": 5}

    derived = sprints.derive_close_accounting(
        CURRENT, "2026-01-02T05:04:05+08:00", grown
    )

    assert derived["points_end"] == 11
    assert derived["points_start"] == 8
    assert derived["velocity"] == pytest.approx(2.5)


def test_zero_elapsed_time_is_refused():
    with pytest.raises(sprints.SprintError, match="later than started"):
        sprints.derive_close_accounting(
            CURRENT, "2026-01-02T03:04:05+08:00", METRICS
        )


def test_missing_opening_totals_point_to_legacy_import():
    bare = sprints.Sprint(
        3, "Sprint 3", sprints.STATUS_CURRENT, cycle_id="cycle-3",
        started="2026-01-02T03:04:05+08:00",
    )

    with pytest.raises(sprints.SprintError, match="sprints add"):
        sprints.derive_close_accounting(
            bare, "2026-01-02T04:04:05+08:00", METRICS
        )


class ArchivedCycles:
    """A cycles double for a cycle that is already archived."""

    def __init__(self, log, *, end_date: str | None) -> None:
        self._log = log
        self.cycle = SimpleNamespace(
            id="cycle-3", name="Sprint 3", end_date=end_date,
        )

    def retrieve(self, *args, **kwargs):
        self._log.append(("cycles.retrieve", args, kwargs))
        return self.cycle

    def list_archived(self, *args, **kwargs):
        self._log.append(("cycles.list_archived", args, kwargs))
        return SimpleNamespace(
            results=[self.cycle], next_page_results=False, next_cursor=None
        )


def test_an_already_archived_cycle_with_matching_end_is_idempotent(
    board: Board, client: FakeClient
):
    client.cycles = ArchivedCycles(
        client.calls, end_date="2026-01-02T04:04:05+08:00"
    )

    name = board.close_sprint_cycle("cycle-3", "2026-01-02T04:04:05+08:00")

    assert name == "Sprint 3"
    assert client.write_calls == []


def test_an_archived_cycle_with_a_different_end_date_conflicts(
    board: Board, client: FakeClient
):
    client.cycles = ArchivedCycles(
        client.calls, end_date="2026-01-01T04:04:05+08:00"
    )

    with pytest.raises(GuardViolation, match="already archived"):
        board.close_sprint_cycle("cycle-3", "2026-01-02T04:04:05+08:00")

    assert client.write_calls == []


def test_closing_one_parallel_sprint_leaves_the_other_current(
    tmp_path: Path
):
    database = tmp_path / "SPRINTS.sqlite"
    sprints.create_database(database)
    with sprints.connect_database(database, writable=True) as connection:
        for sprint_id, cycle in ((1, "cycle-1"), (2, "cycle-2")):
            sprints.plan_sprint(
                connection, sprint_id, f"Sprint {sprint_id}", sprint_id,
                "goal", "", ("criterion",),
            )
            sprints.start_sprint(
                connection, sprint_id, "2026-01-02T03:04:05+08:00",
                cycle, 3, 8,
            )
        sprints.close_sprint(
            connection, 1, "2026-01-02T04:04:05+08:00", 1.0, 3, 3, 8, 8,
            3.0, "Delivered", "",
        )
        remaining = {
            sprint.sprint_id: sprint.status
            for sprint in sprints.fetch_sprints(connection)
        }
    assert remaining == {1: "completed", 2: "current"}


class PreflightBoard:
    """Board double: one settled card, one in progress."""

    def __init__(self) -> None:
        self.project = SimpleNamespace(
            key="DEMO",
            states={"In Progress": "state-progress", "Done": "state-done"},
        )
        self.cards = [
            Card(id="done-card", sequence_id=1, state="state-done"),
            Card(id="open-card", sequence_id=2, state="state-progress"),
        ]

    def cycle_cards(self, cycle_id: str):
        return self.cards

    def sprint_cycle_metrics(self, cycle_id: str):
        return dict(METRICS)


@pytest.fixture
def register(tmp_path: Path):
    database = tmp_path / "SPRINTS.sqlite"
    sprints.create_database(database)
    connection = sprints.connect_database(database, writable=True)
    sprints.plan_sprint(
        connection, 3, "Sprint 3", 1, "goal", "", ("criterion",)
    )
    sprints.start_sprint(
        connection, 3, "2026-01-02T03:04:05+08:00", "cycle-3", 3, 8
    )
    yield connection
    connection.close()


def test_preflight_reports_blockers_and_derived_accounting(register):
    sprints.record_execution_snapshot(
        register, sprint_id=3, work_item_id="open-card",
        card_reference="DEMO-2", captured_at="2026-01-02T03:30:05+08:00",
        is_final=False,
        stats={"open_timer": {"category": "coding",
                              "started": "2026-01-02T03:10:05+08:00"}},
    )

    payload = sprints.preflight_report(
        register, PreflightBoard(), 3, now="2026-01-02T04:04:05+08:00",
    )

    assert payload["ready"] is False
    assert payload["nonterminal_cards"] == [
        {"card": "DEMO-2", "state": "In Progress"}
    ]
    assert payload["missing_final_snapshots"] == ["DEMO-1", "DEMO-2"]
    assert payload["open_timers"] == [
        {"card": "DEMO-2", "category": "coding"}
    ]
    assert payload["derived"]["velocity"] == pytest.approx(3.0)


def test_preflight_mutates_nothing(register):
    before = register.execute(
        "SELECT count(*) FROM card_execution_snapshots"
    ).fetchone()[0]

    sprints.preflight_report(
        register, PreflightBoard(), 3, now="2026-01-02T04:04:05+08:00",
    )

    after = register.execute(
        "SELECT count(*) FROM card_execution_snapshots"
    ).fetchone()[0]
    assert (before, after) == (0, 0)
