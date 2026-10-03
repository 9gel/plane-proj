"""Every open card belongs to a sprint, and `sprints check` says which do not.

The board here is the real `Board` over `FakeClient`, with cycles and their
members answered per cycle id, so membership is computed exactly as it is
against Plane rather than asserted by a stub.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from plane_proj import execution
from plane_proj.board import Board
from plane_proj.guards import OrphanedCard
from tests.conftest import Card, FakeClient, Recorder, _page

CURRENT = {5: "cycle-5"}
PLANNED = {6, 7}


class Cycles(Recorder):
    """Cycles listed by name; members answered per cycle id."""

    def __init__(self, client: FakeClient, members: dict[str, list[Card]],
                 names: dict[str, str]) -> None:
        super().__init__(client.calls, "cycles")
        self.members, self.names = members, names

    def list(self, *args: Any, **kwargs: Any) -> Any:
        super().__getattr__("list")(*args, **kwargs)
        return _page([
            SimpleNamespace(id=cycle_id, name=name)
            for cycle_id, name in self.names.items()
        ])

    def list_work_items(self, *args: Any, **kwargs: Any) -> Any:
        super().__getattr__("list_work_items")(*args, **kwargs)
        return _page(self.members.get(str(args[2]), []))


def _card(sequence_id: int, state: str) -> Card:
    return Card(id=f"card-{sequence_id}", sequence_id=sequence_id, state=state)


@pytest.fixture
def cards(client: FakeClient) -> dict[str, Card]:
    cards = {
        "loose": _card(1, "state-backlog"),
        "closed-sprint": _card(2, "state-backlog"),
        "admitted": _card(3, "state-todo"),
        "deferred": _card(4, "state-backlog"),
        "early": _card(5, "state-progress"),
        "future": _card(6, "state-backlog"),
        "timed": _card(7, "state-done"),
        "finished": _card(8, "state-done"),
    }
    client.cards = list(cards.values())
    client.cycles = Cycles(
        client,
        members={
            "cycle-4": [cards["closed-sprint"]],
            "cycle-5": [cards["admitted"], cards["deferred"], cards["timed"]],
            "cycle-6": [cards["early"], cards["future"]],
        },
        names={
            "cycle-4": "Sprint 4",
            "cycle-5": "Sprint 5",
            "cycle-6": "Sprint 6 — next",
        },
    )
    client.comments.append(SimpleNamespace(
        id="timer", created_at="2026-09-16T00:30:00Z", actor="worker",
        comment_html=f"<p>{execution.event_text('start', 'coding')}</p>",
    ))
    return cards


def test_check_reports_every_sprint_membership_defect(board: Board, cards):
    findings = {
        (item["card"], item["finding"])
        for item in board.sprint_findings(CURRENT, PLANNED)
    }

    assert findings == {
        ("DEMO-1", "in no sprint"),
        ("DEMO-2", "in no sprint"),  # its sprint closed; it is not planned
        ("DEMO-4", "backlog in running sprint"),
        ("DEMO-5", "active in unstarted sprint"),
        ("DEMO-7", "open timer on settled card"),
        ("—", "planned sprint has no cycle"),
    }


def test_check_writes_nothing(board: Board, client: FakeClient, cards):
    board.sprint_findings(CURRENT, PLANNED)

    assert client.write_calls == []


def test_a_clean_board_has_no_findings(board: Board, client: FakeClient, cards):
    client.cards = [cards["admitted"], cards["future"], cards["finished"]]
    client.comments.clear()

    assert board.sprint_findings(CURRENT, {6}) == []


def test_orphans_refuse_before_any_write(board: Board, client: FakeClient, cards):
    with pytest.raises(OrphanedCard, match="DEMO-1, DEMO-2"):
        board.require_no_orphans(CURRENT, PLANNED)

    assert client.write_calls == []


def test_without_cycles_no_card_is_orphaned(board: Board, client: FakeClient, cards):
    board.project = replace(
        board.project, rules=replace(board.project.rules, require_cycle=False)
    )

    board.require_no_orphans(CURRENT, PLANNED)
    assert "in no sprint" not in {
        item["finding"] for item in board.sprint_findings(CURRENT, PLANNED)
    }
