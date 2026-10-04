from __future__ import annotations

import json
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from plane_proj import execution, sprints
from plane_proj.cli import Context, cli
from plane_proj.guards import OrphanedCard
from tests.conftest import Card


@pytest.mark.parametrize(
    ("zone", "expected"),
    [("UTC0", "2026-09-16 09:46:59+00:00"),
     ("HKT-8", "2026-09-16 17:46:59+08:00")],
)
def test_timestamp_display_uses_machine_timezone(monkeypatch, zone, expected):
    try:
        with monkeypatch.context() as local:
            local.setenv("TZ", zone)
            time.tzset()
            assert sprints.format_timestamp(
                "2026-09-16T09:46:59Z"
            ) == expected
    finally:
        time.tzset()


def test_detail_formats_dates_without_changing_record(monkeypatch, capsys):
    record = {"status": "Completed", "started": "2026-09-16T09:46:59Z",
              "ended": None}
    original = record.copy()
    monkeypatch.setattr(sprints, "format_timestamp", lambda value: "LOCAL")
    sprints.render_sprint_detail(record)
    assert capsys.readouterr().out.count("LOCAL") == 2
    assert record == original


class SprintBoard:
    def __init__(self) -> None:
        self.started: list[tuple[str, str]] = []
        self.closed: list[tuple[str, str]] = []
        self.orphan_checks: list[tuple[dict[int, str], set[int]]] = []
        self.project = SimpleNamespace(
            key="DEMO",
            states={"In Progress": "state-progress", "Done": "state-done"},
            estimates_enabled=True,
        )
        self.telemetry_cards = [Card(id="card-uuid", sequence_id=12, state="state-done")]

    def cycle_sprint_id(self, cycle_id: str):
        return int(cycle_id), SimpleNamespace(name=f"Sprint {cycle_id}")

    def require_no_orphans(self, current, planned):
        self.orphan_checks.append((dict(current), set(planned)))

    def start_sprint_cycle(self, cycle_id: str, started: str):
        self.started.append((cycle_id, started))
        return f"Sprint {cycle_id}", [], []

    def close_sprint_cycle(self, cycle_id: str, ended: str):
        self.closed.append((cycle_id, ended))
        return f"Sprint {cycle_id}"

    def sprint_cycles(self, sprint_ids: set[int]):
        return {sprint_id: str(sprint_id) for sprint_id in sprint_ids}

    def planned_sprint_totals(self, sprint_ids: set[int]):
        return {sprint_id: (3, 8) for sprint_id in sprint_ids}

    def sprint_cycle_metrics(self, cycle_id: str):
        return {
            "cards_current": 3, "points_current": 8,
            "cards_done": 1, "points_done": 3,
            "cards_cancelled": 1, "points_cancelled": 2,
        }

    def cycle_cards(self, cycle_id: str):
        return self.telemetry_cards

    def activities(self, card):
        return [
            SimpleNamespace(
                id="activity-start", created_at="2026-01-02T03:10:00+08:00",
                field="state", old_value="Todo", new_value="In Progress", actor="worker",
            ),
            SimpleNamespace(
                id="activity-done", created_at="2026-01-02T04:10:00+08:00",
                field="state", old_value="In Progress", new_value="Done", actor="worker",
            ),
        ]

    def comments(self, card):
        return []


@pytest.fixture(autouse=True)
def sprint_board(monkeypatch, tmp_path):
    config = tmp_path / "test-config.json"
    config.write_text(json.dumps({
        "defaults": {"workspace": "test", "project": "DEMO"},
        "estimate_points": {"1": "uuid-1"},
    }), encoding="utf-8")
    monkeypatch.setenv("PLANE_PROJ_CONFIG", str(config))
    monkeypatch.setenv("PLANE_API_HOST_URL", "https://plane.test")
    monkeypatch.delenv("PLANE_PROJ_PROJECT", raising=False)
    board = SprintBoard()
    monkeypatch.setattr(Context, "board", property(lambda self: board))
    return board


def create_register(database: Path) -> None:
    sprints.create_database(database, ("https://plane.test", "test", "DEMO"))


def invoke(database: Path, *arguments: str):
    return CliRunner().invoke(cli, ["sprints", "--database", str(database), *arguments])


def plan(database: Path, sprint_id: int, position: int = 1):
    return invoke(
        database, "plan", "--id", str(sprint_id), "--title", f"Sprint {sprint_id}",
        "--position", str(position), "--goal", "Ship the outcome",
        "--execution", "Build before integration", "--acceptance", "The gate passes",
    )


@pytest.mark.parametrize("alias", [
    "AB", "A1", "12", "00", "DBT-CM1", "A-12345", "ABCDE-1", "1234567",
])
def test_alias_valid_boundaries(tmp_path, alias):
    database = tmp_path / "sprints.sqlite"
    create_register(database)
    assert plan(database, 3).exit_code == 0
    result = invoke(database, "alias", "3", "--", alias)
    assert result.exit_code == 0, result.output
    with sprints.connect_database(database, writable=False) as connection:
        assert sprints.fetch_sprint(connection, 3).alias == alias
        assert sprints.resolve_sprint_id(connection, alias) == 3
        assert sprints.resolve_sprint_id(connection, "3") == 3


@pytest.mark.parametrize("alias", [
    "", "A", "ABCDEFGH", "ab", "Ab", "A_B", "-AB", "AB-", "A--B",
    "A-B-C", "A B", "ÄB", "A１", "AB\n", " AB",
])
def test_invalid_alias_leaves_register_and_board_unchanged(
    tmp_path, sprint_board, alias,
):
    database = tmp_path / "sprints.sqlite"
    create_register(database)
    assert plan(database, 3).exit_code == 0
    before = database.read_bytes()
    result = invoke(database, "alias", "3", "--", alias)
    assert result.exit_code != 0
    assert "Sprint alias rule" in result.output
    assert database.read_bytes() == before
    assert sprint_board.started == sprint_board.closed == []


def test_alias_lifecycle_and_all_reference_commands(tmp_path):
    database = tmp_path / "sprints.sqlite"
    create_register(database)
    result = invoke(
        database, "plan", "--id", "3", "--alias", "DBT-CM1",
        "--title", "Database", "--position", "1", "--goal", "Ship",
        "--acceptance", "Works",
    )
    assert result.exit_code == 0, result.output
    assert plan(database, 4, 2).exit_code == 0
    assert invoke(database, "reorder", "4", "DBT-CM1").exit_code == 0
    assert invoke(database, "reorder", "3", "DBT-CM1", "4").exit_code != 0
    replan = invoke(
        database, "plan", "--id", "DBT-CM1", "--title", "Replanned",
        "--position", "2", "--goal", "Ship", "--acceptance", "Works",
    )
    assert replan.exit_code == 0, replan.output
    assert "DBT-CM1" in invoke(database, "list").output
    shown = CliRunner().invoke(cli, [
        "--json", "sprints", "--database", str(database), "show", "DBT-CM1",
    ])
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.output)["sprint_id"] == 3
    assert json.loads(shown.output)["alias"] == "DBT-CM1"
    for arguments in [
        ("start", "DBT-CM1", "--started", "2026-01-02T03:04:05+08:00"),
        ("collect", "--sprint", "DBT-CM1"),
        ("telemetry", "DBT-CM1"),
        ("preflight", "DBT-CM1"),
        ("close", "DBT-CM1", "--ended", "2026-01-02T05:04:05+08:00",
         "--delivered", "Shipped"),
        ("alias", "DBT-CM1", "NEXT-1"),
        ("show", "NEXT-1"),
        ("alias", "NEXT-1", "--clear"),
        ("show", "3"),
    ]:
        result = invoke(database, *arguments)
        assert result.exit_code == 0, (arguments, result.output)
    assert invoke(database, "show", "NEXT-1").exit_code != 0


