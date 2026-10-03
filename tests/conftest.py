"""Fixtures: a config file on disk, and a board whose client records instead of sending.

The guards are the point of this tool, and every one of them must be watchable
firing without a Plane instance. `FakeClient` therefore records calls and
returns plausible objects, so a test can assert both that a refused write sent
nothing and that an accepted one sent the right thing.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

import pytest
from plane.client.plane_client import PlaneClient

from plane_proj.board import Board
from plane_proj.config import Config, project_from_facts

# A real client, built only so the doubles can read the true method
# signatures off it. No request is ever made through it.
_PROBE = PlaneClient(base_url="https://example.invalid", api_key="not-a-key")

ALICE = "2f7c8d91-6a43-4e5b-9c12-8d34f7a6b901"
AUTOMATION = "8a1e4c73-2d95-47b6-a821-5f09c3d7e462"

SCALE = {
    "1": "uuid-1", "2": "uuid-2", "3": "uuid-3",
    "5": "uuid-5", "10": "uuid-10", "22": "uuid-22",
}


def config_document(**project_overrides: Any) -> dict[str, Any]:
    """A complete, valid config document, with one project's entry patchable."""
    project: dict[str, Any] = {
        "id": "project-uuid",
        "name": "Example Project",
        "estimate_points": dict(SCALE),
        "cycles": {"Sprint 1": "cycle-1"},
        "modules": {"pipeline": "module-pipeline", "viewer": "module-viewer"},
        "states": {
            "Backlog": "state-backlog",
            "Todo": "state-todo",
            "In Progress": "state-progress",
            "Verifying": "state-verifying",
            "Done": "state-done",
            "Cancelled": "state-cancelled",
        },
        "labels": {"blocked": "label-blocked"},
        "states_outside_cycles": ["Backlog", "Cancelled"],
        "rules": {
            "require_cycle": True,
            "require_module": True,
            "require_estimate": True,
            "cycle_estimate_max": 3,
            "wip_limit": 2,
            "wip_states": ["In Progress"],
        },
    }
    project.update(project_overrides)
    return {
        "workspace": {
            "slug": "example-workspace",
            "members": {ALICE: "alice", AUTOMATION: "automation"},
            "unestimated_assignees": [ALICE],
        },
        "defaults": {"project": "DEMO"},
        "projects": {"DEMO": project},
    }


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "defaults": {"workspace": "example-workspace", "project": "DEMO"},
        "estimate_points": SCALE,
    }), encoding="utf-8")
    return path


@pytest.fixture
def config(config_path: Path):
    document = config_document()
    return Config(
        path=config_path, workspace_slug="example-workspace",
        members={ALICE: "alice", AUTOMATION: "automation"},
        unestimated_assignees=frozenset({ALICE}),
        default_project="DEMO",
        projects={"DEMO": project_from_facts("DEMO", document["projects"]["DEMO"], config_path)},
        document=json.loads(config_path.read_text(encoding="utf-8")),
    )


#: Resource attribute on PlaneClient -> the real SDK class, for signature checks.
REAL_RESOURCES = {
    name: type(getattr(_PROBE, name))
    for name in dir(_PROBE)
    if not name.startswith("_") and name != "config"
}


class Recorder:
    """A stand-in resource that records calls — and rejects ones the SDK would.

    **It binds every call against the real SDK signature before recording it.**
    Without that, a double built on `__getattr__` accepts anything, and a
    call that would raise `TypeError` against the live client passes every
    test. That is not hypothetical: both membership calls passed
    `data={"issues": [...]}` to methods whose parameter is a positional
    `issue_ids`, so placing a card in its cycle — the guard this tool exists
    for — was broken, and the suite was green.
    """

    def __init__(self, log: list[tuple[str, tuple, dict]], name: str, result: Any = None) -> None:
        self._log, self._name, self._result = log, name, result

    def __getattr__(self, method: str) -> Any:
        real = getattr(REAL_RESOURCES.get(self._name), method, None)

        def call(*args: Any, **kwargs: Any) -> Any:
            if real is not None:
                # Raises TypeError exactly as the live client would.
                inspect.signature(real).bind(None, *args, **kwargs)
            self._log.append((f"{self._name}.{method}", args, kwargs))
            return self._result() if callable(self._result) else self._result
        return call


class Card:
    """The subset of a work item this tool reads."""

    def __init__(self, **fields: Any) -> None:
        self.id = fields.get("id", "card-uuid")
        self.sequence_id = fields.get("sequence_id", 12)
        self.name = fields.get("name", "A card")
        self.state = fields.get("state", "state-todo")
        self.estimate_point = fields.get("estimate_point")
        self.point = fields.get("point")
        self.assignees = fields.get("assignees", [AUTOMATION])
        self.description_html = fields.get("description_html", "<p>body</p>")
        self.priority = fields.get("priority", "none")
        self.cycle_id = fields.get("cycle_id")


