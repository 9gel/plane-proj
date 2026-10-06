"""Tests for sprint readiness, code overlap, and dependency graph."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from plane_proj import sprints as s_module
from plane_proj.cli import Context, cli
from plane_proj.delivery_plan import DeliveryPlan, TouchedPath
from plane_proj.readiness import (
    STATE_CAN_START,
    STATE_NOT_READY,
    STATE_OVERLAP,
    STATE_UNVERIFIED,
    CardFact,
    SprintFact,
    evaluate_readiness,
    paths_overlap,
)
from tests.conftest import Card


@pytest.fixture
def sprint_db(tmp_path) -> Path:
    db = Path(tmp_path) / "SPRINTS.sqlite"
    s_module.create_database(db, ("https://plane.test", "example-workspace", "DEMO"))
    return db


def test_paths_overlap_detects_equality_and_nesting() -> None:
    assert paths_overlap("src/plane_proj/cli.py", "src/plane_proj/cli.py")
    assert paths_overlap("src/plane_proj/", "src/plane_proj/cli.py")
    assert paths_overlap("src/plane_proj/cli.py", "src/plane_proj/")
    assert paths_overlap("src/plane_proj/readiness.py (new)", "src/plane_proj/readiness.py")
    assert not paths_overlap("src/plane_proj/cli.py", "src/plane_proj/web.py")
    assert not paths_overlap("none", "src/plane_proj/cli.py")


def test_four_readiness_states_and_reasons_naming_cards() -> None:
    """pytest gives one sprint per state from constructed facts and checks

    the reasons name the blocking or shared cards.
    """
    plan_run = (
        "<div><h2>Delivery plan</h2><p>Touches:<br>- src/run.py<br>"
        "Dependencies: assessed</p></div>"
    )
    running_card = CardFact(
        id="c-run",
        ref="DEMO-1",
        title="Running card",
        state="In Progress",
        points=3,
        sprint_id=1,
        description_html=plan_run,
    )
    running_sprint = SprintFact(
        sprint_id=1,
        title="Current Sprint",
        alias="RUN-1",
        is_current=True,
        cards=(running_card,),
    )

    # 1. Not ready: card waits on unfinished card in another sprint
    plan_blocked = (
        "<div><h2>Delivery plan</h2><p>Touches:<br>- src/blocked.py<br>"
        "Dependencies: assessed</p></div>"
    )
    card_blocked = CardFact(
        id="c-not-ready",
        ref="DEMO-10",
        title="Blocked card",
        state="Todo",
        points=3,
        sprint_id=10,
        blocked_by=("c-run",),
        description_html=plan_blocked,
    )
    sprint_not_ready = SprintFact(
        sprint_id=10,
        title="Not Ready Sprint",
        alias="WAIT-1",
        position=1,
        cards=(card_blocked,),
    )

    # 2. Overlap: shares code with running sprint
    card_overlap = CardFact(
        id="c-overlap",
        ref="DEMO-11",
        title="Overlap card",
        state="Todo",
        points=2,
        sprint_id=11,
        description_html=plan_run,
    )
    sprint_overlap = SprintFact(
        sprint_id=11,
        title="Overlap Sprint",
        alias="OVL-1",
        position=2,
        cards=(card_overlap,),
    )

    # 3. Unverified: card lacks declared scope and dependency assessment
    card_unverified = CardFact(
        id="c-unverified",
        ref="DEMO-12",
        title="Unverified card",
        state="Todo",
        points=4,
        sprint_id=12,
        description_html="<p>No delivery plan section here</p>",
    )
    sprint_unverified = SprintFact(
        sprint_id=12,
        title="Unverified Sprint",
        alias="UNV-1",
        position=3,
        cards=(card_unverified,),
    )

    # 4. Can start: unblocked, verified, no overlap
    plan_fresh = (
        "<div><h2>Delivery plan</h2><p>Touches:<br>- src/fresh.py<br>"
        "Dependencies: assessed</p></div>"
    )
    card_can_start = CardFact(
        id="c-can-start",
        ref="DEMO-13",
        title="Can start card",
        state="Todo",
        points=5,
        sprint_id=13,
        description_html=plan_fresh,
    )
    sprint_can_start = SprintFact(
        sprint_id=13,
        title="Can Start Sprint",
        alias="GO-1",
        position=4,
        cards=(card_can_start,),
    )

    report = evaluate_readiness(
        [running_sprint],
        [sprint_not_ready, sprint_overlap, sprint_unverified, sprint_can_start],
        {},
    )

    states = {row["sprint_id"]: row["state"] for row in report["queue"]}
    whys = {row["sprint_id"]: row["why"] for row in report["queue"]}

    assert states[10] == STATE_NOT_READY
    assert any("DEMO-10" in r and "DEMO-1" in r for r in whys[10])

    assert states[11] == STATE_OVERLAP
    assert any("DEMO-11" in r and "DEMO-1" in r and "src/run.py" in r for r in whys[11])

    assert states[12] == STATE_UNVERIFIED
    assert any("DEMO-12 lacks declared scope" in r for r in whys[12])
    assert any("DEMO-12 dependencies not assessed" in r for r in whys[12])

    assert states[13] == STATE_CAN_START
    assert whys[13] == []


def test_overlap_only_against_running_and_earlier_startable_sprints() -> None:
    """pytest shows overlap only against running sprints and earlier startable

    sprints, so two Can start sprints never share a path.
    """
    plan_shared = (
        "<div><h2>Delivery plan</h2><p>Touches:<br>- src/shared.py<br>"
        "Dependencies: assessed</p></div>"
    )
    # Sprint 20: blocked by external card -> Not ready. Touches src/shared.py
    card_20 = CardFact(
        id="c-20",
        ref="DEMO-20",
        title="Card 20",
        state="Todo",
        points=3,
        sprint_id=20,
        blocked_by=("ext-1",),
        description_html=plan_shared,
    )
    sprint_20 = SprintFact(
        sprint_id=20,
        title="Sprint 20",
        position=1,
        cards=(card_20,),
    )

    # Sprint 21: clear to start. Touches src/shared.py
    # Since Sprint 20 is Not ready, Sprint 21 DOES NOT clash with 20.
    card_21 = CardFact(
        id="c-21",
        ref="DEMO-21",
        title="Card 21",
        state="Todo",
        points=3,
        sprint_id=21,
        description_html=plan_shared,
    )
    sprint_21 = SprintFact(
        sprint_id=21,
        title="Sprint 21",
        position=2,
        cards=(card_21,),
    )

    # Sprint 22: clear of blockers, touches src/shared.py.
    # Since Sprint 21 is Can start, Sprint 22 clashes with 21 -> Overlap.
    card_22 = CardFact(
        id="c-22",
        ref="DEMO-22",
        title="Card 22",
        state="Todo",
        points=3,
        sprint_id=22,
        description_html=plan_shared,
    )
    sprint_22 = SprintFact(
        sprint_id=22,
        title="Sprint 22",
        position=3,
        cards=(card_22,),
    )

    report = evaluate_readiness(
        [],
        [sprint_20, sprint_21, sprint_22],
        {"ext-1": ("EXT-1", "In Progress")},
    )

    queue = {row["sprint_id"]: row for row in report["queue"]}
    assert queue[20]["state"] == STATE_NOT_READY
    assert queue[21]["state"] == STATE_CAN_START
    assert queue[22]["state"] == STATE_OVERLAP
    assert any("DEMO-22" in r and "DEMO-21" in r for r in queue[22]["why"])

    # Two Can start sprints never share code
    can_start = [r for r in report["queue"] if r["state"] == STATE_CAN_START]
    assert len(can_start) == 1
    assert can_start[0]["sprint_id"] == 21


def test_serial_and_parallel_times_with_assumed_and_median_velocity() -> None:
    """pytest checks serial and parallel times, including the 3.5 pts/h

    assumption when no sprint is completed.
    """
    # Chain: S1 (running, 7 remaining pts) -> S2 (planned, 7 pts) -> S3 (planned, 14 pts)
    # S4 (planned, 7 pts) independent
    c1 = CardFact(
        id="c1", ref="DEMO-1", title="C1", state="In Progress", points=7, sprint_id=1,
        delivery_plan=DeliveryPlan(touches=[TouchedPath("src/a.py")], dependencies_assessed=True),
    )
    c2 = CardFact(
        id="c2", ref="DEMO-2", title="C2", state="Todo", points=7, sprint_id=2, blocked_by=("c1",),
        delivery_plan=DeliveryPlan(touches=[TouchedPath("src/b.py")], dependencies_assessed=True),
    )
    c3 = CardFact(
        id="c3", ref="DEMO-3", title="C3", state="Todo", points=14, sprint_id=3, blocked_by=("c2",),
        delivery_plan=DeliveryPlan(touches=[TouchedPath("src/c.py")], dependencies_assessed=True),
    )
    c4 = CardFact(
        id="c4", ref="DEMO-4", title="C4", state="Todo", points=7, sprint_id=4,
        delivery_plan=DeliveryPlan(touches=[TouchedPath("src/d.py")], dependencies_assessed=True),
    )

    s1 = SprintFact(sprint_id=1, title="S1", is_current=True, cards=(c1,))
    s2 = SprintFact(sprint_id=2, title="S2", position=1, cards=(c2,))
    s3 = SprintFact(sprint_id=3, title="S3", position=2, cards=(c3,))
    s4 = SprintFact(sprint_id=4, title="S4", position=3, cards=(c4,))

    # 1. No completed sprints -> Assumed 3.5 pts/h
    # Total planned points = 7 + 14 + 7 = 28 pts
    # Serial hours = 28 / 3.5 = 8.0h
    # Longest chain to planned: s1 (7 pts = 2h) -> s2 (7 pts = 2h) -> s3 (14 pts = 4h) = 8.0h
    # s4 = 7 pts = 2h
    # Parallel hours = 8.0h
    rep_assumed = evaluate_readiness([s1], [s2, s3, s4], {}, completed_velocities=[])
    assert rep_assumed["summary"]["velocity"] == 3.5
    assert rep_assumed["summary"]["velocity_source"] == "assumed"
    assert rep_assumed["summary"]["serial_hours"] == 8.0
    assert rep_assumed["summary"]["parallel_hours"] == 8.0
    assert rep_assumed["graph"]["critical_path"] == [1, 2, 3]

    # 2. Completed sprints with velocities [6.0, 7.0, 8.0] -> median 7.0 pts/h
    # Serial hours = 28 / 7.0 = 4.0h
    # Parallel hours = (7 + 7 + 14) / 7.0 = 4.0h
    rep_median = evaluate_readiness(
        [s1], [s2, s3, s4], {}, completed_velocities=[6.0, 7.0, 8.0]
    )
    assert rep_median["summary"]["velocity"] == 7.0
    assert rep_median["summary"]["velocity_source"] == "median"
    assert rep_median["summary"]["serial_hours"] == 4.0
    assert rep_median["summary"]["parallel_hours"] == 4.0


def test_board_sprint_readiness_cards_and_facts(board, client, monkeypatch) -> None:
    plan_alpha = (
        "<div><h2>Delivery plan</h2><p>Touches:<br>- src/alpha.py<br>"
        "Dependencies: assessed</p></div>"
    )
    cards = [
        Card(
            id="card-1",
            sequence_id=1,
            name="Alpha",
            state="state-todo",
            estimate_point="uuid-3",
            description_html=plan_alpha,
        )
    ]
    monkeypatch.setattr(
        client.cycles,
        "list_work_items",
        lambda slug, project_id, cycle_id, params=None: SimpleNamespace(
            results=cards, next_page_results=False, next_cursor=None
        ),
    )
    monkeypatch.setattr(
        board,
        "relations",
        lambda card_id: {"blocked_by": [], "blocking": []},
    )

    res = board.sprint_readiness_cards("cycle-10")
    assert len(res) == 1
    assert res[0]["ref"] == "DEMO-1"
    assert res[0]["points"] == 3
    assert "src/alpha.py" in res[0]["description_html"]

    facts = board.readiness_facts({10: "cycle-10"})
    assert 10 in facts["members"]
    assert facts["members"][10][0]["ref"] == "DEMO-1"


def test_sprints_readiness_cli_text_and_json(
    board, client, sprint_db, monkeypatch, config_path
) -> None:
    monkeypatch.setattr(Context, "board", property(lambda self: board))
    monkeypatch.setattr(board, "sprint_cycles", lambda ids: {2: "cycle-2"} if 2 in ids else {})

    # Set up sprints in DB: 1 current, 2 planned
    with s_module.connect_database(sprint_db, writable=True) as conn:
        s_module.plan_sprint(
            conn, 1, "Sprint 1", 1, "Current goal", "", ("criterion",),
        )
        s_module.set_alias(conn, 1, "RUN-1")
        s_module.start_sprint(
            conn, 1, "2026-10-01T00:00:00+00:00", "cycle-1", 1, 3,
        )
        s_module.plan_sprint(
            conn, 2, "Sprint 2", 1, "Planned goal", "", ("criterion",),
        )
        s_module.set_alias(conn, 2, "PLAN-1")

    plan_core = (
        "<div><h2>Delivery plan</h2><p>Touches:<br>- src/core.py<br>"
        "Dependencies: assessed</p></div>"
    )
    cards = {
        "cycle-1": [
            Card(
                id="c1", sequence_id=1, name="Card 1", state="state-in-prog",
                estimate_point="uuid-3",
                description_html=plan_core,
            )
        ],
        "cycle-2": [
            Card(
                id="c2", sequence_id=2, name="Card 2", state="state-todo",
                estimate_point="uuid-5",
                description_html=plan_core,
            )
        ],
    }

    monkeypatch.setattr(
        client.cycles,
        "list_work_items",
        lambda slug, project_id, cycle_id, params=None: SimpleNamespace(
            results=cards.get(cycle_id, []), next_page_results=False, next_cursor=None
        ),
    )
    monkeypatch.setattr(
        board,
        "relations",
        lambda card_id: {"blocked_by": [], "blocking": []},
    )

    runner = CliRunner()
    # Test plain text output
    res = runner.invoke(
        cli,
        [
            "--conf", str(config_path),
            "sprints",
            "--database", str(sprint_db),
            "readiness",
        ],
        catch_exceptions=False,
    )
    assert res.exit_code == 0
    assert "Planned: 1 sprints" in res.output
    assert "Queue" in res.output
    assert "Overlap" in res.output
    assert "Code overlap" in res.output

    # Test --json output
    res_json = runner.invoke(
        cli,
        [
            "--conf", str(config_path),
            "sprints",
            "--database", str(sprint_db),
            "readiness",
            "--json",
        ],
        catch_exceptions=False,
    )
    assert res_json.exit_code == 0
    data = json.loads(res_json.output)
    assert "summary" in data
    assert "queue" in data
    assert len(data["queue"]) == 1
    assert data["queue"][0]["state"] == "Overlap"
    assert "overlap_pairs" in data
    assert len(data["overlap_pairs"]) == 1


def test_two_sprints_sharing_only_undeclared_shared_path_not_in_overlap_explicit_overlaps() -> None:
    """pytest from constructed facts: two sprints sharing only an undeclared
    shared path are not in overlap; listing it explicitly in both makes them
    overlap.
    """
    plan_1 = (
        "<div><h2>Delivery plan</h2><p>Touches:<br>- src/a.py<br>"
        "Dependencies: assessed</p></div>"
    )
    card_1 = CardFact(
        id="c-1",
        ref="DEMO-1",
        title="Card 1",
        state="In Progress",
        points=3,
        sprint_id=1,
        description_html=plan_1,
    )
    sprint_1 = SprintFact(
        sprint_id=1,
        title="Running Sprint",
        alias="RUN-1",
        is_current=True,
        cards=(card_1,),
    )

    plan_2_clean = (
        "<div><h2>Delivery plan</h2><p>Touches:<br>- src/b.py<br>"
        "Dependencies: assessed</p></div>"
    )
    card_2_clean = CardFact(
        id="c-2",
        ref="DEMO-2",
        title="Card 2",
        state="Todo",
        points=2,
        sprint_id=2,
        description_html=plan_2_clean,
    )
    sprint_2_clean = SprintFact(
        sprint_id=2,
        title="Planned Sprint",
        alias="PLAN-2",
        position=1,
        cards=(card_2_clean,),
    )

    shared_paths = ("pyproject.toml", "uv.lock")

    report_clean = evaluate_readiness(
        [sprint_1],
        [sprint_2_clean],
        {},
        shared_paths=shared_paths,
    )
    assert report_clean["queue"][0]["state"] == STATE_CAN_START
    assert report_clean["queue"][0]["why"] == []
    assert report_clean["overlap_pairs"] == []

    plan_1_explicit = (
        "<div><h2>Delivery plan</h2><p>Touches:<br>- src/a.py<br>- pyproject.toml<br>"
        "Dependencies: assessed</p></div>"
    )
    card_1_explicit = CardFact(
        id="c-1",
        ref="DEMO-1",
        title="Card 1",
        state="In Progress",
        points=3,
        sprint_id=1,
        description_html=plan_1_explicit,
    )
    sprint_1_explicit = SprintFact(
        sprint_id=1,
        title="Running Sprint",
        alias="RUN-1",
        is_current=True,
        cards=(card_1_explicit,),
    )

    plan_2_explicit = (
        "<div><h2>Delivery plan</h2><p>Touches:<br>- src/b.py<br>- pyproject.toml<br>"
        "Dependencies: assessed</p></div>"
    )
    card_2_explicit = CardFact(
        id="c-2",
        ref="DEMO-2",
        title="Card 2",
        state="Todo",
        points=2,
        sprint_id=2,
        description_html=plan_2_explicit,
    )
    sprint_2_explicit = SprintFact(
        sprint_id=2,
        title="Planned Sprint",
        alias="PLAN-2",
        position=1,
        cards=(card_2_explicit,),
    )

    report_overlap = evaluate_readiness(
        [sprint_1_explicit],
        [sprint_2_explicit],
        {},
        shared_paths=shared_paths,
    )
    assert report_overlap["queue"][0]["state"] == STATE_OVERLAP
    assert any("pyproject.toml" in r for r in report_overlap["queue"][0]["why"])
    assert len(report_overlap["overlap_pairs"]) == 1
    assert "pyproject.toml" in report_overlap["overlap_pairs"][0]["paths"]



def test_queue_rows_carry_every_reason_not_only_the_winning_state() -> None:
    """A blocked sprint whose card is also undeclared reports both, so the
    sprint page can say everything that stands in its way.
    """
    running = SprintFact(
        sprint_id=1,
        title="Running",
        is_current=True,
        cards=(
            CardFact(
                id="c-run", ref="DEMO-1", title="Run", state="In Progress",
                points=1, sprint_id=1,
            ),
        ),
    )
    planned = SprintFact(
        sprint_id=2,
        title="Planned",
        position=1,
        cards=(
            CardFact(
                id="c-wait", ref="DEMO-2", title="Wait", state="Todo",
                points=1, sprint_id=2, blocked_by=("c-run",),
                description_html="<p>No delivery plan</p>",
            ),
        ),
    )

    (row,) = evaluate_readiness([running], [planned], {})["queue"]

    assert row["state"] == STATE_NOT_READY
    assert row["blockers"] == ["Waits on running #1: DEMO-2 needs DEMO-1"]
    assert row["unverified"] == [
        "DEMO-2 lacks declared scope",
        "DEMO-2 dependencies not assessed",
    ]