def test_alias_conflicts_and_future_numeric_ids(tmp_path):
    database = tmp_path / "sprints.sqlite"
    create_register(database)
    assert plan(database, 3).exit_code == 0
    assert plan(database, 12).exit_code == 0
    assert invoke(database, "alias", "3", "DBT-CM1").exit_code == 0
    for target, alias in [("12", "DBT-CM1"), ("3", "12"), ("3", "012")]:
        before = database.read_bytes()
        result = invoke(database, "alias", target, alias)
        assert result.exit_code != 0
        assert "conflicts" in result.output
        assert database.read_bytes() == before
    assert invoke(database, "alias", "3", "099").exit_code == 0
    before = database.read_bytes()
    result = plan(database, 99)
    assert result.exit_code != 0
    assert "conflicts" in result.output
    assert database.read_bytes() == before


@pytest.mark.parametrize("problem", ["unknown", "missing", "renamed"])
def test_alias_start_refuses_bad_cycle_before_board_write(
    tmp_path, sprint_board, monkeypatch, problem,
):
    database = tmp_path / "sprints.sqlite"
    create_register(database)
    assert plan(database, 3).exit_code == 0
    assert invoke(database, "alias", "3", "DBT-CM1").exit_code == 0
    if problem == "missing":
        monkeypatch.setattr(sprint_board, "sprint_cycles", lambda ids: {})
    elif problem == "renamed":
        monkeypatch.setattr(
            sprint_board, "cycle_sprint_id",
            lambda cycle: (4, SimpleNamespace(name="Sprint 4")),
        )
    before = database.read_bytes()
    result = invoke(
        database, "start", "UNKNOWN" if problem == "unknown" else "DBT-CM1",
        "--started", "2026-01-02T03:04:05+08:00",
    )
    assert result.exit_code != 0
    assert sprint_board.started == []
    assert database.read_bytes() == before


def test_alias_readback_failure_rolls_back(tmp_path):
    database = tmp_path / "sprints.sqlite"
    create_register(database)
    assert plan(database, 3).exit_code == 0
    with sprints.connect_database(database, writable=True) as connection:
        connection.execute(
            "CREATE TRIGGER corrupt_alias AFTER UPDATE OF alias ON sprints "
            "BEGIN UPDATE sprints SET alias = NULL "
            "WHERE sprint_id = NEW.sprint_id; END"
        )
        with pytest.raises(sprints.SprintError, match="readback failed"):
            sprints.set_alias(connection, 3, "DBT-CM1")
        assert sprints.fetch_sprint(connection, 3).alias is None


def test_v7_alias_migration_preserves_every_existing_table(tmp_path):
    database = tmp_path / "sprints.sqlite"
    create_register(database)
    assert plan(database, 3).exit_code == 0
    assert invoke(
        database, "start", "3", "--started", "2026-01-02T03:04:05+08:00",
    ).exit_code == 0
    assert invoke(database, "collect", "--sprint", "3").exit_code == 0
    with sqlite3.connect(database) as connection:
        connection.execute("DROP INDEX sprint_alias")
        connection.execute("ALTER TABLE sprints DROP COLUMN alias")
        connection.execute("PRAGMA user_version = 7")
        connection.execute(
            "INSERT INTO operation_journal VALUES "
            "('op', 'transition', 'DEMO-12', '{}', '[]', NULL, 'now', 'now')"
        )
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )]
        before = {
            table: connection.execute(f"SELECT * FROM {table}").fetchall()
            for table in tables
        }
    old_bytes = database.read_bytes()
    assert invoke(database, "show", "3").exit_code != 0
    assert database.read_bytes() == old_bytes
    result = invoke(database, "migrate")
    assert result.exit_code == 0, result.output
    assert "schema v7 → v9" in result.output
    with sprints.connect_database(database, writable=False) as connection:
        for table, expected in before.items():
            actual = [tuple(row) for row in connection.execute(
                f"SELECT * FROM {table}"
            )]
            if table == "sprints":
                assert actual == [(*row, None) for row in expected]
            else:
                assert actual == expected
    migrated = database.read_bytes()
    assert invoke(database, "migrate").exit_code == 0
    assert database.read_bytes() == migrated
    with sqlite3.connect(database) as connection:
        connection.execute(  # v9 accepts the kinds 0.8-0.19 registers use
            "INSERT INTO operation_journal VALUES ('start', 'sprint-start',"
            " 'Sprint 3', '{}', '[]', NULL, 'now', 'now')"
        )


def test_start_refuses_orphans_before_any_board_write(
    tmp_path: Path, sprint_board, monkeypatch,
) -> None:
    database = tmp_path / "SPRINTS.sqlite"
    create_register(database)
    plan(database, 3)

    def refuse(current, planned):
        raise OrphanedCard("DEMO-9 belongs to no planned or current sprint")

    monkeypatch.setattr(sprint_board, "require_no_orphans", refuse)
    started = invoke(database, "start", "3", "--started", "2026-01-02T03:04:05+08:00")

    assert isinstance(started.exception, OrphanedCard)
    assert sprint_board.started == []
    with sprints.connect_database(database, writable=False) as connection:
        assert sprints.fetch_sprint(connection, 3).status == sprints.STATUS_PLANNED


