"""Delayed activity intervals: provenance-backed, separate, never invented.

Delayed intervals need evidence, cannot overlap anything already timed,
keep occurrence and receipt times apart, and never merge into
live-measured figures.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from plane_proj import execution, operations
from plane_proj import sprints as sprints_module
from plane_proj.board import Board
from plane_proj.guards import GuardViolation
from tests.conftest import Card, FakeClient

NOW = datetime.fromisoformat("2026-09-16T12:00:00+00:00")
OP = "5d7a2b1e-0000-4000-8000-000000000002"


def _timer_comment(when: str, action: str, category: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"comment-{when}", created_at=when, actor="worker-id",
        comment_html=f"<p>{execution.event_text(action, category)}</p>",
    )


def _delayed_comment(
    received: str, *, category: str = "blocking-run",
    started: str = "2026-09-16T02:00:00+00:00",
    ended: str = "2026-09-16T02:20:00+00:00",
    evidence: str = "run.log", operation_id: str = OP,
) -> SimpleNamespace:
    body = execution.EVENT_PREFIX_V2 + (
        f'{{"action":"interval","category":"{category}",'
        f'"started_at":"{started}","ended_at":"{ended}",'
        f'"evidence_ref":"{evidence}","operation_id":"{operation_id}"}}'
    )
    return SimpleNamespace(
        id=f"comment-{received}", created_at=received, actor="worker-id",
        comment_html=f"<p>{body}</p>",
    )


def _record(comments: list[Any], **overrides: Any) -> str | None:
    arguments: dict[str, Any] = {
        "category": "blocking-run",
        "started_at": "2026-09-16T02:00:00+00:00",
        "ended_at": "2026-09-16T02:20:00+00:00",
        "evidence_ref": "run.log",
        "operation_id": OP,
        "now": NOW,
    }
    arguments.update(overrides)
    return execution.delayed_interval_event(comments, **arguments)


def test_delayed_minutes_stay_separate_from_live_measurement():
    comments = [
        _timer_comment("2026-09-16T01:00:00Z", "start", "coding"),
        _timer_comment("2026-09-16T01:30:00Z", "stop", "coding"),
        _delayed_comment("2026-09-16T09:00:00Z"),
    ]

    stats = execution.execution_stats([], comments, as_of=NOW)

    assert stats["execution_minutes"] == {"coding": 30}
    assert stats["delayed_minutes"] == {"blocking-run": 20}


def test_nothing_is_invented_when_nothing_was_timed():
    stats = execution.execution_stats([], [], as_of=NOW)

    assert stats["execution_minutes"] == {}
    assert stats["delayed_minutes"] == {}


def test_a_recorded_event_keeps_occurrence_and_receipt_apart():
    """Received hours later, the interval still reports its own times."""
    rows = execution.timeline([], [_delayed_comment("2026-09-16T09:00:00Z")])

    assert len(rows) == 1
    assert rows[0]["kind"] == "delayed"
    assert rows[0]["when"] == "2026-09-16T02:00:00+00:00"
    assert "received 2026-09-16T09:00:00+00:00" in rows[0]["detail"]


def test_offsets_normalize_to_the_same_instant():
    """+02:00 and Z spellings of one instant are one instant."""
    comments = [
        _timer_comment("2026-09-16T01:50:00Z", "start", "coding"),
        _timer_comment("2026-09-16T02:10:00Z", "stop", "coding"),
    ]

    with pytest.raises(GuardViolation, match="overlap"):
        _record(
            comments,
            started_at="2026-09-16T04:00:00+02:00",
            ended_at="2026-09-16T04:20:00+02:00",
        )


def test_an_interval_needs_evidence():
    with pytest.raises(GuardViolation, match="evidence"):
        _record([], evidence_ref="   ")


def test_a_future_interval_is_refused():
    with pytest.raises(GuardViolation, match="future"):
        _record(
            [],
            started_at="2026-09-16T13:00:00+00:00",
            ended_at="2026-09-16T13:20:00+00:00",
        )


def test_an_inverted_interval_is_refused():
    with pytest.raises(GuardViolation, match="impossible ordering"):
        _record(
            [],
            started_at="2026-09-16T02:20:00+00:00",
            ended_at="2026-09-16T02:00:00+00:00",
        )


def test_overlap_with_a_live_interval_is_refused():
    comments = [
        _timer_comment("2026-09-16T02:10:00Z", "start", "coding"),
        _timer_comment("2026-09-16T02:40:00Z", "stop", "coding"),
    ]

    with pytest.raises(GuardViolation, match="live coding"):
        _record(comments)


def test_overlap_with_an_open_timer_is_refused():
    comments = [_timer_comment("2026-09-16T01:00:00Z", "start", "coding")]

    with pytest.raises(GuardViolation, match="open coding"):
        _record(comments)


def test_overlap_with_another_delayed_interval_is_refused():
    comments = [
        _delayed_comment("2026-09-16T09:00:00Z", operation_id="other-op")
    ]

    with pytest.raises(GuardViolation, match="delayed blocking-run"):
        _record(comments, started_at="2026-09-16T02:10:00+00:00",
                ended_at="2026-09-16T02:30:00+00:00")


def test_the_same_operation_and_interval_converges_to_no_write():
    assert _record([_delayed_comment("2026-09-16T09:00:00Z")]) is None


def test_the_same_operation_with_a_different_interval_is_refused():
    comments = [_delayed_comment("2026-09-16T09:00:00Z")]

    with pytest.raises(GuardViolation, match="different"):
        _record(comments, ended_at="2026-09-16T02:30:00+00:00")


def test_legacy_v1_events_and_ordinary_comments_still_parse():
    comments = [
        SimpleNamespace(
            id="ordinary", created_at="2026-09-16T00:59:00Z",
            actor="worker-id", comment_html="<p>just words</p>",
        ),
        _timer_comment("2026-09-16T01:00:00Z", "start", "coding"),
        _timer_comment("2026-09-16T01:30:00Z", "stop", "coding"),
        _delayed_comment("2026-09-16T09:00:00Z"),
    ]

    rows = execution.timeline([], comments)

    assert [row["kind"] for row in rows] == ["timer", "timer", "delayed"]


@pytest.fixture
def connection(tmp_path: Path):
    path = tmp_path / "SPRINTS.sqlite"
    sprints_module.create_database(path)
    connection = sprints_module.connect_database(path, writable=True)
    yield connection
    connection.close()


def test_run_delayed_activity_records_once_and_receipts(
    connection, board: Board, client: FakeClient
):
    client.cards = [Card(id="card-uuid", sequence_id=12)]

    receipt = operations.run_delayed_activity(
        connection, board, reference="DEMO-12", category="blocking-run",
        started_at="2026-09-16T02:00:00+00:00",
        ended_at="2026-09-16T02:20:00+00:00",
        evidence_ref="run.log", operation_id=OP,
    )

    assert receipt["recorded"] is True
    assert len(client.named("comments.create")) == 1

    again = operations.run_delayed_activity(
        connection, board, reference="DEMO-12", category="blocking-run",
        started_at="2026-09-16T02:00:00+00:00",
        ended_at="2026-09-16T02:20:00+00:00",
        evidence_ref="run.log", operation_id=OP,
    )

    assert again == receipt
    assert len(client.named("comments.create")) == 1


def test_run_delayed_activity_refuses_bad_input_before_any_write(
    connection, board: Board, client: FakeClient
):
    client.cards = [Card(id="card-uuid", sequence_id=12)]

    with pytest.raises(GuardViolation, match="evidence"):
        operations.run_delayed_activity(
            connection, board, reference="DEMO-12",
            category="blocking-run",
            started_at="2026-09-16T02:00:00+00:00",
            ended_at="2026-09-16T02:20:00+00:00",
            evidence_ref=" ", operation_id=OP,
        )

    assert client.named("comments.create") == []