class FakeClient:
    """Records writes; answers reads from whatever the test put in `cards`."""

    def __init__(self, cards: list[Card] | None = None) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        # The SDK client's request config; board code resolves relative
        # attachment URLs against `base_path` exactly as the live client.
        self.config = type(
            "ClientConfig", (), {"base_path": "https://plane.example/api/v1/"}
        )()
        self.cards = cards if cards is not None else []
        self.created = Card(id="new-card", sequence_id=13)
        self.retrieved: Card | None = None
        # Raw card JSON with `estimate_point` expanded, as the API returns it
        # for `?expand=estimate_point`.
        self.expanded: list[dict[str, Any]] = []
        self.intake_records: list[Any] = []
        self.intake_retrieved: Any | None = None
        self.comments: list[Any] = []
        self.activities: list[Any] = []

        outer = self

        class Comments(Recorder):
            def __init__(self) -> None:
                super().__init__(outer.calls, "comments")

            def list(self, *args: Any, **kwargs: Any) -> Any:
                outer.calls.append(("comments.list", args, kwargs))
                return _page(outer.comments)

            def create(self, *args: Any, **kwargs: Any) -> Any:
                outer.calls.append(("comments.create", args, kwargs))
                sent = kwargs["data"]
                created = type("Comment", (), {
                    "id": f"comment-{len(outer.comments) + 1}",
                    "comment_html": sent.comment_html,
                    "created_at": "2026-09-16T01:00:00Z",
                    "actor": AUTOMATION,
                })()
                outer.comments.append(created)
                return created

        class WorkItems(Recorder):
            def __init__(self) -> None:
                super().__init__(outer.calls, "work_items")
                self.relations = Recorder(outer.calls, "relations")
                self.comments = Comments()
                self.activities = Recorder(
                    outer.calls, "activities", result=lambda: _page(outer.activities)
                )

            def create(self, *args: Any, **kwargs: Any) -> Card:
                outer.calls.append(("work_items.create", args, kwargs))
                # Reflect what was sent, the way a real server does. A fake
                # that always answers "unestimated" makes the readback guard
                # fire on every correct write, which reads as the guard being
                # broken rather than the fake being unfaithful.
                sent = kwargs.get("data")
                outer.created = Card(
                    id="new-card",
                    sequence_id=13,
                    estimate_point=getattr(sent, "estimate_point", None),
                    point=getattr(sent, "point", None),
                    state=getattr(sent, "state", None) or "state-todo",
                )
                return outer.created

            def retrieve(self, *args: Any, **kwargs: Any) -> Card:
                outer.calls.append(("work_items.retrieve", args, kwargs))
                if outer.retrieved is not None:
                    return outer.retrieved
                wanted = str(args[2])
                return next(
                    (card for card in outer.cards if str(card.id) == wanted),
                    outer.created,
                )

            def list(self, *args: Any, **kwargs: Any) -> Any:
                outer.calls.append(("work_items.list", args, kwargs))
                return _page(outer.cards)

            def _get(self, endpoint: str, params: Any = None) -> Any:
                """The raw path `_expanded_cards` uses; answers from `expanded`."""
                outer.calls.append(("work_items._get", (endpoint,), {"params": params}))
                return {"results": outer.expanded, "next_page_results": False}

            def _patch(self, endpoint: str, data: Any = None) -> Any:
                """The raw write path. Records the plain dict actually sent."""
                outer.calls.append(("work_items._patch", (endpoint,), {"data": data}))
                sent = data or {}
                wanted = endpoint.rstrip("/").rsplit("/", 1)[-1]
                card = next(
                    (card for card in outer.cards if str(card.id) == wanted),
                    Card(id=wanted),
                )
                for name, value in sent.items():
                    setattr(card, name, value)
                outer.created = card
                return {}

        self.work_items = WorkItems()
        self.work_items.attachments = Recorder(self.calls, "attachments", result=[])
        class Intake(Recorder):
            def __init__(self) -> None:
                super().__init__(outer.calls, "intake")

            def create(self, *args: Any, **kwargs: Any) -> Any:
                super().__getattr__("create")(*args, **kwargs)
                issue = kwargs["data"].issue
                item = type("IntakeItem", (), {
                    "id": "new-intake", "issue": "new-intake-issue",
                    "status": -2, "source": "in_app",
                    "issue_detail": Card(
                        id="new-intake-issue", sequence_id=14,
                        name=issue.name,
                        description_html=issue.description_html,
                    ),
                })()
                outer.intake_records.append(item)
                return item

            def list(self, *args: Any, **kwargs: Any) -> Any:
                super().__getattr__("list")(*args, **kwargs)
                return _page(outer.intake_records)

            def update(self, *args: Any, **kwargs: Any) -> Any:
                super().__getattr__("update")(*args, **kwargs)
                issue_id = args[2]
                for item in outer.intake_records:
                    if getattr(item, "issue", None) == issue_id:
                        item.status = kwargs["data"].status
                        return item
                return None

            def retrieve(self, *args: Any, **kwargs: Any) -> Any:
                super().__getattr__("retrieve")(*args, **kwargs)
                if outer.intake_retrieved is not None:
                    return outer.intake_retrieved
                issue_id = args[2]
                return next(
                    item for item in outer.intake_records
                    if getattr(item, "issue", None) == issue_id
                )

        self.intake = Intake()
        self.cycles = Recorder(self.calls, "cycles", result=_page([]))
        self.modules = Recorder(self.calls, "modules", result=_page([]))
        self.states = Recorder(self.calls, "states", result=_page([]))
        self.labels = Recorder(self.calls, "labels", result=_page([]))
        self.estimates = Recorder(self.calls, "estimates")

    def named(self, name: str) -> list[tuple[str, tuple, dict]]:
        return [call for call in self.calls if call[0] == name]

    @property
    def write_calls(self) -> list[str]:
        """Every call that changes the board — what a refused guard must leave empty."""
        return [
            name for name, _, _ in self.calls
            if any(name.endswith(verb) for verb in
                   (".create", ".update", "._patch", ".add_work_items"))
        ]


class _Page:
    def __init__(self, results: list[Any]) -> None:
        self.results = results
        self.next_page_results = False
        self.next_cursor = None


def _page(results: list[Any]) -> _Page:
    return _Page(results)


@pytest.fixture
def client() -> FakeClient:
    return FakeClient()


@pytest.fixture
def board(config, client: FakeClient) -> Board:
    return Board(client, config, config.project("DEMO"), "example-workspace")
