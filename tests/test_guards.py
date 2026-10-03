"""Every guard, watched firing — and watched leaving the board untouched.

A guard that raises after sending the create is not a guard: the card exists,
unplaced, and nobody is looking for it. So each refusal here asserts twice —
that the exception came, and that `client.write_calls` is empty.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from plane_proj.board import Board
from plane_proj.guards import (
    CrossCycleDependency,
    EstimateOnUnestimatedAssignee,
    EstimateTooLargeForCycle,
    GuardViolation,
    MissingCycle,
    MissingEstimate,
    MissingModule,
    OrphanedCard,
    ReadbackFailed,
    UnknownEstimateValue,
    UnknownModule,
)
from tests.conftest import ALICE, AUTOMATION, Card, FakeClient

GOOD = {
    "title": "Acquire zoning bytes",
    "description_html": "<p>Build it.</p>",
    "assignee_id": AUTOMATION,
    "module_name": "pipeline",
    "cycle_name": "Sprint 1",
    "estimate": 3,
}


def _cycles_off(board: Board) -> None:
    board.project = replace(
        board.project, rules=replace(board.project.rules, require_cycle=False)
    )


def create(board: Board, **overrides):
    return board.create_card(**{**GOOD, **overrides})


def test_a_complete_card_is_created_placed_and_verified(board: Board, client: FakeClient):
    written = create(board)

    assert written.card_id == "new-card"
    assert client.named("cycles.add_work_items"), "a created card joins no cycle on its own"
    assert client.named("modules.add_work_items"), "a created card joins no module on its own"
    assert client.named("work_items.retrieve"), "a write is not claimed without a readback"
    assert "verified" in written.steps


def test_estimate_writes_both_fields(board: Board, client: FakeClient):
    """`point` alone displays as unestimated; `estimate_point` alone is unrecoverable."""
    create(board, estimate=3)
    payload = client.named("work_items.create")[0][2]["data"]

    assert payload.estimate_point == "uuid-3"
    assert payload.point == 3


def test_estimate_above_the_point_ceiling_omits_the_legacy_field(board: Board, client: FakeClient):
    """The API caps `point` at 12, so 22 travels in `estimate_point` alone."""
    _cycles_off(board)
    create(board, estimate=22, cycle_name=None)
    payload = client.named("work_items.create")[0][2]["data"]

    assert payload.estimate_point == "uuid-22"
    assert payload.point is None


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"cycle_name": None}, MissingCycle),
        ({"module_name": None}, MissingModule),
        ({"module_name": "webapp"}, UnknownModule),
        ({"estimate": None}, MissingEstimate),
        ({"estimate": 4}, UnknownEstimateValue),
        ({"estimate": 5}, EstimateTooLargeForCycle),
        ({"assignee_id": ALICE}, EstimateOnUnestimatedAssignee),
        ({"title": "  "}, GuardViolation),
        ({"description_html": ""}, GuardViolation),
    ],
)
def test_a_refused_card_sends_nothing(board: Board, client: FakeClient, overrides, expected):
    with pytest.raises(expected):
        create(board, **overrides)

    assert client.write_calls == [], "the guard fired after the board was already changed"


def test_a_card_for_an_unestimated_assignee_is_created_blank(board: Board, client: FakeClient):
    """Blank, never 0 — zero counts in totals as an estimate of nothing."""
    create(board, assignee_id=ALICE, estimate=None)
    payload = client.named("work_items.create")[0][2]["data"]

    assert payload.estimate_point is None
    assert payload.point is None


def test_an_off_scale_estimate_names_the_scale(board: Board):
    with pytest.raises(UnknownEstimateValue, match="1, 2, 3, 5, 10, 22"):
        create(board, estimate=4)


def test_the_cycle_ceiling_does_not_apply_without_cycles(board: Board, client: FakeClient):
    """With cycles off a 5 is an ordinary card; with cycles on it must be split."""
    _cycles_off(board)
    create(board, estimate=5, cycle_name=None)

    assert client.named("work_items.create")


def test_an_open_card_leaves_its_sprint_only_for_another(board: Board, client: FakeClient):
    with pytest.raises(OrphanedCard, match="set-cycle"):
        board.clear_card_cycle(Card(state="state-backlog"), "cycle-1")

    assert client.write_calls == []
    assert not client.named("cycles.remove_work_item")


def test_an_oversized_card_may_wait_in_backlog_in_a_planned_sprint(
    board: Board, client: FakeClient,
):
    written = create(board, estimate=5, state_name="Backlog")

    assert "cycle=Sprint 1" in written.steps


@pytest.mark.parametrize("state_name", ["Todo", "In Progress", None])
def test_an_oversized_card_is_never_created_for_execution(
    board: Board, client: FakeClient, state_name,
):
    # With no state named, Plane's default may be Todo; refuse rather than guess.
    with pytest.raises(EstimateTooLargeForCycle, match="Backlog"):
        create(board, estimate=5, state_name=state_name)

    assert client.write_calls == []


@pytest.mark.parametrize("target", ["Todo", "In Progress", "Verifying"])
def test_an_oversized_card_never_leaves_backlog_for_execution(
    board: Board, client: FakeClient, target,
):
    card = Card(state="state-backlog", estimate_point="uuid-5")
    client.cards = [card]

    with pytest.raises(EstimateTooLargeForCycle):
        board.move_state(card, target)
    with pytest.raises(EstimateTooLargeForCycle):
        board.move_states(["DEMO-12"], from_state="Backlog", to_state=target)

    assert client.write_calls == []
    assert card.state == "state-backlog"


def test_an_oversized_card_may_be_cancelled(board: Board, client: FakeClient):
    card = Card(state="state-backlog", estimate_point="uuid-5")
    client.cards = [card]

    board.move_state(card, "Cancelled")

    assert card.state == "state-cancelled"


def test_cycle_membership_follows_the_card_state(board: Board, client: FakeClient, monkeypatch):
    monkeypatch.setattr(board, "cycle_card_ids", lambda cycle_id: {"card-uuid"})
    board.set_card_cycle(Card(state="state-backlog", estimate_point="uuid-5"), "cycle-1")

    client.calls.clear()
    with pytest.raises(EstimateTooLargeForCycle):
        board.set_card_cycle(Card(state="state-todo", estimate_point="uuid-5"), "cycle-1")
    assert client.write_calls == []


def test_an_admitted_card_cannot_be_resized_above_the_ceiling(
    board: Board, client: FakeClient,
):
    with pytest.raises(EstimateTooLargeForCycle):
        board.set_estimate(Card(state="state-todo"), 5)
    assert client.write_calls == []

    client.retrieved = Card(state="state-backlog", estimate_point="uuid-5", point=5)
    board.set_estimate(Card(state="state-backlog"), 5)
    assert client.named("work_items._patch")


def test_blanking_an_estimate_sends_explicit_nulls(board: Board, client: FakeClient):
    """The SDK's typed update drops None fields, so a blank must go out raw."""
    board.set_estimate(Card(assignees=[ALICE]), None)
    payload = client.named("work_items._patch")[0][2]["data"]

    assert payload == {"estimate_point": None, "point": None}
    assert "point" in payload, "an omitted key leaves the old value in place"