def test_default_database_is_under_plane(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    assert sprints.default_database_path() == tmp_path / "plane/SPRINTS.sqlite"


def test_full_lifecycle_marks_exactly_one_current_sprint(tmp_path: Path) -> None:
    database = tmp_path / "SPRINTS.sqlite"
    create_register(database)
    assert plan(database, 3).exit_code == 0
    assert plan(database, 4, 2).exit_code == 0

    started = invoke(database, "start", "3", "--started", "2026-01-02T03:04:05+08:00")
    assert started.exit_code == 0
    retry = invoke(database, "start", "3", "--started", "2026-01-02T03:04:05+08:00")
    assert retry.exit_code == 0
    with sprints.connect_database(database, writable=False) as connection:
        current = sprints.fetch_sprint(connection, 3)
        assert current.cycle_id == "3"
        # Opening totals exclude the cycle's cancelled member (1 card,
        # 2 points in the fake metrics).
        assert (current.cards_start, current.points_start) == (2, 6)
    with (
        sprints.connect_database(database, writable=True) as connection,
        pytest.raises(sprints.SprintError, match="cycle 3 is already in use"),
    ):
        sprints.validate_sprint_start(
            connection, 4, "2026-01-03T03:04:05+08:00", "3"
        )

    listing = CliRunner().invoke(
        cli, ["--json", "sprints", "--database", str(database), "list"]
    )
    assert listing.exit_code == 0
    assert '"status": "current"' in listing.output
    assert '"status": "planned"' in listing.output
    payload = json.loads(listing.output)
    assert payload["current"][0]["cards_start"] == 2
    assert payload["current"][0]["points_start"] == 6
    assert payload["current"][0]["cards_current"] == 3
    assert payload["current"][0]["points_current"] == 8
    assert payload["current"][0]["cards_done"] == 1
    assert payload["current"][0]["points_done"] == 3
    assert payload["current"][0]["cards_cancelled"] == 1
    assert payload["current"][0]["points_cancelled"] == 2
    assert isinstance(payload["current"][0]["velocity"], float)
    assert payload["planned"][0]["cards"] == 3
    assert payload["planned"][0]["points"] == 8
    current_json = json.loads(invoke(database, "list", "current", "--json").output)
    assert current_json["current"][0]["cards_done"] == 1
    planned_json = json.loads(invoke(database, "list", "planned", "--json").output)
    assert planned_json["planned"][0]["points"] == 8

    missing_telemetry = invoke(
        database, "close", "3", "--ended", "2026-01-02T04:04:05+08:00",
        "--delivered", "The outcome",
    )
    assert missing_telemetry.exit_code == 1
    assert "final execution snapshot missing" in missing_telemetry.output
    assert invoke(database, "collect").exit_code == 0

    preflight = invoke(database, "preflight", "3")
    assert preflight.exit_code == 0
    assert "READY" in preflight.output

    closed = invoke(
        database, "close", "3", "--ended", "2026-01-02T04:04:05+08:00",
        "--delivered", "The outcome", "--retrospective", "Plan less",
    )
    assert closed.exit_code == 0
    with sprints.connect_database(database, writable=False) as connection:
        completed = sprints.fetch_sprint(connection, 3)
    # Derived, never hand-typed: opening totals stay as recorded at
    # start; hours from start/end; velocity is Done points per hour.
    assert (completed.cards_start, completed.points_start) == (2, 6)
    assert (completed.cards_end, completed.points_end) == (2, 6)
    assert completed.hours == pytest.approx(1.0)
    assert completed.velocity == pytest.approx(3.0)
    assert invoke(database, "start", "4", "--started", "2026-01-03T03:04:05+08:00").exit_code == 0


def test_list_subcommands_filter_current_planned_and_past(tmp_path: Path) -> None:
    database = tmp_path / "SPRINTS.sqlite"
    create_register(database)
    plan(database, 1)
    plan(database, 2, 2)
    invoke(database, "start", "1", "--started", "2026-01-02T03:04:05+08:00")
    invoke(
        database, "add", "--id", "3", "--title", "Past",
        "--started", "2026-01-01T03:04:05+08:00",
        "--ended", "2026-01-01T04:04:05+08:00", "--hours", "1",
        "--cards-start", "1", "--cards-end", "1", "--points-start", "1",
        "--points-end", "1", "--velocity", "1", "--delivered", "History",
    )

    all_sprints = invoke(database, "list")
    assert all(name in all_sprints.output for name in ("Sprint 1", "Sprint 2", "Past"))
    assert all(
        heading in all_sprints.output
        for heading in ("Current sprint", "Planned sprints", "Past sprints")
    )
    assert "╭" in all_sprints.output
    current = invoke(database, "list", "current")
    assert "Sprint 1" in current.output and "Sprint 2" not in current.output
    planned = invoke(database, "list", "planned")
    assert "Sprint 2" in planned.output and "Sprint 1" not in planned.output
    assert "Cards" in planned.output and "3" in planned.output
    assert "Points" in planned.output and "8" in planned.output
    past = invoke(database, "list", "past")
    assert "Past" in past.output and "Sprint 1" not in past.output


def test_sprint_reports_omit_estimate_metrics_when_disabled(
    tmp_path: Path, sprint_board,
) -> None:
    database = tmp_path / "SPRINTS.sqlite"
    create_register(database)
    plan(database, 1)
    invoke(database, "start", "1", "--started", "2026-01-02T03:04:05+08:00")
    sprint_board.project.estimates_enabled = False

    result = invoke(database, "list", "--json")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    current = payload["current"][0]
    assert current["cards_current"] == 3
    assert not (
        {"points_start", "points_current", "points_done", "velocity"}
        & current.keys()
    )
    human = invoke(database, "list")
    assert "points" not in human.output.casefold()
    assert "velocity" not in human.output.casefold()


def test_list_all_labels_an_empty_current_section(tmp_path: Path) -> None:
    database = tmp_path / "SPRINTS.sqlite"
    create_register(database)
    plan(database, 2)
    result = invoke(database, "list")
    assert "Current sprint\nNo current sprint" in result.output
    assert "Planned sprints" in result.output


def test_stats_use_upper_median_sd_and_readable_duration() -> None:
    summary = sprints.metric_summary([1, 2, 3, 4])
    assert summary["average"] == 2.5
    assert summary["median"] == 3
    assert summary["sd"] == pytest.approx(1.11803398875)
    assert sprints.format_duration(1.0013888888888889) == "01h 00m 05s"
    assert sprints.current_velocity(
        "2026-01-02T03:04:05+08:00",
        3,
        now=datetime.fromisoformat("2026-01-02T04:34:05+08:00"),
    ) == 2.0


def test_stats_and_past_list_have_tables_and_local_json(tmp_path: Path) -> None:
    database = tmp_path / "SPRINTS.sqlite"
    create_register(database)
    for sprint_id, hours, points in ((1, "1.0002777777777778", "1"), (2, "2", "3")):
        result = invoke(
            database, "add", "--id", str(sprint_id), "--title", f"Past {sprint_id}",
            "--started", f"2026-01-0{sprint_id}T03:04:05+08:00",
            "--ended", f"2026-01-0{sprint_id}T05:04:05+08:00", "--hours", hours,
            "--cards-start", "1", "--cards-end", "2", "--points-start", points,
            "--points-end", points, "--velocity", points, "--delivered", "Done",
        )
        assert result.exit_code == 0

    human = invoke(database, "stats")
    assert human.exit_code == 0
    assert "{" not in human.output
    assert all(heading in human.output for heading in ("Metric", "Avg", "Med", "Min", "Max", "SD"))
    assert "01h 30m 01s" in human.output
    assert "2.00" in human.output
    assert "2/h" in human.output
    assert "…" not in human.output

    past = invoke(database, "list", "past")
    assert "Statistics" in past.output
    assert "Cards start" in past.output
    assert (past.output.index("Time start") < past.output.index("Time end")
            < past.output.index("Elapsed"))

    stats_json = invoke(database, "stats", "--json")
    assert json.loads(stats_json.output)["points_start"]["median"] == 3
    list_json = invoke(database, "list", "--json")
    parsed = json.loads(list_json.output)
    assert set(parsed) == {"current", "planned", "past", "stats"}
    assert parsed["stats"]["points_start"]["median"] == 3
    past_json = invoke(database, "list", "--json", "past")
    assert set(json.loads(past_json.output)) == {"past", "stats"}


def test_plan_updates_future_sprint_but_not_current(tmp_path: Path) -> None:
    database = tmp_path / "SPRINTS.sqlite"
    create_register(database)
    assert plan(database, 7).exit_code == 0
    updated = invoke(
        database, "plan", "--id", "7", "--title", "Changed", "--position", "2",
        "--goal", "Changed goal", "--acceptance", "Changed criterion",
    )
    assert updated.exit_code == 0
    assert "Changed goal" in invoke(database, "show", "7").output
    invoke(database, "start", "7", "--started", "2026-01-02T03:04:05+08:00")
    refused = plan(database, 7)
    assert refused.exit_code == 1
    assert "cannot be replanned" in refused.output


def test_reorder_assigns_complete_planned_order_without_replacing_content(
    tmp_path: Path,
) -> None:
    database = tmp_path / "SPRINTS.sqlite"
    create_register(database)
    assert plan(database, 7, 2).exit_code == 0
    assert plan(database, 8, 1).exit_code == 0
    assert plan(database, 9, 2).exit_code == 0

    reordered = invoke(database, "reorder", "9", "7", "8")

    assert reordered.exit_code == 0
    assert reordered.output == "Reordered 3 planned sprints.\n"
    with sprints.connect_database(database, writable=False) as connection:
        planned = [
            item for item in sprints.fetch_sprints(connection)
            if item.status == sprints.STATUS_PLANNED
        ]
    assert [(item.sprint_id, item.position) for item in planned] == [
        (9, 1), (7, 2), (8, 3),
    ]
    assert planned[1].goal == "Ship the outcome"
    assert planned[1].execution == "Build before integration"
    assert planned[1].acceptance == "The gate passes"


@pytest.mark.parametrize(
    ("ordered_ids", "message"),
    [
        ((7, 7), "duplicate: 7"),
        ((7,), "missing: 8"),
        ((7, 99), "not planned: 99"),
    ],
)
def test_reorder_refuses_invalid_complete_order_without_writing(
    tmp_path: Path,
    ordered_ids: tuple[int, ...],
    message: str,
) -> None:
    database = tmp_path / "SPRINTS.sqlite"
    create_register(database)
    assert plan(database, 7, 1).exit_code == 0
    assert plan(database, 8, 2).exit_code == 0

    refused = invoke(database, "reorder", *(str(item) for item in ordered_ids))

    assert refused.exit_code == 1
    assert message in refused.output
    with sprints.connect_database(database, writable=False) as connection:
        planned = [
            item for item in sprints.fetch_sprints(connection)
            if item.status == sprints.STATUS_PLANNED
        ]
    assert [(item.sprint_id, item.position) for item in planned] == [(7, 1), (8, 2)]


def test_write_command_migrates_v1_without_losing_history(tmp_path: Path) -> None:
    database = tmp_path / "SPRINTS.sqlite"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """CREATE TABLE sprints (
                sprint_id INTEGER PRIMARY KEY, title TEXT NOT NULL, started TEXT NOT NULL,
                ended TEXT NOT NULL, hours REAL NOT NULL, cards_start INTEGER NOT NULL,
                cards_end INTEGER NOT NULL, points_start INTEGER NOT NULL,
                points_end INTEGER NOT NULL, velocity REAL NOT NULL,
                delivered TEXT NOT NULL, retrospective TEXT NOT NULL) STRICT;
            INSERT INTO sprints VALUES (1, 'Old', '2026-01-01T00:00:00+08:00',
                '2026-01-01T01:00:00+08:00', 1, 1, 2, 3, 5, 5, 'Done', 'Learned');
            PRAGMA user_version = 1;"""
        )
    read_before_upgrade = invoke(database, "list")
    assert read_before_upgrade.exit_code == 1
    assert "bind" in read_before_upgrade.output
    assert invoke(database, "bind").exit_code == 0
    assert plan(database, 2).exit_code == 0
    with sprints.connect_database(database, writable=False) as connection:
        migrated = sprints.fetch_sprint(connection, 1)
        assert migrated is not None
        assert migrated.status == "completed"
        assert migrated.delivered == "Done"
        assert (
            connection.execute("PRAGMA user_version").fetchone()[0]
            == sprints.SCHEMA_VERSION
        )


