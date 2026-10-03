"""Execution telemetry derived from Plane activities and structured comments.

Plane Community Edition has no worklogs.  Short, visible comments therefore
carry timer boundaries while Plane's immutable activity stream remains the
authority for state transitions.  The format is deliberately portable: no
project names, state names, member names, or server details are encoded here.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from plane_proj import text as text_module
from plane_proj.guards import GuardViolation

EVENT_PREFIX = "plane-proj-execution/v1 "
EVENT_PREFIX_V2 = "plane-proj-execution/v2 "
_CATEGORY = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise GuardViolation("Execution telemetry has no timestamp; refusing partial statistics.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise GuardViolation(f"Execution telemetry has invalid timestamp {value!r}.") from error
    if parsed.tzinfo is None:
        raise GuardViolation(f"Execution telemetry timestamp {value!r} has no timezone.")
    return parsed


def _comment_events(comments: Iterable[Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for comment in comments:
        plain = text_module.to_plain(getattr(comment, "comment_html", ""))
        if not plain.startswith(EVENT_PREFIX):
            continue
        encoded = plain.removeprefix(EVENT_PREFIX)
        try:
            payload = json.loads(encoded)
        except json.JSONDecodeError as error:
            raise GuardViolation("A plane-proj execution comment contains invalid JSON.") from error
        if not isinstance(payload, dict):
            raise GuardViolation("A plane-proj execution comment must contain a JSON object.")
        action, category = payload.get("action"), payload.get("category")
        if action not in {"start", "stop"} or not isinstance(category, str):
            raise GuardViolation(
                "A plane-proj execution comment has an invalid action or category."
            )
        if _CATEGORY.fullmatch(category) is None:
            raise GuardViolation(f"Execution category {category!r} is not a lowercase slug.")
        events.append({
            "when": _timestamp(getattr(comment, "created_at", None)),
            "action": action,
            "category": category,
            "actor": getattr(comment, "actor", None),
            "source_id": str(getattr(comment, "id", "")),
        })
    return sorted(events, key=lambda event: (event["when"], event["source_id"]))


def _delayed_events(comments: Iterable[Any]) -> list[dict[str, Any]]:
    """Every v2 delayed-interval event, strictly validated.

    A delayed event retains both the reported occurrence interval and the
    server receipt time; neither may stand in for the other. A corrupt
    record refuses statistics rather than degrading them.
    """
    events: list[dict[str, Any]] = []
    for comment in comments:
        plain = text_module.to_plain(getattr(comment, "comment_html", ""))
        if not plain.startswith(EVENT_PREFIX_V2):
            continue
        encoded = plain.removeprefix(EVENT_PREFIX_V2)
        try:
            payload = json.loads(encoded)
        except json.JSONDecodeError as error:
            raise GuardViolation(
                "A plane-proj delayed-execution comment contains "
                "invalid JSON."
            ) from error
        if not isinstance(payload, dict) or payload.get("action") != "interval":
            raise GuardViolation(
                "A plane-proj delayed-execution comment must be an "
                "interval object."
            )
        category = payload.get("category")
        if not isinstance(category, str) or _CATEGORY.fullmatch(category) is None:
            raise GuardViolation(
                f"Delayed-execution category {category!r} is not a "
                "lowercase slug."
            )
        evidence_ref = payload.get("evidence_ref")
        if not isinstance(evidence_ref, str) or not evidence_ref.strip():
            raise GuardViolation(
                "A delayed execution interval has no evidence reference; "
                "unproven times remain missing rather than recorded."
            )
        started = _timestamp(payload.get("started_at"))
        ended = _timestamp(payload.get("ended_at"))
        if ended <= started:
            raise GuardViolation(
                "A delayed execution interval ends before it starts."
            )
        events.append({
            "category": category,
            "started": started,
            "ended": ended,
            "evidence_ref": evidence_ref,
            "operation_id": str(payload.get("operation_id") or ""),
            "received": _timestamp(getattr(comment, "created_at", None)),
            "source_id": str(getattr(comment, "id", "")),
        })
    return sorted(events, key=lambda event: (event["started"], event["source_id"]))


def delayed_interval_event(
    comments: Iterable[Any],
    *,
    category: str,
    started_at: str,
    ended_at: str,
    evidence_ref: str,
    operation_id: str,
    now: datetime,
) -> str | None:
    """Validate one delayed interval and return its comment text.

    Returns None when an identical interval under the same operation id
    is already recorded — the retry converges instead of duplicating.
    Everything else that cannot be honestly recorded raises: a future or
    inverted interval, missing provenance, an operation id reused for a
    different interval, or overlap with any interval already timed. Live
    state history is never rewritten; this only adds a provenance-backed
    record beside it.
    """
    require_category(category)
    if not evidence_ref.strip():
        raise GuardViolation(
            "A delayed execution interval requires --evidence-ref; "
            "unproven times remain missing rather than recorded."
        )
    if not operation_id.strip():
        raise GuardViolation(
            "A delayed execution interval requires an operation id."
        )
    started = _timestamp(started_at)
    ended = _timestamp(ended_at)
    if ended <= started:
        raise GuardViolation(
            f"Delayed interval ends at {ended_at} which is not after "
            f"{started_at}; impossible ordering is refused."
        )
    if ended > now:
        raise GuardViolation(
            f"Delayed interval ends at {ended_at}, in the future; an "
            "interval can only be reported after it happened."
        )
    payload = {
        "action": "interval",
        "category": category,
        "started_at": started.isoformat(),
        "ended_at": ended.isoformat(),
        "evidence_ref": evidence_ref,
        "operation_id": operation_id,
    }
    delayed = _delayed_events(comments)
    for event in delayed:
        if event["operation_id"] != operation_id:
            continue
        if (event["category"], event["started"], event["ended"],
                event["evidence_ref"]) == (
                category, started, ended, evidence_ref):
            return None
        raise GuardViolation(
            f"Operation {operation_id} already recorded a different "
            "delayed interval; use a new operation id."
        )
    occupied: list[tuple[datetime, datetime, str]] = [
        (event["started"], event["ended"],
         f"delayed {event['category']}")
        for event in delayed
    ]
    boundary_events = _comment_events(comments)
    opened: dict[str, Any] | None = None
    for event in boundary_events:
        if event["action"] == "start":
            opened = event
            continue
        if opened is not None:
            occupied.append(
                (opened["when"], event["when"], f"live {event['category']}")
            )
        opened = None
    if opened is not None:
        occupied.append((opened["when"], now, f"open {opened['category']}"))
    for existing_start, existing_end, label in occupied:
        if started < existing_end and existing_start < ended:
            raise GuardViolation(
                f"Delayed interval overlaps the already-timed {label} "
                f"interval ({existing_start.isoformat()} → "
                f"{existing_end.isoformat()}); retroactive overlap is "
                "unsupported."
            )
    return EVENT_PREFIX_V2 + json.dumps(
        payload, separators=(",", ":"), sort_keys=True
    )


def _open_timer(events: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    opened: dict[str, Any] | None = None
    for event in events:
        if event["action"] == "start":
            if opened is not None:
                raise GuardViolation(
                    f"Execution timer {opened['category']!r} was already running when "
                    f"{event['category']!r} started."
                )
            opened = event
            continue
        if opened is None:
            raise GuardViolation("Execution timer stop has no preceding start.")
        if opened["category"] != event["category"]:
            raise GuardViolation(
                f"Execution timer started as {opened['category']!r} but stopped as "
                f"{event['category']!r}."
            )
        opened = None
    return opened


def open_timer(comments: Iterable[Any]) -> dict[str, Any] | None:
    """The currently open timer event, or None when nothing is running."""
    return _open_timer(_comment_events(comments))


def require_category(category: str) -> None:
    """Refuse a category that is not a lowercase slug, or is a timer verb."""
    if _CATEGORY.fullmatch(category) is None:
        raise GuardViolation(
            f"Execution category {category!r} is not a lowercase slug."
        )
    if category in {"start", "stop"}:
        raise GuardViolation(
            f"{category!r} is a timer action, not an activity category; it "
            "would open a timer named after itself. Close the open timer "
            "with `card timer stop CARD`."
        )


def timer_event(
    comments: Iterable[Any], *, action: str, category: str | None
) -> tuple[str, str]:
    """Validate one timer boundary and return its action and resolved category."""
    events = _comment_events(comments)
    opened = _open_timer(events)
    if action == "start":
        if opened is not None:
            raise GuardViolation(f"Execution timer {opened['category']!r} is already running.")
        if category is None or _CATEGORY.fullmatch(category) is None:
            raise GuardViolation("Timer start requires a lowercase slug category.")
        return action, category
    if action != "stop":
        raise GuardViolation(f"Unknown timer action {action!r}.")
    if opened is None:
        raise GuardViolation("Timer stop requires an open timer.")
    return action, str(opened["category"])


def event_text(action: str, category: str) -> str:
    """Return the stable visible comment representation of a timer boundary."""
    return EVENT_PREFIX + json.dumps(
        {"action": action, "category": category}, separators=(",", ":"), sort_keys=True
    )


def _state_events(activities: Iterable[Any]) -> list[dict[str, Any]]:
    events = []
    for activity in activities:
        if getattr(activity, "field", None) != "state":
            continue
        old = getattr(activity, "old_value", None)
        new = getattr(activity, "new_value", None)
        if not isinstance(old, str) or not isinstance(new, str):
            raise GuardViolation("A Plane state activity is missing its old or new state name.")
        events.append({
            "when": _timestamp(getattr(activity, "created_at", None)),
            "old": old,
            "new": new,
            "actor": getattr(activity, "actor", None),
            "source_id": str(getattr(activity, "id", "")),
        })
    return sorted(events, key=lambda event: (event["when"], event["source_id"]))


def timeline(activities: Iterable[Any], comments: Iterable[Any]) -> list[dict[str, Any]]:
    """Return state transitions and timer boundaries in one chronological stream."""
    rows = [{
        "when": event["when"].isoformat(), "kind": "state", "actor": event["actor"],
        "detail": f"{event['old']} → {event['new']}", "source_id": event["source_id"],
    } for event in _state_events(activities)]
    rows.extend({
        "when": event["when"].isoformat(), "kind": "timer", "actor": event["actor"],
        "detail": f"{event['action']} {event['category']}", "source_id": event["source_id"],
    } for event in _comment_events(comments))
    rows.extend({
        "when": event["started"].isoformat(), "kind": "delayed",
        "actor": None,
        "detail": (
            f"{event['category']} → {event['ended'].isoformat()} "
            f"({event['evidence_ref']}, received "
            f"{event['received'].isoformat()})"
        ),
        "source_id": event["source_id"],
    } for event in _delayed_events(comments))
    return sorted(rows, key=lambda row: (row["when"], row["source_id"]))


def execution_stats(
    activities: Iterable[Any], comments: Iterable[Any], *, as_of: datetime
) -> dict[str, Any]:
    """Summarize wall-clock state residence and explicitly timed active work."""
    if as_of.tzinfo is None:
        raise GuardViolation("Statistics cutoff must include a timezone.")
    states: dict[str, float] = defaultdict(float)
    state_events = _state_events(activities)
    for index, event in enumerate(state_events[:-1]):
        end = state_events[index + 1]["when"]
        if end < event["when"]:
            raise GuardViolation("Statistics cutoff precedes recorded execution telemetry.")
        minutes = (end - event["when"]).total_seconds() / 60
        if minutes > 0:
            states[event["new"]] += minutes

    durations: dict[str, float] = defaultdict(float)
    timer_events = _comment_events(comments)
    opened: dict[str, Any] | None = None
    for event in timer_events:
        if event["action"] == "start":
            if opened is not None:
                _open_timer(timer_events)
            opened = event
            continue
        if opened is None or opened["category"] != event["category"]:
            _open_timer(timer_events)
        durations[event["category"]] += (event["when"] - opened["when"]).total_seconds() / 60
        opened = None
    _open_timer(timer_events)

    current_state = None
    if state_events:
        latest = state_events[-1]
        if as_of < latest["when"]:
            raise GuardViolation("Statistics cutoff precedes recorded execution telemetry.")
        current_state = {
            "name": latest["new"],
            "started": latest["when"].isoformat(),
            "elapsed_minutes": round((as_of - latest["when"]).total_seconds() / 60, 2),
        }
    delayed: dict[str, float] = defaultdict(float)
    for event in _delayed_events(comments):
        delayed[event["category"]] += (
            event["ended"] - event["started"]
        ).total_seconds() / 60

    return {
        "rework_count": sum(
            event["old"].casefold() == "verifying"
            and event["new"].casefold() == "in progress"
            for event in state_events
        ),
        "state_minutes": {name: round(value, 2) for name, value in sorted(states.items())},
        "execution_minutes": {
            name: round(value, 2) for name, value in sorted(durations.items())
        },
        # Live-measured and provenance-backed delayed intervals are never
        # merged; a reader must see which is which.
        "delayed_minutes": {
            name: round(value, 2) for name, value in sorted(delayed.items())
        },
        "open_timer": None if opened is None else {
            "category": opened["category"], "started": opened["when"].isoformat(),
        },
        "current_state": current_state,
    }
