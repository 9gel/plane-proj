"""Ready work and the critical path across open sprints."""

from __future__ import annotations

import pytest

from plane_proj import dependencies
from plane_proj.guards import GuardViolation


def card(card_id: str, state: str = "Todo", points: int = 1,
         sprint: int = 1) -> dict:
    return {"id": card_id, "ref": f"DEMO-{card_id}", "title": card_id,
            "state": state, "points": points, "sprint": sprint}


def facts(cards: list[dict], blocked_by: dict[str, list[str]],
          states: dict[str, tuple[str, str]] | None = None) -> dict:
    known = {item["id"]: (item["ref"], item["state"]) for item in cards}
    return {"cards": cards, "blocked_by": blocked_by,
            "states": known | (states or {})}


def test_ready_lists_waiting_cards_whose_blockers_are_settled() -> None:
    found = facts(
        [card("a"), card("b", sprint=2), card("c", sprint=2),
         card("d", state="Backlog", sprint=3), card("e", "In Progress")],
        {"b": ["done"], "c": ["a"], "d": ["gone"]},
        {"done": ("DEMO-done", "Done")},
    )

    result = dependencies.ready(found)

    assert [item["ref"] for item in result["ready"]] == ["DEMO-a", "DEMO-b"]
    # A blocker no longer on the board keeps the card blocked.
    assert {item["ref"]: item["blocked_by"] for item in result["blocked"]} == {
        "DEMO-c": ["DEMO-a"], "DEMO-d": ["gone"],
    }


def test_critical_path_follows_the_heaviest_chain_across_sprints() -> None:
    found = facts(
        [card("a", points=3), card("b", points=1), card("c", points=2,
                                                      sprint=2),
         card("d", points=5, sprint=2)],
        # a → c and b → c; d is independent but heaviest on its own.
        {"c": ["a", "b"]},
    )

    result = dependencies.critical_path(found, use_points=True)

    assert result["total"] == 11
    assert result["critical_path"] == 5
    assert [item["ref"] for item in result["chain"]] in (
        ["DEMO-a", "DEMO-c"], ["DEMO-d"],
    )
    assert result["speedup_ceiling"] == 2.2


def test_critical_path_counts_cards_without_estimates() -> None:
    found = facts([card("a"), card("b"), card("c")],
                  {"b": ["a"], "c": ["b"]})

    result = dependencies.critical_path(found, use_points=False)

    assert (result["unit"], result["critical_path"]) == ("cards", 3)
    assert result["speedup_ceiling"] == 1.0


def test_a_dependency_cycle_is_reported_by_its_rule() -> None:
    found = facts([card("a"), card("b")], {"a": ["b"], "b": ["a"]})

    with pytest.raises(GuardViolation, match="Dependency rule"):
        dependencies.critical_path(found, use_points=True)


def test_no_open_cards_has_no_ceiling() -> None:
    result = dependencies.critical_path(facts([], {}), use_points=True)

    assert (result["total"], result["speedup_ceiling"]) == (0, None)
