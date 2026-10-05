"""Acceptance tests for card PLANEPROJ-48: Register stores every timestamp in UTC.

Covers:
1. Sprints start and close store timestamps normalized to UTC (+00:00),
   including input with non-zero offsets (+08:00) and UTC Z.
2. Schema migration (v9 -> v10) rewrites existing timestamp columns
   (started, ended, captured_at, created_at, updated_at) to the same instant
   in UTC (+00:00) while leaving existing UTC values unchanged, and re-running
   the migration is idempotent.
3. Register merge driver merges migrated (v10) and unmigrated (v9) registers
   without false conflict on timestamp representations or schema version.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from plane_proj import sprints
from plane_proj.cli import Context, cli

BINDING = ("https://plane.test", "test", "DEMO")

V9_SCHEMA_SQL = """
CREATE TABLE sprints (
    sprint_id INTEGER PRIMARY KEY CHECK (sprint_id > 0),
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    status TEXT NOT NULL CHECK (status IN ('planned', 'current', 'completed')),
    cycle_id TEXT,
    position INTEGER CHECK (position IS NULL OR position > 0),
    goal TEXT NOT NULL DEFAULT '',
    execution TEXT NOT NULL DEFAULT '',
    acceptance TEXT NOT NULL DEFAULT '',
    started TEXT,
    ended TEXT,
    hours REAL,
    cards_start INTEGER,
    cards_end INTEGER,
    points_start INTEGER,
    points_end INTEGER,
    velocity REAL,
    delivered TEXT,
    retrospective TEXT NOT NULL DEFAULT '',
    alias TEXT CHECK (alias IS NULL OR (
        length(alias) BETWEEN 2 AND 7
        AND alias NOT GLOB '*[^A-Z0-9-]*'
        AND substr(alias, 1, 1) != '-'
        AND substr(alias, -1, 1) != '-'
        AND length(alias) - length(replace(alias, '-', '')) <= 1
    )),
    CHECK ((status = 'planned' AND started IS NULL AND ended IS NULL)
        OR (status = 'current' AND started IS NOT NULL AND ended IS NULL)
        OR (status = 'completed' AND started IS NOT NULL AND ended IS NOT NULL)),
    CHECK (hours IS NULL OR (hours > 0 AND hours < 1.0e308)),
    CHECK (cards_start IS NULL OR cards_start >= 0),
    CHECK (cards_end IS NULL OR cards_end >= 0),
    CHECK (points_start IS NULL OR points_start >= 0),
    CHECK (points_end IS NULL OR points_end >= 0),
    CHECK (velocity IS NULL OR (velocity >= 0 AND velocity < 1.0e308)),
    CHECK (delivered IS NULL OR length(trim(delivered)) > 0),
    CHECK (ended IS NULL OR unixepoch(ended) > unixepoch(started))
) STRICT;
CREATE TABLE IF NOT EXISTS card_execution_snapshots (
    sprint_id INTEGER NOT NULL REFERENCES sprints(sprint_id),
    work_item_id TEXT NOT NULL CHECK (length(trim(work_item_id)) > 0),
    card_reference TEXT NOT NULL CHECK (length(trim(card_reference)) > 0),
    captured_at TEXT NOT NULL,
    is_final INTEGER NOT NULL CHECK (is_final IN (0, 1)),
    stats_json TEXT NOT NULL CHECK (json_valid(stats_json)),
    PRIMARY KEY (sprint_id, work_item_id, captured_at)
) STRICT;
CREATE INDEX IF NOT EXISTS card_execution_latest
    ON card_execution_snapshots(sprint_id, work_item_id, captured_at DESC);