def test_write_command_migrates_v2_to_persist_plane_cycle_ids(tmp_path: Path) -> None:
    database = tmp_path / "SPRINTS.sqlite"
    create_register(database)
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE sprints DROP COLUMN cycle_id")
        connection.execute("PRAGMA user_version = 2")
    assert plan(database, 2).exit_code == 0
    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(sprints)")}
        assert "cycle_id" in columns
        assert (
            connection.execute("PRAGMA user_version").fetchone()[0]
            == sprints.SCHEMA_VERSION
        )


def test_execution_snapshots_preserve_early_and_final_card_stats(tmp_path: Path) -> None:
    database = tmp_path / "SPRINTS.sqlite"
    create_register(database)
    assert plan(database, 7).exit_code == 0
    with sprints.connect_database(database, writable=True) as connection:
        sprints.start_sprint(connection, 7, "2026-01-02T03:04:05+08:00", "cycle-7", 1, 3)
        sprints.record_execution_snapshot(
            connection,
            sprint_id=7,
            work_item_id="card-uuid",
            card_reference="DEMO-12",
            captured_at="2026-01-02T04:00:00+08:00",
            is_final=False,
            stats={"state_minutes": {"In Progress": 45},
                   "execution_minutes": {"coding": 20}, "open_timer": None,
                   "current_state": {"name": "In Progress"}},
        )
        sprints.record_execution_snapshot(
            connection,
            sprint_id=7,
            work_item_id="card-uuid",
            card_reference="DEMO-12",
            captured_at="2026-01-02T05:00:00+08:00",
            is_final=True,
            stats={"state_minutes": {"In Progress": 70, "Verifying": 15},
                   "execution_minutes": {"coding": 40, "manual-qa": 10},
                   "open_timer": None, "current_state": {"name": "Done"}},
        )

    with sprints.connect_database(database, writable=False) as connection:
        snapshots = sprints.fetch_execution_snapshots(connection, 7)

    assert [snapshot["is_final"] for snapshot in snapshots] == [False, True]
    assert snapshots[-1]["stats"]["execution_minutes"] == {"coding": 40, "manual-qa": 10}


def test_collect_persists_current_sprint_card_stats(tmp_path: Path) -> None:
    database = tmp_path / "SPRINTS.sqlite"
    create_register(database)
    assert plan(database, 7).exit_code == 0
    assert invoke(
        database, "start", "7", "--started", "2026-01-02T03:04:05+08:00"
    ).exit_code == 0

    result = invoke(database, "collect", "DEMO-12")

    assert result.exit_code == 0, result.output
    with sprints.connect_database(database, writable=False) as connection:
        snapshots = sprints.fetch_execution_snapshots(connection, 7)
    assert len(snapshots) == 1
    assert snapshots[0]["card_reference"] == "DEMO-12"
    assert snapshots[0]["is_final"] is True
    assert snapshots[0]["stats"]["state_minutes"] == {"In Progress": 60.0}


def test_write_command_migrates_v3_to_execution_snapshots(tmp_path: Path) -> None:
    database = tmp_path / "SPRINTS.sqlite"
    create_register(database)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE card_execution_snapshots")
        connection.execute("PRAGMA user_version = 3")
    assert plan(database, 2).exit_code == 0
    with sqlite3.connect(database) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(card_execution_snapshots)")
        }
        assert {"sprint_id", "work_item_id", "stats_json"} <= columns
        assert (
            connection.execute("PRAGMA user_version").fetchone()[0]
            == sprints.SCHEMA_VERSION
        )


def test_write_command_migrates_v5_to_v6_and_drops_unique_index(
    tmp_path: Path,
) -> None:
    database = tmp_path / "SPRINTS.sqlite"
    create_register(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE UNIQUE INDEX one_current_sprint "
            "ON sprints(status) WHERE status = 'current'"
        )
        connection.execute("PRAGMA user_version = 5")
    assert plan(database, 2).exit_code == 0
    with sqlite3.connect(database) as connection:
        indices = {
            row[1] for row in connection.execute("PRAGMA index_list(sprints)")
        }
        assert "one_current_sprint" not in indices
        assert (
            connection.execute("PRAGMA user_version").fetchone()[0]
            == sprints.SCHEMA_VERSION
        )



def test_add_preserves_the_history_import_command(tmp_path: Path) -> None:
    database = tmp_path / "SPRINTS.sqlite"
    create_register(database)
    result = invoke(
        database, "add", "--id", "9", "--alias", "OLD-9",
        "--title", "Imported",
        "--started", "2026-01-02T03:04:05+08:00",
        "--ended", "2026-01-02T04:04:05+08:00", "--hours", "1",
        "--cards-start", "1", "--cards-end", "2", "--points-start", "3",
        "--points-end", "5", "--velocity", "5", "--delivered", "Imported facts",
    )
    assert result.exit_code == 0
    listing = invoke(database, "list")
    assert "Past sprints" in listing.output
    assert "Imported" in listing.output
    assert "OLD-9" in listing.output
    assert invoke(database, "show", "OLD-9").exit_code == 0


def test_invalid_timestamp_and_empty_plan_are_clean_cli_errors(tmp_path: Path) -> None:
    database = tmp_path / "SPRINTS.sqlite"
    create_register(database)
    invalid = invoke(
        database, "add", "--id", "1", "--title", "Bad", "--started", "yesterday",
        "--ended", "tomorrow", "--hours", "1", "--cards-start", "0",
        "--cards-end", "0", "--points-start", "0", "--points-end", "0",
        "--velocity", "0", "--delivered", "Nothing",
    )
    assert invalid.exit_code == 1
    assert "invalid timestamp" in invalid.output


def test_start_retry_accepts_equivalent_utc_timestamp(tmp_path):
    database = tmp_path / "sprints.sqlite"
    create_register(database)
    assert plan(database, 7).exit_code == 0
    assert invoke(database, "start", "7", "--started", "2026-09-16T17:46:59+08:00").exit_code == 0
    retry = invoke(database, "start", "7", "--started", "2026-09-16T09:46:59Z")
    assert retry.exit_code == 0, retry.output
    mismatch = invoke(database, "start", "7", "--started", "2026-09-16T09:47:00Z")
    assert mismatch.exit_code != 0


