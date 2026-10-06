"""Fill a real Plane project with the invented Wayfinder demo.

The demo dashboard (run_demo.py) is static and invented. For a
presentation its links should open real Plane pages, so this script
creates the same sprints as cycles and the same cards on a Plane
project, and records their ids in demo/plane-ids.json. run_demo.py
--plane-url then links the board, every cycle and every running or
planned card to that project.

    python demo/build_plane_demo.py --conf DIR/plane/plane-proj.json \\
        --env-file .env_plane_so

The config is a throwaway one made with `plane-proj init --project WAY`
in an empty folder, with its delivery rules turned off: past cards go
straight to Done and some planned cards are deliberately undeclared.
The project needs the usual states; Verifying is added if missing.

Writes go through plane-proj's own board code. Requests are paced to one
a second and a rate-limited request waits and retries, so the run takes
a while. It resumes: each sprint's existing cards are matched by title
and the ids file is saved after every write, so a rerun creates nothing
twice.
"""

import argparse
import importlib.util
import json
import random
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import requests

from plane_proj import text
from plane_proj.cli import Context

HERE = Path(__file__).parent


def load_demo():
    spec = importlib.util.spec_from_file_location(
        "run_demo", HERE / "run_demo.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pace_requests(gap=1.05):
    """Space every Plane request out and retry those Plane rate-limits."""
    original = requests.Session.request
    last = [0.0]

    def request(self, method, url, *args, **kwargs):
        while True:
            wait = last[0] + gap - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            last[0] = time.monotonic()
            response = original(self, method, url, *args, **kwargs)
            if response.status_code != 429:
                return response
            delay = float(response.headers.get("Retry-After") or 30)
            print(f"  rate limited; waiting {delay:.0f}s", flush=True)
            time.sleep(delay)

    requests.Session.request = request


def description(title, sprint_title, goal, plan_section):
    body = (
        f'## What to build\n\n{title}, for the sprint "{sprint_title}": '
        f"{goal}\n\n## Current status\n\n"
        "Part of the invented Wayfinder demo.\n\n"
        f"## Acceptance criteria\n\n- {title} works on a phone and a tablet.\n"
        "- Independent review accepts the card.\n"
    )
    return text.to_html(body + ("\n" + plan_section if plan_section else ""))


class Builder:
    def __init__(self, board, ids_file):
        self.board, self.ids_file = board, ids_file
        self.ids = (
            json.loads(ids_file.read_text())
            if ids_file.exists()
            else {"cycles": {}, "cards": {}}
        )
        self.ids["workspace"] = board.slug
        self.ids["project_id"] = board.project.id
        self.me = board.me()
        self.cycles_by_name = {
            str(c.name): str(c.id) for c in board.groupings("cycle")
        }
        # Cards already on the board, by title, in case one was created but
        # never placed in its cycle; titles are unique within the demo.
        self.stray = {
            str(c.name): {
                "id": str(c.id),
                "ref": f"{board.project.key}-{c.sequence_id}",
            }
            for c in board.cards()
        }

    def save(self):
        self.ids_file.write_text(json.dumps(self.ids, indent=2) + "\n")

    def cycle(self, number, title, start=None, end=None):
        name = f"Sprint {number}"
        if name not in self.cycles_by_name:
            fields = {"description": title}
            if start:
                fields |= {"start_date": start, "end_date": end}
            try:
                created = self.board.create_cycle(name, fields)
            except Exception as error:  # noqa: BLE001 (Plane's date rules vary)
                # Plane may refuse overlapping or past dates; the demo only
                # needs the cycle, so keep it undated rather than stop.
                print(f"  {name}: dates refused ({error}); undated", flush=True)
                created = self.board.create_cycle(name, {"description": title})
            self.cycles_by_name[name] = str(created.id)
            print(f"cycle {name}", flush=True)
        self.ids["cycles"][str(number)] = self.cycles_by_name[name]
        self.save()
        return name

    def drop_locked_empty_cycles(self, now):
        """Delete ended, empty cycles that an interrupted run left locked."""
        for cycle in self.board.groupings("cycle"):
            end = getattr(cycle, "end_date", None)
            if (
                end is None
                or datetime.fromisoformat(str(end).replace("Z", "+00:00"))
                >= now
            ):
                continue
            members = self.board.client.cycles.list_work_items(
                self.board.slug, self.board.project.id, str(cycle.id)
            )
            if not getattr(members, "results", None):
                self.board.client.cycles.delete(
                    self.board.slug, self.board.project.id, str(cycle.id)
                )
                self.cycles_by_name.pop(str(cycle.name), None)
                print(f"dropped locked empty {cycle.name}", flush=True)

    def cards(self, number, cards, ended=None):
        """Create a sprint's cards that do not exist yet; return their ids.

        `cards` is a list of (key, title, html, points, state); key names
        the card in plane-ids.json, or None for cards nothing links to.
        Plane locks a cycle once it has ended, so a past sprint's cycle is
        created ending in the future and gets its real end date (`ended`)
        only after its cards are in.
        """
        cycle_id = self.ids["cycles"][str(number)]
        existing = {
            c["title"]: c for c in self.board.sprint_cycle_cards(cycle_id)
        }
        made = self._add(number, cycle_id, cards, existing)
        if ended and str(number) not in self.ids.setdefault("closed", []):
            # Only now the real end date: an ended cycle is locked.
            self.board.update_cycle(cycle_id, {"end_date": ended})
            self.ids["closed"].append(str(number))
            self.save()
        return made

    def _add(self, number, cycle_id, cards, existing):
        made = {}
        for key, title, html, points, state in cards:
            found = existing.get(title)
            if found is None and title in self.stray:
                # Created before a failed placement: put it in its cycle.
                found = self.stray.pop(title)
                self.board.add_to_cycle(found["id"], cycle_id)
                print(f"  {found['ref']} placed {title}", flush=True)
            if found is None:
                write = self.board.create_card(
                    title=title,
                    description_html=html,
                    assignee_id=self.me,
                    module_name=None,
                    cycle_name=f"Sprint {number}",
                    estimate=points,
                    state_name=state,
                )
                found = {"id": write.card_id, "ref": write.reference}
                print(f"  {found['ref']} {state} {title}", flush=True)
            made[key or title] = found
            if key:
                self.ids["cards"][key] = {
                    "id": found["id"],
                    "ref": found["ref"],
                }
                self.save()
        return made

    def relate(self, blocked_id, blocker_ids):
        have = set(self.board.relations(blocked_id)["blocked_by"])
        missing = [b for b in blocker_ids if b not in have]
        if missing:
            card = self.board.find(blocked_id)
            self.board.add_relation(
                card, "blocked_by", [self.board.find(b) for b in missing]
            )


def ensure_verifying(board):
    if "Verifying" in board.project.states:
        return False
    from plane.models.states import CreateState

    board.client.states.create(
        board.slug,
        board.project.id,
        CreateState(name="Verifying", color="#ffcc00", group="started"),
    )
    print("added the Verifying state", flush=True)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--conf", required=True, type=Path)
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--ids", type=Path, default=HERE / "plane-ids.json")
    args = parser.parse_args()
    pace_requests()
    context = Context(str(args.conf), None, False, args.env_file)
    if ensure_verifying(context.board):
        context = Context(str(args.conf), None, False, args.env_file)
    build = Builder(context.board, args.ids)

    demo = load_demo()
    now = datetime.now(UTC).replace(microsecond=0)
    past, current, cards, running, planned, planned_facts = demo.demo_data(now)
    rng = random.Random(11)

    build.drop_locked_empty_cycles(now)
    # Every sprint's cycle first: the board caches the project's cycles when
    # it connects, so cards can only join cycles that existed by then.
    end = (now + timedelta(days=1)).isoformat()
    for sprint in past:
        build.cycle(
            sprint["sprint"],
            sprint["title"],
            sprint["started"],
            end,
        )
    for sprint in current:
        build.cycle(sprint["sprint"], sprint["title"], sprint["started"], end)
    for sprint in planned:
        build.cycle(sprint["sprint"], sprint["title"])
    context = Context(str(args.conf), None, False, args.env_file)
    build.board = context.board

    # Completed sprints: delivered cards Done, the rest Cancelled.
    for sprint in past:
        number, title = sprint["sprint"], sprint["title"]
        total = max(sprint["cards_start"], sprint["cards_end"])
        steps = demo.CARD_STEPS
        names = [
            f"{steps[k % len(steps)]} {title.lower()}" for k in range(total)
        ]
        build.cards(
            number,
            [
                (
                    None,
                    name,
                    description(name, title, sprint["goal"], ""),
                    rng.choice([1, 2, 2, 3, 3]),
                    "Done" if k < sprint["cards_end"] else "Cancelled",
                )
                for k, name in enumerate(names)
            ],
            ended=sprint["ended"],
        )

    # Running sprints: their cards in their current states.
    for sprint in current:
        number = sprint["sprint"]
        members = cards[str(number)]
        touches = demo.CURRENT_TOUCHES[sprint["alias"]]
        build.cards(
            number,
            [
                (
                    c["id"],
                    c["title"],
                    description(
                        c["title"],
                        sprint["title"],
                        sprint["goal"],
                        f"## Delivery plan\nTouches:\n- {touches[k]}\n"
                        "Dependencies: assessed",
                    ),
                    c["points"],
                    c["state"],
                )
                for k, c in enumerate(members)
            ],
        )

    # Planned sprints: Backlog cards with their declarations.
    for sprint, fact in zip(planned, planned_facts, strict=True):
        number = sprint["sprint"]
        build.cards(
            number,
            [
                (
                    c.id,
                    c.title,
                    description(
                        c.title,
                        fact.title,
                        fact.goal,
                        demo.delivery_plan.render_section(c.delivery_plan),
                    ),
                    c.points,
                    "Backlog",
                )
                for c in fact.cards
            ],
        )

    # Dependencies between cards, running and planned alike.
    real = build.ids["cards"]
    for fact in running + planned_facts:
        for c in fact.cards:
            if c.blocked_by:
                build.relate(
                    real[c.id]["id"], [real[b]["id"] for b in c.blocked_by]
                )
    for members in cards.values():
        for c in members:
            blockers = [
                m["id"]
                for ms in cards.values()
                for m in ms
                if m["ref"] in {b["ref"] for b in c["blocked_by"]}
            ]
            if blockers:
                build.relate(
                    real[c["id"]]["id"], [real[b]["id"] for b in blockers]
                )
    print("done", flush=True)


if __name__ == "__main__":
    main()