CREATE TABLE IF NOT EXISTS register_binding (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    host TEXT NOT NULL CHECK (length(trim(host)) > 0),
    workspace TEXT NOT NULL CHECK (length(trim(workspace)) > 0),
    project TEXT NOT NULL CHECK (length(trim(project)) > 0)
) STRICT;
CREATE TABLE IF NOT EXISTS operation_journal (
    operation_id TEXT PRIMARY KEY
        CHECK (length(trim(operation_id)) > 0),
    kind TEXT NOT NULL CHECK (kind IN ('transition', 'activity',
        'sprint-start', 'sprint-close')),
    card_reference TEXT NOT NULL
        CHECK (length(trim(card_reference)) > 0),
    request_json TEXT NOT NULL CHECK (json_valid(request_json)),
    steps_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(steps_json)),
    receipt_json TEXT CHECK (receipt_json IS NULL OR json_valid(receipt_json)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;
CREATE UNIQUE INDEX IF NOT EXISTS sprint_alias ON sprints(alias);
PRAGMA user_version = 9;
"""


class FakeUtcBoard:
    """Mock board for sprint lifecycle operations."""

    def __init__(self) -> None:
        self.cycles: dict[int, SimpleNamespace] = {}
        self.project = SimpleNamespace(
            key="DEMO",
            states={"Todo": "state-todo", "Done": "state-done"},
            estimates_enabled=True,
        )
        self.cards: list[object] = []

    def cycle_sprint_id(self, cycle_id: str) -> tuple[int, SimpleNamespace]:
        return int(cycle_id), SimpleNamespace(name=f"Sprint {cycle_id}")

    def require_no_orphans(self, current: object, planned: object) -> None:
        pass

    def sprint_cycles(self, sprint_ids: set[int]) -> dict[int, str]:
        return {sid: str(sid) for sid in sprint_ids}

    def cycle_description(self, cycle_id: str) -> str:
        return ""

    def write_sprint_cycle(
        self, sprint_id: int, cycle_id: str | None, description: str
    ) -> str:
        self.cycles[sprint_id] = SimpleNamespace(
            id=str(sprint_id), name=f"Sprint {sprint_id}", description=description
        )
        return str(sprint_id)

    def start_sprint_cycle(
        self, cycle_id: str, started: str
    ) -> tuple[str, list[str], list[str]]:
        return f"Sprint {cycle_id}", [], []

    def close_sprint_cycle(self, cycle_id: str, ended: str) -> str:
        return f"Sprint {cycle_id}"

    def sprint_cycle_metrics(self, cycle_id: str) -> dict[str, int]:
        return {
            "cards_current": 2,
            "points_current": 5,
            "cards_done": 2,
            "points_done": 5,
            "cards_cancelled": 0,
            "points_cancelled": 0,
        }

    def cycle_cards(self, cycle_id: str) -> list[object]:
        return self.cards


@pytest.fixture(autouse=True)
def fake_board(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeUtcBoard:
    config = tmp_path / "test-config.json"
    config.write_text(
        json.dumps({
            "defaults": {"workspace": "test", "project": "DEMO"},
            "estimate_points": {"1": "uuid-1"},
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("PLANE_PROJ_CONFIG", str(config))
    monkeypatch.setenv("PLANE_API_HOST_URL", "https://plane.test")
    monkeypatch.delenv("PLANE_PROJ_PROJECT", raising=False)
    board = FakeUtcBoard()
    monkeypatch.setattr(Context, "board", property(lambda self: board))
    return board


def create_v9_register(path: Path, binding: tuple[str, str, str] = BINDING) -> Path:
    """Create a database initialized with schema v9."""
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(V9_SCHEMA_SQL)
        conn.execute(
            "INSERT INTO register_binding VALUES (1, ?, ?, ?)",
            ("unused", *binding[1:]),
        )
        conn.commit()
    return path


def invoke_sprints(database: Path, *arguments: str) -> pytest.RunResult:
    return CliRunner().invoke(
        cli, ["sprints", "--database", str(database), *arguments]
    )


def invoke_merge(base: Path, ours: Path, theirs: Path) -> pytest.RunResult:
    return CliRunner().invoke(
        cli, ["register", "merge", str(base), str(ours), str(theirs)]
    )


def plan_sprint(database: Path, sprint_id: int, position: int = 1) -> None:
    result = invoke_sprints(
        database,
        "plan",
        "--id",
        str(sprint_id),
        "--title",
        f"Sprint {sprint_id}",
        "--position",
        str(position),
        "--goal",
        "Deliver card",
        "--execution",
        "",
        "--acceptance",
        "Passes acceptance tests",
    )
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# Test 1: sprints start and sprints close store timestamps in UTC (+00:00)
# ---------------------------------------------------------------------------


def test_sprints_start_stores_utc_timestamp_with_offset(
    fake_board: FakeUtcBoard, tmp_path: Path
) -> None:
    """sprints start with numeric offset (+08:00) stores instant in UTC (+00:00)."""
    database = tmp_path / "sprints.sqlite"
    sprints.create_database(database, BINDING)
    plan_sprint(database, 1)

    result = invoke_sprints(
        database, "start", "1", "--started", "2026-10-06T05:51:10+08:00"
    )
    assert result.exit_code == 0, result.output

    with closing(sqlite3.connect(database)) as conn:
        started = conn.execute(
            "SELECT started FROM sprints WHERE sprint_id = 1"
        ).fetchone()[0]
    assert started == "2026-10-05T21:51:10+00:00"


def test_sprints_start_stores_utc_timestamp_with_z(
    fake_board: FakeUtcBoard, tmp_path: Path
) -> None:
    """sprints start with 'Z' suffix stores canonical +00:00 representation."""
    database = tmp_path / "sprints.sqlite"
    sprints.create_database(database, BINDING)
    plan_sprint(database, 2)

    result = invoke_sprints(
        database, "start", "2", "--started", "2026-10-05T21:51:10Z"
    )
    assert result.exit_code == 0, result.output

    with closing(sqlite3.connect(database)) as conn:
        started = conn.execute(
            "SELECT started FROM sprints WHERE sprint_id = 2"
        ).fetchone()[0]
    assert started == "2026-10-05T21:51:10+00:00"


def test_sprints_close_stores_utc_timestamp_with_offset(
    fake_board: FakeUtcBoard, tmp_path: Path
) -> None:
    """sprints close with numeric offset (+08:00) stores instant in UTC (+00:00)."""
    database = tmp_path / "sprints.sqlite"
    sprints.create_database(database, BINDING)
    plan_sprint(database, 1)

    # Start the sprint
    assert (
        invoke_sprints(
            database, "start", "1", "--started", "2026-10-05T20:00:00+00:00"
        ).exit_code
        == 0
    )

    # Close the sprint with +08:00 offset: 2026-10-06T05:51:10+08:00 == 2026-10-05T21:51:10+00:00
    result = invoke_sprints(
        database,
        "close",
        "1",
        "--ended",
        "2026-10-06T05:51:10+08:00",
        "--delivered",
        "Shipped feature",
    )
    assert result.exit_code == 0, result.output

    with closing(sqlite3.connect(database)) as conn:
        ended = conn.execute(
            "SELECT ended FROM sprints WHERE sprint_id = 1"
        ).fetchone()[0]
    assert ended == "2026-10-05T21:51:10+00:00"


def test_sprints_close_stores_utc_timestamp_with_z(
    fake_board: FakeUtcBoard, tmp_path: Path
) -> None:
    """sprints close with 'Z' suffix stores canonical +00:00 representation."""
    database = tmp_path / "sprints.sqlite"
    sprints.create_database(database, BINDING)
    plan_sprint(database, 2)

    assert (
        invoke_sprints(
            database, "start", "2", "--started", "2026-10-05T20:00:00+00:00"
        ).exit_code
        == 0
    )

    result = invoke_sprints(
        database,
        "close",
        "2",
        "--ended",
        "2026-10-05T21:51:10Z",
        "--delivered",
        "Shipped feature",
    )
    assert result.exit_code == 0, result.output

    with closing(sqlite3.connect(database)) as conn:
        ended = conn.execute(
            "SELECT ended FROM sprints WHERE sprint_id = 2"
        ).fetchone()[0]
    assert ended == "2026-10-05T21:51:10+00:00"


def test_sprints_add_stores_utc_timestamps(tmp_path: Path) -> None:
    """sprints add converts both started and ended timestamps to UTC (+00:00)."""
    database = tmp_path / "sprints.sqlite"
    sprints.create_database(database, BINDING)

    result = invoke_sprints(
        database,
        "add",
        "--id",
        "10",
        "--title",
        "Sprint 10",
        "--started",
        "2026-10-06T05:51:10+08:00",
        "--ended",
        "2026-10-05T23:51:10Z",
        "--hours",
        "2.0",
        "--cards-start",
        "1",
        "--cards-end",
        "1",
        "--points-start",
        "2",
        "--points-end",
        "2",
        "--velocity",
        "1.0",
        "--delivered",
        "Imported sprint",
    )
    assert result.exit_code == 0, result.output

    with closing(sqlite3.connect(database)) as conn:
        row = conn.execute(
            "SELECT started, ended FROM sprints WHERE sprint_id = 10"
        ).fetchone()
    assert row[0] == "2026-10-05T21:51:10+00:00"
    assert row[1] == "2026-10-05T23:51:10+00:00"


# ---------------------------------------------------------------------------
# Test 2: Schema migration (v9 -> v10) rewrites all timestamps to UTC (+00:00)
# ---------------------------------------------------------------------------


def test_migration_v9_to_v10_rewrites_mixed_offsets_to_utc(
    tmp_path: Path,
) -> None:
    """Migration converts existing started/ended/captured_at/journal timestamps to UTC.

    Preserves NULLs on planned sprints and leaves existing UTC (+00:00) values unchanged.
    """
    database = tmp_path / "sprints.sqlite"
    create_v9_register(database)

    with closing(sqlite3.connect(database)) as conn:
        # 1. Completed sprint with +08:00 started and Z ended
        conn.execute(
            """INSERT INTO sprints (
                sprint_id, title, status, started, ended, hours,
                cards_start, cards_end, points_start, points_end, velocity, delivered
            ) VALUES (1, 'Sprint 1', 'completed', '2026-10-06T05:51:10+08:00',
                      '2026-10-05T23:51:10Z', 2.0, 1, 1, 3, 3, 1.5, 'Outcome 1')"""
        )
        # 2. Completed sprint with negative offset -05:00
        conn.execute(
            """INSERT INTO sprints (
                sprint_id, title, status, started, ended, hours,
                cards_start, cards_end, points_start, points_end, velocity, delivered
            ) VALUES (2, 'Sprint 2', 'completed', '2026-10-05T16:51:10-05:00',
                      '2026-10-05T18:51:10-05:00', 2.0, 2, 2, 5, 5, 2.5, 'Outcome 2')"""
        )
        # 3. Current sprint already in UTC (+00:00)
        conn.execute(
            """INSERT INTO sprints (
                sprint_id, title, status, cycle_id, started, cards_start, points_start
            ) VALUES (3, 'Sprint 3', 'current', 'cycle-3',
                      '2026-10-05T21:51:10+00:00', 3, 8)"""
        )
        # 4. Planned sprint with NULL timestamps
        conn.execute(
            """INSERT INTO sprints (sprint_id, title, status, position, goal, acceptance)
               VALUES (4, 'Sprint 4', 'planned', 1, 'Future', 'Criteria')"""
        )
        # 5. Snapshots: one with +08:00, one already in UTC
        conn.execute(
            """INSERT INTO card_execution_snapshots (
                sprint_id, work_item_id, card_reference, captured_at, is_final, stats_json
            ) VALUES (1, 'item-1', 'DEMO-1', '2026-10-06T06:00:00+08:00', 1, '{"done": 1}')"""
        )
        conn.execute(
            """INSERT INTO card_execution_snapshots (
                sprint_id, work_item_id, card_reference, captured_at, is_final, stats_json
            ) VALUES (3, 'item-2', 'DEMO-2', '2026-10-05T22:00:00+00:00', 0, '{"done": 0}')"""
        )
        # 6. Operation journal: mixed offsets
        conn.execute(
            """INSERT INTO operation_journal (
                operation_id, kind, card_reference, request_json, steps_json,
                receipt_json, created_at, updated_at
            ) VALUES ('op-1', 'transition', 'DEMO-1', '{}', '[]', NULL,
                      '2026-10-06T05:51:10+08:00', '2026-10-05T22:00:00Z')"""
        )
        conn.commit()

    # Pre-migration sanity check: schema version is 9
    assert sprints.schema_version(database) == 9

    # Run migration
    result = invoke_sprints(database, "migrate")
    assert result.exit_code == 0, result.output
    assert "schema v9 → v10" in result.output

    # Post-migration assertions:
    assert sprints.schema_version(database) == 10

    with closing(sqlite3.connect(database)) as conn:
        conn.row_factory = sqlite3.Row
        sprints_rows = {
            row["sprint_id"]: dict(row)
            for row in conn.execute(
                "SELECT * FROM sprints ORDER BY sprint_id"
            ).fetchall()
        }
        snapshots_rows = {
            (row["sprint_id"], row["work_item_id"]): dict(row)
            for row in conn.execute(
                "SELECT * FROM card_execution_snapshots"
            ).fetchall()
        }
        journal_rows = {
            row["operation_id"]: dict(row)
            for row in conn.execute("SELECT * FROM operation_journal").fetchall()
        }

    # Sprint 1 converted from +08:00 and Z to +00:00
    assert sprints_rows[1]["started"] == "2026-10-05T21:51:10+00:00"
    assert sprints_rows[1]["ended"] == "2026-10-05T23:51:10+00:00"
    assert sprints_rows[1]["delivered"] == "Outcome 1"

    # Sprint 2 converted from -05:00 to +00:00
    assert sprints_rows[2]["started"] == "2026-10-05T21:51:10+00:00"
    assert sprints_rows[2]["ended"] == "2026-10-05T23:51:10+00:00"

    # Sprint 3 UTC unchanged, ended remains NULL
    assert sprints_rows[3]["started"] == "2026-10-05T21:51:10+00:00"
    assert sprints_rows[3]["ended"] is None

    # Sprint 4 planned remains NULL
    assert sprints_rows[4]["started"] is None
    assert sprints_rows[4]["ended"] is None

    # Snapshots converted
    assert (
        snapshots_rows[(1, "item-1")]["captured_at"]
        == "2026-10-05T22:00:00+00:00"
    )
    assert (
        snapshots_rows[(3, "item-2")]["captured_at"]
        == "2026-10-05T22:00:00+00:00"
    )

    # Journal converted
    assert journal_rows["op-1"]["created_at"] == "2026-10-05T21:51:10+00:00"
    assert journal_rows["op-1"]["updated_at"] == "2026-10-05T22:00:00+00:00"


def test_migration_v9_to_v10_is_idempotent(tmp_path: Path) -> None:
    """Re-running migration on an already-migrated v10 database changes nothing."""
    database = tmp_path / "sprints.sqlite"
    create_v9_register(database)

    with closing(sqlite3.connect(database)) as conn:
        conn.execute(
            """INSERT INTO sprints (
                sprint_id, title, status, started, ended, hours,
                cards_start, cards_end, points_start, points_end, velocity, delivered
            ) VALUES (1, 'Sprint 1', 'completed', '2026-10-06T05:51:10+08:00',
                      '2026-10-05T23:51:10Z', 2.0, 1, 1, 3, 3, 1.5, 'Outcome 1')"""
        )
        conn.commit()

    # First migration: 9 -> 10
    assert invoke_sprints(database, "migrate").exit_code == 0
    assert sprints.schema_version(database) == 10

    with closing(sqlite3.connect(database)) as conn:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        snapshot_after_first = {
            table: conn.execute(f"SELECT * FROM {table}").fetchall()
            for table in tables
        }

    # Second migration: already at v10
    result = invoke_sprints(database, "migrate")
    assert result.exit_code == 0, result.output
    assert "already at schema v10" in result.output

    with closing(sqlite3.connect(database)) as conn:
        snapshot_after_second = {
            table: conn.execute(f"SELECT * FROM {table}").fetchall()
            for table in tables
        }

    assert snapshot_after_first == snapshot_after_second


# ---------------------------------------------------------------------------
# Test 3: Register merge driver merges migrated (v10) and unmigrated (v9) registers
# ---------------------------------------------------------------------------


def test_register_merge_v10_ours_and_v9_theirs_without_false_conflict(
    tmp_path: Path,
) -> None:
    """Register merge driver merges a migrated (v10) register and an unmigrated (v9) register."""
    base = create_v9_register(tmp_path / "base.sqlite")
    # Base contains Sprint 1 in v9 with +08:00
    with closing(sqlite3.connect(base)) as conn:
        conn.execute(
            """INSERT INTO sprints (
                sprint_id, title, status, started, ended, hours,
                cards_start, cards_end, points_start, points_end, velocity, delivered
            ) VALUES (1, 'Sprint 1', 'completed', '2026-10-06T05:51:10+08:00',
                      '2026-10-06T07:51:10+08:00', 2.0, 1, 1, 3, 3, 1.5, 'Base outcome')"""
        )
        conn.commit()

    ours = tmp_path / "ours.sqlite"
    theirs = tmp_path / "theirs.sqlite"
    shutil.copy(base, ours)
    shutil.copy(base, theirs)

    # OURS: migrated to v10, and adds planned Sprint 2
    assert invoke_sprints(ours, "migrate").exit_code == 0
    assert sprints.schema_version(ours) == 10
    plan_sprint(ours, 2, position=1)

    # THEIRS: remains unmigrated v9, adds planned Sprint 3
    with closing(sqlite3.connect(theirs)) as conn:
        conn.execute(
            """INSERT INTO sprints (sprint_id, title, status, position, goal, acceptance)
               VALUES (3, 'Sprint 3', 'planned', 2, 'Goal 3', 'Criterion 3')"""
        )
        conn.commit()
    assert sprints.schema_version(theirs) == 9

    result = invoke_merge(base, ours, theirs)
    assert result.exit_code == 0, f"Merge failed: {result.output}\n{result.stderr}"

    # Verify merged result in ours:
    assert sprints.schema_version(ours) == 10
    with closing(sqlite3.connect(ours)) as conn:
        conn.row_factory = sqlite3.Row
        sprints_by_id = {
            row["sprint_id"]: dict(row)
            for row in conn.execute(
                "SELECT * FROM sprints ORDER BY sprint_id"
            ).fetchall()
        }

    # Sprints from both branches are present
    assert set(sprints_by_id.keys()) == {1, 2, 3}

    # Sprint 1 has normalized UTC timestamps
    assert sprints_by_id[1]["started"] == "2026-10-05T21:51:10+00:00"
    assert sprints_by_id[1]["ended"] == "2026-10-05T23:51:10+00:00"


def test_register_merge_v9_ours_and_v10_theirs_without_false_conflict(
    tmp_path: Path,
) -> None:
    """Register merge driver merges unmigrated (v9) ours and migrated (v10) theirs."""
    base = create_v9_register(tmp_path / "base.sqlite")
    with closing(sqlite3.connect(base)) as conn:
        conn.execute(
            """INSERT INTO sprints (
                sprint_id, title, status, started, ended, hours,
                cards_start, cards_end, points_start, points_end, velocity, delivered
            ) VALUES (1, 'Sprint 1', 'completed', '2026-10-06T05:51:10+08:00',
                      '2026-10-06T07:51:10+08:00', 2.0, 1, 1, 3, 3, 1.5, 'Base outcome')"""
        )
        conn.commit()

    ours = tmp_path / "ours.sqlite"
    theirs = tmp_path / "theirs.sqlite"
    shutil.copy(base, ours)
    shutil.copy(base, theirs)

    # OURS: remains unmigrated v9, adds planned Sprint 2
    with closing(sqlite3.connect(ours)) as conn:
        conn.execute(
            """INSERT INTO sprints (sprint_id, title, status, position, goal, acceptance)
               VALUES (2, 'Sprint 2', 'planned', 1, 'Goal 2', 'Criterion 2')"""
        )
        conn.commit()
    assert sprints.schema_version(ours) == 9

    # THEIRS: migrated to v10, adds planned Sprint 3
    assert invoke_sprints(theirs, "migrate").exit_code == 0
    assert sprints.schema_version(theirs) == 10
    plan_sprint(theirs, 3, position=2)

    result = invoke_merge(base, ours, theirs)
    assert result.exit_code == 0, f"Merge failed: {result.output}\n{result.stderr}"

    # Verify merged result in ours is upgraded to v10 with UTC timestamps
    assert sprints.schema_version(ours) == 10
    with closing(sqlite3.connect(ours)) as conn:
        conn.row_factory = sqlite3.Row
        sprints_by_id = {
            row["sprint_id"]: dict(row)
            for row in conn.execute(
                "SELECT * FROM sprints ORDER BY sprint_id"
            ).fetchall()
        }

    assert set(sprints_by_id.keys()) == {1, 2, 3}
    assert sprints_by_id[1]["started"] == "2026-10-05T21:51:10+00:00"
    assert sprints_by_id[1]["ended"] == "2026-10-05T23:51:10+00:00"


def test_register_merge_different_instant_string_format_no_false_conflict(
    tmp_path: Path,
) -> None:
    """Modifying a field on an unmigrated branch does not conflict on identical instant timestamps.

    Base has +08:00 timestamp.
    Ours migrated to +00:00 timestamp.
    Theirs changed retrospective in v9 (still +08:00 timestamp).
    Since timestamps represent the same instant, no conflict is raised.
    """
    base = create_v9_register(tmp_path / "base.sqlite")
    with closing(sqlite3.connect(base)) as conn:
        conn.execute(
            """INSERT INTO sprints (
                sprint_id, title, status, started, ended, hours,
                cards_start, cards_end, points_start, points_end, velocity, delivered, retrospective
            ) VALUES (1, 'Sprint 1', 'completed', '2026-10-06T05:51:10+08:00',
                      '2026-10-06T07:51:10+08:00', 2.0, 1, 1, 3, 3, 1.5, 'Base outcome', '')"""
        )
        conn.commit()

    ours = tmp_path / "ours.sqlite"
    theirs = tmp_path / "theirs.sqlite"
    shutil.copy(base, ours)
    shutil.copy(base, theirs)

    # OURS migrates to v10 (started/ended rewritten to UTC)
    assert invoke_sprints(ours, "migrate").exit_code == 0

    # THEIRS updates retrospective on Sprint 1 in v9
    with closing(sqlite3.connect(theirs)) as conn:
        conn.execute(
            "UPDATE sprints SET retrospective = 'Retrospective notes' WHERE sprint_id = 1"
        )
        conn.commit()

    result = invoke_merge(base, ours, theirs)
    assert result.exit_code == 0, f"Merge failed: {result.output}\n{result.stderr}"

    with closing(sqlite3.connect(ours)) as conn:
        conn.row_factory = sqlite3.Row
        sprint1 = dict(
            conn.execute(
                "SELECT * FROM sprints WHERE sprint_id = 1"
            ).fetchone()
        )

    assert sprint1["started"] == "2026-10-05T21:51:10+00:00"
    assert sprint1["ended"] == "2026-10-05T23:51:10+00:00"
    assert sprint1["retrospective"] == "Retrospective notes"


def test_register_merge_true_timestamp_conflict_is_refused(
    tmp_path: Path,
) -> None:
    """True conflict where both branches changed ended to different instants is refused."""
    base = create_v9_register(tmp_path / "base.sqlite")
    with closing(sqlite3.connect(base)) as conn:
        conn.execute(
            """INSERT INTO sprints (
                sprint_id, title, status, started, ended, hours,
                cards_start, cards_end, points_start, points_end, velocity, delivered
            ) VALUES (1, 'Sprint 1', 'completed', '2026-10-05T21:51:10+00:00',
                      '2026-10-05T23:00:00+00:00', 1.0, 1, 1, 3, 3, 3.0, 'Outcome')"""
        )
        conn.commit()

    ours = tmp_path / "ours.sqlite"
    theirs = tmp_path / "theirs.sqlite"
    shutil.copy(base, ours)
    shutil.copy(base, theirs)

    # OURS migrates to v10 and changes ended to 23:30 UTC
    assert invoke_sprints(ours, "migrate").exit_code == 0
    with closing(sqlite3.connect(ours)) as conn:
        conn.execute(
            "UPDATE sprints SET ended = '2026-10-05T23:30:00+00:00' WHERE sprint_id = 1"
        )
        conn.commit()

    # THEIRS (v9) changes ended to 2026-10-06T08:00:00+08:00
    # (which is 2026-10-06T00:00:00+00:00, different instant)
    with closing(sqlite3.connect(theirs)) as conn:
        conn.execute(
            "UPDATE sprints SET ended = '2026-10-06T08:00:00+08:00' WHERE sprint_id = 1"
        )
        conn.commit()

    before_ours = ours.read_bytes()
    result = invoke_merge(base, ours, theirs)

    # Must fail with conflict under register merge rule
    assert result.exit_code == 1
    assert "Register merge rule" in result.stderr
    assert "sprints 1: changed on both branches: ended" in result.stderr
    # OURS must remain untouched on conflict
    assert ours.read_bytes() == before_ours
