"""Batch card mutations are bounded, preflighted, and read back."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from plane_proj.cli import Context, cli
from plane_proj.guards import GuardViolation, PlaneProjError, ReadbackFailed
from tests.conftest import Card, FakeClient


def test_move_many_selects_one_source_state_and_reads_every_write_back(
    board, client: FakeClient, config_path, monkeypatch
):
    client.cards = [
        Card(id="one", sequence_id=1, state="state-backlog"),
        Card(id="two", sequence_id=2, state="state-backlog"),
        Card(id="three", sequence_id=3, state="state-todo"),
    ]
    monkeypatch.setattr(Context, "board", property(lambda self: board))

    result = CliRunner().invoke(
        cli,
        ["--conf", str(config_path), "--json", "card", "move-many",
         "--from", "Backlog", "--to", "Todo"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "from": "Backlog", "to": "Todo", "cards": ["DEMO-1", "DEMO-2"]
    }
    assert len(client.named("work_items.list")) == 1
    assert len(client.named("work_items._patch")) == 2
    assert len(client.named("work_items.retrieve")) == 2


def test_in_progress_card_can_be_cancelled_with_readback(
    board, client: FakeClient,
):
    card = Card(id="one", sequence_id=1, state="state-progress")
    client.cards = [card]

    board.move_state(card, "Cancelled")

    assert client.cards[0].state == "state-cancelled"
    assert len(client.named("work_items._patch")) == 1
    assert len(client.named("work_items.retrieve")) == 1


def test_move_many_refuses_a_stale_explicit_selection_before_writing(board, client: FakeClient):
    client.cards = [
        Card(id="one", sequence_id=1, state="state-backlog"),
        Card(id="two", sequence_id=2, state="state-todo"),
    ]

    with pytest.raises(GuardViolation, match="DEMO-2.*Todo.*expected Backlog"):
        board.move_states(["DEMO-1", "DEMO-2"], from_state="Backlog", to_state="Todo")

    assert client.write_calls == []


def test_move_many_refuses_more_than_batch_maximum_cards_before_writing(
    board, client: FakeClient
):
    client.cards = [
        Card(id=f"card-{number}", sequence_id=number, state="state-backlog")
        for number in range(1, 102)
    ]

    with pytest.raises(GuardViolation, match=r"101 cards.*maximum is 100"):
        board.move_states([], from_state="Backlog", to_state="Todo")

    assert client.write_calls == []


@pytest.mark.parametrize(
    ("cards", "references", "from_state", "to_state", "message"),
    [
        ([Card(id="one", sequence_id=1)], ["DEMO-1"], "Todo", "Todo", "must differ"),
        ([], [], "Backlog", "Todo", "No cards are in 'Backlog'"),
        (
            [Card(id="one", sequence_id=1)],
            ["DEMO-1", "1"],
            "Todo",
            "Done",
            "appears more than once",
        ),
        ([Card(id="one", sequence_id=1)], ["not-a-card"], "Todo", "Done", "not a card"),
        ([Card(id="one", sequence_id=1)], ["DEMO-2"], "Todo", "Done", "No card"),
    ],
)
def test_move_many_preflight_guards_leave_every_card_untouched(
    board,
    client: FakeClient,
    cards: list[Card],
    references: list[str],
    from_state: str,
    to_state: str,
    message: str,
):
    client.cards = cards

    with pytest.raises(PlaneProjError, match=message):
        board.move_states(references, from_state=from_state, to_state=to_state)

    assert client.write_calls == []


def test_move_many_readback_must_confirm_each_card(board, client: FakeClient):
    client.cards = [Card(id="one", sequence_id=1, state="state-backlog")]
    client.retrieved = Card(id="one", sequence_id=1, state="state-backlog")

    with pytest.raises(ReadbackFailed, match="State 'Todo'.*different state"):
        board.move_states(["DEMO-1"], from_state="Backlog", to_state="Todo")

    assert client.named("work_items._patch")
    assert client.named("work_items.retrieve")
