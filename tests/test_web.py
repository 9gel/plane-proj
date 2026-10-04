"""The web view's HTTP routes and page asset."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from plane_proj import web
from plane_proj.config import DEFAULT_WEB_URL
from plane_proj.guards import GuardViolation


def test_default_board_link_is_plane_cloud() -> None:
    assert web.board_url(DEFAULT_WEB_URL, "ws", "pid") == (
        "https://app.plane.so/ws/projects/pid/issues/"
    )


@pytest.fixture
def serve() -> Iterator:
    servers = []

    def start(load, version=lambda: "v1"):
        routes = {"/api/sprints": load,
                  "/api/version": lambda: {"version": version()}}
        server = web.make_server("127.0.0.1", 0, routes)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_port}"

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def test_routes_serve_page_payload_version_and_not_found(serve) -> None:
    base = serve(lambda: {"listing": {}}, lambda: "v7")

    with urlopen(base + "/") as response:
        assert "api/sprints" in response.read().decode()
    with urlopen(base + "/api/sprints") as response:
        assert json.load(response) == {"listing": {}}
    with urlopen(base + "/api/version") as response:
        assert json.load(response) == {"version": "v7"}
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
