"""The static demo is the unchanged page plus every response it reads."""

import importlib.util
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from plane_proj import web

DEMO = Path(__file__).parents[1] / "demo" / "run_demo.py"


def load_demo():
    spec = importlib.util.spec_from_file_location("run_demo", DEMO)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_writes_the_page_and_every_response_it_requests(tmp_path):
    load_demo().build(tmp_path, datetime(2026, 10, 5, tzinfo=UTC))

    assert (tmp_path / "index.html").read_text(encoding="utf-8") == web.page()
    # Every relative JSON path the page fetches must exist in the copy.
    requested = set(re.findall(r"getJSON\('(api/[a-z/]+)'\)", web.page()))
    assert requested == {
        "api/sprints",
        "api/sprints/local",
        "api/version",
        "api/readiness",
    }
    for path in requested:
        body = json.loads((tmp_path / path / "index.html").read_text())
        assert body
    payload = json.loads((tmp_path / "api/sprints/index.html").read_text())
    assert payload["project"] == {"key": "WAY", "name": "Wayfinder"}
    assert len(payload["listing"]["current"]) == 2
    readiness = json.loads((tmp_path / "api/readiness/index.html").read_text())
    assert readiness["summary"]["sprints"] == 20
    states = {row["state"] for row in readiness["queue"]}
    assert states == {"Can start", "Overlap", "Unverified", "Not ready"}
    assert len(readiness["overlap_pairs"]) >= 5
    assert len(readiness["graph"]["dependencies"]) > 0
    current, planned = payload["listing"]["current"], payload["listing"]["planned"]
    assert len(readiness["graph"]["columns"]) == len(current) + len(planned)
    assert len(readiness["graph"]["critical_path"]) > 0


def test_the_invented_history_is_the_same_on_every_build(tmp_path):
    demo, now = load_demo(), datetime(2026, 10, 5, tzinfo=UTC)
    assert demo.demo_responses(now) == demo.demo_responses(now)
