"""Card dependencies across open sprints: what can start, and the longest chain.

Facts come from `Board.dependency_facts`: every open card in the current and
planned sprints, its `blocked_by` card ids, and the state of every card on
the board. Nothing here reads the network.
"""

from __future__ import annotations

from typing import Any

from plane_proj.guards import GuardViolation

# Plane archives only Completed or Cancelled work items, so an archived
# blocker is settled.
SETTLED = frozenset({"done", "cancelled", "archived"})
WAITING = frozenset({"todo", "backlog"})


def _blockers(facts: dict[str, Any], card: dict[str, Any]) -> list[str]:
    """References of the card's blockers that are not settled."""
    states = facts["states"]
    open_blockers = []
    for blocker in facts["blocked_by"].get(card["id"], []):
        reference, state = states.get(blocker, (blocker, "unknown"))
        if state.casefold() not in SETTLED:
            open_blockers.append(reference)
    return open_blockers


def ready(facts: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Waiting cards whose blockers are all settled, and those still blocked.

    A blocker missing from the board (deleted or archived) is not settled,
    so the card stays blocked until someone resolves the relation.
    """
    result: dict[str, list[dict[str, Any]]] = {"ready": [], "blocked": []}
    for card in facts["cards"]:
        if card["state"].casefold() not in WAITING:
            continue
        blockers = _blockers(facts, card)
        if blockers:
            result["blocked"].append(card | {"blocked_by": blockers})
        else:
            result["ready"].append(card)
    return result


def critical_path(
    facts: dict[str, Any], *, use_points: bool,
) -> dict[str, Any]:
    """The longest dependency chain among open cards, and the speedup cap.

    Each card weighs its points, or 1 when the project has no estimates.
    Total work divided by the chain is the most any number of agents can
    shorten delivery.
    """
    cards = {card["id"]: card for card in facts["cards"]}

    def weight(card_id: str) -> int:
        return cards[card_id]["points"] if use_points else 1

    best: dict[str, tuple[int, list[str]]] = {}
    visiting: set[str] = set()

    def longest(card_id: str) -> tuple[int, list[str]]:
        if card_id in best:
            return best[card_id]
        if card_id in visiting:
            raise GuardViolation(
                "Dependency rule: blocked_by relations form a cycle through "
                f"{cards[card_id]['ref']}; no order can satisfy them."
            )
        visiting.add(card_id)
        before = (0, [])
        for blocker in facts["blocked_by"].get(card_id, []):
            if blocker in cards:
                candidate = longest(blocker)
                if candidate[0] > before[0]:
                    before = candidate
        visiting.discard(card_id)
        best[card_id] = (before[0] + weight(card_id), [*before[1], card_id])
        return best[card_id]

    total = sum(weight(card_id) for card_id in cards)
    length, chain = max(
        (longest(card_id) for card_id in cards), default=(0, []),
        key=lambda found: found[0],
    )
    return {
        "unit": "points" if use_points else "cards",
        "total": total,
        "critical_path": length,
        "speedup_ceiling": round(total / length, 2) if length else None,
        "chain": [
            {key: cards[card_id][key]
             for key in ("ref", "title", "state", "points", "sprint")}
            for card_id in chain
        ],
    }