@pytest.mark.parametrize("command", ["sprint", "sprints"])
def test_show_defaults_to_current_and_renders_field_table(tmp_path, command):
    database = tmp_path / "sprints.sqlite"
    create_register(database)
    assert plan(database, 7).exit_code == 0
    args = [command, "--database", str(database), "show"]
    empty = CliRunner().invoke(cli, args)
    assert empty.exit_code == 0, empty.output
    assert "No current sprint" in empty.output
    assert invoke(database, "start", "7", "--started", "2026-09-16T17:46:59+08:00").exit_code == 0
    shown = CliRunner().invoke(cli, args)
    assert shown.exit_code == 0, shown.output
    assert "│ Title" in shown.output
    assert "│ Sprint 7" in shown.output
    explicit = CliRunner().invoke(cli, [*args, "7"])
    assert "│ Sprint 7" in explicit.output
    payload = CliRunner().invoke(cli, ["--json", *args])
    assert json.loads(payload.output)["sprint_id"] == 7
    missing = CliRunner().invoke(cli, [*args, "99"])
    assert missing.exit_code != 0
    assert "not found" in missing.output


@pytest.mark.parametrize("elapsed,expected_time,expected_velocity", [
    (5400, "01:30:00", 2.0),
    (90061, "25:01:01", 3 / (90061 / 3600)),
    (0, "00:00:00", 0.0),
])
def test_current_show_live_progress(
    tmp_path, monkeypatch, elapsed, expected_time, expected_velocity
):
    started = datetime.fromisoformat("2026-09-16T17:46:59+08:00")

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return (started + timedelta(seconds=elapsed)).astimezone(tz)

    monkeypatch.setattr(sprints, "datetime", Clock)
    database = tmp_path / "SPRINTS.sqlite"
    create_register(database)
    plan(database, 7)
    with sprints.connect_database(database, writable=True) as connection:
        sprints.start_sprint(connection, 7, started.isoformat(), "7", 2, 5)
    args = ["sprint", "--database", str(database), "show"]
    result = CliRunner().invoke(cli, ["--json", *args])
    assert result.exit_code == 0, result.output
    record = json.loads(result.output)
    assert record["time_elapsed"] == expected_time
    assert record["velocity"] == pytest.approx(expected_velocity)
    assert [record[key] for key in ("cards_start", "cards_done", "cards_end")] == [2, 1, 2]
    assert [record[key] for key in ("points_start", "points_done", "points_end")] == [5, 3, 6]
    shown = CliRunner().invoke(cli, args)
    assert shown.exit_code == 0, shown.output
    labels = [line.split("│")[1].strip() for line in shown.output.splitlines() if "│" in line]
    assert labels[-11:] == [
        "Time elapsed",
        "Velocity",
        "Timing",
        "Started",
        "Cards start",
        "Cards done",
        "Cards end",
        "Points start",
        "Points done",
        "Points end",
        "Ended",
    ]
    assert "Hours" not in labels
    assert "Time elapsed" in labels
    assert expected_time in shown.output
    from plane_proj.sprints import format_velocity
    assert f"{format_velocity(expected_velocity)}/h" in shown.output
    with sprints.connect_database(database, writable=False) as connection:
        stored = sprints.fetch_sprint(connection, 7)
    assert stored.cards_end is None
    assert stored.points_end is None


def test_timing_reports_latest_card_snapshots_and_excludes_legacy_and_partial(
    tmp_path, monkeypatch
):
    database = tmp_path / "SPRINTS.sqlite"
    create_register(database)
    with sprints.connect_database(database, writable=True) as connection:
        for sprint_id, cards in ((1, 2), (2, 1), (3, 10), (4, 2)):
            sprints.add_completed_sprint(connection, sprints.Sprint(
                sprint_id=sprint_id, title=f"Sprint {sprint_id}", status="completed",
                started="2026-01-01T00:00:00Z", ended="2026-01-03T00:00:00Z",
                hours=48, cards_start=cards, cards_end=cards,
                points_start=3, points_end=3, velocity=1, delivered="Outcome",
            ))
        for sprint_id, card, captured, state_minutes, active in (
            (1, "a", "2026-01-02T18:00:00+08:00", 999, 999),
            (1, "a", "2026-01-02T11:00:00Z", 30, 15),
            (1, "b", "2026-01-02T11:00:00Z", 10, 5),
            (2, "c", "2026-01-02T11:00:00Z", 90, 40),
            (4, "d", "2026-01-02T11:00:00Z", 9999, 9999),
        ):
            sprints.record_execution_snapshot(
                connection, sprint_id=sprint_id, work_item_id=card,
                card_reference=f"DEMO-{card}", captured_at=captured, is_final=True,
                stats={"state_minutes": {"In Progress": state_minutes},
                       "execution_minutes": {
                           "coding": active, "verification": 900,
                           "review": 800,
                       },
                       "current_state": None,
                       "open_timer": {
                           "category": "verification",
                           "started": "2026-01-01T00:00:00Z",
                       },
                       **({"rework_count": 2 if card == "a" else 1} if sprint_id == 1 else {})},
            )

    def no_board(self):
        pytest.fail("Timing reports for past sprints must not call Plane")

    monkeypatch.setattr(Context, "board", property(no_board))
    args = ["--json", "sprint", "--database", str(database)]
    shown = CliRunner().invoke(cli, [*args, "show", "1"])
    assert shown.exit_code == 0, shown.output
    timing = json.loads(shown.output)["timing"]
    assert timing["observed_cards"] == 2
    assert timing["state_minutes"] == {"In Progress": 40}
    assert timing["execution_minutes"] == {"coding": 20}
    assert timing["open_timer_minutes"] == {}
    assert timing["captured_through"] == "2026-01-02T11:00:00Z"
    assert timing["rework_count"] == 3
    assert timing["reworked_cards"] == 2
    assert timing["rework_observed_cards"] == 2
    listing = CliRunner().invoke(cli, [*args, "list", "past"])
    assert listing.exit_code == 0, listing.output
    records = json.loads(listing.output)
    assert records["past"][0]["timing"] == timing
    assert records["past"][2]["timing"] is None
    aggregate = CliRunner().invoke(cli, [*args, "stats"])
    assert aggregate.exit_code == 0, aggregate.output
    stats = json.loads(aggregate.output)["timing"]
    assert stats == records["stats"]["timing"]
    assert stats["included_sprints"] == 2
    assert stats["excluded_without_timing"] == 1
    assert stats["excluded_incomplete"] == 1
    assert stats["state_minutes"]["In Progress"]["average"] == 65
    assert stats["execution_minutes"]["coding"]["average"] == 30
    assert stats["execution_minutes_total"]["min"] == 20
    assert stats["active_minutes_total"]["average"] == 65
    assert stats["rework"]["included_sprints"] == 1
    assert stats["rework"]["excluded_sprints"] == 3
    assert stats["rework"]["count"]["average"] == 3
    for columns in (80, 160):
        human = CliRunner().invoke(
            cli, ["sprint", "--database", str(database), "list", "past"],
            env={"COLUMNS": str(columns)},
        )
        assert human.exit_code == 0, human.output
        assert "Sprint 1 timing" not in human.output
        sprint_table = next(
            block for block in human.output.split("╭")
            if "Sprint 1" in block and "Time spent: coding" in block
        )
        assert "00:20:00" in sprint_table.split("╰")[0]
        assert "Time spent: coding" in human.output
        assert "Time spent: verification" not in human.output
        assert "Time spent: review" not in human.output
        assert "00:20:00" in human.output
        assert "No timing data" not in human.output
        assert "Statistics coverage" in human.output
    detail = invoke(database, "show", "1")
    assert detail.exit_code == 0, detail.output
    assert "Time spent: coding" in detail.output
    assert "Observed cards" in detail.output
    assert "Reworked cards" in detail.output
    assert "Rework coverage" not in detail.output
    assert "00:20:00" in detail.output


