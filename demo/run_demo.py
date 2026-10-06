"""Show the sprint dashboard with an invented project, Wayfinder.

Wayfinder is a made-up offline museum guide app. Its sprints, cards and
timers are generated here, so the dashboard runs without Plane, a
register or credentials.

    python demo/run_demo.py [--host 127.0.0.1] [--port 8780] [--out DIR]

writes a static site to DIR (default demo/site), then serves that folder
as a plain static web host would. To publish the demo, upload DIR to any
static host.

The site is the unchanged dashboard page plus the JSON it reads. The
page requests api/sprints and api/sprints/local, so a path is both a
response and a directory; each response is therefore written as the
index.html of its directory, which static hosts serve after redirecting
api/sprints to api/sprints/. The browser parses the body as JSON either
way.
"""

import argparse
import contextlib
import functools
import json
import random
import statistics
from datetime import UTC, datetime, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from plane_proj import delivery_plan, readiness, web

PAST = [
    ("CAT-1", "Exhibit catalog import"),
    ("CAT-2", "Catalog search and filters"),
    ("MAP-1", "Floor plan rendering"),
    ("MAP-2", "Gallery hotspots"),
    ("OFF-1", "Offline catalog cache"),
    ("OFF-2", "Cache invalidation on publish"),
    ("AUD-1", "Audio guide player"),
    ("AUD-2", "Transcript sync"),
    (None, "Accessibility audit fixes"),
    ("RTE-1", "Route builder"),
    ("RTE-2", "Saved routes"),
    ("RTE-3", "Route sharing links"),
    ("I18N-1", "Translation pipeline"),
    ("I18N-2", "Right-to-left layouts"),
    (None, "Performance pass on gallery list"),
    ("QR-1", "QR exhibit lookup"),
    ("QR-2", "Printable QR labels"),
    ("ADM-1", "Curator admin login"),
    ("ADM-2", "Exhibit editor"),
    ("ADM-3", "Media upload and crop"),
    (None, "Crash reporting"),
    ("NOT-1", "Visit reminders"),
    ("TIX-1", "Ticket wallet"),
    ("TIX-2", "Timed entry slots"),
    ("FAM-1", "Family trail mode"),
    ("FAM-2", "Trail badges"),
    (None, "Dependency upgrades"),
    ("ANL-1", "Visit analytics"),
    ("ANL-2", "Curator dashboard"),
    ("SHOP-1", "Gift shop catalog"),
    (None, "Bug bash: routes"),
    ("EVT-1", "Events calendar"),
    ("EVT-2", "Event reminders"),
    ("A11Y-2", "Screen reader labels"),
    ("OFF-3", "Offline maps"),
    ("MAP-3", "Indoor positioning beacons"),
    ("MAP-4", "Step-free routes"),
    ("AUD-3", "Kids audio track"),
]
CURRENT = [
    (
        "OFF-4",
        "Offline route pilot",
        "Visitors can browse one wing and save a route without network access.",
    ),
    (
        "TIX-3",
        "Member pass check-in",
        "Members scan their pass at the door and see today's timed slots.",
    ),
]
# Planned work at the scale of a busy real project: about twenty sprints of
# one to eleven cards. Each sprint works in one area of an invented code
# tree; some cards also touch shared files, which produces code overlap.
PLANNED_AREAS = [
    (
        "RTE",
        "routes",
        "Accessible route variants",
        "Every saved route offers a step-free and a quiet variant.",
    ),
    (
        "AUD",
        "audio",
        "Multilingual audio guide",
        "Audio stops play in the visitor's chosen language with matching "
        "transcripts.",
    ),
    (
        "ANL",
        "analytics",
        "Popular exhibit heatmap",
        "Curators see which galleries draw visitors at each hour.",
    ),
    (
        "SHOP",
        "shop",
        "Click and collect",
        "Visitors reserve gift shop items and collect them at the exit.",
    ),
    (
        "FAM",
        "family",
        "Trail creator for educators",
        "Educators build a family trail from existing exhibits in under ten "
        "minutes.",
    ),
    (
        None,
        "deps",
        "Dependency and security upgrades",
        "Toolchain and libraries are current with no open advisories.",
    ),
    (
        "EVT",
        "events",
        "Event booking",
        "Visitors book a seat at a talk and receive a reminder.",
    ),
    (
        "MAP",
        "map",
        "Indoor wayfinding arrows",
        "Arrows on the floor plan lead from the visitor's spot to an exhibit.",
    ),
    (
        "OFF",
        "offline",
        "Offline audio packs",
        "Visitors download a gallery's audio before they arrive.",
    ),
    (
        "TIX",
        "tickets",
        "Group ticket booking",
        "A school books one visit for a whole class with one payment.",
    ),
    (
        "A11Y",
        "a11y",
        "High contrast and large text",
        "Every screen passes contrast checks in a high-contrast theme.",
    ),
    (
        "CAT",
        "catalog",
        "Catalog import from collections system",
        "New acquisitions appear in the guide the day they are catalogued.",
    ),
    (
        "NOT",
        "notify",
        "Closing time reminders",
        "Visitors get a reminder fifteen minutes before their wing closes.",
    ),
    (
        "ADM",
        "admin",
        "Curator scheduling",
        "Curators schedule exhibit text changes for a future date.",
    ),
    (
        None,
        "perf",
        "Startup time under two seconds",
        "The guide opens in under two seconds on a mid-range phone.",
    ),
    (
        "MEM",
        "members",
        "Member benefits page",
        "Members see their benefits and next renewal date.",
    ),
    (
        "I18N",
        "i18n",
        "Simplified and traditional Chinese",
        "Every screen is available in simplified and traditional Chinese.",
    ),
    (
        "QR",
        "qr",
        "QR codes for temporary exhibits",
        "Temporary exhibits get scannable labels on the day they open.",
    ),
    (
        "ANL",
        "analytics",
        "Dwell time per exhibit",
        "Curators see how long visitors stay at each exhibit.",
    ),
    (
        "RTE",
        "routes",
        "Shared family routes",
        "A family plans one route together on several phones.",
    ),
]
CARD_STEPS = [
    "Data model for",
    "API for",
    "Screens for",
    "Sync for",
    "Settings for",
    "Tests on device for",
    "Copy review for",
    "Migration for",
    "Analytics events for",
    "Docs for",
    "Rollout of",
]
AREA_FILES = ["model", "api", "view", "sync", "store"]


