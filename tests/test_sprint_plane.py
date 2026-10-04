"""Plane-side sprint lifecycle and card-cycle membership."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from plane.errors import HttpError

from plane_proj import dependencies
from plane_proj.guards import (
    ConfigError,
    EmptyCycle,
    EstimateTooLargeForCycle,
    GuardViolation,
    ReadbackFailed,
)
from tests.conftest import Card, FakeClient


def test_planned_sprint_totals_read_live_cycle_cards(board, client, monkeypatch) -> None:
    cycles = [
        SimpleNamespace(id="cycle-7", name="Sprint 7 — Delivery"),
        SimpleNamespace(id="cycle-8", name="Sprint 8 — Later"),
    ]
    members = {
        "cycle-7": [
            Card(estimate_point="uuid-3"), Card(estimate_point="uuid-5"),
            Card(estimate_point="uuid-10", state="state-cancelled"),
        ],
        "cycle-8": [Card(estimate_point="uuid-22")],
    }
    monkeypatch.setattr(board, "groupings", lambda kind: cycles)
    monkeypatch.setattr(
        client.cycles,
        "list_work_items",
        lambda slug, project_id, cycle_id, params=None: SimpleNamespace(
            results=members[cycle_id], next_page_results=False, next_cursor=None
        ),
    )

    assert board.planned_sprint_totals({7}) == {7: (2, 8)}


def test_sprint_cycle_cards_report_reference_title_state_and_points(
    board, client, monkeypatch
) -> None:
    members = [Card(id="one", sequence_id=4, name="Ship it", state="state-done",
                    estimate_point="uuid-3")]
    monkeypatch.setattr(
        client.cycles,
        "list_work_items",
        lambda slug, project_id, cycle_id, params=None: SimpleNamespace(
            results=members, next_page_results=False, next_cursor=None
        ),
    )

    assert board.sprint_cycle_cards("cycle-7") == [{
        "id": "one", "ref": "DEMO-4", "title": "Ship it", "state": "Done",
        "points": 3,
    }]


def test_current_sprint_metrics_split_done_and_cancelled(board, client, monkeypatch) -> None:
    members = [
        Card(state="state-todo", estimate_point="uuid-1"),
        Card(state="state-done", estimate_point="uuid-3"),
        Card(state="state-cancelled", estimate_point="uuid-5"),
    ]
    monkeypatch.setattr(
        client.cycles,
        "list_work_items",
        lambda slug, project_id, cycle_id, params=None: SimpleNamespace(
            results=members, next_page_results=False, next_cursor=None
        ),
    )

    assert board.sprint_cycle_metrics("cycle-7") == {
        "cards_current": 3,
        "points_current": 9,
        "cards_done": 1,
        "points_done": 3,
        "cards_cancelled": 1,
        "points_cancelled": 5,
    }


def test_sprint_start_is_convergent_after_partial_or_complete_retries(
    board, client: FakeClient, monkeypatch
) -> None:
    cycle = SimpleNamespace(id="cycle-7", name="Sprint 7 — Delivery", start_date=None)
    later = SimpleNamespace(id="cycle-8", name="Sprint 8 — Later")
    client.cards = [
        Card(id="admitted", sequence_id=1, state="state-backlog"),
        Card(id="outside", sequence_id=2, state="state-todo"),
        Card(id="later", sequence_id=3, state="state-backlog"),
        Card(id="settled", sequence_id=4, state="state-done"),
        Card(id="dropped", sequence_id=5, state="state-cancelled"),
    ]
    memberships = {"cycle-7": {"admitted", "dropped"}, "cycle-8": {"later"}}
    monkeypatch.setattr(board, "cycle_sprint_id", lambda cycle_id: (7, cycle))
    monkeypatch.setattr(board, "groupings", lambda kind: [cycle, later])
    monkeypatch.setattr(board, "cycle_card_ids", lambda cycle_id: memberships[cycle_id])

    def update_cycle(cycle_id: str, fields: dict[str, str]) -> None:
        cycle.start_date = fields["start_date"]

    monkeypatch.setattr(board, "update_cycle", update_cycle)
    monkeypatch.setattr(
        client.cycles, "retrieve", lambda slug, project_id, cycle_id: cycle
    )

    name, admitted, backlogged = board.start_sprint_cycle(
        "cycle-7", "2026-01-02T03:04:05+08:00"
    )
    assert (name, admitted, backlogged) == (
        "Sprint 7 — Delivery", ["admitted"], ["outside"]
    )
    assert len(client.named("work_items._patch")) == 2

    board.start_sprint_cycle("cycle-7", "2026-01-02T03:04:05+08:00")
    assert len(client.named("work_items._patch")) == 2


def test_sprint_start_refuses_an_oversized_card_before_writing(
    board, client: FakeClient, monkeypatch
) -> None:
    cycle = SimpleNamespace(id="cycle-7", name="Sprint 7 — Delivery", start_date=None)
    client.cards = [
        Card(id="small", sequence_id=1, state="state-backlog", estimate_point="uuid-3"),
        Card(id="large", sequence_id=2, state="state-backlog", estimate_point="uuid-5"),
    ]
    monkeypatch.setattr(board, "cycle_sprint_id", lambda cycle_id: (7, cycle))
    monkeypatch.setattr(board, "groupings", lambda kind: [cycle])
    monkeypatch.setattr(board, "cycle_card_ids", lambda cycle_id: {"small", "large"})

    with pytest.raises(EstimateTooLargeForCycle):
        board.start_sprint_cycle("cycle-7", "2026-01-02T03:04:05+08:00")

    assert client.write_calls == []
    assert cycle.start_date is None


@pytest.mark.parametrize("members", [set(), {"dropped"}])
def test_sprint_start_refuses_a_cycle_with_no_admissible_card_before_writing(
    board, client: FakeClient, monkeypatch, members: set[str]
) -> None:
    # A real register recorded a sprint opened at 0 cards/0 points because
    # its cycle was empty at start; its later cards then read as added scope.
    cycle = SimpleNamespace(id="cycle-7", name="Sprint 7 — Delivery", start_date=None)
    client.cards = [
        Card(id="dropped", sequence_id=1, state="state-cancelled"),
        Card(id="stray", sequence_id=2, state="state-todo"),
    ]
    monkeypatch.setattr(board, "cycle_sprint_id", lambda cycle_id: (7, cycle))
    monkeypatch.setattr(board, "groupings", lambda kind: [cycle])
    monkeypatch.setattr(board, "cycle_card_ids", lambda cycle_id: members)

    with pytest.raises(EmptyCycle, match="Sprint 7"):
        board.start_sprint_cycle("cycle-7", "2026-01-02T03:04:05+08:00")

    assert client.write_calls == []
    assert cycle.start_date is None


def test_sprint_close_refuses_unsettled_cycle_cards_before_writing(
    board, client: FakeClient, monkeypatch
) -> None:
    cycle = SimpleNamespace(id="cycle-7", name="Sprint 7", end_date=None)
    client.cards = [Card(id="one", sequence_id=1, state="state-todo")]
    monkeypatch.setattr(board, "cycle_sprint_id", lambda cycle_id: (7, cycle))
    monkeypatch.setattr(board, "cycle_card_ids", lambda cycle_id: {"one"})
    updates: list[dict[str, str]] = []
    monkeypatch.setattr(board, "update_cycle", lambda cycle_id, fields: updates.append(fields))

    with pytest.raises(GuardViolation, match="DEMO-1.*Todo"):
        board.close_sprint_cycle("cycle-7", "2026-01-02T04:04:05+08:00")

    assert updates == []


def test_sprint_close_ends_without_archiving_cycle(
    board, client: FakeClient, monkeypatch
) -> None:
    cycle = SimpleNamespace(id="cycle-7", name="Sprint 7", end_date=None)
    client.cards = [
        Card(id="done", sequence_id=1, state="state-done"),
        Card(id="cancelled", sequence_id=2, state="state-cancelled"),
    ]
    monkeypatch.setattr(board, "cycle_sprint_id", lambda cycle_id: (7, cycle))
    monkeypatch.setattr(board, "cycle_card_ids", lambda cycle_id: {"done", "cancelled"})
    monkeypatch.setattr(
        client.cycles, "retrieve", lambda slug, project_id, cycle_id: cycle
    )

    def update_cycle(cycle_id: str, fields: dict[str, str]) -> None:
        cycle.end_date = fields["end_date"]

    monkeypatch.setattr(board, "update_cycle", update_cycle)
    monkeypatch.setattr(
        client.cycles,
        "archive",
        lambda *args, **kwargs: pytest.fail("sprint close archived its cycle"),
    )
    monkeypatch.setattr(board, "groupings", lambda kind, archived=False: [])

    assert board.close_sprint_cycle(
        "cycle-7", "2026-01-02T04:04:05+08:00"
    ) == "Sprint 7"
    assert cycle.end_date == "2026-01-02T04:04:05+08:00"


def test_card_cycle_membership_requires_readback(board, monkeypatch) -> None:
    card = Card(id="one", estimate_point="uuid-2", state="state-done")
    members: set[str] = set()
    monkeypatch.setattr(board, "cycle_card_ids", lambda cycle_id: set(members))
    monkeypatch.setattr(board, "add_to_cycle", lambda card_id, cycle_id: members.add(card_id))
    monkeypatch.setattr(
        board, "remove_from_cycle", lambda card_id, cycle_id: members.discard(card_id)
    )

    board.set_card_cycle(card, "cycle-1")
    assert members == {"one"}
    board.clear_card_cycle(card, "cycle-1")
    assert members == set()


@pytest.mark.parametrize("operation,field", [("start", "start_date"), ("close", "end_date")])
def test_sprint_timestamp_normalization_and_retry(board, client, monkeypatch, operation, field):
    cycle = SimpleNamespace(id="cycle-7", name="Sprint 7", start_date=None, end_date=None)
    # A settled member keeps the cycle non-empty without any card write.
    client.cards = [Card(id="settled", sequence_id=1, state="state-done")]
    monkeypatch.setattr(board, "cycle_sprint_id", lambda cycle_id: (7, cycle))
    monkeypatch.setattr(board, "cycle_card_ids", lambda cycle_id: {"settled"})
    monkeypatch.setattr(
        board, "groupings",
        lambda kind, archived=False: [] if archived else [cycle],
    )
    monkeypatch.setattr(client.cycles, "retrieve", lambda *args: cycle)
    updates = []

    def update(cycle_id, fields):
        updates.append(fields)
        setattr(cycle, field, "2026-09-16T09:46:59Z")

    monkeypatch.setattr(board, "update_cycle", update)
    action = board.start_sprint_cycle if operation == "start" else board.close_sprint_cycle
    action("cycle-7", "2026-09-16T17:46:59+08:00")
    action("cycle-7", "2026-09-16T17:46:59+08:00")
    assert updates == [{field: "2026-09-16T17:46:59+08:00"}]


@pytest.mark.parametrize("actual", [None, "invalid", "2026-09-16T09:47:00Z"])
def test_cycle_timestamp_readback_rejects_mismatch(board, client, monkeypatch, actual):
    monkeypatch.setattr(
        client.cycles, "retrieve", lambda *args: SimpleNamespace(start_date=actual)
    )
    with pytest.raises(ReadbackFailed):
        board._verify_cycle_field("cycle-7", "start_date", "2026-09-16T17:46:59+08:00")


@pytest.mark.parametrize("report", ["current", "planned"])
def test_sprint_totals_refuse_unknown_estimates(board, client, monkeypatch, report):
    cycle = SimpleNamespace(id="cycle-7", name="Sprint 7")
    monkeypatch.setattr(board, "groupings", lambda kind: [cycle])
    monkeypatch.setattr(board, "_cycle_cards", lambda cycle_id: [
        Card(estimate_point="unknown-estimate")
    ])
    with pytest.raises(ConfigError, match="project scale --write"):
        if report == "current":
            board.sprint_cycle_metrics("cycle-7")
        else:
            board.planned_sprint_totals({7})
    assert client.calls == []


def test_dependency_facts_read_each_cycle_once_and_settle_archived_blockers(
    board, client, monkeypatch
) -> None:
    members = {
        "cycle-7": [Card(id="open", sequence_id=1, name="Open",
                         state="state-todo", estimate_point="uuid-3"),
                    Card(id="settled", sequence_id=2, state="state-done")],
    }
    cycle_reads: list[str] = []

    def list_work_items(slug, project_id, cycle_id, params=None):
        cycle_reads.append(cycle_id)
        return SimpleNamespace(results=members[cycle_id],
                               next_page_results=False, next_cursor=None)

    monkeypatch.setattr(client.cycles, "list_work_items", list_work_items)
    elsewhere = Card(id="elsewhere", sequence_id=8, state="state-progress")
    client.cards = [elsewhere]
    monkeypatch.setattr(
        board, "relations",
        lambda card_id: {"blocked_by": ["settled", "elsewhere", "gone"]},
    )
    real_retrieve = client.work_items.retrieve

    def retrieve(slug, project_id, card_id):
        if card_id == "gone":
            raise HttpError("Page not found.", status_code=404)
        return real_retrieve(slug, project_id, card_id)

    monkeypatch.setattr(client.work_items, "retrieve", retrieve)

    facts = board.dependency_facts({7: "cycle-7"})

    assert cycle_reads == ["cycle-7"]
    assert [card["ref"] for card in facts["cards"]] == ["DEMO-1"]
    assert [card["ref"] for card in facts["members"][7]] == ["DEMO-1", "DEMO-2"]
    assert facts["states"]["settled"] == ("DEMO-2", "Done")
    assert facts["states"]["elsewhere"] == ("DEMO-8", "In Progress")
    assert facts["states"]["gone"] == ("archived gone", "Archived")
    # No archived-items listing: some servers answer it with 404.
    assert not client.named("work_items.list_archived")
    assert dependencies.ready(facts)["blocked"][0]["blocked_by"] == ["DEMO-8"]