def test_a_blank_that_did_not_clear_the_board_is_a_failure(board: Board, client: FakeClient):
    """The bug this guard exists for: point stayed 0 and the tool said done."""
    client.retrieved = Card(id="card-uuid", point=0, estimate_point=None)

    with pytest.raises(ReadbackFailed, match="still carries"):
        board.set_estimate(Card(assignees=[ALICE]), None)


def test_an_estimate_above_the_ceiling_clears_the_legacy_point(board: Board):
    """Omitting `point` left a card reading point=5 beside the UUID for 22."""
    fields = board.project.estimate_fields(22)

    assert fields["estimate_point"] == "uuid-22"
    assert fields["point"] is None


def test_a_readback_that_disagrees_is_a_failure(board: Board, client: FakeClient):
    """The board and the transcript must not be allowed to disagree silently."""
    client.retrieved = Card(id="new-card", estimate_point="uuid-1")

    with pytest.raises(ReadbackFailed, match="reads back"):
        create(board, estimate=3)


def test_a_cycle_card_refuses_a_blocker_outside_the_cycle(board: Board, client: FakeClient):
    inside = Card(id="inside", sequence_id=1, cycle_id="cycle-1")
    outside = Card(id="outside", sequence_id=2)
    client.cycles = type(client.cycles)(client.calls, "cycles", result=_one_page([inside]))

    with pytest.raises(CrossCycleDependency, match="cannot complete"):
        board.add_relation(inside, "blocked_by", [outside])

    assert client.named("relations.create") == []


def test_a_blocker_inside_the_cycle_is_allowed(board: Board, client: FakeClient):
    inside = Card(id="inside", sequence_id=1, cycle_id="cycle-1")
    other = Card(id="other", sequence_id=2, cycle_id="cycle-1")
    client.cycles = type(client.cycles)(client.calls, "cycles", result=_one_page([inside, other]))
    client.work_items.relations = type(client.work_items.relations)(
        client.calls, "relations", result={"blocked_by": [{"issue_id": "other"}]}
    )

    board.add_relation(inside, "blocked_by", [other])

    assert client.named("relations.create")


def _one_page(results):
    class Page:
        def __init__(self) -> None:
            self.results = results
            self.next_page_results = False
            self.next_cursor = None
    return Page()


def test_an_unestimated_assignee_is_reported_before_the_value_is_judged(board: Board):
    """`4` is both off-scale and forbidden here; the useful message is the assignee one."""
    with pytest.raises(EstimateOnUnestimatedAssignee):
        create(board, assignee_id=ALICE, estimate=4)


def test_an_off_scale_value_is_reported_before_the_cycle_ceiling(board: Board):
    """`4` is off-scale and above the ceiling; splitting the card would not help."""
    with pytest.raises(UnknownEstimateValue):
        create(board, estimate=4)


def test_the_double_rejects_a_call_the_sdk_would_reject(client: FakeClient):
    """The regression that hid the broken membership calls.

    `cycles.add_work_items` takes a positional `issue_ids`. Passing
    `data={...}` raises TypeError against the live client, and used to pass
    silently here.
    """
    with pytest.raises(TypeError):
        client.cycles.add_work_items("slug", "project", "cycle", data={"issues": ["x"]})


def test_the_double_accepts_the_call_the_sdk_accepts(client: FakeClient):
    client.cycles.add_work_items("slug", "project", "cycle", ["card"])

    assert client.named("cycles.add_work_items")