def test_current_timing_keeps_open_work_at_capture_and_excludes_it_from_history(tmp_path):
    database = tmp_path / "SPRINTS.sqlite"
    create_register(database)
    plan(database, 7)
    with sprints.connect_database(database, writable=True) as connection:
        sprints.start_sprint(connection, 7, "2026-01-02T00:00:00Z", "7", 3, 8)
        sprints.record_execution_snapshot(
            connection, sprint_id=7, work_item_id="one", card_reference="DEMO-1",
            captured_at="2026-01-02T01:00:00Z", is_final=False,
            stats={"state_minutes": {"Todo": 10}, "execution_minutes": {"coding": 5},
                   "current_state": {"name": "In Progress", "elapsed_minutes": 30},
                   "open_timer": {"category": "coding", "started": "2026-01-02T00:45:00Z"}},
        )
    args = ["--json", "sprint", "--database", str(database)]
    shown = CliRunner().invoke(cli, [*args, "show"])
    assert shown.exit_code == 0, shown.output
    timing = json.loads(shown.output)["timing"]
    assert timing["open_timer_minutes"] == {"coding": 15}
    assert timing["execution_minutes"] == {"coding": 5}
    assert timing["current_state_minutes"] == {"In Progress": 30}
    assert timing["active_minutes"] == {"In Progress": 30}
    listing = CliRunner().invoke(cli, [*args, "list", "current"])
    assert listing.exit_code == 0, listing.output
    assert json.loads(listing.output)["current"][0]["timing"] == timing
    stats = CliRunner().invoke(cli, [*args, "stats"])
    assert stats.exit_code == 0, stats.output
    assert json.loads(stats.output)["timing"]["included_sprints"] == 0
    human = invoke(database, "show")
    assert "Open timer: coding" in human.output
    assert "Current state:" not in human.output
    assert "State total" not in human.output
    assert "00:15:00" in human.output
    listing = invoke(database, "list", "current")
    assert listing.exit_code == 0, listing.output
    sprint_table = listing.output.split("╰")[0]
    assert "Sprint 7" in sprint_table
    assert "Open timer: coding" in sprint_table


def test_active_residence_includes_open_work_but_never_done(tmp_path):
    from plane_proj import sprint_timing

    database = tmp_path / "SPRINTS.sqlite"
    create_register(database)
    plan(database, 7)
    with sprints.connect_database(database, writable=True) as connection:
        sprints.start_sprint(
            connection, 7, "2026-01-02T00:00:00Z", "7", 2, 5
        )
        for card, current in (("a", "Verifying"), ("b", "Done")):
            sprints.record_execution_snapshot(
                connection, sprint_id=7, work_item_id=card,
                card_reference=f"DEMO-{card}",
                captured_at="2026-01-02T01:00:00Z", is_final=False,
                stats={
                    "state_minutes": {
                        "Todo": 100, "In Progress": 20,
                        "Verifying": 10, "Done": 999,
                    },
                    "current_state": {
                        "name": current, "elapsed_minutes": 30,
                    },
                    "execution_minutes": {"coding": 5},
                },
            )
        timing = sprint_timing.summaries(
            connection, [sprints.fetch_sprint(connection, 7)]
        )[7]
    assert timing["active_minutes"] == {
        "In Progress": 40, "Verifying": 50,
    }
    assert timing["residence_minutes"]["Todo"] == 200
    assert "Done" not in timing["residence_minutes"]
    fields = dict(sprint_timing.fields(timing))
    assert fields["Active total"] == "01:30:00"
    assert fields["Active: Verifying"] == "00:50:00"
    assert fields["Time spent total"] == "00:10:00"
    for category in (
        "dependency-wait", "service-wait", "blocking-run", "manual-qa",
        "user-ask",
    ):
        assert fields[f"Time spent: {category}"] == "00:00:00"
    assert "Time spent: waiting" not in fields
    labels = list(fields)
    index = labels.index("Time spent total")
    assert labels[index - 2:index] == [
        "Earliest card timestamp", "Latest card timestamp",
    ]
    assert "State: Done" not in fields
    assert "Current state: Verifying" not in fields


def test_rework_zero_is_measured_and_old_snapshot_is_unknown(tmp_path):
    database = tmp_path / "SPRINTS.sqlite"
    create_register(database)
    with sprints.connect_database(database, writable=True) as connection:
        for sprint_id in (1, 2):
            sprints.add_completed_sprint(connection, sprints.Sprint(
                sprint_id=sprint_id, title=f"Sprint {sprint_id}", status="completed",
                started="2026-01-01T00:00:00Z", ended="2026-01-02T00:00:00Z",
                hours=24, cards_start=1, cards_end=1, points_start=1, points_end=1,
                velocity=1, delivered="Outcome",
            ))
            sprints.record_execution_snapshot(
                connection, sprint_id=sprint_id, work_item_id=str(sprint_id),
                card_reference=f"DEMO-{sprint_id}", captured_at="2026-01-02T00:00:00Z",
                is_final=True, stats={"state_minutes": {"In Progress": 10},
                                      "execution_minutes": {}, "current_state": None,
                                      "open_timer": None,
                                      **({"rework_count": 0} if sprint_id == 1 else {})},
            )
    result = invoke(database, "stats", "--json")
    assert result.exit_code == 0, result.output
    rework = json.loads(result.output)["timing"]["rework"]
    assert rework["included_sprints"] == 1
    assert rework["excluded_sprints"] == 1
    assert rework["count"]["average"] == 0
    shown = invoke(database, "show", "2")
    assert shown.exit_code == 0, shown.output
    line = next(line for line in shown.output.splitlines() if "Rework count" in line)
    assert "—" in line


def test_collect_persists_rework_for_sprint_reporting(tmp_path, sprint_board, monkeypatch):
    database = tmp_path / "SPRINTS.sqlite"
    create_register(database)
    plan(database, 7)
    invoke(database, "start", "7", "--started", "2026-01-02T00:00:00Z")
    events = [
        SimpleNamespace(id="sent-back", field="state", old_value="Verifying",
                        new_value="In Progress", created_at="2026-01-02T01:00:00Z"),
        SimpleNamespace(id="accepted", field="state", old_value="Verifying",
                        new_value="Done", created_at="2026-01-02T02:00:00Z"),
    ]
    monkeypatch.setattr(sprint_board, "activities", lambda card: events)
    collected = invoke(database, "collect")
    assert collected.exit_code == 0, collected.output
    shown = CliRunner().invoke(cli, [
        "--json", "sprint", "--database", str(database), "show",
    ])
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.output)["timing"]["rework_count"] == 1


def test_collect_follows_telemetry_recorded_in_the_same_second(
    tmp_path, sprint_board, monkeypatch,
):
    database = tmp_path / "SPRINTS.sqlite"
    create_register(database)
    plan(database, 7)
    invoke(database, "start", "7", "--started", "2026-01-02T00:00:00Z")

    def landed_now(card):
        return [SimpleNamespace(
            id="moved", field="state", old_value="Todo",
            new_value="In Progress",
            created_at=datetime.now(UTC).isoformat(),
        )]

    monkeypatch.setattr(sprint_board, "activities", landed_now)
    collected = invoke(database, "collect")
    assert collected.exit_code == 0, collected.output


def test_collect_marks_cancelled_card_final_and_keeps_timer(
    tmp_path, sprint_board, monkeypatch,
):
    database = tmp_path / "SPRINTS.sqlite"
    create_register(database)
    plan(database, 7)
    invoke(database, "start", "7", "--started", "2026-01-02T00:00:00Z")
    sprint_board.telemetry_cards[0].state = "state-cancelled"
    sprint_board.project.states["Cancelled"] = "state-cancelled"
    events = [
        SimpleNamespace(
            id="started",
            field="state",
            old_value="Todo",
            new_value="In Progress",
            created_at="2026-01-02T01:00:00Z",
        ),
        SimpleNamespace(
            id="cancelled",
            field="state",
            old_value="In Progress",
            new_value="Cancelled",
            created_at="2026-01-02T02:00:00Z",
        ),
    ]
    comments = [
        SimpleNamespace(
            id="timer-start",
            created_at="2026-01-02T01:10:00Z",
            comment_html=(
                f"<p>{execution.EVENT_PREFIX}"
                '{"action":"start","category":"coding"}</p>'
            ),
            actor="worker",
        ),
        SimpleNamespace(
            id="timer-stop",
            created_at="2026-01-02T01:40:00Z",
            comment_html=(
                f"<p>{execution.EVENT_PREFIX}"
                '{"action":"stop","category":"coding"}</p>'
            ),
            actor="worker",
        ),
    ]
    monkeypatch.setattr(sprint_board, "activities", lambda card: events)
    monkeypatch.setattr(sprint_board, "comments", lambda card: comments)

    collected = invoke(database, "collect")

    assert collected.exit_code == 0, collected.output
    result = CliRunner().invoke(cli, [
        "--json", "sprints", "--database", str(database),
        "telemetry", "7",
    ])
    assert result.exit_code == 0, result.output
    telemetry = json.loads(result.output)
    assert telemetry[0]["is_final"] is True
    assert telemetry[0]["stats"]["execution_minutes"] == {"coding": 30}
    assert telemetry[0]["stats"]["current_state"]["name"] == "Cancelled"


