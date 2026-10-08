"""The web view's HTTP routes and page asset."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from plane_proj import web
from plane_proj.config import DEFAULT_WEB_URL, Config
from plane_proj.guards import GuardViolation


def test_default_board_link_is_plane_cloud() -> None:
    assert web.board_url(DEFAULT_WEB_URL, "ws", "pid") == (
        "https://app.plane.so/ws/projects/pid/issues/"
    )


def test_browse_link_is_the_browse_page() -> None:
    assert web.browse_url(DEFAULT_WEB_URL, "ws") == (
        "https://app.plane.so/ws/browse/"
    )


def test_cycle_link_is_the_cycle_page() -> None:
    assert web.cycle_url("https://plane.example/", "ws", "pid", "cid") == (
        "https://plane.example/ws/projects/pid/cycles/cid"
    )


@pytest.fixture
def serve() -> Iterator:
    servers = []

    def start(
        load,
        version=lambda: "v1",
        readiness=lambda: {"summary": {}},
    ):
        routes = {
            "/api/sprints": load,
            "/api/version": lambda: {"version": version()},
            "/api/readiness": readiness,
        }
        server = web.make_server("127.0.0.1", 0, routes)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_port}"

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def test_routes_serve_page_payload_version_and_not_found(serve) -> None:
    base = serve(lambda: {"listing": {}}, lambda: "v7", lambda: {"summary": {"sprints": 3}})

    with urlopen(base + "/") as response:
        assert "api/sprints" in response.read().decode()
    with urlopen(base + "/api/sprints") as response:
        assert json.load(response) == {"listing": {}}
    with urlopen(base + "/api/version") as response:
        assert json.load(response) == {"version": "v7"}
    with urlopen(base + "/api/readiness") as response:
        assert json.load(response) == {"summary": {"sprints": 3}}
    with pytest.raises(HTTPError) as missing:
        urlopen(base + "/elsewhere")
    assert missing.value.code == 404


def test_version_checks_are_not_logged_but_loads_are(serve, capsys) -> None:
    base = serve(lambda: {"listing": {}})

    for path in ("/api/version", "/api/version", "/api/sprints"):
        with urlopen(base + path):
            pass

    logged = capsys.readouterr().err
    assert "GET /api/sprints" in logged
    assert "/api/version" not in logged


def test_a_failed_read_reports_its_rule_and_keeps_serving(serve) -> None:
    calls = []

    def load():
        calls.append(1)
        if len(calls) == 1:
            raise GuardViolation("Example rule: the register is unbound")
        raise RuntimeError("unexpected detail")

    base = serve(load)
    for expected in ("Example rule: the register is unbound", "RuntimeError; see the server log."):
        with pytest.raises(HTTPError) as failed:
            urlopen(base + "/api/sprints")
        assert failed.value.code == 500
        assert json.load(failed.value) == {"error": expected}


def test_current_sprint_timers_computed_live_from_plane(tmp_path: Path, config: Config) -> None:
    from types import SimpleNamespace

    from plane_proj import sprints as sprints_module
    from plane_proj.board import Board
    from plane_proj.cli import sprints_web_routes
    from tests.conftest import AUTOMATION, Card, FakeClient

    database = tmp_path / "SPRINTS.sqlite"
    sprints_module.create_database(database, ("https://plane.example", "example-workspace", "DEMO"))
    with sprints_module.connect_database(database, writable=True) as conn:
        sprints_module.plan_sprint(conn, 1, "Test Sprint", 1, "Goal", "Exec", ("Accept",))
        sprints_module.start_sprint(conn, 1, "2026-10-01T00:00:00Z", "cycle-1", 2, 5)
        # Snapshot for settled card (c-done): 30 minutes coding, final
        sprints_module.record_execution_snapshot(
            conn, sprint_id=1, work_item_id="c-done", card_reference="DEMO-2",
            captured_at="2026-10-01T01:00:00Z", is_final=True,
            stats={
                "state_minutes": {"Done": 60.0},
                "execution_minutes": {"coding": 30.0},
                "open_timer": None,
            },
        )
        # Snapshot for open card (c-open): captured earlier at 01:00 with 10 mins coding
        sprints_module.record_execution_snapshot(
            conn, sprint_id=1, work_item_id="c-open", card_reference="DEMO-1",
            captured_at="2026-10-01T01:00:00Z", is_final=False,
            stats={
                "state_minutes": {"In Progress": 30.0},
                "current_state": {
                    "name": "In Progress", "started": "2026-10-01T00:30:00Z",
                    "elapsed_minutes": 30.0,
                },
                "execution_minutes": {"coding": 10.0},
                "open_timer": None,
            },
        )

    c_open = Card(
        id="c-open", sequence_id=1, name="Open Card", state="state-progress",
        cycle_id="cycle-1",
    )
    c_done = Card(
        id="c-done", sequence_id=2, name="Done Card", state="state-done",
        cycle_id="cycle-1",
    )
    client = FakeClient([c_open, c_done])

    # Timer comments for c-open newer than its last snapshot (captured at 01:00:00Z)
    open_comments = [
        SimpleNamespace(
            id="comm-1",
            comment_html='<p>plane-proj-execution/v1 {"action":"start","category":"coding"}</p>',
            created_at="2026-10-01T01:10:00Z",
            actor=AUTOMATION,
        ),
        SimpleNamespace(
            id="comm-2",
            comment_html='<p>plane-proj-execution/v1 {"action":"stop","category":"coding"}</p>',
            created_at="2026-10-01T01:30:00Z",
            actor=AUTOMATION,
        ),
        SimpleNamespace(
            id="comm-3",
            comment_html='<p>plane-proj-execution/v1 {"action":"start","category":"coding"}</p>',
            created_at="2026-10-01T01:35:00Z",
            actor=AUTOMATION,
        ),
    ]
    client.comments = {"c-open": open_comments}

    board = Board(client, config, config.project("DEMO"), "example-workspace")
    obj = {
        "root": SimpleNamespace(board=board, config=config, as_json=False, project_key="DEMO"),
        "database": database,
        "binding": ("https://plane.example", "example-workspace", "DEMO"),
    }

    routes = sprints_web_routes(obj)

    # 1. Fetch /api/sprints
    payload = routes["/api/sprints"]()

    # Verify: comments.list called for c-open, NOT for c-done
    comment_calls = [call for call in client.calls if call[0] == "comments.list"]
    called_card_ids = [call[1][2] for call in comment_calls]
    assert "c-open" in called_card_ids
    assert "c-done" not in called_card_ids

    # Verify: current sprint timing has closed timer minutes (30 + 20 = 50) and open timer
    current_timing = payload["listing"]["current"][0]["timing"]
    assert current_timing is not None
    assert current_timing["execution_minutes"]["coding"] == 50.0
    assert "coding" in current_timing["open_timer_minutes"]
    assert current_timing["open_timer_minutes"]["coding"] > 0

    # 2. Verify register-only payload makes no Plane request
    client.calls.clear()
    local_payload = routes["/api/sprints/local"]()
    assert local_payload["partial"] is True
    assert client.calls == []


def test_current_page_refetches_timing_about_once_a_minute() -> None:
    content = web.page()
    assert "isCurrentOpen" in content
    assert "60000" in content


def test_web_page_contains_parallel_readiness_elements() -> None:
    content = web.page()
    assert "--parallel" in content
    assert "blocked" in content
    assert "can start" in content
    assert "clear to start" in content
    assert "cardIconsHtml" in content
    assert "formatWhyTooltip" in content