def step_file(k):
    """A new file named after the card's step, such as `screens.ts`."""
    return CARD_STEPS[k % len(CARD_STEPS)].split()[0].lower()


# Files that cards outside an area also change, so sprints collide.
SHARED_CODE = [
    "src/api/client.ts",
    "src/api/members.ts",
    "src/ui/theme.css",
    "db/schema.sql",
    "src/i18n/strings.json",
    "src/ui/nav.tsx",
]
# Files that only planned sprints share, so they collide with each other.
PLANNED_SHARED = [
    "src/ui/theme.css",
    "src/i18n/strings.json",
    "src/ui/nav.tsx",
    "src/ui/onboarding.tsx",
]
# Areas whose cards reach into another area's whole directory.
AREA_OWNERS = {
    "family": "routes",
    "a11y": "ui",
    "notify": "events",
    "qr": "catalog",
    "perf": "map",
}
# Changed by almost every card for mechanical reasons; never an overlap.
SHARED_PATHS = ["package.json", "package-lock.json"]
CARD_TITLES = {
    "OFF-4": [
        "Route cache schema",
        "Prefetch wing assets",
        "Offline route editor",
        "Sync saved routes on reconnect",
        "Offline smoke test on device",
    ],
    "TIX-3": [
        "Pass QR scanner",
        "Member lookup API",
        "Timed slot list",
        "Door staff override",
        "Check-in audit log",
    ],
}
# Card links have nowhere real to go: the project is invented.
CARD_URL = "#"


