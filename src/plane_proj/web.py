"""A read-only web view of the sprint register.

The page is one static file; `/api/sprints` returns the `sprints list --json
--all` payload plus the facts the page draws that the listing lacks: current
cycle cards with their blockers, and Plane links. `/api/version` changes
whenever the register file does, so the page polls it and reloads the payload
only then. `/api/dependencies` runs the critical-path and ready reports on
request, because they read every planned card's relations. Nothing here writes
to the register or the board.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any

from plane_proj.guards import PlaneProjError


def board_url(plane_url: str, workspace: str, project_id: str) -> str:
    """The project's work-item list in the Plane web app."""
    return f"{plane_url.rstrip('/')}/{workspace}/projects/{project_id}/issues/"


def browse_url(plane_url: str, workspace: str) -> str:
    """The browse URL prefix for work items in the Plane web app."""
    base = plane_url.rstrip("/")
    return f"{base}/{workspace}/browse/"


def cycle_url(
    plane_url: str, workspace: str, project_id: str, cycle_id: str,
) -> str:
    """A cycle's page in the Plane web app: only that sprint's cards."""
    base = plane_url.rstrip("/")
    return f"{base}/{workspace}/projects/{project_id}/cycles/{cycle_id}"


def build_payload(
    listing: dict[str, Any], *, project: dict[str, str], board: str | None,
    cards: dict[int, list[dict[str, Any]]], estimates: bool,
    cycle_urls: dict[int, str] | None = None,
    browse: str | None = None,
) -> dict[str, Any]:
    """Everything the page renders, in one JSON document."""
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "project": project,
        "board_url": board,
        "browse_url": browse,
        "cycle_urls": {
            str(sprint_id): url for sprint_id, url in (cycle_urls or {}).items()
        },
        "estimates": estimates,
        "listing": listing,
        "cards": {
            str(sprint_id): [
                card | {"url": board + card["id"]} for card in members
            ]
            for sprint_id, members in cards.items()
        },
    }


def with_blockers(
    cards: list[dict[str, Any]], facts: dict[str, Any],
) -> list[dict[str, Any]]:
    """Each card with the reference and state of every card it waits on."""
    states = facts["states"]
    return [
        card | {"blocked_by": [
            dict(zip(("ref", "state"),
                     states.get(blocker, (blocker, "unknown")), strict=True))
            for blocker in facts["blocked_by"].get(card["id"], [])
        ]}
        for card in cards
    ]


def page() -> str:
    return files("plane_proj").joinpath("web.html").read_text(encoding="utf-8")


def register_version(database: Path) -> str:
    """Changes whenever the register or its WAL is written; no Plane request."""
    stamps = [
        f"{path.stat().st_mtime_ns}:{path.stat().st_size}"
        for path in (database, database.with_name(database.name + "-wal"))
        if path.exists()
    ]
    return "|".join(stamps)


def make_server(
    host: str, port: int, routes: dict[str, Callable[[], dict[str, Any]]],
) -> HTTPServer:
    """A single-threaded server: board reads never run concurrently.

    `routes` maps each JSON path to the function that builds its body.
    """
    html = page().encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # http.server dispatches on this name
            path = self.path.split("?", 1)[0]
            if path == "/":
                self._send(200, "text/html; charset=utf-8", html)
            elif path in routes:
                try:
                    body = json.dumps(routes[path]()).encode()
                except PlaneProjError as error:
                    self._error(str(error))
                    return
                except Exception as error:  # report it and keep serving
                    self.log_error("%s: %s", type(error).__name__, error)
                    self._error(f"{type(error).__name__}; see the server log.")
                    return
                self._send(200, "application/json", body)
            else:
                self._send(404, "text/plain; charset=utf-8", b"Not found")

        def log_request(self, code: Any = "-", size: Any = "-") -> None:
            # The page checks /api/version every 2 s per tab; logging each
            # successful check would bury the loads and errors that matter.
            quiet = self.path.split("?", 1)[0] == "/api/version"
            if not (quiet and str(code) == "200"):
                super().log_request(code, size)

        def _error(self, message: str) -> None:
            self._send(500, "application/json", json.dumps({"error": message}).encode())

        def _send(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return HTTPServer((host, port), Handler)
