"""A read-only web view of the sprint register.

The page is one static file; `/api/sprints` returns the `sprints list --json
--all` payload plus the facts the page draws that the listing lacks: current
cycle cards and Plane links. `/api/version` changes whenever the register file
does, so the page polls it and reloads the payload only then. Nothing here
writes to the register or the board.
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

DEFAULT_PLANE_URL = "https://app.plane.so"


def board_url(plane_url: str, workspace: str, project_id: str) -> str:
    """The project's work-item list in the Plane web app."""
    return f"{plane_url.rstrip('/')}/{workspace}/projects/{project_id}/issues/"


def build_payload(
    listing: dict[str, Any], *, project: dict[str, str], board: str,
    cards: dict[int, list[dict[str, Any]]], estimates: bool,
) -> dict[str, Any]:
    """Everything the page renders, in one JSON document."""
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "project": project,
        "board_url": board,
        "estimates": estimates,
        "listing": listing,
        "cards": {
            str(sprint_id): [card | {"url": board + card["id"]} for card in members]
            for sprint_id, members in cards.items()
        },
    }


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
    host: str, port: int, load: Callable[[], dict[str, Any]],
    version: Callable[[], str],
) -> HTTPServer:
    """A single-threaded server: board reads never run concurrently."""
    html = page().encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # http.server dispatches on this name
            path = self.path.split("?", 1)[0]
            if path == "/":
                self._send(200, "text/html; charset=utf-8", html)
            elif path == "/api/version":
                body = json.dumps({"version": version()}).encode()
                self._send(200, "application/json", body)
            elif path == "/api/sprints":
                try:
                    body = json.dumps(load()).encode()
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
