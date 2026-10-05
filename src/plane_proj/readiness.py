"""Sprint readiness assessment, code overlap analysis, and dependency graph.

Evaluates each planned sprint's readiness to start according to cross-sprint
dependencies and declared code scope (Touches).
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from plane_proj import delivery_plan as delivery_plan_module
from plane_proj import scope as scope_module
from plane_proj import sprints as sprints_module

STATE_CAN_START = "Can start"
STATE_OVERLAP = "Overlap"
STATE_UNVERIFIED = "Unverified"
STATE_NOT_READY = "Not ready"

ASSUMED_VELOCITY = 3.5


@dataclass(frozen=True)
class CardFact:
    """Card facts required for readiness assessment."""

    id: str
    ref: str
    title: str
    state: str
    points: int
    sprint_id: int | None
    description_html: str = ""
    blocked_by: tuple[str, ...] = ()
    delivery_plan: delivery_plan_module.DeliveryPlan | None = None

    @property
    def is_settled(self) -> bool:
        return self.state.casefold() in {"done", "cancelled"}

    @property
    def parsed_plan(self) -> delivery_plan_module.DeliveryPlan:
        if self.delivery_plan is not None:
            return self.delivery_plan
        return delivery_plan_module.parse_delivery_plan(self.description_html)


@dataclass(frozen=True)
class SprintFact:
    """Sprint facts required for readiness assessment."""

    sprint_id: int
    title: str
    alias: str | None = None
    goal: str = ""
    position: int = 1
    is_current: bool = False
    cards: tuple[CardFact, ...] = ()

    @property
    def points(self) -> int:
        return sum(c.points for c in self.cards)

    @property
    def open_cards(self) -> list[CardFact]:
        return [c for c in self.cards if not c.is_settled]

    @property
    def remaining_points(self) -> int:
        return sum(c.points for c in self.open_cards)


@dataclass
class OverlapPair:
    """A pair of sprints and cards that share declared code paths."""

    sprint_a: int
    sprint_b: int
    card_a: str
    card_b: str
    paths: list[str]
    is_running: bool = False


def paths_overlap(
    decl_a: str | delivery_plan_module.TouchedPath,
    decl_b: str | delivery_plan_module.TouchedPath,
) -> bool:
    """Return True if two path declarations overlap (one equals or lies under the other)."""
    path_a = (
        decl_a.path if isinstance(decl_a, delivery_plan_module.TouchedPath) else decl_a
    ).strip().removeprefix("./")
    path_b = (
        decl_b.path if isinstance(decl_b, delivery_plan_module.TouchedPath) else decl_b
    ).strip().removeprefix("./")

    if path_a.lower().endswith("(new)"):
        path_a = path_a[:-5].rstrip()
    if path_b.lower().endswith("(new)"):
        path_b = path_b[:-5].rstrip()

    path_a = path_a.removeprefix("./").rstrip("/")
    path_b = path_b.removeprefix("./").rstrip("/")

    if not path_a or not path_b or path_a.lower() == "none" or path_b.lower() == "none":
        return False

    return (
        path_a == path_b
        or path_b.startswith(path_a + "/")
        or path_a.startswith(path_b + "/")
        or scope_module.path_covered(path_a, path_b)
        or scope_module.path_covered(path_b, path_a)
    )


def _explicitly_lists(plan: delivery_plan_module.DeliveryPlan, target: str) -> bool:
    clean_target = target.removeprefix("./").rstrip()
    for decl in (plan.touches or ()):
        p = decl.path.removeprefix("./").rstrip()
        if p.lower().endswith("(new)"):
            p = p[:-5].rstrip()
        if p == clean_target:
            return True
    return False


def card_shared_paths(
    card_a: CardFact,
    card_b: CardFact,
    *,
    shared_paths: Sequence[str] = (),
) -> list[str]:
    """Return list of overlapping paths declared between card_a and card_b."""
    plan_a = card_a.parsed_plan
    plan_b = card_b.parsed_plan

    if plan_a.is_touches_none or plan_b.is_touches_none:
        return []
    if not plan_a.touches or not plan_b.touches:
        return []

    shared: list[str] = []
    for p_a in plan_a.touches:
        for p_b in plan_b.touches:
            if paths_overlap(p_a, p_b):
                clean_a = p_a.path.removeprefix("./").rstrip()
                if clean_a.lower().endswith("(new)"):
                    clean_a = clean_a[:-5].rstrip()
                clean_b = p_b.path.removeprefix("./").rstrip()
                if clean_b.lower().endswith("(new)"):
                    clean_b = clean_b[:-5].rstrip()
                rep = clean_a if len(clean_a) <= len(clean_b) else clean_b
                is_undeclared_shared = False
                for sp in shared_paths:
                    if paths_overlap(rep, sp) and not (
                        _explicitly_lists(plan_a, sp) and _explicitly_lists(plan_b, sp)
                    ):
                        is_undeclared_shared = True
                        break
                if is_undeclared_shared:
                    continue
                if rep not in shared:
                    shared.append(rep)
    return shared


def sprint_name(sprint_id: int, alias: str | None = None) -> str:
    if alias:
        return f"{alias} #{sprint_id}"
    return f"#{sprint_id}"


def gather_sprint_facts(
    sprints: Sequence[sprints_module.Sprint],
    facts: Mapping[str, Any],
) -> tuple[list[SprintFact], list[SprintFact], dict[str, tuple[str, str]]]:
    """Build current and planned SprintFacts and external blocker states from board facts."""
    members = facts.get("members", {})
    blocked_by_map = facts.get("blocked_by", {})
    states = dict(facts.get("states", {}))

    current_sprints: list[SprintFact] = []
    planned_sprints: list[SprintFact] = []

    for sprint in sprints:
        if sprint.status not in {sprints_module.STATUS_CURRENT, sprints_module.STATUS_PLANNED}:
            continue

        raw_cards = members.get(sprint.sprint_id, [])
        card_facts: list[CardFact] = []
        for raw in raw_cards:
            cid = str(raw["id"])
            card_facts.append(
                CardFact(
                    id=cid,
                    ref=raw["ref"],
                    title=raw.get("title", ""),
                    state=raw.get("state", "Todo"),
                    points=int(raw.get("points") or 0),
                    sprint_id=sprint.sprint_id,
                    description_html=str(raw.get("description_html") or ""),
                    blocked_by=tuple(blocked_by_map.get(cid, ())),
                )
            )

        fact = SprintFact(
            sprint_id=sprint.sprint_id,
            title=sprint.title,
            alias=sprint.alias,
            goal=sprint.goal,
            position=sprint.position or 1,
            is_current=(sprint.status == sprints_module.STATUS_CURRENT),
            cards=tuple(card_facts),
        )

        if sprint.status == sprints_module.STATUS_CURRENT:
            current_sprints.append(fact)
        else:
            planned_sprints.append(fact)

    return current_sprints, planned_sprints, states


def evaluate_readiness(
    current_sprints: list[SprintFact],
    planned_sprints: list[SprintFact],
    blocker_states: dict[str, tuple[str, str]],
    *,
    completed_velocities: list[float] | None = None,
    shared_paths: Sequence[str] = (),
) -> dict[str, Any]:
    """Evaluate readiness, overlap, dependencies, and duration estimates."""
    all_sprints = current_sprints + planned_sprints
    card_index: dict[str, CardFact] = {}
    card_sprint_map: dict[str, SprintFact] = {}
    for sprint in all_sprints:
        for card in sprint.cards:
            card_index[card.id] = card
            card_sprint_map[card.id] = sprint

    valid_velocities = [v for v in (completed_velocities or []) if v > 0]
    if valid_velocities:
        velocity = statistics.median(valid_velocities)
        velocity_source = "median"
    else:
        velocity = ASSUMED_VELOCITY
        velocity_source = "assumed"

    running_sprints = [s for s in current_sprints if s.is_current]
    ordered_planned = sorted(planned_sprints, key=lambda s: s.position)

    sprint_states: dict[int, str] = {}
    sprint_reasons: dict[int, list[str]] = {}
    sprint_undeclared: dict[int, int] = {}
    sprint_unassessed: dict[int, int] = {}
    sprint_overlaps: dict[int, list[str]] = {}
    overlap_pairs: list[dict[str, Any]] = []

    sprint_deps: list[tuple[int, int, str]] = []
    dep_edges: set[tuple[int, int]] = set()

    for sprint in ordered_planned:
        for card in sprint.open_cards:
            for blocker_id in card.blocked_by:
                blocker_card = card_index.get(blocker_id)
                if blocker_card is not None:
                    blocker_sprint = card_sprint_map.get(blocker_id)
                    if (
                        blocker_sprint is not None
                        and blocker_sprint.sprint_id != sprint.sprint_id
                        and not blocker_card.is_settled
                    ):
                        upstream_id = blocker_sprint.sprint_id
                        downstream_id = sprint.sprint_id
                        reason = f"{card.ref} needs {blocker_card.ref}"
                        if (upstream_id, downstream_id) not in dep_edges:
                            dep_edges.add((upstream_id, downstream_id))
                            sprint_deps.append((upstream_id, downstream_id, reason))

    startable_sprints: list[SprintFact] = []

    for sprint in ordered_planned:
        reasons: list[str] = []
        overlaps_list: list[str] = []

        # 1. Check Not ready: card waits on unfinished card in another sprint
        is_blocked = False
        for card in sprint.open_cards:
            for blocker_id in card.blocked_by:
                blocker_card = card_index.get(blocker_id)
                if blocker_card is not None:
                    blocker_sprint = card_sprint_map.get(blocker_id)
                    if (
                        blocker_sprint is not None
                        and blocker_sprint.sprint_id != sprint.sprint_id
                        and not blocker_card.is_settled
                    ):
                        is_blocked = True
                        s_name = sprint_name(
                            blocker_sprint.sprint_id, blocker_sprint.alias
                        )
                        prefix = "running" if blocker_sprint.is_current else "planned"
                        b_sprint_desc = f"{prefix} {s_name}"
                        reason_msg = (
                            f"Waits on {b_sprint_desc}: {card.ref} needs {blocker_card.ref}"
                        )
                        if reason_msg not in reasons:
                            reasons.append(reason_msg)
                else:
                    ref_state = blocker_states.get(blocker_id)
                    if ref_state is not None:
                        b_ref, b_state = ref_state
                        if b_state.casefold() not in {"done", "cancelled", "archived"}:
                            is_blocked = True
                            reason_msg = (
                                f"Waits on external card: {card.ref} needs {b_ref} ({b_state})"
                            )
                            if reason_msg not in reasons:
                                reasons.append(reason_msg)
                    else:
                        is_blocked = True
                        reason_msg = f"Waits on external card: {card.ref} needs {blocker_id}"
                        if reason_msg not in reasons:
                            reasons.append(reason_msg)

        # 2. Check Overlap against running sprints and earlier startable planned sprints
        has_overlap = False
        candidates_to_check = running_sprints + startable_sprints

        for other_sprint in candidates_to_check:
            for card in sprint.open_cards:
                for other_card in other_sprint.open_cards:
                    shared = card_shared_paths(card, other_card, shared_paths=shared_paths)
                    if shared:
                        has_overlap = True
                        other_desc = (
                            f"running {sprint_name(other_sprint.sprint_id, other_sprint.alias)}"
                            if other_sprint.is_current
                            else f"{sprint_name(other_sprint.sprint_id, other_sprint.alias)}"
                        )
                        paths_str = ", ".join(shared)
                        overlap_str = (
                            f"Shares {paths_str} with {other_desc} ({card.ref}, {other_card.ref})"
                        )
                        if overlap_str not in overlaps_list:
                            overlaps_list.append(overlap_str)
                        overlap_pairs.append({
                            "sprint_a": sprint.sprint_id,
                            "sprint_b": other_sprint.sprint_id,
                            "card_a": card.ref,
                            "card_b": other_card.ref,
                            "paths": shared,
                            "is_running": other_sprint.is_current,
                        })

        # 3. Check Unverified: some card lacks Touches declaration or assessed marker
        undeclared_count = 0
        unassessed_count = 0
        unverified_reasons: list[str] = []
        for card in sprint.open_cards:
            plan = card.parsed_plan
            card_undeclared = plan.touches is None and not plan.is_touches_none
            card_unassessed = not plan.dependencies_assessed

            if card_undeclared:
                undeclared_count += 1
                msg = f"{card.ref} lacks declared scope"
                if msg not in unverified_reasons:
                    unverified_reasons.append(msg)
            if card_unassessed:
                unassessed_count += 1
                msg = f"{card.ref} dependencies not assessed"
                if msg not in unverified_reasons:
                    unverified_reasons.append(msg)

        is_unverified = bool(undeclared_count or unassessed_count)

        # Precedence: Not ready -> Overlap -> Unverified -> Can start
        if is_blocked:
            state = STATE_NOT_READY
            why = reasons
        elif has_overlap:
            state = STATE_OVERLAP
            why = overlaps_list
        elif is_unverified:
            state = STATE_UNVERIFIED
            why = unverified_reasons
        else:
            state = STATE_CAN_START
            why = []
            startable_sprints.append(sprint)

        sprint_states[sprint.sprint_id] = state
        sprint_reasons[sprint.sprint_id] = why
        sprint_undeclared[sprint.sprint_id] = undeclared_count
        sprint_unassessed[sprint.sprint_id] = unassessed_count
        sprint_overlaps[sprint.sprint_id] = overlaps_list

    # Serial and parallel durations
    total_planned_points = sum(s.points for s in ordered_planned)
    serial_hours = total_planned_points / velocity if velocity > 0 else 0.0

    sprint_hours: dict[int, float] = {}
    for s in running_sprints:
        sprint_hours[s.sprint_id] = s.remaining_points / velocity if velocity > 0 else 0.0
    for s in ordered_planned:
        sprint_hours[s.sprint_id] = s.points / velocity if velocity > 0 else 0.0

    earliest_finish: dict[int, float] = {}
    predecessor: dict[int, int | None] = {}
    finish_visiting: set[int] = set()

    def get_longest_finish(s_id: int) -> float:
        if s_id in earliest_finish:
            return earliest_finish[s_id]
        if s_id in finish_visiting:
            return sprint_hours.get(s_id, 0.0)
        finish_visiting.add(s_id)
        incoming = [u for u, v, _ in sprint_deps if v == s_id]
        if not incoming:
            earliest_finish[s_id] = sprint_hours.get(s_id, 0.0)
            predecessor[s_id] = None
        else:
            best_p = max(incoming, key=get_longest_finish)
            earliest_finish[s_id] = earliest_finish[best_p] + sprint_hours.get(s_id, 0.0)
            predecessor[s_id] = best_p
        finish_visiting.remove(s_id)
        return earliest_finish[s_id]

    planned_ids = [s.sprint_id for s in ordered_planned]
    for s_id in [s.sprint_id for s in all_sprints]:
        get_longest_finish(s_id)

    parallel_hours = max((earliest_finish.get(s_id, 0.0) for s_id in planned_ids), default=0.0)

    critical_chain: list[int] = []
    critical_edges: list[str] = []
    if planned_ids:
        end_sprint = max(planned_ids, key=lambda s_id: earliest_finish.get(s_id, 0.0))
        curr: int | None = end_sprint
        while curr is not None:
            critical_chain.append(curr)
            curr = predecessor.get(curr)
        critical_chain.reverse()
        for i in range(len(critical_chain) - 1):
            critical_edges.append(f"{critical_chain[i]}-{critical_chain[i+1]}")

    col: dict[int, int] = {}
    col_visiting: set[int] = set()

    def get_col(s_id: int) -> int:
        if s_id in col:
            return col[s_id]
        if s_id in col_visiting:
            return 0
        col_visiting.add(s_id)
        is_clear = (
            any(s.sprint_id == s_id for s in running_sprints)
            or sprint_states.get(s_id) == STATE_CAN_START
        )
        base = 0 if is_clear else 1
        incoming = [u for u, v, _ in sprint_deps if v == s_id]
        if not incoming:
            col[s_id] = base
        else:
            col[s_id] = max(base, *(get_col(u) + 1 for u in incoming))
        col_visiting.remove(s_id)
        return col[s_id]

    for s_id in [s.sprint_id for s in all_sprints]:
        get_col(s_id)

    queue_rows = []
    for s in ordered_planned:
        pts = s.points
        hours = pts / velocity if velocity > 0 else 0.0
        why_list = sprint_reasons[s.sprint_id]
        queue_rows.append({
            "position": s.position,
            "sprint_id": s.sprint_id,
            "alias": s.alias,
            "title": s.title,
            "goal": s.goal,
            "state": sprint_states[s.sprint_id],
            "why": why_list,
            "reasons": why_list,
            "cards_count": len(s.cards),
            "points": pts,
            "hours": hours,
            "undeclared": sprint_undeclared[s.sprint_id],
            "unassessed": sprint_unassessed[s.sprint_id],
            "overlaps": sprint_overlaps[s.sprint_id],
        })

    return {
        "summary": {
            "sprints": len(ordered_planned),
            "cards": sum(len(s.cards) for s in ordered_planned),
            "points": total_planned_points,
            "velocity": round(velocity, 2),
            "velocity_source": velocity_source,
            "serial_hours": round(serial_hours, 2),
            "parallel_hours": round(parallel_hours, 2),
        },
        "queue": queue_rows,
        "overlap_pairs": overlap_pairs,
        "graph": {
            "columns": col,
            "dependencies": [
                {"from": u, "to": v, "reason": r} for u, v, r in sprint_deps
            ],
            "critical_path": critical_chain,
            "critical_edges": critical_edges,
        },
    }
