"""Local reporting of the latest card timing observations in each sprint."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime
from typing import Any

from rich import box
from rich.console import Console
from rich.table import Table

from plane_proj.sprints import (
    STATUS_COMPLETED,
    Sprint,
    fetch_execution_snapshots,
    format_timestamp,
    metric_summary,
    parse_timestamp,
)

STATE_TIMER_CATEGORIES = {"verification", "verifying", "review"}
ACTIVITY_CATEGORIES = (
    "coding", "dependency-wait", "service-wait", "blocking-run",
    "manual-qa", "user-ask",
)


def summaries(
    connection: sqlite3.Connection, found: list[Sprint]
) -> dict[int, dict[str, Any] | None]:
    """Aggregate one latest observation per card, comparing capture instants."""
    result: dict[int, dict[str, Any] | None] = {}
    for sprint in found:
        latest = {}
        for snapshot in fetch_execution_snapshots(connection, sprint.sprint_id):
            card_id = snapshot["work_item_id"]
            if card_id not in latest or parse_timestamp(snapshot["captured_at"]) > parse_timestamp(
                latest[card_id]["captured_at"]
            ):
                latest[card_id] = snapshot
        if not latest:
            result[sprint.sprint_id] = None
            continue
        groups = {
            name: defaultdict(float)
            for name in (
                "state_minutes",
                "execution_minutes",
                "delayed_minutes",
                "current_state_minutes",
                "open_timer_minutes",
                "rework_execution_minutes",
            )
        }
        timed_cards = 0
        rework_counts = []
        # Rework cost exists only in snapshots taken since it was recorded;
        # older ones leave it unknown rather than zero.
        rework_costs = []
        rework_reasons: dict[str, int] = defaultdict(int)
        for snapshot in latest.values():
            stats = snapshot["stats"]
            if "rework_count" in stats:
                rework_counts.append(stats["rework_count"])
            if "rework_minutes" in stats:
                rework_costs.append(stats["rework_minutes"])
            for reason, count in stats.get("rework_reasons", {}).items():
                rework_reasons[reason] += count
            for name in ("state_minutes", "execution_minutes",
                         "delayed_minutes", "rework_execution_minutes"):
                for category, minutes in stats.get(name, {}).items():
                    if (
                        name in {"execution_minutes",
                                 "rework_execution_minutes"}
                        and category.casefold() in STATE_TIMER_CATEGORIES
                    ):
                        continue
                    groups[name][category] += minutes
            state = stats.get("current_state")
            if state is not None and "elapsed_minutes" in state:
                groups["current_state_minutes"][state["name"]] += state["elapsed_minutes"]
            timer = stats.get("open_timer")
            if (
                timer is not None
                and timer["category"].casefold() not in STATE_TIMER_CATEGORIES
            ):
                duration = (
                    parse_timestamp(snapshot["captured_at"])
                    - datetime.fromisoformat(timer["started"])
                ).total_seconds() / 60
                groups["open_timer_minutes"][timer["category"]] += max(0, duration)
            if stats.get("state_minutes") or stats.get("execution_minutes") or state or timer:
                timed_cards += 1
        captures = sorted(
            (snapshot["captured_at"] for snapshot in latest.values()), key=parse_timestamp
        )
        residence = defaultdict(float)
        for group in ("state_minutes", "current_state_minutes"):
            for name, minutes in groups[group].items():
                if name.casefold() != "done":
                    residence[name] += minutes
        groups["residence_minutes"] = residence
        groups["active_minutes"] = {
            name: minutes for name, minutes in residence.items()
            if name.casefold() in {"in progress", "verifying"}
        }
        result[sprint.sprint_id] = {
            "observed_cards": len(latest),
            "rework_count": sum(rework_counts) if rework_counts else None,
            "reworked_cards": (
                sum(count > 0 for count in rework_counts)
                if rework_counts else None
            ),
            "rework_observed_cards": len(rework_counts),
            "rework_minutes": sum(rework_costs) if rework_costs else None,
            "rework_cost_cards": len(rework_costs),
            "rework_reasons": dict(sorted(rework_reasons.items())),
            "timed_cards": timed_cards,
            "final_cards": sum(snapshot["is_final"] for snapshot in latest.values()),
            "captured_from": captures[0],
            "captured_through": captures[-1],
            **{name: dict(sorted(values.items())) for name, values in groups.items()},
        }
    return result


def statistics(
    found: list[Sprint], timings: dict[int, dict[str, Any] | None]
) -> dict[str, Any]:
    """Use completed sprints with complete final timing coverage for averages."""
    included = []
    missing = partial = 0
    for sprint in found:
        if sprint.status != STATUS_COMPLETED:
            continue
        timing = timings.get(sprint.sprint_id)
        if timing is None or timing["timed_cards"] == 0:
            missing += 1
        elif (
            timing["observed_cards"] != sprint.cards_end
            or timing["timed_cards"] != sprint.cards_end
            or timing["final_cards"] != sprint.cards_end
            or timing["open_timer_minutes"]
        ):
            partial += 1
        else:
            included.append(timing)
    metrics = {}
    for group in (
        "state_minutes", "execution_minutes", "residence_minutes",
        "active_minutes",
    ):
        categories = sorted({category for timing in included for category in timing[group]})
        if group == "execution_minutes" and included:
            categories = sorted(set(categories) | set(ACTIVITY_CATEGORIES))
        metrics[group] = {
            category: metric_summary([timing[group].get(category, 0) for timing in included])
            for category in categories
        }
        metrics[group + "_total"] = (
            metric_summary([sum(timing[group].values()) for timing in included])
            if included
            else None
        )
    rework_values = []
    rework_costs = []
    rework_reasons: dict[str, int] = defaultdict(int)
    for sprint in found:
        timing = timings.get(sprint.sprint_id)
        if sprint.status != STATUS_COMPLETED or timing is None:
            continue
        for reason, count in timing.get("rework_reasons", {}).items():
            rework_reasons[reason] += count
        if (
            timing["final_cards"] == sprint.cards_end
            and timing.get("rework_cost_cards") == sprint.cards_end
        ):
            rework_costs.append(timing["rework_minutes"])
    for sprint in found:
        timing = timings.get(sprint.sprint_id)
        if (
            sprint.status == STATUS_COMPLETED
            and timing is not None
            and timing["observed_cards"] == sprint.cards_end
            and timing["final_cards"] == sprint.cards_end
            and timing["rework_observed_cards"] == sprint.cards_end
            and timing["rework_count"] is not None
        ):
            rework_values.append(timing["rework_count"])
    return {
        "rework": {
            "included_sprints": len(rework_values),
            "excluded_sprints": sum(s.status == STATUS_COMPLETED for s in found)
            - len(rework_values),
            "count": metric_summary(rework_values) if rework_values else None,
            "minutes_included_sprints": len(rework_costs),
            "minutes": metric_summary(rework_costs) if rework_costs else None,
            "reasons": dict(sorted(rework_reasons.items())),
        },
        "included_sprints": len(included),
        "excluded_without_timing": missing,
        "excluded_incomplete": partial,
        **metrics,
    }


def duration(minutes: float) -> str:
    seconds = round(minutes * 60)
    hours, remainder = divmod(seconds, 3600)
    mins, secs = divmod(remainder, 60)
    return f"{hours:02d}:{mins:02d}:{secs:02d}"


def category_label(label: str, name: str) -> str:
    if label == "State" and name.casefold() in {"in progress", "verifying"}:
        label = "Active"
    return f"{label}: {name}"


def settled_last(names: list[str]) -> list[str]:
    """Order state names: plain states, active states, then Cancelled."""
    return sorted(names, key=lambda name: (
        name.casefold() == "cancelled",
        name.casefold() in {"in progress", "verifying"},
        name,
    ))


def fields(timing: dict[str, Any] | None) -> tuple[tuple[str, object], ...]:
    if timing is None:
        return (("Timing", "No timing data"),)
    rows: list[tuple[str, object]] = [
        ("Observed cards", timing["observed_cards"]),
        ("Timed cards", timing["timed_cards"]),
        ("Cards with final snapshot", timing["final_cards"]),
        ("Reworked cards", timing["reworked_cards"]),
        ("Rework count", timing["rework_count"]),
    ]
    if timing.get("rework_minutes") is not None:
        rows.append(("Rework time", duration(timing["rework_minutes"])))
    rows.extend(
        (f"Rework reason: {name}", count)
        for name, count in timing.get("rework_reasons", {}).items()
    )
    rows.append(
        ("Active total", duration(sum(timing["active_minutes"].values())))
    )
    for group, label in (
        ("residence_minutes", "State"),
        ("execution_minutes", "Time spent"),
        ("delayed_minutes", "Delayed"),
        ("open_timer_minutes", "Open timer"),
    ):
        values = timing[group]
        if group == "execution_minutes":
            rows.extend((
                ("Earliest card timestamp",
                 format_timestamp(timing["captured_from"])),
                ("Latest card timestamp",
                 format_timestamp(timing["captured_through"])),
            ))
            rows.append((
                "Time spent total", duration(sum(timing[group].values()))
            ))
            values = dict.fromkeys(ACTIVITY_CATEGORIES, 0) | values
        names = settled_last(list(values)) if label == "State" else list(values)
        rows.extend(
            (category_label(label, name), duration(values[name]))
            for name in names
            if label != "State" or name.casefold() != "todo"
        )
    return tuple(rows)


def render_statistics(payload: dict[str, Any]) -> None:
    console = Console(highlight=False)
    rework = payload["rework"]
    coverage = Table(title="Statistics coverage", box=box.ROUNDED, highlight=False)
    coverage.add_column("Metric")
    coverage.add_column("Included", justify="right")
    coverage.add_column("Excluded", justify="right")
    coverage.add_column("Exclusion reason", overflow="fold")
    coverage.add_row(
        "Timing", str(payload["included_sprints"]),
        str(payload["excluded_without_timing"] + payload["excluded_incomplete"]),
        f"{payload['excluded_without_timing']} without timing; "
        f"{payload['excluded_incomplete']} incomplete",
    )
    coverage.add_row(
        "Rework", str(rework["included_sprints"]), str(rework["excluded_sprints"]),
        "Without complete rework coverage",
    )
    console.print(coverage)
    if payload["included_sprints"] == 0 and rework["count"] is None:
        return
    table = Table(
        title="Detailed metrics", box=box.ROUNDED, padding=(0, 0),
        highlight=False,
    )
    table.add_column("Metric")
    for label in ("Avg", "Med", "Min", "Max", "SD"):
        table.add_column(label, justify="right")
    for group, label in (
        ("active_minutes", "Active"),
        ("residence_minutes", "State"),
        ("execution_minutes", "Time spent"),
    ):
        if payload[group + "_total"] is None:
            continue
        entries = [(f"{label} total", payload[group + "_total"])]
        if group == "residence_minutes":
            entries = []
        if group == "active_minutes":
            table.add_row(
                "Active total",
                *(duration(payload[group + "_total"][key]) for key in
                  ("average", "median", "min", "max", "sd")),
            )
            continue
        names = list(payload[group])
        if label == "State":
            names = settled_last(names)
        entries.extend(
            (category_label(label, name), payload[group][name])
            for name in names
            if label != "State" or name.casefold() != "todo"
        )
        for name, summary in entries:
            table.add_row(
                name, *(duration(summary[key]) for key in ("average", "median", "min", "max", "sd"))
            )
    if rework["count"] is not None:
        table.add_row(
            "Rework count",
            *(f"{rework['count'][key]:.2f}" for key in ("average", "median", "min", "max", "sd")),
        )
    if rework.get("minutes") is not None:
        table.add_row(
            "Rework time",
            *(duration(rework["minutes"][key])
              for key in ("average", "median", "min", "max", "sd")),
        )
    console.print(table)
    if rework.get("reasons"):
        reasons = Table(title="Rework reasons", box=box.ROUNDED,
                        highlight=False)
        reasons.add_column("Reason")
        reasons.add_column("Send-backs", justify="right")
        for name, count in rework["reasons"].items():
            reasons.add_row(name, str(count))
        console.print(reasons)