def test_list_omits_timing_for_planned_and_current_without_observations(tmp_path):
    database = tmp_path / "SPRINTS.sqlite"
    create_register(database)
    plan(database, 7)
    plan(database, 8, 2)
    invoke(database, "start", "7", "--started", "2026-01-02T00:00:00Z")
    result = invoke(database, "list")
    assert result.exit_code == 0, result.output
    assert "Sprint 7" in result.output
    assert "Sprint 8" in result.output
    sprint_sections = result.output.split("Statistics coverage")[0]
    assert "Timing" not in sprint_sections
    assert "timing" not in sprint_sections
    assert "No timing data" not in result.output
    with sprints.connect_database(database, writable=True) as connection:
        sprints.record_execution_snapshot(
            connection, sprint_id=7, work_item_id="empty", card_reference="DEMO-1",
            captured_at="2026-01-02T01:00:00Z", is_final=False,
            stats={"state_minutes": {}, "execution_minutes": {},
                   "current_state": None, "open_timer": None, "rework_count": 0},
        )
    basic = invoke(database, "list", "current")
    assert basic.exit_code == 0, basic.output
    assert "Timed cards" not in basic.output
    assert "Active total" not in basic.output


def test_empty_timing_statistics_render_coverage_table(tmp_path):
    database = tmp_path / "SPRINTS.sqlite"
    create_register(database)
    with sprints.connect_database(database, writable=True) as connection:
        sprints.add_completed_sprint(connection, sprints.Sprint(
            sprint_id=1, title="Legacy", status="completed",
            started="2026-01-01T00:00:00Z", ended="2026-01-02T00:00:00Z",
            hours=24, cards_start=1, cards_end=1, points_start=1, points_end=1,
            velocity=1, delivered="Outcome",
        ))
    for command in (("stats",), ("list", "past")):
        result = invoke(database, *command)
        assert result.exit_code == 0, result.output
        assert "Timing statistics:" not in result.output
        assert "Rework statistics:" not in result.output
        coverage = result.output.split("Statistics coverage")[1]
        assert "Included" in coverage
        assert "Excluded" in coverage
        assert "│ Timing" in coverage
        assert "│ Rework" in coverage
        assert "1 without timing" in coverage


def test_parallel_sprints_lifecycle_and_disambiguation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "SPRINTS.sqlite"
    create_register(database)
    assert plan(database, 3).exit_code == 0
    assert plan(database, 4, 2).exit_code == 0

    assert invoke(
        database, "start", "3", "--started", "2026-01-02T03:04:05+08:00"
    ).exit_code == 0
    assert invoke(
        database, "start", "4", "--started", "2026-01-02T03:05:00+08:00"
    ).exit_code == 0

    listing = invoke(database, "list")
    assert listing.exit_code == 0
    assert "Current sprints" in listing.output
    assert "Sprint 3" in listing.output
    assert "Sprint 4" in listing.output

    all_show = invoke(database, "show")
    assert all_show.exit_code == 0
    assert "Sprint 3" in all_show.output
    assert "Sprint 4" in all_show.output

    json_res = CliRunner().invoke(
        cli, ["--json", "sprints", "--database", str(database), "show"]
    )
    assert json_res.exit_code == 0
    json_show = json.loads(json_res.output)
    assert isinstance(json_show, list)
    assert len(json_show) == 2
    assert {s["sprint_id"] for s in json_show} == {3, 4}

    show_3 = invoke(database, "show", "3")
    assert show_3.exit_code == 0
    assert "Sprint 3" in show_3.output
    assert "Sprint 4" not in show_3.output

    show_4 = invoke(database, "show", "4")
    assert show_4.exit_code == 0
    assert "Sprint 4" in show_4.output
    assert "Sprint 3" not in show_4.output

    ambiguous_telemetry = invoke(database, "telemetry")
    assert ambiguous_telemetry.exit_code == 1
    assert "Multiple sprints are current (3, 4); specify SPRINT_ID" in (
        ambiguous_telemetry.output
    )

    telemetry_3 = invoke(database, "telemetry", "3")
    assert telemetry_3.exit_code == 0


def test_parallel_sprints_collect_with_card_routing_and_filter(
    tmp_path: Path, sprint_board
) -> None:
    database = tmp_path / "SPRINTS.sqlite"
    create_register(database)
    assert plan(database, 3).exit_code == 0
    assert plan(database, 4, 2).exit_code == 0
    assert invoke(
        database, "start", "3", "--started", "2026-01-02T03:04:05+08:00"
    ).exit_code == 0
    assert invoke(
        database, "start", "4", "--started", "2026-01-02T03:05:00+08:00"
    ).exit_code == 0

    cards_by_cycle = {
        "3": [
            Card(id="uuid-3", sequence_id=12, state="state-done"),
            Card(id="uuid-3b", sequence_id=14, state="state-done"),
        ],
        "4": [Card(id="uuid-4", sequence_id=13, state="state-done")],
    }
    sprint_board.cycle_cards = lambda cycle_id: cards_by_cycle.get(
        cycle_id, []
    )

    ambiguous = invoke(database, "collect")
    assert ambiguous.exit_code == 1
    assert "Multiple sprints are current (3, 4); specify --sprint" in (
        ambiguous.output
    )

    collect_3 = invoke(database, "collect", "--sprint", "3", "DEMO-12")
    assert collect_3.exit_code == 0
    with sprints.connect_database(database, writable=False) as conn:
        snaps_3 = sprints.fetch_execution_snapshots(conn, 3)
        snaps_4 = sprints.fetch_execution_snapshots(conn, 4)
    assert len(snaps_3) == 1
    assert snaps_3[0]["card_reference"] == "DEMO-12"
    assert len(snaps_4) == 0

    collect_both = invoke(database, "collect", "DEMO-14", "DEMO-13")
    assert collect_both.exit_code == 0
    with sprints.connect_database(database, writable=False) as conn:
        snaps_3 = sprints.fetch_execution_snapshots(conn, 3)
        snaps_4 = sprints.fetch_execution_snapshots(conn, 4)
    assert len(snaps_3) == 2
    refs_3 = {s["card_reference"] for s in snaps_3}
    assert refs_3 == {"DEMO-12", "DEMO-14"}
    assert len(snaps_4) == 1
    assert snaps_4[0]["card_reference"] == "DEMO-13"

    bad_ref = invoke(database, "collect", "DEMO-99")
    assert bad_ref.exit_code == 1
    assert "cards are not in current sprints: DEMO-99" in bad_ref.output

    bad_ref_sprint = invoke(database, "collect", "--sprint", "3", "DEMO-13")
    assert bad_ref_sprint.exit_code == 1
    assert "cards are not in current sprint 3: DEMO-13" in (
        bad_ref_sprint.output
    )

    closed = invoke(
        database, "close", "3", "--ended", "2026-01-02T04:04:05+08:00",
        "--delivered", "Sprint 3 done",
    )
    assert closed.exit_code == 0

    listing_after = invoke(database, "list")
    assert "Current sprint\n" in listing_after.output
    assert "Current sprints" not in listing_after.output

    show_default = invoke(database, "show")
    assert show_default.exit_code == 0
    assert "Sprint 4" in show_default.output



