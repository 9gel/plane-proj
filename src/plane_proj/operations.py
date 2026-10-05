"""Resumable board operations, journaled in the sprint register.

A transition is inspect → stop activity → move (with readback) → collect
→ receipt, and any remote step can lose its response after landing. The
journal records completed steps under the caller's operation id, so a
retry resumes exactly where the last attempt stopped: it never replays a
move, never writes a second timer event, and returns the stored receipt
once one exists.

This is not a distributed transaction. Plane CE has no conditional
writes, so a competing writer is detected — an unexpected state on a
fresh operation is a conflict — never prevented. Convergence on retry is
scoped to an operation's own journaled intent: a state or timer already
matching what *this* operation set is its own landed write, while the
same observation on a fresh operation refuses.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from plane_proj import execution as execution_module
from plane_proj import sprints as sprints_module
from plane_proj import text as text_module
from plane_proj.guards import GuardViolation

STEP_ACTIVITY_STOPPED = "activity-stopped"
STEP_REASON_RECORDED = "rework-reason-recorded"
STEP_MOVED = "moved"
STEP_COLLECTED = "collected"

TERMINAL_STATES = frozenset({"done", "cancelled"})


def _state_label(project: Any, state_id: str) -> str:
    for name, known_id in project.states.items():
        if known_id == state_id:
            return name
    return state_id or "unknown"


def _containing_sprint(
    connection: Any, board: Any, item: Any
) -> sprints_module.Sprint:
    """The current sprint whose cycle holds this card, or a refusal.

    Off-sprint assignments have no card timers and no snapshots, so a
    transition outside every current sprint cycle has nothing to collect
    and is refused before any write.
    """
    for sprint in sprints_module.fetch_sprints(connection):
        if sprint.status != sprints_module.STATUS_CURRENT:
            continue
        if sprint.cycle_id is None:
            continue
        if str(item.id) in board.cycle_card_ids(sprint.cycle_id):
            return sprint
    raise sprints_module.SprintError(
        f"card {getattr(item, 'sequence_id', item.id)} is in no current "
        "sprint cycle; transition collects telemetry and requires one. "
        "Off-sprint work has no card timers or snapshots."
    )


def _stop_open_timer(board: Any, item: Any) -> str | None:
    """Stop whatever timer is open; None when nothing is running.

    Nothing running is satisfied rather than an error: on a resumed
    operation the earlier stop may have landed with its response lost,
    and refusing here would wedge the retry forever.
    """
    opened = execution_module.open_timer(board.comments(item))
    if opened is None:
        return None
    board.comment(item, text_module.to_html(
        execution_module.event_text("stop", str(opened["category"]))
    ))
    return str(opened["category"])


def _collect_snapshot(
    connection: Any, board: Any, item: Any,
    sprint: sprints_module.Sprint, *, final_state: str,
) -> str:
    activities, comments = board.activities(item), board.comments(item)
    # Read the clock after the telemetry and keep full precision for the
    # cutoff: Plane stamps the move just written with sub-second precision,
    # so a cutoff read first, or truncated to the second, precedes it. The
    # register stores whole seconds; only the label is truncated.
    as_of = datetime.now(UTC)
    captured_at = as_of.isoformat(timespec="seconds")
    stats = execution_module.execution_stats(activities, comments, as_of=as_of)
    sprints_module.record_execution_snapshot(
        connection,
        sprint_id=sprint.sprint_id,
        work_item_id=str(item.id),
        card_reference=(
            f"{board.project.key}-{getattr(item, 'sequence_id', '?')}"
        ),
        captured_at=captured_at,
        is_final=final_state.casefold() in TERMINAL_STATES,
        stats=stats,
    )
    return captured_at


def run_transition(
    connection: Any,
    board: Any,
    *,
    reference: str,
    from_state: str,
    to_state: str,
    stop_activity: bool,
    operation_id: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """One resumable state transition with immediate collection.

    A review send-back (Verifying → In Progress) must name its reason,
    which is posted as a visible comment before the move.
    """
    request = {
        "card": reference,
        "from": from_state,
        "to": to_state,
        "stop_activity": stop_activity,
    }
    if reason is not None:
        request["reason"] = reason
    claimed = sprints_module.claim_operation(
        connection, operation_id, "transition", reference, request,
    )
    if claimed["receipt"] is not None:
        return claimed["receipt"]
    steps: list[str] = claimed["steps"]

    # Every check before the first write.
    sendback = execution_module.is_rework(from_state, to_state)
    if sendback:
        # Refuses a missing or unknown reason.
        execution_module.rework_text(reason or "")
    elif reason is not None:
        raise GuardViolation(
            "Rework reason rule: only a send-back (Verifying → In Progress) "
            f"takes --reason; {from_state} → {to_state} is not one."
        )
    source_id = board.project.state_id(from_state)
    target_id = board.project.state_id(to_state)
    if source_id == target_id:
        raise GuardViolation("Transition source and target must differ.")
    item = board.find(reference)
    sprint = _containing_sprint(connection, board, item)
    state_id = str(getattr(item, "state", ""))
    if STEP_MOVED not in steps:
        board.check_admission(item, to_state)
        board.check_transition(item, from_state, to_state)

    if STEP_MOVED in steps:
        if state_id != target_id:
            raise GuardViolation(
                f"Transition conflict: this operation already moved "
                f"{reference} to {to_state!r}, but the card now reads "
                f"{_state_label(board.project, state_id)!r} — a competing "
                "writer changed it. Resolve the board before retrying."
            )
    elif state_id == target_id:
        if claimed["fresh"]:
            raise GuardViolation(
                f"Transition conflict: {reference} is already in "
                f"{to_state!r} and nothing records this operation moving "
                "it. Refusing to fabricate a transition; inspect who "
                "moved it."
            )
        # A resumed operation whose move landed with the response lost.
        sprints_module.record_operation_step(
            connection, operation_id, STEP_MOVED
        )
        steps.append(STEP_MOVED)
    elif state_id != source_id:
        raise GuardViolation(
            f"Transition conflict: {reference} is in "
            f"{_state_label(board.project, state_id)!r}, expected "
            f"{from_state!r}. No write was sent."
        )

    if (
        to_state.casefold() in TERMINAL_STATES
        and not stop_activity
        and STEP_MOVED not in steps
    ):
        opened = execution_module.open_timer(board.comments(item))
        if opened is not None:
            raise GuardViolation(
                f"{reference} has a running {opened['category']!r} timer; a "
                f"card settles in {to_state!r} only with its timer closed, or "
                "closure preflight refuses later. Retry with --stop-activity."
            )

    stopped_category: str | None = None
    if stop_activity and STEP_ACTIVITY_STOPPED not in steps:
        stopped_category = _stop_open_timer(board, item)
        sprints_module.record_operation_step(
            connection, operation_id, STEP_ACTIVITY_STOPPED
        )
        steps.append(STEP_ACTIVITY_STOPPED)

    if sendback and STEP_REASON_RECORDED not in steps:
        comments = board.comments(item)
        if not execution_module.rework_recorded(comments, operation_id):
            board.comment(item, text_module.to_html(
                execution_module.rework_text(str(reason), operation_id)
            ))
        sprints_module.record_operation_step(
            connection, operation_id, STEP_REASON_RECORDED
        )
        steps.append(STEP_REASON_RECORDED)

    if STEP_MOVED not in steps:
        # Checked above, before the timer-stop and reason writes.
        board.move_state(item, to_state, transition_checked=True)
        sprints_module.record_operation_step(
            connection, operation_id, STEP_MOVED
        )
        steps.append(STEP_MOVED)

    captured_at = _collect_snapshot(
        connection, board, item, sprint, final_state=to_state,
    )
    sprints_module.record_operation_step(
        connection, operation_id, STEP_COLLECTED
    )
    steps.append(STEP_COLLECTED)

    receipt = {
        "operation_id": operation_id,
        "card": f"{board.project.key}-{getattr(item, 'sequence_id', '?')}",
        "from": from_state,
        "to": to_state,
        "stopped_activity": stopped_category,
        "reason": reason,
        "sprint_id": sprint.sprint_id,
        "collected_at": captured_at,
        "steps": list(dict.fromkeys(steps)),
        "completed": True,
    }
    sprints_module.complete_operation(connection, operation_id, receipt)
    return receipt


def run_delayed_activity(
    connection: Any,
    board: Any,
    *,
    reference: str,
    category: str,
    started_at: str,
    ended_at: str,
    evidence_ref: str,
    operation_id: str,
) -> dict[str, Any]:
    """Record one provenance-backed delayed activity interval, once.

    The interval keeps its reported occurrence times and its server
    receipt time separately; missing intervals stay missing rather than
    being inferred from message order. Validation refuses future or
    inverted times, missing evidence, and overlap with anything already
    timed — all before the write.
    """
    request = {
        "card": reference,
        "category": category,
        "started_at": started_at,
        "ended_at": ended_at,
        "evidence_ref": evidence_ref,
    }
    claimed = sprints_module.claim_operation(
        connection, operation_id, "activity", reference, request,
    )
    if claimed["receipt"] is not None:
        return claimed["receipt"]

    item = board.find(reference)
    body = execution_module.delayed_interval_event(
        board.comments(item),
        category=category,
        started_at=started_at,
        ended_at=ended_at,
        evidence_ref=evidence_ref,
        operation_id=operation_id,
        now=datetime.now(UTC),
    )
    if body is not None:
        board.comment(item, text_module.to_html(body))

    receipt = {
        "operation_id": operation_id,
        "card": f"{board.project.key}-{getattr(item, 'sequence_id', '?')}",
        "category": category,
        "started_at": started_at,
        "ended_at": ended_at,
        "evidence_ref": evidence_ref,
        "recorded": body is not None,
        "completed": True,
    }
    sprints_module.complete_operation(connection, operation_id, receipt)
    return receipt


def run_activity_switch(
    connection: Any,
    board: Any,
    *,
    reference: str,
    category: str,
    operation_id: str,
) -> dict[str, Any]:
    """Switch the card's open activity timer to `category`, exactly once.

    A timer already open under `category` is convergent success with no
    new event — which is both idempotency for retries and the guard
    against duplicate events.
    """
    execution_module.require_category(category)
    request = {"card": reference, "category": category}
    claimed = sprints_module.claim_operation(
        connection, operation_id, "activity", reference, request,
    )
    if claimed["receipt"] is not None:
        return claimed["receipt"]

    item = board.find(reference)
    opened = execution_module.open_timer(board.comments(item))
    events: list[str] = []
    if opened is None or opened["category"] != category:
        if opened is not None:
            board.comment(item, text_module.to_html(
                execution_module.event_text("stop", str(opened["category"]))
            ))
            events.append(f"stop {opened['category']}")
        board.comment(item, text_module.to_html(
            execution_module.event_text("start", category)
        ))
        events.append(f"start {category}")

    receipt = {
        "operation_id": operation_id,
        "card": f"{board.project.key}-{getattr(item, 'sequence_id', '?')}",
        "category": category,
        "events": events,
        "completed": True,
    }
    sprints_module.complete_operation(connection, operation_id, receipt)
    return receipt
