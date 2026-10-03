"""Execution telemetry combines Plane history with CE-compatible timer comments."""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from plane_proj import execution
from plane_proj.cli import Context, cli
from plane_proj.guards import GuardViolation
from tests.conftest import Card


def activity(when: str, old: str, new: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"activity-{when}", created_at=when, field="state",
        old_value=old, new_value=new, old_identifier=None, new_identifier=None,
        actor="worker-id",
    )


def comment(when: str, payload: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"comment-{when}", created_at=when,
        comment_html=f"<p>{execution.EVENT_PREFIX}{payload}</p>", actor="worker-id",
    )


def test_stats_separate_wall_clock_states_from_active_categories() -> None:
    activities = [
        activity("2026-09-16T01:00:00Z", "Todo", "In Progress"),
        activity("2026-09-16T02:30:00Z", "In Progress", "Verifying"),
        activity("2026-09-16T03:00:00Z", "Verifying", "Done"),
    ]
    comments = [
        comment("2026-09-16T01:05:00Z", '{"action":"start","category":"coding"}'),
        comment("2026-09-16T01:45:00Z", '{"action":"stop","category":"coding"}'),
        comment("2026-09-16T01:50:00Z", '{"action":"start","category":"blocking-run"}'),
        comment("2026-09-16T02:10:00Z", '{"action":"stop","category":"blocking-run"}'),
        comment("2026-09-16T02:35:00Z", '{"action":"start","category":"manual-qa"}'),
        comment("2026-09-16T02:50:00Z", '{"action":"stop","category":"manual-qa"}'),
    ]

    stats = execution.execution_stats(
        activities, comments, as_of=datetime.fromisoformat("2026-09-16T03:00:00+00:00")
    )

    assert stats["state_minutes"] == {"In Progress": 90, "Verifying": 30}
    assert stats["execution_minutes"] == {
        "blocking-run": 20, "coding": 40, "manual-qa": 15,
    }
    assert stats["open_timer"] is None


def test_cancelled_work_keeps_completed_timer_data_and_is_final() -> None:
    activities = [
        activity("2026-09-16T01:00:00Z", "Todo", "In Progress"),
        activity("2026-09-16T02:00:00Z", "In Progress", "Cancelled"),
    ]
    comments = [
        comment(
            "2026-09-16T01:10:00Z",
            '{"action":"start","category":"coding"}',
        ),
        comment(
            "2026-09-16T01:40:00Z",
            '{"action":"stop","category":"coding"}',
        ),
    ]

    stats = execution.execution_stats(
        activities,
        comments,
        as_of=datetime.fromisoformat("2026-09-16T02:00:00+00:00"),
    )

    assert stats["state_minutes"] == {"In Progress": 60}
    assert stats["execution_minutes"] == {"coding": 30}
    assert stats["current_state"]["name"] == "Cancelled"
    assert stats["open_timer"] is None


def test_timer_events_reject_nested_and_mismatched_sessions() -> None:
    started = [comment(
        "2026-09-16T01:05:00Z", '{"action":"start","category":"coding"}'
    )]

    with pytest.raises(GuardViolation, match="already running"):
        execution.timer_event(started, action="start", category="manual-qa")
    with pytest.raises(GuardViolation, match="requires an open timer"):
        execution.timer_event([], action="stop", category=None)


def test_timeline_keeps_rework_transitions_and_ignores_ordinary_comments() -> None:
    activities = [
        activity("2026-09-16T01:00:00Z", "Todo", "In Progress"),
        activity("2026-09-16T02:00:00Z", "In Progress", "Verifying"),
        activity("2026-09-16T02:10:00Z", "Verifying", "In Progress"),
    ]
    comments = [
        SimpleNamespace(id="ordinary", created_at="2026-09-16T01:01:00Z",
                        comment_html="<p>ordinary progress</p>", actor="worker-id"),
        comment("2026-09-16T01:05:00Z", '{"action":"start","category":"coding"}'),
    ]

    rows = execution.timeline(activities, comments)

    assert [row["kind"] for row in rows] == ["state", "timer", "state", "state"]
    assert rows[-1]["detail"] == "Verifying → In Progress"


def test_timeline_command_reads_plane_activities_and_names_the_actor(
    config_path, board, client, monkeypatch
) -> None:
    client.cards = [Card(id="card-uuid", sequence_id=12)]
    client.activities = [activity("2026-09-16T01:00:00Z", "Todo", "In Progress")]
    monkeypatch.setattr(Context, "board", property(lambda self: board))

    result = CliRunner().invoke(
        cli, ["--conf", str(config_path), "--json", "card", "timeline", "DEMO-12"]
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)[0] == {
        "when": "2026-09-16T01:00:00+00:00",
        "kind": "state",
        "actor": "worker-id",
        "detail": "Todo → In Progress",
        "source_id": "activity-2026-09-16T01:00:00Z",
    }


def test_timer_start_writes_a_visible_machine_readable_comment_with_readback(
    config_path, board, client, monkeypatch
) -> None:
    client.cards = [Card(id="card-uuid", sequence_id=12)]
    monkeypatch.setattr(Context, "board", property(lambda self: board))

    result = CliRunner().invoke(
        cli, ["--conf", str(config_path), "card", "timer", "start", "DEMO-12", "coding"]
    )

    assert result.exit_code == 0, result.output
    assert "timer start coding" in result.output
    assert execution.EVENT_PREFIX in client.comments[0].comment_html
    assert [call[0] for call in client.calls[-2:]] == ["comments.create", "comments.list"]


def test_stats_count_each_review_sendback_but_not_other_reopenings() -> None:
    activities = [
        activity("2026-09-16T01:00:00Z", "Todo", "In Progress"),
        activity("2026-09-16T02:00:00Z", "In Progress", "Verifying"),
        activity("2026-09-16T02:10:00Z", "Verifying", "In Progress"),
        activity("2026-09-16T03:00:00Z", "In Progress", "Verifying"),
        activity("2026-09-16T03:10:00Z", "Verifying", "In Progress"),
        activity("2026-09-16T04:00:00Z", "In Progress", "Done"),
        activity("2026-09-16T04:10:00Z", "Done", "In Progress"),
    ]
    result = execution.execution_stats(
        activities, [], as_of=datetime.fromisoformat("2026-09-16T05:00:00Z")
    )
    assert result["rework_count"] == 2
    empty = execution.execution_stats(
        [], [], as_of=datetime.fromisoformat("2026-09-16T05:00:00Z")
    )
    assert empty["rework_count"] == 0


def test_card_stats_reports_rework_in_human_and_json(config_path, board, client, monkeypatch):
    client.cards = [Card(id="card-uuid", sequence_id=12)]
    client.activities = [activity("2026-09-16T01:00:00Z", "Verifying", "In Progress")]
    monkeypatch.setattr(Context, "board", property(lambda self: board))
    args = ["--conf", str(config_path), "card", "stats", "DEMO-12"]
    result = CliRunner().invoke(cli, ["--json", *args])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["rework_count"] == 1
    human = CliRunner().invoke(cli, args)
    assert human.exit_code == 0, human.output
    assert "Rework count: 1" in human.output