def test_statistics_render_titles_and_settled_state_ordering(capsys):
    summary = {"average": 60.0, "median": 60.0, "min": 60.0, "max": 60.0,
               "sd": 0.0}
    from plane_proj import sprint_timing
    sprint_timing.render_statistics({
        "rework": {"included_sprints": 1, "excluded_sprints": 0,
                   "count": None},
        "included_sprints": 1,
        "excluded_without_timing": 0,
        "excluded_incomplete": 0,
        "active_minutes_total": summary,
        "residence_minutes_total": summary,
        "residence_minutes": {
            "Backlog": summary, "Cancelled": summary,
            "In Progress": summary, "Verifying": summary,
        },
        "execution_minutes_total": summary,
        "execution_minutes": {"coding": summary},
    })
    out = capsys.readouterr().out
    assert "Detailed metrics" in out
    assert out.index("State: Backlog") < out.index("Active: In Progress")
    assert out.index("Active: In Progress") < out.index("Active: Verifying")
    assert out.index("Active: Verifying") < out.index("State: Cancelled")
    assert out.index("State: Cancelled") < out.index("Time spent: coding")


def test_migrate_upgrades_an_old_unbound_register(tmp_path: Path) -> None:
    database = tmp_path / "SPRINTS.sqlite"
    create_register(database)
    with sqlite3.connect(database) as connection:
        connection.executescript(
            "DROP TABLE operation_journal;"
            "DROP TABLE register_binding;"
            "PRAGMA user_version = 4;"
        )

    result = invoke(database, "migrate")

    assert result.exit_code == 0, result.output
    assert f"schema v4 → v{sprints.SCHEMA_VERSION}" in result.output
    with sqlite3.connect(database) as connection:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    assert {"operation_journal", "register_binding"} <= tables


def test_migrate_reports_a_current_register_unchanged(tmp_path: Path) -> None:
    database = tmp_path / "SPRINTS.sqlite"
    create_register(database)

    result = invoke(database, "migrate")

    assert result.exit_code == 0, result.output
    assert f"already at schema v{sprints.SCHEMA_VERSION}" in result.output


def test_migrate_refuses_a_missing_register(tmp_path: Path) -> None:
    result = invoke(tmp_path / "absent.sqlite", "migrate")

    assert result.exit_code == 1
    assert "cannot open database" in result.output


def test_old_schema_error_names_both_versions_and_migrate(tmp_path: Path) -> None:
    database = tmp_path / "SPRINTS.sqlite"
    create_register(database)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version = 6")

    with pytest.raises(sprints.SprintError) as caught:
        sprints.connect_database(database, writable=False)

    assert str(caught.value.message) == (
        f"database schema is version {sprints.SCHEMA_VERSION}; your "
        "database is version 6; run plane-proj sprints migrate or a "
        "write command to upgrade it"
    )


def test_per_sprint_timing_orders_cancelled_after_active_states() -> None:
    from plane_proj import sprint_timing
    rows = sprint_timing.fields({
        "observed_cards": 1, "timed_cards": 1, "final_cards": 1,
        "rework_count": 0, "reworked_cards": 0,
        "active_minutes": {"In Progress": 5},
        "residence_minutes": {
            "Backlog": 1, "Cancelled": 2, "In Progress": 5, "Verifying": 3,
        },
        "execution_minutes": {}, "delayed_minutes": {},
        "open_timer_minutes": {},
        "captured_from": "2026-01-02T03:04:05+08:00",
        "captured_through": "2026-01-02T04:04:05+08:00",
    })
    labels = [name for name, _ in rows]
    assert labels.index("Reworked cards") < labels.index("Rework count")
    assert (labels.index("State: Backlog")
            < labels.index("Active: In Progress")
            < labels.index("Active: Verifying")
            < labels.index("State: Cancelled"))


@pytest.mark.parametrize(("value", "expected"), [
    (3.0, "3"), (2.5, "2.5"), (2.4567, "2.46"), (0.0, "0"),
])
def test_velocity_formats_to_at_most_two_decimals(value, expected):
    assert sprints.format_velocity(value) == expected


def test_web_serves_the_listing_with_live_cycle_cards_and_links(
    tmp_path: Path, sprint_board, monkeypatch,
) -> None:
    database = tmp_path / "SPRINTS.sqlite"
    create_register(database)
    plan(database, 3)
    plan(database, 4, 2)
    assert invoke(
        database, "start", "3", "--started", "2026-01-02T03:04:05+08:00"
    ).exit_code == 0
    sprint_board.slug = "test"
    sprint_board.project.id = "project-uuid"
    sprint_board.project.name = "Demo"
    sprint_board.sprint_cycle_cards = lambda cycle_id: [{
        "id": f"card-{cycle_id}", "ref": "DEMO-1", "title": "Ship it",
        "state": "Todo", "points": 3,
    }]
    served: dict[str, object] = {}

    class Server:
        server_port = 9999

        def serve_forever(self) -> None:
            raise KeyboardInterrupt

        def server_close(self) -> None:
            served["closed"] = True

    def make_server(host, port, load, version):
        served.update(host=host, port=port, load=load, version=version)
        return Server()

    monkeypatch.setattr("plane_proj.web.make_server", make_server)
    result = invoke(database, "web", "--plane-url", "https://plane.example/")

    assert result.exit_code == 0, result.output
    assert served["closed"] is True
    assert (served["host"], served["port"]) == ("127.0.0.1", 8765)
    payload = served["load"]()
    board = "https://plane.example/test/projects/project-uuid/issues/"
    assert payload["board_url"] == board
    assert payload["project"] == {"key": "DEMO", "name": "Demo"}
    assert [s["sprint"] for s in payload["listing"]["current"]] == [3]
    assert [s["sprint"] for s in payload["listing"]["planned"]] == [4]
    assert payload["cards"] == {"3": [{
        "id": "card-3", "ref": "DEMO-1", "title": "Ship it", "state": "Todo",
        "points": 3, "url": board + "card-3",
    }]}
    # The page's live check moves when the register is written.
    before = served["version"]()
    assert invoke(database, "alias", "4", "NEXT").exit_code == 0
    assert served["version"]() != before


def test_rework_cost_and_reasons_reach_reports_and_old_snapshots_stay_unknown(
    tmp_path,
):
    database = tmp_path / "SPRINTS.sqlite"
    create_register(database)
    with sprints.connect_database(database, writable=True) as connection:
        for sprint_id in (1, 2):
            sprints.add_completed_sprint(connection, sprints.Sprint(
                sprint_id=sprint_id, title=f"Sprint {sprint_id}",
                status="completed", started="2026-01-01T00:00:00Z",
                ended="2026-01-02T00:00:00Z", hours=24, cards_start=1,
                cards_end=1, points_start=1, points_end=1, velocity=1,
                delivered="Outcome",
            ))
            cost = {
                "rework_minutes": 45,
                "rework_execution_minutes": {"blocking-run": 30},
                "rework_reasons": {"defect": 1, "spec": 2},
            } if sprint_id == 1 else {}
            sprints.record_execution_snapshot(
                connection, sprint_id=sprint_id, work_item_id=str(sprint_id),
                card_reference=f"DEMO-{sprint_id}",
                captured_at="2026-01-02T00:00:00Z", is_final=True,
                stats={"state_minutes": {"In Progress": 10},
                       "execution_minutes": {}, "current_state": None,
                       "open_timer": None, "rework_count": 3, **cost},
            )

    stats = invoke(database, "stats", "--json")
    assert stats.exit_code == 0, stats.output
    rework = json.loads(stats.output)["timing"]["rework"]
    # Sprint 2's snapshot predates rework cost: unknown, not zero.
    assert rework["minutes_included_sprints"] == 1
    assert rework["minutes"]["average"] == 45
    assert rework["reasons"] == {"defect": 1, "spec": 2}
    listing = invoke(database, "list", "--json")
    first = json.loads(listing.output)["past"][0]["timing"]
    assert first["rework_minutes"] == 45
    assert first["rework_execution_minutes"] == {"blocking-run": 30}
    human = invoke(database, "stats")
    assert "Rework time" in human.output and "Rework reasons" in human.output
    shown = invoke(database, "show", "1")
    assert "Rework reason: spec" in shown.output
