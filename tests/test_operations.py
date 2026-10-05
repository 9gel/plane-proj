"""Resumable transitions: journaled steps, no replays, honest conflicts.

Failure is injected after each step; the retry must converge without
writing anything twice.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from plane_proj import execution, operations, verdicts
from plane_proj import sprints as sprints_module
from plane_proj.board import Board
from plane_proj.guards import (
    EstimateTooLargeForCycle,
    GuardViolation,
    MissingIndependentVerdict,
    TransitionNotAllowed,
)
from tests.conftest import Card, FakeClient

OP = "3e1f8f9c-0000-4000-8000-000000000001"


def _page(results: list[Any]) -> SimpleNamespace:
    return SimpleNamespace(
        results=results, next_page_results=False, next_cursor=None
    )


def _timer_comment(when: str, action: str, category: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"comment-{when}", created_at=when, actor="worker-id",
        comment_html=(
            f"<p>{execution.event_text(action, category)}</p>"
        ),
    )


@pytest.fixture
def connection(tmp_path: Path):
    path = tmp_path / "SPRINTS.sqlite"
    sprints_module.create_database(path)
    connection = sprints_module.connect_database(path, writable=True)
    sprints_module.plan_sprint(
        connection, 1, "Sprint 1", 1, "goal", "", ("criterion",)
    )
    sprints_module.start_sprint(
        connection, 1, "2026-09-16T00:00:00+00:00", "cycle-1", 1, 3
    )
    yield connection
    connection.close()


@pytest.fixture
def sprint_card(board: Board, client: FakeClient) -> Card:
    card = Card(id="card-uuid", sequence_id=12, state="state-progress")
    client.cards = [card]
    client.cycles = type(client.cycles)(
        client.calls, "cycles", result=_page([card])
    )
    return card


def _moves(client: FakeClient) -> list[dict]:
    return [
        kwargs["data"] for name, args, kwargs in client.calls
        if name == "work_items._patch" and "state" in (kwargs["data"] or {})
    ]


def _snapshots(connection: sqlite3.Connection) -> list[dict]:
    return sprints_module.fetch_execution_snapshots(connection, 1)


def test_a_transition_moves_collects_and_receipts(
    connection, board: Board, client: FakeClient, sprint_card: Card
):
    receipt = operations.run_transition(
        connection, board, reference="DEMO-12",
        from_state="In Progress", to_state="Verifying",
        stop_activity=False, operation_id=OP,
    )

    assert receipt["completed"] is True
    assert receipt["card"] == "DEMO-12"
    assert sprint_card.state == "state-verifying"
    assert len(_moves(client)) == 1
    assert len(_snapshots(connection)) == 1
    assert not _snapshots(connection)[0]["is_final"]


def test_a_terminal_transition_records_a_final_snapshot(
    connection, board: Board, client: FakeClient, sprint_card: Card
):
    sprint_card.state = "state-verifying"

    operations.run_transition(
        connection, board, reference="DEMO-12",
        from_state="Verifying", to_state="Done",
        stop_activity=False, operation_id=OP,
    )

    assert _snapshots(connection)[0]["is_final"]


def test_collection_follows_a_move_recorded_in_the_same_second(
    connection, board: Board, client: FakeClient, sprint_card: Card,
    monkeypatch,
):
    # Plane stamps the move's activity with sub-second precision at the
    # moment it lands. A cutoff truncated to whole seconds, or read before
    # the telemetry, precedes that stamp and refused the collection on
    # nearly every first attempt (RETRO-18, -28, -29, -31).
    def landed_now(card: Any) -> list[Any]:
        return [SimpleNamespace(
            id="moved", field="state", old_value="In Progress",
            new_value="Verifying", actor="worker-id",
            created_at=datetime.now(UTC).isoformat(),
        )]

    monkeypatch.setattr(board, "activities", landed_now)

    receipt = operations.run_transition(
        connection, board, reference="DEMO-12",
        from_state="In Progress", to_state="Verifying",
        stop_activity=False, operation_id=OP,
    )

    assert receipt["completed"] is True
    snapshot = _snapshots(connection)[0]
    assert snapshot["stats"]["current_state"]["name"] == "Verifying"


def test_stop_activity_closes_the_open_timer_once(
    connection, board: Board, client: FakeClient, sprint_card: Card
):
    client.comments.append(
        _timer_comment("2026-09-16T00:30:00Z", "start", "coding")
    )

    receipt = operations.run_transition(
        connection, board, reference="DEMO-12",
        from_state="In Progress", to_state="Verifying",
        stop_activity=True, operation_id=OP,
    )

    assert receipt["stopped_activity"] == "coding"
    stops = [
        comment for comment in client.comments
        if '"action":"stop"' in getattr(comment, "comment_html", "")
    ]
    assert len(stops) == 1


def test_an_unexpected_source_state_conflicts_without_writes(
    connection, board: Board, client: FakeClient, sprint_card: Card
):
    sprint_card.state = "state-todo"

    with pytest.raises(GuardViolation, match="expected 'In Progress'"):
        operations.run_transition(
            connection, board, reference="DEMO-12",
            from_state="In Progress", to_state="Verifying",
            stop_activity=False, operation_id=OP,
        )

    assert _moves(client) == []
    assert _snapshots(connection) == []


def test_a_fresh_operation_finding_the_target_state_conflicts(
    connection, board: Board, client: FakeClient, sprint_card: Card
):
    """A competing writer already moved the card; do not absorb it."""
    sprint_card.state = "state-verifying"

    with pytest.raises(GuardViolation, match="fabricate"):
        operations.run_transition(
            connection, board, reference="DEMO-12",
            from_state="In Progress", to_state="Verifying",
            stop_activity=False, operation_id=OP,
        )

    assert _moves(client) == []


def test_a_card_outside_every_current_sprint_is_refused(
    connection, board: Board, client: FakeClient
):
    card = Card(id="card-uuid", sequence_id=12, state="state-progress")
    client.cards = [card]

    with pytest.raises(sprints_module.SprintError, match="no current"):
        operations.run_transition(
            connection, board, reference="DEMO-12",
            from_state="In Progress", to_state="Verifying",
            stop_activity=False, operation_id=OP,
        )

    assert _moves(client) == []


def test_a_retry_after_a_lost_move_response_does_not_replay(
    connection, board: Board, client: FakeClient, sprint_card: Card,
    monkeypatch: pytest.MonkeyPatch,
):
    """The move landed; the crash hit before its journal step."""
    real_move = board.move_state

    def move_then_crash(item: Any, state_name: str, **options: Any) -> None:
        real_move(item, state_name, **options)
        raise ConnectionError("response lost")

    monkeypatch.setattr(board, "move_state", move_then_crash)
    with pytest.raises(ConnectionError):
        operations.run_transition(
            connection, board, reference="DEMO-12",
            from_state="In Progress", to_state="Verifying",
            stop_activity=False, operation_id=OP,
        )
    monkeypatch.setattr(board, "move_state", real_move)

    receipt = operations.run_transition(
        connection, board, reference="DEMO-12",
        from_state="In Progress", to_state="Verifying",
        stop_activity=False, operation_id=OP,
    )

    assert receipt["completed"] is True
    assert len(_moves(client)) == 1, "the retry must not replay the move"
    assert len(_snapshots(connection)) == 1


def test_a_retry_after_a_failed_collection_resumes_without_a_second_move(
    connection, board: Board, client: FakeClient, sprint_card: Card,
    monkeypatch: pytest.MonkeyPatch,
):
    real_record = sprints_module.record_execution_snapshot
    monkeypatch.setattr(
        sprints_module, "record_execution_snapshot",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            sqlite3.OperationalError("database is locked")
        ),
    )
    with pytest.raises(sqlite3.OperationalError):
        operations.run_transition(
            connection, board, reference="DEMO-12",
            from_state="In Progress", to_state="Verifying",
            stop_activity=False, operation_id=OP,
        )
    monkeypatch.setattr(
        sprints_module, "record_execution_snapshot", real_record
    )

    receipt = operations.run_transition(
        connection, board, reference="DEMO-12",
        from_state="In Progress", to_state="Verifying",
        stop_activity=False, operation_id=OP,
    )

    assert receipt["completed"] is True
    assert len(_moves(client)) == 1
    assert len(_snapshots(connection)) == 1


def test_a_retry_after_a_lost_timer_stop_writes_no_second_stop(
    connection, board: Board, client: FakeClient, sprint_card: Card,
    monkeypatch: pytest.MonkeyPatch,
):
    client.comments.append(
        _timer_comment("2026-09-16T00:30:00Z", "start", "coding")
    )
    real_comment = board.comment

    def comment_then_crash(item: Any, html: str) -> Any:
        real_comment(item, html)
        raise ConnectionError("response lost")

    monkeypatch.setattr(board, "comment", comment_then_crash)
    with pytest.raises(ConnectionError):
        operations.run_transition(
            connection, board, reference="DEMO-12",
            from_state="In Progress", to_state="Verifying",
            stop_activity=True, operation_id=OP,
        )
    monkeypatch.setattr(board, "comment", real_comment)

    operations.run_transition(
        connection, board, reference="DEMO-12",
        from_state="In Progress", to_state="Verifying",
        stop_activity=True, operation_id=OP,
    )

    stops = [
        comment for comment in client.comments
        if '"action":"stop"' in getattr(comment, "comment_html", "")
    ]
    assert len(stops) == 1


def test_a_completed_operation_returns_its_receipt_without_new_writes(
    connection, board: Board, client: FakeClient, sprint_card: Card
):
    first = operations.run_transition(
        connection, board, reference="DEMO-12",
        from_state="In Progress", to_state="Verifying",
        stop_activity=False, operation_id=OP,
    )
    calls_after_first = len(client.calls)

    second = operations.run_transition(
        connection, board, reference="DEMO-12",
        from_state="In Progress", to_state="Verifying",
        stop_activity=False, operation_id=OP,
    )

    assert second == first
    assert len(client.calls) == calls_after_first
    assert len(_snapshots(connection)) == 1


def test_the_same_operation_id_with_a_different_request_conflicts(
    connection, board: Board, client: FakeClient, sprint_card: Card
):
    operations.run_transition(
        connection, board, reference="DEMO-12",
        from_state="In Progress", to_state="Verifying",
        stop_activity=False, operation_id=OP,
    )

    with pytest.raises(sprints_module.SprintError, match="different"):
        operations.run_transition(
            connection, board, reference="DEMO-12",
            from_state="Verifying", to_state="Done",
            stop_activity=False, operation_id=OP,
        )


def test_activity_switch_stops_the_old_and_starts_the_new(
    connection, board: Board, client: FakeClient, sprint_card: Card
):
    client.comments.append(
        _timer_comment("2026-09-16T00:30:00Z", "start", "coding")
    )

    receipt = operations.run_activity_switch(
        connection, board, reference="DEMO-12",
        category="manual-qa", operation_id=OP,
    )

    assert receipt["events"] == ["stop coding", "start manual-qa"]


def test_activity_switch_to_the_open_category_writes_nothing(
    connection, board: Board, client: FakeClient, sprint_card: Card
):
    client.comments.append(
        _timer_comment("2026-09-16T00:30:00Z", "start", "coding")
    )

    receipt = operations.run_activity_switch(
        connection, board, reference="DEMO-12",
        category="coding", operation_id=OP,
    )

    assert receipt["events"] == []
    assert client.named("comments.create") == []


def test_activity_switch_refuses_a_bad_category_before_any_request(
    connection, board: Board, client: FakeClient, sprint_card: Card
):
    with pytest.raises(GuardViolation, match="lowercase slug"):
        operations.run_activity_switch(
            connection, board, reference="DEMO-12",
            category="Manual QA", operation_id=OP,
        )

    assert client.calls == []


@pytest.mark.parametrize("category", ["stop", "start"])
def test_activity_refuses_a_timer_verb_as_a_category(
    connection, board: Board, client: FakeClient, sprint_card: Card,
    category: str,
):
    # `card activity CARD stop` once started a timer named "stop" (RETRO-30).
    with pytest.raises(GuardViolation, match="card timer stop"):
        operations.run_activity_switch(
            connection, board, reference="DEMO-12",
            category=category, operation_id=OP,
        )

    assert client.calls == []


@pytest.mark.parametrize("target", ["Done", "Cancelled"])
def test_settling_a_card_with_a_running_timer_refuses_before_any_write(
    connection, board: Board, client: FakeClient, sprint_card: Card,
    target: str,
):
    # A settled card with a running timer blocked closure preflight in
    # Sprints 30, 32 and 34, long after the card was accepted.
    sprint_card.state = "state-verifying"
    client.comments.append(
        _timer_comment("2026-09-16T00:30:00Z", "start", "coding")
    )

    with pytest.raises(GuardViolation, match="--stop-activity"):
        operations.run_transition(
            connection, board, reference="DEMO-12",
            from_state="Verifying", to_state=target,
            stop_activity=False, operation_id=OP,
        )

    assert client.write_calls == []
    assert sprint_card.state == "state-verifying"


def test_admitting_an_oversized_card_refuses_before_any_write(
    connection, board: Board, client: FakeClient, sprint_card: Card,
):
    sprint_card.state = "state-backlog"
    sprint_card.estimate_point = "uuid-5"
    client.comments.append(
        _timer_comment("2026-09-16T00:30:00Z", "start", "coding")
    )

    with pytest.raises(EstimateTooLargeForCycle):
        operations.run_transition(
            connection, board, reference="DEMO-12",
            from_state="Backlog", to_state="Todo",
            stop_activity=True, operation_id=OP,
        )

    assert client.write_calls == []
    assert sprint_card.state == "state-backlog"


def _with_rules(board: Board, **rules: bool) -> None:
    board.project = replace(
        board.project, rules=replace(board.project.rules, **rules)
    )


def test_done_without_independent_verdicts_refuses_before_any_write(
    connection, board: Board, client: FakeClient, sprint_card: Card,
):
    _with_rules(board, require_independent_verdicts=True)
    sprint_card.state = "state-verifying"
    client.activities = [SimpleNamespace(
        id="entered", field="state", old_value="In Progress",
        new_value="Verifying", actor="worker-id",
        created_at="2026-09-16T00:10:00Z",
    )]
    # The timer stop would otherwise be the first write.
    client.comments.append(
        _timer_comment("2026-09-16T00:30:00Z", "start", "coding")
    )

    with pytest.raises(MissingIndependentVerdict, match="no qa verdict"):
        operations.run_transition(
            connection, board, reference="DEMO-12",
            from_state="Verifying", to_state="Done",
            stop_activity=True, operation_id=OP,
        )

    assert client.write_calls == []
    assert sprint_card.state == "state-verifying"


def test_done_with_independent_verdicts_transitions(
    connection, board: Board, client: FakeClient, sprint_card: Card,
):
    _with_rules(board, require_independent_verdicts=True,
                require_transition_table=True)
    sprint_card.state = "state-verifying"
    client.comments.extend(
        SimpleNamespace(
            id=f"verdict-{role}", created_at="2026-09-16T00:40:00Z",
            actor="worker-id",
            comment_html=verdicts.verdict_html(verdicts.verdict_fields(
                role=role, result="pass", revision="0123abc",
                author=f"{role}-agent", note="", operation_id=role,
            )),
        )
        for role in verdicts.ROLES
    )

    receipt = operations.run_transition(
        connection, board, reference="DEMO-12",
        from_state="Verifying", to_state="Done",
        stop_activity=False, operation_id=OP,
    )

    assert receipt["completed"] is True
    assert sprint_card.state == "state-done"


def test_a_transition_outside_the_table_refuses_before_any_write(
    connection, board: Board, client: FakeClient, sprint_card: Card,
):
    _with_rules(board, require_transition_table=True)
    client.comments.append(
        _timer_comment("2026-09-16T00:30:00Z", "start", "coding")
    )

    with pytest.raises(TransitionNotAllowed, match="Transition table rule"):
        operations.run_transition(
            connection, board, reference="DEMO-12",
            from_state="In Progress", to_state="Done",
            stop_activity=True, operation_id=OP,
        )

    assert client.write_calls == []
    assert sprint_card.state == "state-progress"


def test_settling_with_stop_activity_closes_the_timer(
    connection, board: Board, client: FakeClient, sprint_card: Card,
):
    sprint_card.state = "state-verifying"
    client.comments.append(
        _timer_comment("2026-09-16T00:30:00Z", "start", "coding")
    )

    receipt = operations.run_transition(
        connection, board, reference="DEMO-12",
        from_state="Verifying", to_state="Done",
        stop_activity=True, operation_id=OP,
    )

    assert receipt["stopped_activity"] == "coding"
    assert _snapshots(connection)[0]["stats"]["open_timer"] is None


def test_activity_retry_after_a_lost_stop_converges_to_one_of_each(
    connection, board: Board, client: FakeClient, sprint_card: Card,
    monkeypatch: pytest.MonkeyPatch,
):
    client.comments.append(
        _timer_comment("2026-09-16T00:30:00Z", "start", "coding")
    )
    real_comment = board.comment
    crashes = {"armed": True}

    def crash_once(item: Any, html: str) -> Any:
        result = real_comment(item, html)
        if crashes["armed"]:
            crashes["armed"] = False
            raise ConnectionError("response lost")
        return result

    monkeypatch.setattr(board, "comment", crash_once)
    with pytest.raises(ConnectionError):
        operations.run_activity_switch(
            connection, board, reference="DEMO-12",
            category="manual-qa", operation_id=OP,
        )

    receipt = operations.run_activity_switch(
        connection, board, reference="DEMO-12",
        category="manual-qa", operation_id=OP,
    )

    written = [
        getattr(comment, "comment_html", "")
        for comment in client.comments[1:]
    ]
    assert len([html for html in written if '"stop"' in html]) == 1
    assert len([html for html in written if '"start"' in html]) == 1
    assert receipt["completed"] is True


def test_a_version_6_register_gains_the_journal_on_write_open(
    tmp_path: Path
):
    path = tmp_path / "SPRINTS.sqlite"
    sprints_module.create_database(path)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            "DROP TABLE operation_journal; PRAGMA user_version = 6;"
        )

    connection = sprints_module.connect_database(path, writable=True)
    try:
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "operation_journal" in tables
    finally:
        connection.close()


def _reasons(client: FakeClient) -> list[str]:
    return [
        getattr(comment, "comment_html", "") for comment in client.comments
        if execution.REWORK_PREFIX in getattr(comment, "comment_html", "")
    ]


def test_a_sendback_records_its_reason_before_moving(
    connection, board: Board, client: FakeClient, sprint_card: Card
):
    sprint_card.state = "state-verifying"

    receipt = operations.run_transition(
        connection, board, reference="DEMO-12",
        from_state="Verifying", to_state="In Progress",
        stop_activity=False, operation_id=OP, reason="defect",
    )

    assert receipt["reason"] == "defect"
    assert len(_reasons(client)) == 1
    assert '"reason":"defect"' in _reasons(client)[0]
    assert sprint_card.state == "state-progress"


@pytest.mark.parametrize(
    ("source", "target", "reason", "message"),
    [
        ("Verifying", "In Progress", None, "names one of"),
        ("Verifying", "In Progress", "typo", "names one of"),
        ("In Progress", "Verifying", "defect", "only a send-back"),
    ],
)
def test_a_missing_or_misplaced_reason_is_refused_before_any_write(
    connection, board: Board, client: FakeClient, sprint_card: Card,
    source: str, target: str, reason: str | None, message: str,
):
    sprint_card.state = board.project.state_id(source)

    with pytest.raises(GuardViolation, match=f"Rework reason rule.*{message}"):
        operations.run_transition(
            connection, board, reference="DEMO-12",
            from_state=source, to_state=target,
            stop_activity=False, operation_id=OP, reason=reason,
        )

    assert _moves(client) == []
    assert _reasons(client) == []
    assert _snapshots(connection) == []


def test_a_retry_after_a_lost_reason_response_writes_no_second_reason(
    connection, board: Board, client: FakeClient, sprint_card: Card,
    monkeypatch: pytest.MonkeyPatch,
):
    sprint_card.state = "state-verifying"
    real_comment = board.comment

    def comment_then_crash(item: Any, html: str) -> Any:
        real_comment(item, html)
        raise ConnectionError("response lost")

    monkeypatch.setattr(board, "comment", comment_then_crash)
    with pytest.raises(ConnectionError):
        operations.run_transition(
            connection, board, reference="DEMO-12",
            from_state="Verifying", to_state="In Progress",
            stop_activity=False, operation_id=OP, reason="spec",
        )
    monkeypatch.setattr(board, "comment", real_comment)

    operations.run_transition(
        connection, board, reference="DEMO-12",
        from_state="Verifying", to_state="In Progress",
        stop_activity=False, operation_id=OP, reason="spec",
    )

    assert len(_reasons(client)) == 1
    assert len(_moves(client)) == 1