def summary(values):
    values = sorted(values)
    return {
        "average": statistics.fmean(values),
        "median": values[len(values) // 2],
        "min": values[0],
        "max": values[-1],
        "sd": statistics.pstdev(values),
    }


def elapsed(hours):
    minutes = round(hours * 60)
    return f"{minutes // 60}h {minutes % 60:02d}m"


def timing(rng, cards, hours, final=True):
    active_ip = hours * 60 * rng.uniform(0.9, 1.6)
    verifying = active_ip * rng.uniform(0.2, 0.4)
    execution = {
        "coding": active_ip * rng.uniform(0.55, 0.75),
        "blocking-run": active_ip * rng.uniform(0.05, 0.2),
        "dependency-wait": active_ip
        * rng.choice([0, 0, rng.uniform(0.02, 0.1)]),
        "user-ask": active_ip * rng.choice([0, 0, 0, rng.uniform(0.02, 0.08)]),
        "manual-qa": verifying * rng.choice([0, rng.uniform(0.1, 0.4)]),
        "service-wait": active_ip * rng.choice([0, rng.uniform(0.01, 0.05)]),
        "waiting": active_ip * rng.choice([0, 0, rng.uniform(0.01, 0.04)]),
    }
    rework = rng.choice([0, 0, 0, 0, 1, 1, 2])
    residence = {
        "Backlog": rng.choice([0, 0, rng.uniform(5, 90)]),
        "Todo": hours * 60 * rng.uniform(0.3, 0.8),
        "In Progress": active_ip,
        "Verifying": verifying,
    }
    reasons = {}
    for _ in range(rework):
        reason = rng.choice(
            ["test-gap", "test-gap", "spec", "defect", "missed-gate"]
        )
        reasons[reason] = reasons.get(reason, 0) + 1
    return {
        "observed_cards": cards,
        "rework_count": rework,
        "reworked_cards": min(rework, cards),
        "rework_observed_cards": cards,
        "timed_cards": cards,
        "final_cards": cards if final else 1,
        "captured_from": None,
        "captured_through": None,
        "state_minutes": residence,
        "execution_minutes": execution,
        "delayed_minutes": {},
        "current_state_minutes": {},
        "open_timer_minutes": {},
        "residence_minutes": residence,
        "active_minutes": {"In Progress": active_ip, "Verifying": verifying},
        "rework_minutes": rework * rng.uniform(25, 55),
        "rework_cost_cards": cards,
        "rework_reasons": dict(sorted(reasons.items())),
        "rework_execution_minutes": {"blocking-run": rework * 15.0}
        if rework
        else {},
    }


def text(title):
    return {
        "goal": f"{title} is usable end to end by visitors or curators.",
        "execution": "Build the data path first, then the screens. "
        "No account or payment changes.",
        "acceptance": f"{title} works on a phone and a tablet.\n"
        "Accessibility checks pass.\nIndependent review accepts every card.",
    }


def past_sprints(rng, now):
    past = []
    start = now - timedelta(days=12)
    for number, (alias, title) in enumerate(PAST, start=1):
        hours = min(9.0, max(0.6, rng.lognormvariate(0.9, 0.55)))
        cards_start = rng.randint(3, 6)
        points_start = sum(
            rng.choice([1, 2, 2, 3, 3]) for _ in range(cards_start)
        )
        grow = rng.random() < 0.18
        shrink = not grow and rng.random() < 0.06
        cards_end = cards_start + (1 if grow else -1 if shrink else 0)
        points_end = points_start + (
            rng.randint(1, 3) if grow else -2 if shrink else 0
        )
        started = start + timedelta(hours=number * 6.7 + rng.uniform(-1, 1))
        past.append(
            {
                "sprint": number,
                "status": "completed",
                "title": title,
                "alias": alias,
                "cards": f"{cards_start} → {cards_end}",
                "points": f"{points_start} → {points_end}",
                "velocity": points_end / hours,
                "elapsed": elapsed(hours),
                "sprint_id": number,
                "cycle_id": None,
                "position": None,
                "started": started.isoformat(),
                "ended": (started + timedelta(hours=hours)).isoformat(),
                "hours": hours,
                "cards_start": cards_start,
                "cards_end": cards_end,
                "points_start": points_start,
                "points_end": points_end,
                "delivered": f"{title} shipped; every card independently "
                "accepted "
                "and the full test suite passed.",
                "retrospective": "Scope held; review caught one edge case "
                "before merge.",
                # The oldest sprints predate timers, as in a real register,
                # and two lack final snapshots for some cards.
                "timing": (
                    timing(rng, cards_end, hours, final=number not in (9, 15))
                    if number > 6
                    else None
                ),
            }
            | text(title)
        )
    return past


def current_sprints(rng, now):
    current, cards, ref = [], {}, 300
    for offset, (alias, title, goal) in enumerate(CURRENT):
        number = len(PAST) + 1 + offset
        states = (
            ["Done", "Done", "Verifying", "In Progress", "Todo"]
            if offset == 0
            else ["Done", "In Progress", "In Progress", "Todo", "Todo"]
        )
        points = [3, 2, 3, 2, 3] if offset == 0 else [2, 3, 2, 2, 3]
        members = []
        for card_title, state, point in zip(
            CARD_TITLES[alias], states, points, strict=True
        ):
            ref += 1
            members.append(
                {
                    "id": f"card-{ref}",
                    "ref": f"WAY-{ref}",
                    "title": card_title,
                    "state": state,
                    "points": point,
                    "url": CARD_URL,
                    "blocked_by": [],
                }
            )
        cards[str(number)] = members
        hours = 2.6 - offset * 1.1
        done = sum(c["points"] for c in members if c["state"] == "Done")
        record = timing(rng, len(members), hours, final=False)
        record["open_timer_minutes"] = {"coding": 18.0} if offset == 0 else {}
        current.append(
            {
                "sprint": number,
                "status": "current",
                "title": title,
                "alias": alias,
                "started": (now - timedelta(hours=hours)).isoformat(),
                "cards_start": len(members),
                "points_start": sum(points),
                "cards_current": len(members),
                "points_current": sum(points),
                "cards_done": sum(c["state"] == "Done" for c in members),
                "points_done": done,
                "cards_cancelled": 0,
                "points_cancelled": 0,
                "velocity": done / hours,
                "sprint_id": number,
                "cycle_id": "cycle",
                "position": None,
                "ended": None,
                "hours": None,
                "cards_end": None,
                "points_end": None,
                "delivered": None,
                "retrospective": "",
                "timing": record,
            }
            | text(title)
            | {"goal": goal}
        )
    first, second = (str(len(PAST) + 1), str(len(PAST) + 2))
    cards[first][4]["blocked_by"] = [
        {"ref": "WAY-303", "state": "Verifying"},
        {"ref": "WAY-304", "state": "In Progress"},
    ]
    cards[second][3]["blocked_by"] = [
        {"ref": "WAY-307", "state": "In Progress"}
    ]
    return current, cards


def planned_sprints(rng, current_cards, first_ref):
    """Planned sprints, their cards' readiness facts, and the next card ref.

    Cards declare files in their sprint's area and sometimes a shared file.
    Some sprints wait on cards of an earlier sprint or a running one, and a
    few have cards whose scope or dependencies are not yet declared, so the
    queue shows every readiness state.
    """
    planned, facts, ref = [], [], first_ref
    earlier_cards = [c for cards in current_cards.values() for c in cards]
    for position, (alias, area, title, goal) in enumerate(PLANNED_AREAS, 1):
        number = len(PAST) + len(CURRENT) + position
        count = rng.choice([1, 2, 3, 3, 4, 4, 4, 5, 5, 6, 7, 8, 10, 11])
        undeclared = position > 2 and rng.random() < 0.3
        cards = []
        for k in range(count):
            ref += 1
            touches = [f"src/{area}/{rng.choice(AREA_FILES)}.ts"]
            if rng.random() < 0.12:
                touches.append(rng.choice(SHARED_CODE))
            # Planned sprints also collide with each other: on shared screens,
            # and on a whole area directory that another sprint works in.
            if rng.random() < 0.08:
                touches.append(rng.choice(PLANNED_SHARED))
            if area in AREA_OWNERS and k == 0:
                touches.append(f"src/{AREA_OWNERS[area]}/")
            if rng.random() < 0.3:
                touches.append(f"src/{area}/{step_file(k)}.ts (new)")
            blocked = []
            # The first sprint stays clear to start; later ones may wait on
            # earlier planned work or on a running sprint.
            if position > 1 and k == 0 and rng.random() < 0.45:
                blocked.append(rng.choice(earlier_cards))
            missing = undeclared and rng.random() < 0.5
            plan = delivery_plan.DeliveryPlan(
                touches=None
                if missing and rng.random() < 0.5
                else tuple(
                    delivery_plan.TouchedPath.parse(t)
                    for t in dict.fromkeys(touches)
                ),
                dependencies_assessed=None
                if missing and rng.random() < 0.6
                else True,
            )
            card = readiness.CardFact(
                id=f"card-{ref}",
                ref=f"WAY-{ref}",
                title=f"{CARD_STEPS[k % len(CARD_STEPS)]} {title.lower()}",
                state="Backlog",
                points=rng.choice([1, 2, 2, 3, 3, 5]),
                sprint_id=number,
                blocked_by=tuple(b.id for b in blocked),
                delivery_plan=plan,
            )
            cards.append(card)
        earlier_cards += cards
        facts.append(
            readiness.SprintFact(
                sprint_id=number,
                title=title,
                alias=f"{alias}-{position + 3}" if alias else None,
                goal=goal,
                position=position,
                cards=tuple(cards),
            )
        )
        planned.append(
            {
                "sprint": number,
                "status": "planned",
                "title": title,
                "alias": facts[-1].alias,
                "position": position,
                "cards": count,
                "points": facts[-1].points,
                "sprint_id": None,
                "cycle_id": None,
                "started": None,
                "ended": None,
                "hours": None,
                "cards_start": None,
                "cards_end": None,
                "points_start": None,
                "points_end": None,
                "velocity": None,
                "delivered": None,
                "retrospective": "",
                "timing": None,
            }
            | text(title)
            | {"goal": goal}
        )
    return planned, facts, ref


# What each running sprint's cards touch, so planned work can overlap it.
CURRENT_TOUCHES = {
    "OFF-4": [
        "src/offline/cache.ts",
        "src/offline/",
        "src/offline/editor.tsx",
        "src/offline/sync.ts",
        "src/api/client.ts",
    ],
    "TIX-3": [
        "src/tickets/scanner.ts",
        "src/api/members.ts",
        "src/tickets/slots.ts",
        "src/tickets/override.ts",
        "db/schema.sql",
    ],
}


def current_facts(current, cards):
    """Running sprints as readiness facts, from the listing's cards."""
    facts = []
    for sprint in current:
        members = cards[str(sprint["sprint"])]
        touches = CURRENT_TOUCHES[sprint["alias"]]
        facts.append(
            readiness.SprintFact(
                sprint_id=sprint["sprint"],
                title=sprint["title"],
                alias=sprint["alias"],
                goal=sprint["goal"],
                is_current=True,
                cards=tuple(
                    readiness.CardFact(
                        id=c["id"],
                        ref=c["ref"],
                        title=c["title"],
                        state=c["state"],
                        points=c["points"],
                        sprint_id=sprint["sprint"],
                        delivery_plan=delivery_plan.DeliveryPlan(
                            touches=(
                                delivery_plan.TouchedPath.parse(touches[k]),
                            ),
                            dependencies_assessed=True,
                        ),
                    )
                    for k, c in enumerate(members)
                ),
            )
        )
    return facts


def sprint_stats(past):
    timed = [s["timing"] for s in past if s["timing"]]
    stats = {
        key: summary([s[key] for s in past])
        for key in (
            "hours",
            "cards_start",
            "cards_end",
            "points_start",
            "points_end",
            "velocity",
        )
    }

    def group(name):
        keys = sorted({k for t in timed for k in t[name]})
        return {k: summary([t[name].get(k, 0) for t in timed]) for k in keys}

    def total(name):
        return summary([sum(t[name].values()) for t in timed])

    reasons = {}
    for t in timed:
        for k, v in t["rework_reasons"].items():
            reasons[k] = reasons.get(k, 0) + v
    stats["timing"] = {
        "rework": {
            "included_sprints": len(timed),
            "excluded_sprints": len(past) - len(timed),
            "count": summary([t["rework_count"] for t in timed]),
            "minutes_included_sprints": len(timed),
            "minutes": summary([t["rework_minutes"] for t in timed]),
            "reasons": dict(sorted(reasons.items())),
        },
        "included_sprints": len(timed),
        "excluded_without_timing": len(past) - len(timed),
        "excluded_incomplete": 0,
        "state_minutes": group("state_minutes"),
        "execution_minutes": group("execution_minutes"),
        "execution_minutes_total": total("execution_minutes"),
        "residence_minutes": group("residence_minutes"),
        "residence_minutes_total": total("residence_minutes"),
        "active_minutes": group("active_minutes"),
        "active_minutes_total": total("active_minutes"),
    }
    return stats


def demo_responses(now):
    """Every response the dashboard reads, keyed by its request path."""
    rng = random.Random(7)  # the same invented history on every run
    past = past_sprints(rng, now)
    current, cards = current_sprints(rng, now)
    running = current_facts(current, cards)
    planned, planned_facts, _ = planned_sprints(
        rng, {s.sprint_id: s.cards for s in running}, 400
    )
    payload = {
        "generated_at": now.isoformat(),
        "project": {"key": "WAY", "name": "Wayfinder"},
        "board_url": None,
        "estimates": True,
        "listing": {
            "current": current,
            "planned": planned,
            "past": past,
            "stats": sprint_stats(past),
        },
        "cards": cards,
    }
    all_sprints = current + planned + past
    payload["cycle_urls"] = {
        str(s["sprint"]): web.cycle_url(
            "https://plane.example",
            "wayfinder",
            "proj-uuid",
            f"cycle-{s['sprint']}",
        )
        for s in all_sprints
    }
    chain = (
        ("WAY-303", 39),
        ("WAY-305", 39),
        ("WAY-318", 41),
        ("WAY-326", 43),
        ("WAY-331", 45),
    )
    dependencies = {
        "ready": [{"ref": f"WAY-{n}"} for n in (305, 309, 310, 321, 322, 331)],
        "blocked": [{"ref": f"WAY-{n}"} for n in range(323, 335)],
        "critical_path": {
            "unit": "points",
            "total": 78,
            "critical_path": 21,
            "speedup_ceiling": 3.71,
            "chain": [
                {"ref": r, "sprint": s, "title": "", "state": "", "points": 0}
                for r, s in chain
            ],
        },
    }
    states = {
        c.id: (c.ref, c.state)
        for sprint in running + planned_facts
        for c in sprint.cards
    }
    readiness_payload = readiness.evaluate_readiness(
        running,
        planned_facts,
        states,
        completed_velocities=[s["velocity"] for s in past],
        shared_paths=SHARED_PATHS,
    )
    return {
        "/api/sprints": payload,
        "/api/sprints/local": payload,
        "/api/version": {"version": "demo"},
        "/api/dependencies": dependencies,
        "/api/readiness": readiness_payload,
    }


def build(out: Path, now: datetime) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(web.page(), encoding="utf-8")
    for path, body in demo_responses(now).items():
        target = out / path.lstrip("/") / "index.html"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(body), encoding="utf-8")


class QuietHandler(SimpleHTTPRequestHandler):
    """Log requests, except the page's change poll every two seconds."""

    def log_message(self, format, *args):  # noqa: A002 (http.server's name)
        if not self.path.startswith("/api/version"):
            super().log_message(format, *args)


def serve(out: Path, host: str, port: int) -> None:
    handler = functools.partial(QuietHandler, directory=out)
    with ThreadingHTTPServer((host, port), handler) as server:
        print(f"Wayfinder demo at http://{host}:{port}/ (Ctrl-C to stop)")
        with contextlib.suppress(KeyboardInterrupt):
            server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8780)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).parent / "site",
        help="where to write the static site",
    )
    args = parser.parse_args()
    build(args.out, datetime.now(UTC).replace(microsecond=0))
    serve(args.out.resolve(), args.host, args.port)


if __name__ == "__main__":
    main()
