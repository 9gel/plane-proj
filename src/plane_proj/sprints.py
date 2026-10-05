"""Persistent sprint plans and history stored in SPRINTS.sqlite."""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import statistics
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from urllib.parse import quote

import click
from rich import box
from rich.console import Console
from rich.table import Table

from plane_proj.guards import SprintCycleBound, SprintCycleTaken

SCHEMA_VERSION = 10
STATUS_PLANNED = "planned"
STATUS_CURRENT = "current"
STATUS_COMPLETED = "completed"

BINDING_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS register_binding (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    host TEXT NOT NULL CHECK (length(trim(host)) > 0),
    workspace TEXT NOT NULL CHECK (length(trim(workspace)) > 0),
    project TEXT NOT NULL CHECK (length(trim(project)) > 0)
) STRICT;
"""

JOURNAL_SCHEMA_SQL = """
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
"""

SNAPSHOT_SCHEMA_SQL = """
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
"""

ALIAS_COLUMN_SQL = """alias TEXT CHECK (alias IS NULL OR (
    length(alias) BETWEEN 2 AND 7
    AND alias NOT GLOB '*[^A-Z0-9-]*'
    AND substr(alias, 1, 1) != '-'
    AND substr(alias, -1, 1) != '-'
    AND length(alias) - length(replace(alias, '-', '')) <= 1
))"""
ALIAS_INDEX_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS sprint_alias ON sprints(alias);"
)

SCHEMA_SQL = """
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
    """ + ALIAS_COLUMN_SQL + """,
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
""" + (
    SNAPSHOT_SCHEMA_SQL + BINDING_SCHEMA_SQL + JOURNAL_SCHEMA_SQL
    + ALIAS_INDEX_SQL + "PRAGMA user_version = 10;"
)


@dataclass(frozen=True, slots=True)
class Sprint:
    sprint_id: int
    title: str
    status: str
    cycle_id: str | None = None
    position: int | None = None
    goal: str = ""
    execution: str = ""
    acceptance: str = ""
    started: str | None = None
    ended: str | None = None
    hours: float | None = None
    cards_start: int | None = None
    cards_end: int | None = None
    points_start: int | None = None
    points_end: int | None = None
    velocity: float | None = None
    delivered: str | None = None
    retrospective: str = ""
    alias: str | None = None


class SprintError(click.ClickException):
    """A user-facing sprint-register error."""


def default_database_path() -> Path:
    return Path.cwd() / "plane" / "SPRINTS.sqlite"


def parse_timestamp(value: str) -> datetime:
    """Any ISO 8601 instant with an offset: Z, +HH:MM, fractional seconds.

    Plane and live timing report fractional seconds, so readers accept
    them; writers store whole seconds in UTC through `to_utc_timestamp`.
    Without an offset the instant is ambiguous and is refused.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise SprintError(
            f"invalid timestamp {value!r}; expected an ISO 8601 time with an "
            "offset, e.g. 2026-10-05T22:23:59+00:00"
        ) from error
    if parsed.tzinfo is None:
        raise SprintError(
            f"invalid timestamp {value!r}; add an offset such as +00:00 or Z"
        )
    return parsed


def to_utc_timestamp(value: str) -> str:
    """Parse an RFC 3339 timestamp and format it in canonical UTC (+00:00)."""
    parsed = parse_timestamp(value)
    return parsed.astimezone(UTC).isoformat(timespec="seconds")


def timestamps_equal(actual: object, expected: str) -> bool:
    """Compare instants, treating absent or invalid readbacks as mismatches."""
    if actual is None:
        return False
    try:
        parsed = datetime.fromisoformat(str(actual))
        return parsed.tzinfo is not None and parsed == parse_timestamp(expected)
    except (ValueError, SprintError):
        return False


def current_sprint_detail(
    sprint: Sprint, metrics: dict[str, int | float]
) -> dict[str, object]:
    """Combine stored starting totals with one live progress observation."""
    if sprint.started is None:
        raise SprintError("current sprint must have a start timestamp")
    began = parse_timestamp(sprint.started)
    now = datetime.now(tz=began.tzinfo)
    seconds = max(0, int((now - began).total_seconds()))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    record = sprint_record(sprint, include_all=True)
    record.update(
        time_elapsed=f"{hours:02d}:{minutes:02d}:{seconds:02d}",
        cards_done=metrics["cards_done"],
        cards_end=metrics["cards_current"] - metrics["cards_cancelled"],
        points_done=metrics["points_done"],
        points_end=metrics["points_current"] - metrics["points_cancelled"],
        velocity=current_velocity(sprint.started, int(metrics["points_done"]), now=now),
    )
    return record


def render_sprint_detail(
    record: dict[str, object], *, timing_fields: tuple[tuple[str, object], ...] = ()
) -> None:
    """Render descriptive details before the sprint's metrics and timestamps."""
    fields: list[tuple[str, object]] = []
    current = record["status"] == STATUS_CURRENT
    grouped = {"cards_done", "cards_end", "points_done", "points_end", "time_elapsed"}
    for name, value in record.items():
        if name == "timing":
            continue
        if current and name in grouped:
            continue
        if current and name == "hours":
            fields.append(("Time elapsed", record["time_elapsed"]))
        elif current and name in {"cards_start", "points_start"}:
            prefix = name.removesuffix("_start")
            fields.extend(
                (f"{prefix.capitalize()} {stage}", record[f"{prefix}_{stage}"])
                for stage in ("start", "done", "end")
            )
        elif current and name == "velocity":
            fields.append(("Velocity", f"{format_velocity(float(value))}/h"))
        elif name in {"started", "ended"}:
            fields.append((name.capitalize(), format_timestamp(value)))
        else:
            fields.append((name.replace("_", " ").capitalize(), value))
    closing_order = (
        "Started",
        "Cards start",
        "Cards done",
        "Cards end",
        "Points start",
        "Points done",
        "Points end",
    )
    metric_labels = {"Cards", "Points", "Hours", "Elapsed", "Time elapsed", "Velocity"}
    details = tuple(field for field in fields if field[0] not in metric_labels
                    and field[0] not in closing_order and field[0] != "Ended")
    metrics = tuple(field for field in fields if field[0] in metric_labels)
    closing = tuple(
        field for label in closing_order for field in fields if field[0] == label
    )
    ended = tuple(field for field in fields if field[0] == "Ended")
    Console(highlight=False).print(
        _field_table(details + metrics + timing_fields + closing + ended)
    )


def database_uri(path: Path, *, mode: str) -> str:
    return f"file:{quote(str(path.resolve()), safe='/')}?mode={mode}"


def create_database(path: Path, binding: tuple[str, str, str] | None = None) -> None:
    try:
        with path.open("xb") as reservation:
            reserved_file = os.fstat(reservation.fileno())
    except FileExistsError as error:
        raise SprintError(f"database already exists: {path}") from error
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(path)
        connection.executescript(SCHEMA_SQL)
        if binding is not None:
            _store_binding(connection, binding)
    except Exception:
        if connection is not None:
            connection.close()
        if path.exists():
            current = path.stat()
            if (current.st_dev, current.st_ino) == (reserved_file.st_dev, reserved_file.st_ino):
                path.unlink()
        raise
    finally:
        if connection is not None:
            connection.close()


def _migrate_v1(connection: sqlite3.Connection) -> None:
    try:
        connection.executescript(
            "BEGIN IMMEDIATE; ALTER TABLE sprints RENAME TO completed_sprints_v1;"
            + SCHEMA_SQL.replace(
                f"PRAGMA user_version = {SCHEMA_VERSION};", ""
            )
            + """INSERT INTO sprints (
                   sprint_id, title, status, started, ended, hours, cards_start, cards_end,
                   points_start, points_end, velocity, delivered, retrospective
               ) SELECT sprint_id, title, 'completed', started, ended, hours, cards_start,
                        cards_end, points_start, points_end, velocity, delivered, retrospective
                 FROM completed_sprints_v1;
               DROP TABLE completed_sprints_v1;
               PRAGMA user_version = 4;
               COMMIT;"""
        )
    except Exception:
        connection.rollback()
        raise


def _migrate_v2(connection: sqlite3.Connection) -> None:
    try:
        connection.executescript(
            """BEGIN IMMEDIATE;
               ALTER TABLE sprints ADD COLUMN cycle_id TEXT;
               """ + SNAPSHOT_SCHEMA_SQL + """
               PRAGMA user_version = 4;
               COMMIT;"""
        )
    except Exception:
        connection.rollback()
        raise


def _migrate_v3(connection: sqlite3.Connection) -> None:
    try:
        connection.executescript(
            "BEGIN IMMEDIATE;" + SNAPSHOT_SCHEMA_SQL + "PRAGMA user_version = 4; COMMIT;"
        )
    except Exception:
        connection.rollback()
        raise


def _migrate_v10(connection: sqlite3.Connection) -> None:
    try:
        connection.execute("BEGIN IMMEDIATE")
        sprints_rows = connection.execute(
            "SELECT sprint_id, started, ended FROM sprints "
            "WHERE started IS NOT NULL OR ended IS NOT NULL"
        ).fetchall()
        for row in sprints_rows:
            sid = row[0]
            try:
                started = to_utc_timestamp(row[1]) if row[1] is not None else None
            except SprintError:
                started = row[1]
            try:
                ended = to_utc_timestamp(row[2]) if row[2] is not None else None
            except SprintError:
                ended = row[2]
            if started != row[1] or ended != row[2]:
                connection.execute(
                    "UPDATE sprints SET started = ?, ended = ? WHERE sprint_id = ?",
                    (started, ended, sid),
                )

        snapshots = connection.execute(
            "SELECT sprint_id, work_item_id, captured_at "
            "FROM card_execution_snapshots"
        ).fetchall()
        for row in snapshots:
            sid, wid, cap = row[0], row[1], row[2]
            try:
                new_cap = to_utc_timestamp(cap)
            except SprintError:
                new_cap = cap
            if new_cap != cap:
                connection.execute(
                    "UPDATE card_execution_snapshots SET captured_at = ? "
                    "WHERE sprint_id = ? AND work_item_id = ? AND captured_at = ?",
                    (new_cap, sid, wid, cap),
                )

        journal_rows = connection.execute(
            "SELECT operation_id, created_at, updated_at FROM operation_journal"
        ).fetchall()
        for row in journal_rows:
            op_id, created, updated = row[0], row[1], row[2]
            try:
                new_created = to_utc_timestamp(created)
            except SprintError:
                new_created = created
            try:
                new_updated = to_utc_timestamp(updated)
            except SprintError:
                new_updated = updated
            if new_created != created or new_updated != updated:
                connection.execute(
                    "UPDATE operation_journal SET created_at = ?, updated_at = ? "
                    "WHERE operation_id = ?",
                    (new_created, new_updated, op_id),
                )

        connection.execute("PRAGMA user_version = 10")
        connection.commit()
    except Exception:
        connection.rollback()
        connection.close()
        raise


def connect_database(path: Path, *, writable: bool) -> sqlite3.Connection:
    mode = "rw" if writable else "ro"
    try:
        connection = sqlite3.connect(database_uri(path, mode=mode), uri=True)
    except sqlite3.Error as error:
        raise SprintError(f"cannot open database {path}: {error}") from error
    connection.row_factory = sqlite3.Row
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if writable and version in {1, 2, 3}:
        {1: _migrate_v1, 2: _migrate_v2, 3: _migrate_v3}[version](connection)
        version = 4
    if writable and version == 4:
        connection.executescript(
            "BEGIN IMMEDIATE;" + BINDING_SCHEMA_SQL + "PRAGMA user_version = 5; COMMIT;"
        )
        version = 5
    if writable and version == 5:
        connection.executescript(
            "BEGIN IMMEDIATE;\n"
            "DROP INDEX IF EXISTS one_current_sprint;\n"
            "PRAGMA user_version = 6;\n"
            "COMMIT;"
        )
        version = 6
    if writable and version == 6:
        connection.executescript(
            "BEGIN IMMEDIATE;"
            + JOURNAL_SCHEMA_SQL
            + "PRAGMA user_version = 7; COMMIT;"
        )
        version = 7
    if writable and version == 7:
        try:
            connection.execute("BEGIN IMMEDIATE")
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(sprints)")
            }
            if "alias" not in columns:
                connection.execute(
                    "ALTER TABLE sprints ADD COLUMN " + ALIAS_COLUMN_SQL
                )
            connection.execute(ALIAS_INDEX_SQL)
            connection.execute("PRAGMA user_version = 8")
            connection.commit()
        except sqlite3.Error:
            connection.rollback()
            connection.close()
            raise
        version = 8
    # v9 only widens the journal kinds; registers written by 0.8-0.19 use it.
    if writable and version == 8:
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "ALTER TABLE operation_journal RENAME TO old_operation_journal"
            )
            connection.execute(JOURNAL_SCHEMA_SQL)
            connection.execute(
                "INSERT INTO operation_journal SELECT * "
                "FROM old_operation_journal"
            )
            connection.execute("DROP TABLE old_operation_journal")
            connection.execute("PRAGMA user_version = 9")
            connection.commit()
        except sqlite3.Error:
            connection.rollback()
            connection.close()
            raise
        version = 9
    # v10 normalizes all stored timestamps to canonical UTC (+00:00).
    if writable and version == 9:
        _migrate_v10(connection)
        version = 10

    if version != SCHEMA_VERSION:
        connection.close()
        action = (
            "run plane-proj sprints migrate or a write command to upgrade it"
            if version in {1, 2, 3, 4, 5, 6, 7, 8, 9}
            else "use a supported database"
        )
        raise SprintError(
            f"database schema is version {SCHEMA_VERSION}; your database "
            f"is version {version}; {action}"
        )
    return connection


def schema_version(path: Path) -> int:
    """Read the register's schema version without migrating it."""
    try:
        with closing(
            sqlite3.connect(database_uri(path, mode="ro"), uri=True)
        ) as connection:
            return connection.execute("PRAGMA user_version").fetchone()[0]
    except sqlite3.Error as error:
        raise SprintError(f"cannot open database {path}: {error}") from error


def read_binding(path: Path) -> tuple[str, str, str] | None:
    """Inspect ownership without migrating or writing a legacy register."""
    with closing(sqlite3.connect(database_uri(path, mode="ro"), uri=True)) as connection:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='register_binding'"
        ).fetchone()
        if table is None:
            return None
        row = connection.execute(
            "SELECT host, workspace, project FROM register_binding WHERE singleton=1"
        ).fetchone()
        return tuple(row) if row is not None else None


def require_binding(path: Path, expected: tuple[str, str, str]) -> None:
    actual = read_binding(path)
    if actual is None:
        raise SprintError(
            f"Sprint register binding rule: {path} is unbound. Select the correct --conf "
            "and run `sprint --database PATH bind` before using it."
        )
    if actual[1:] != expected[1:]:
        raise SprintError(
            f"Sprint register binding rule: {path} belongs to "
            f"{actual[1]} / {actual[2]}, but the selected config targets "
            f"{expected[1]} / {expected[2]}. Use the matching --conf "
            "and --project, or select the correct --database."
        )


def binding_cycles(path: Path) -> list[tuple[int, str]]:
    """Read cycle evidence from any historical register schema."""
    with closing(sqlite3.connect(database_uri(path, mode="ro"), uri=True)) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(sprints)")}
        if "cycle_id" not in columns:
            return []
        return connection.execute(
            "SELECT sprint_id, cycle_id FROM sprints WHERE cycle_id IS NOT NULL"
        ).fetchall()


def _store_binding(connection: sqlite3.Connection, binding: tuple[str, str, str]) -> None:
    existing = connection.execute(
        "SELECT host, workspace, project FROM register_binding WHERE singleton=1"
    ).fetchone()
    if existing is not None and tuple(existing)[1:] != binding[1:]:
        raise SprintError("Sprint register binding rule: cannot replace an existing binding.")
    if existing is None:
        # The legacy schema requires a nonempty host; it is no longer identity.
        connection.execute(
            "INSERT INTO register_binding VALUES (1, ?, ?, ?)", ("unused", *binding[1:])
        )
        connection.commit()
    actual = connection.execute(
        "SELECT host, workspace, project FROM register_binding WHERE singleton=1"
    ).fetchone()
    if tuple(actual)[1:] != binding[1:]:
        raise SprintError("Sprint register binding rule: binding readback failed.")


def bind_database(path: Path, binding: tuple[str, str, str]) -> None:
    if read_binding(path) is not None:
        require_binding(path, binding)
        return
    with closing(connect_database(path, writable=True)) as connection:
        _store_binding(connection, binding)


def row_to_sprint(row: sqlite3.Row) -> Sprint:
    return Sprint(**{name: row[name] for name in Sprint.__dataclass_fields__})


def fetch_sprints(connection: sqlite3.Connection) -> list[Sprint]:
    rows = connection.execute(
        """SELECT * FROM sprints
           ORDER BY CASE status WHEN 'current' THEN 0 WHEN 'planned' THEN 1 ELSE 2 END,
                    CASE WHEN status = 'planned' THEN position END,
                    CASE WHEN status = 'completed' THEN ended END, sprint_id"""
    ).fetchall()
    return [row_to_sprint(row) for row in rows]


def fetch_sprint(connection: sqlite3.Connection, sprint_id: int) -> Sprint | None:
    row = connection.execute("SELECT * FROM sprints WHERE sprint_id = ?", (sprint_id,)).fetchone()
    return None if row is None else row_to_sprint(row)


def resolve_sprint_id(connection: sqlite3.Connection, reference: str) -> int:
    """Resolve aliases locally; numeric IDs retain their original meaning."""
    row = connection.execute(
        "SELECT sprint_id FROM sprints WHERE alias = ?", (reference,)
    ).fetchone()
    if row is not None:
        return row[0]
    if reference.isascii() and reference.isdigit() and int(reference) > 0:
        return int(reference)
    raise SprintError(
        f"Sprint reference rule: unknown ID or alias {reference!r}"
    )


def validate_alias(
    connection: sqlite3.Connection, sprint_id: int, alias: str | None
) -> None:
    """Check both namespaces, including IDs created after numeric aliases."""
    if alias is not None and (
        re.fullmatch(r"[A-Z0-9][-A-Z0-9]{0,5}[A-Z0-9]", alias) is None
        or alias.count("-") > 1
    ):
        raise SprintError(
            "Sprint alias rule: use 2–7 uppercase ASCII letters or digits, "
            "with at most one interior dash (positions 2–6)."
        )
    conflict = connection.execute(
        """SELECT sprint_id FROM sprints WHERE sprint_id != ? AND (
            alias = ?
            OR (? NOT GLOB '*[^0-9]*' AND sprint_id = CAST(? AS INTEGER))
            OR (alias NOT GLOB '*[^0-9]*' AND CAST(alias AS INTEGER) = ?)
        )""",
        (sprint_id, alias, alias, alias, sprint_id),
    ).fetchone()
    if conflict is not None:
        raise SprintError(
            "Sprint alias rule: ID or alias conflicts with "
            f"sprint {conflict[0]}"
        )


def set_alias(
    connection: sqlite3.Connection, sprint_id: int, alias: str | None
) -> None:
    """Assign, replace, or clear an alias without changing lifecycle state."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        if fetch_sprint(connection, sprint_id) is None:
            raise SprintError(f"sprint {sprint_id} not found")
        validate_alias(connection, sprint_id, alias)
        connection.execute(
            "UPDATE sprints SET alias = ? WHERE sprint_id = ?",
            (alias, sprint_id),
        )
        _verify_alias(connection, sprint_id, alias)
        connection.commit()
    except (SprintError, sqlite3.Error):
        connection.rollback()
        raise


def _verify_alias(
    connection: sqlite3.Connection, sprint_id: int, alias: str | None
) -> None:
    actual = fetch_sprint(connection, sprint_id)
    if actual is None or actual.alias != alias:
        raise SprintError("Sprint alias rule: alias readback failed")


def record_execution_snapshot(
    connection: sqlite3.Connection,
    *,
    sprint_id: int,
    work_item_id: str,
    card_reference: str,
    captured_at: str,
    is_final: bool,
    stats: dict[str, object],
) -> None:
    """Append one observed card-stat snapshot to the sprint register."""
    captured_at = to_utc_timestamp(captured_at)
    sprint = fetch_sprint(connection, sprint_id)
    if sprint is None or sprint.status not in {STATUS_CURRENT, STATUS_COMPLETED}:
        raise SprintError(f"sprint {sprint_id} is not current or completed")
    if not work_item_id.strip() or not card_reference.strip():
        raise SprintError("execution snapshots require a work item id and card reference")
    encoded = json.dumps(stats, sort_keys=True, separators=(",", ":"))
    try:
        connection.execute(
            """INSERT INTO card_execution_snapshots
                   (sprint_id, work_item_id, card_reference, captured_at, is_final, stats_json)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(sprint_id, work_item_id, captured_at) DO UPDATE SET
                   card_reference=excluded.card_reference,
                   is_final=excluded.is_final,
                   stats_json=excluded.stats_json""",
            (sprint_id, work_item_id.strip(), card_reference.strip(), captured_at,
             int(is_final), encoded),
        )
        connection.commit()
    except sqlite3.IntegrityError as error:
        connection.rollback()
        raise SprintError(
            f"cannot record execution snapshot for {card_reference}: {error}"
        ) from error


def fetch_execution_snapshots(
    connection: sqlite3.Connection, sprint_id: int
) -> list[dict[str, object]]:
    """Return every stored card-stat observation in chronological order."""
    rows = connection.execute(
        """SELECT sprint_id, work_item_id, card_reference, captured_at, is_final, stats_json
             FROM card_execution_snapshots
            WHERE sprint_id = ?
            ORDER BY captured_at, card_reference""",
        (sprint_id,),
    ).fetchall()
    return [{
        "sprint_id": row["sprint_id"],
        "work_item_id": row["work_item_id"],
        "card_reference": row["card_reference"],
        "captured_at": row["captured_at"],
        "is_final": bool(row["is_final"]),
        "stats": json.loads(row["stats_json"]),
    } for row in rows]


def missing_final_snapshots(
    connection: sqlite3.Connection, sprint_id: int, work_item_ids: set[str]
) -> set[str]:
    """Return cycle work items lacking a persisted final observation."""
    if not work_item_ids:
        return set()
    placeholders = ",".join("?" for _ in work_item_ids)
    rows = connection.execute(
        f"""SELECT DISTINCT work_item_id
              FROM card_execution_snapshots
             WHERE sprint_id = ? AND is_final = 1
               AND work_item_id IN ({placeholders})""",  # noqa: S608 - placeholders only
        (sprint_id, *sorted(work_item_ids)),
    ).fetchall()
    found = {str(row["work_item_id"]) for row in rows}
    return work_item_ids - found


def claim_operation(
    connection: sqlite3.Connection,
    operation_id: str,
    kind: str,
    card_reference: str,
    request: dict[str, object],
) -> dict[str, object]:
    """Find or create one journal row for a resumable operation.

    The same id with a different request is a conflict, never a merge: an
    id names one exact operation, and resuming somebody else's under it
    would replay steps against the wrong intent.
    """
    if not operation_id.strip():
        raise SprintError("operation id must not be blank")
    row = connection.execute(
        "SELECT kind, card_reference, request_json, steps_json, receipt_json"
        " FROM operation_journal WHERE operation_id = ?",
        (operation_id,),
    ).fetchone()
    if row is None:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        connection.execute(
            """INSERT INTO operation_journal
                   (operation_id, kind, card_reference, request_json,
                    steps_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, '[]', ?, ?)""",
            (operation_id, kind, card_reference,
             json.dumps(request, sort_keys=True), now, now),
        )
        connection.commit()
        return {"fresh": True, "steps": [], "receipt": None}
    if row["kind"] != kind or json.loads(row["request_json"]) != request:
        raise SprintError(
            f"operation {operation_id} already names a different "
            f"{row['kind']} request for {row['card_reference']}; "
            "use a new operation id for a new operation"
        )
    return {
        "fresh": False,
        "steps": json.loads(row["steps_json"]),
        "receipt": (
            json.loads(row["receipt_json"])
            if row["receipt_json"] is not None else None
        ),
    }


def record_operation_step(
    connection: sqlite3.Connection, operation_id: str, step: str
) -> None:
    """Append one completed step to the journal, exactly once."""
    row = connection.execute(
        "SELECT steps_json FROM operation_journal WHERE operation_id = ?",
        (operation_id,),
    ).fetchone()
    if row is None:
        raise SprintError(f"operation {operation_id} was never claimed")
    steps = json.loads(row["steps_json"])
    if step in steps:
        return
    steps.append(step)
    connection.execute(
        "UPDATE operation_journal SET steps_json = ?, updated_at = ?"
        " WHERE operation_id = ?",
        (json.dumps(steps),
         datetime.now(UTC).isoformat(timespec="seconds"), operation_id),
    )
    connection.commit()


def complete_operation(
    connection: sqlite3.Connection,
    operation_id: str,
    receipt: dict[str, object],
) -> None:
    """Store the receipt that marks the operation finished."""
    connection.execute(
        "UPDATE operation_journal SET receipt_json = ?, updated_at = ?"
        " WHERE operation_id = ?",
        (json.dumps(receipt, sort_keys=True),
         datetime.now(UTC).isoformat(timespec="seconds"), operation_id),
    )
    connection.commit()


def validate_plan(
    connection: sqlite3.Connection,
    sprint_id: int,
    title: str,
    goal: str,
    acceptance: tuple[str, ...],
    alias: str | None,
) -> str | None:
    """Check a plan against the register; return the alias it will keep."""
    if (
        not title.strip() or not goal.strip() or not acceptance
        or any(not item.strip() for item in acceptance)
    ):
        raise SprintError(
            "planned sprints require a title, goal, and at least one "
            "acceptance criterion, none of them blank"
        )
    existing = fetch_sprint(connection, sprint_id)
    if existing is not None and existing.status != STATUS_PLANNED:
        raise SprintError(
            f"sprint {sprint_id} is {existing.status} and cannot be replanned"
        )
    if alias is None and existing is not None:
        alias = existing.alias
    validate_alias(connection, sprint_id, alias)
    return alias


def plan_description(
    title: str, goal: str, execution: str, acceptance: tuple[str, ...],
) -> str:
    """The sprint plan as Markdown, as its Plane cycle describes it."""
    sections = [f"# {title.strip()}", f"## Goal\n\n{goal.strip()}"]
    if execution.strip():
        sections.append(f"## Execution\n\n{execution.strip()}")
    criteria = "\n".join(f"- {item.strip()}" for item in acceptance)
    sections.append(f"## Acceptance criteria\n\n{criteria}")
    return "\n\n".join(sections)


def require_unbound_cycle(
    connection: sqlite3.Connection, sprint_id: int, cycle_id: str,
) -> None:
    """Refuse a `Sprint N` cycle the register binds to another sprint."""
    row = connection.execute(
        "SELECT sprint_id, status FROM sprints "
        "WHERE cycle_id = ? AND sprint_id != ?",
        (cycle_id, sprint_id),
    ).fetchone()
    if row is not None:
        raise SprintCycleBound(
            f"Sprint cycle rule: Plane cycle {cycle_id} is named for sprint "
            f"{sprint_id}, but the register binds it to {row[1]} sprint "
            f"{row[0]}. Rename cycle {cycle_id} back to `Sprint {row[0]}` "
            f"in Plane, then plan sprint {sprint_id} again."
        )


def require_adoptable_cycle(
    connection: sqlite3.Connection, sprint_id: int, cycle_id: str,
    actual: object, description: str,
) -> None:
    """Refuse to adopt a `Sprint N` cycle that holds someone else's plan.

    A sprint already in the register owns its cycle. A fresh plan adopts an
    existing cycle only when it is blank or already holds this exact plan,
    which is the retry after a failed register write.
    """
    if fetch_sprint(connection, sprint_id) is not None:
        return
    text = "" if actual is None else str(actual)
    if text.strip() and text != description:
        raise _cycle_taken(sprint_id, cycle_id)


def require_unplanned(
    connection: sqlite3.Connection, sprint_id: int, cycle_id: str,
) -> None:
    """Refuse when another planner recorded sprint N during this plan."""
    if fetch_sprint(connection, sprint_id) is not None:
        raise _cycle_taken(sprint_id, cycle_id)


def _cycle_taken(sprint_id: int, cycle_id: str) -> SprintCycleTaken:
    return SprintCycleTaken(
        f"Sprint cycle rule: Plane cycle {cycle_id} is named for sprint "
        f"{sprint_id} and holds a different plan, but this register had no "
        f"sprint {sprint_id}. Either another register (perhaps another git "
        "branch) or planner planned it, or its description was rewritten or "
        "a previous plan attempt failed. Merge the other register, or plan "
        "under an unused sprint number; if this plan should win, re-run "
        "`sprints plan` with --adopt-cycle."
    )


def plan_sprint(
    connection: sqlite3.Connection,
    sprint_id: int,
    title: str,
    position: int,
    goal: str,
    execution: str,
    acceptance: tuple[str, ...],
    alias: str | None = None,
) -> None:
    alias = validate_plan(connection, sprint_id, title, goal, acceptance, alias)
    # `sprints plan` takes the write lock itself to re-check under it.
    if not connection.in_transaction:
        connection.execute("BEGIN IMMEDIATE")
    try:
        validate_alias(connection, sprint_id, alias)
        connection.execute(
            """INSERT INTO sprints
                   (sprint_id, title, status, position, goal, execution,
                    acceptance, alias)
               VALUES (?, ?, 'planned', ?, ?, ?, ?, ?)
               ON CONFLICT(sprint_id) DO UPDATE SET title=excluded.title,
                   position=excluded.position, goal=excluded.goal,
                   execution=excluded.execution, acceptance=excluded.acceptance,
                   alias=excluded.alias""",
            (
                sprint_id, title.strip(), position, goal.strip(), execution.strip(),
                "\n".join(acceptance), alias,
            ),
        )
        _verify_alias(connection, sprint_id, alias)
        connection.commit()
    except (SprintError, sqlite3.Error):
        connection.rollback()
        raise


def reorder_planned_sprints(
    connection: sqlite3.Connection,
    ordered_sprint_ids: tuple[int, ...],
) -> None:
    """Replace the complete planned-sprint order atomically."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        seen: set[int] = set()
        duplicates: set[int] = set()
        for sprint_id in ordered_sprint_ids:
            if sprint_id in seen:
                duplicates.add(sprint_id)
            seen.add(sprint_id)

        planned_ids = {
            row[0]
            for row in connection.execute(
                "SELECT sprint_id FROM sprints WHERE status = 'planned'"
            )
        }
        problems = []
        if duplicates:
            duplicate_list = ", ".join(str(item) for item in sorted(duplicates))
            problems.append(f"duplicate: {duplicate_list}")
        missing = planned_ids - seen
        if missing:
            missing_list = ", ".join(str(item) for item in sorted(missing))
            problems.append(f"missing: {missing_list}")
        not_planned = seen - planned_ids
        if not_planned:
            invalid_list = ", ".join(str(item) for item in sorted(not_planned))
            problems.append(f"not planned: {invalid_list}")
        if problems:
            raise SprintError(f"Sprint reorder rule: {'; '.join(problems)}")

        for position, sprint_id in enumerate(ordered_sprint_ids, start=1):
            connection.execute(
                "UPDATE sprints SET position = ? WHERE sprint_id = ?",
                (position, sprint_id),
            )

        actual = connection.execute(
            "SELECT sprint_id, position FROM sprints "
            "WHERE status = 'planned' ORDER BY position, sprint_id"
        ).fetchall()
        expected = [
            (sprint_id, position)
            for position, sprint_id in enumerate(ordered_sprint_ids, start=1)
        ]
        if [tuple(row) for row in actual] != expected:
            raise SprintError("Sprint reorder rule: position readback failed")
        connection.commit()
    except (SprintError, sqlite3.Error):
        connection.rollback()
        raise


def add_completed_sprint(connection: sqlite3.Connection, sprint: Sprint) -> None:
    """Import one already-completed sprint without inventing a current phase."""
    if sprint.status != STATUS_COMPLETED or sprint.started is None or sprint.ended is None:
        raise SprintError("imported sprint must be completed with start and end timestamps")
    if any(value is None for value in (
        sprint.hours, sprint.cards_start, sprint.cards_end, sprint.points_start,
        sprint.points_end, sprint.velocity, sprint.delivered,
    )):
        raise SprintError("imported sprint requires complete closure accounting")
    started = to_utc_timestamp(sprint.started)
    ended = to_utc_timestamp(sprint.ended)
    if parse_timestamp(ended) <= parse_timestamp(started):
        raise SprintError("ended must be later than started")
    if not sprint.title.strip() or not (sprint.delivered or "").strip():
        raise SprintError("title and delivered must not be blank")
    connection.execute("BEGIN IMMEDIATE")
    try:
        validate_alias(connection, sprint.sprint_id, sprint.alias)
        connection.execute(
            """INSERT INTO sprints (
                   sprint_id, title, status, started, ended, hours, cards_start, cards_end,
                   points_start, points_end, velocity, delivered,
                   retrospective, alias
               ) VALUES (?, ?, 'completed', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (sprint.sprint_id, sprint.title.strip(), started, ended, sprint.hours,
             sprint.cards_start, sprint.cards_end, sprint.points_start, sprint.points_end,
             sprint.velocity, sprint.delivered.strip(),
             sprint.retrospective.strip(),
             sprint.alias),
        )
        _verify_alias(connection, sprint.sprint_id, sprint.alias)
        connection.commit()
    except (SprintError, sqlite3.Error) as error:
        connection.rollback()
        raise SprintError(f"cannot add sprint {sprint.sprint_id}: {error}") from error


def validate_sprint_start(
    connection: sqlite3.Connection, sprint_id: int, started: str, cycle_id: str
) -> Sprint:
    """Preflight a start, accepting an identical retry."""
    parse_timestamp(started)
    sprint = fetch_sprint(connection, sprint_id)
    if (
        sprint is not None
        and sprint.status == STATUS_CURRENT
        and sprint.started is not None
        and parse_timestamp(sprint.started) == parse_timestamp(started)
        and sprint.cycle_id == cycle_id
    ):
        return sprint
    if sprint is None or sprint.status != STATUS_PLANNED:
        raise SprintError(f"sprint {sprint_id} is not planned")
    cycle_in_use = connection.execute(
        "SELECT sprint_id FROM sprints WHERE status = 'current' "
        "AND cycle_id = ? AND sprint_id != ?",
        (cycle_id, sprint_id),
    ).fetchone()
    if cycle_in_use is not None:
        raise SprintError(
            f"cycle {cycle_id} is already in use by current sprint "
            f"{cycle_in_use['sprint_id']}"
        )
    return sprint


def start_sprint(
    connection: sqlite3.Connection,
    sprint_id: int,
    started: str,
    cycle_id: str,
    cards_start: int,
    points_start: int,
) -> None:
    started = to_utc_timestamp(started)
    sprint = validate_sprint_start(connection, sprint_id, started, cycle_id)
    if sprint.status == STATUS_CURRENT:
        return
    connection.execute(
        """UPDATE sprints
              SET status = 'current', position = NULL, started = ?, cycle_id = ?,
                  cards_start = ?, points_start = ?
            WHERE sprint_id = ?""",
        (started, cycle_id, cards_start, points_start, sprint_id),
    )
    connection.commit()


def _require_non_negative(name: str, value: int) -> None:
    if value < 0:
        raise SprintError(f"{name} must be non-negative")


def close_sprint(
    connection: sqlite3.Connection,
    sprint_id: int,
    ended: str,
    hours: float,
    cards_start: int,
    cards_end: int,
    points_start: int,
    points_end: int,
    velocity: float,
    delivered: str,
    retrospective: str,
) -> None:
    ended = to_utc_timestamp(ended)
    validate_sprint_close(
        connection, sprint_id, ended, hours, cards_start, cards_end,
        points_start, points_end, velocity, delivered,
    )
    connection.execute(
        """UPDATE sprints SET status='completed', ended=?, hours=?, cards_start=?, cards_end=?,
                  points_start=?, points_end=?, velocity=?, delivered=?, retrospective=?
             WHERE sprint_id=?""",
        (ended, hours, cards_start, cards_end, points_start, points_end, velocity,
         delivered.strip(), retrospective.strip(), sprint_id),
    )
    connection.commit()


def validate_sprint_close(
    connection: sqlite3.Connection,
    sprint_id: int,
    ended: str,
    hours: float,
    cards_start: int,
    cards_end: int,
    points_start: int,
    points_end: int,
    velocity: float,
    delivered: str,
) -> Sprint:
    """Validate every closure fact before the first Plane write."""
    sprint = fetch_sprint(connection, sprint_id)
    if sprint is None or sprint.status != STATUS_CURRENT or sprint.started is None:
        raise SprintError(f"sprint {sprint_id} is not current")
    if parse_timestamp(ended) <= parse_timestamp(sprint.started):
        raise SprintError("ended must be later than started")
    if not math.isfinite(hours) or hours <= 0:
        raise SprintError("hours must be finite and greater than zero")
    if not math.isfinite(velocity) or velocity < 0:
        raise SprintError("velocity must be finite and non-negative")
    for name, value in (("cards start", cards_start), ("cards end", cards_end),
                        ("points start", points_start), ("points end", points_end)):
        _require_non_negative(name, value)
    if not delivered.strip():
        raise SprintError("delivered must not be blank")
    if sprint.cycle_id is None:
        raise SprintError(f"sprint {sprint_id} has no Plane cycle ID")
    return sprint


def derive_close_accounting(
    sprint: Sprint, ended: str, metrics: dict[str, int]
) -> dict[str, float | int]:
    """Derive closure accounting from stored opening facts and live totals.

    Opening membership comes from what start recorded, never from
    today's board. Velocity is delivered Done points per elapsed hour;
    cancelled points are excluded.
    """
    if sprint.started is None:
        raise SprintError(f"sprint {sprint.sprint_id} has no start timestamp")
    if sprint.cards_start is None or sprint.points_start is None:
        raise SprintError(
            f"sprint {sprint.sprint_id} lacks stored opening totals; "
            "legacy imports use sprints add"
        )
    elapsed = parse_timestamp(ended) - parse_timestamp(sprint.started)
    hours = elapsed.total_seconds() / 3600
    if hours <= 0:
        raise SprintError("ended must be later than started")
    return {
        "hours": hours,
        "cards_start": sprint.cards_start,
        "points_start": sprint.points_start,
        # Cancelled members never count in closing totals.
        "cards_end": metrics["cards_current"] - metrics["cards_cancelled"],
        "points_end": metrics["points_current"] - metrics["points_cancelled"],
        "points_done": metrics["points_done"],
        "velocity": metrics["points_done"] / hours,
    }


def preflight_report(
    connection: sqlite3.Connection,
    board: object,
    sprint_id: int,
    *,
    now: str,
) -> dict[str, object]:
    """Closure readiness and derived accounting. Reads only; writes nothing."""
    sprint = fetch_sprint(connection, sprint_id)
    if sprint is None or sprint.status != STATUS_CURRENT:
        raise SprintError(f"sprint {sprint_id} is not current")
    if sprint.cycle_id is None:
        raise SprintError(f"sprint {sprint_id} has no Plane cycle ID")
    cards = board.cycle_cards(sprint.cycle_id)
    settled = {"done", "cancelled"}

    def state_name(card: object) -> str:
        state_id = str(getattr(card, "state", ""))
        for name, known in board.project.states.items():
            if known == state_id:
                return name
        return state_id or "unknown"

    def reference(card: object) -> str:
        return f"{board.project.key}-{getattr(card, 'sequence_id', '?')}"

    nonterminal = [
        {"card": reference(card), "state": state_name(card)}
        for card in cards
        if state_name(card).casefold() not in settled
    ]
    missing_ids = missing_final_snapshots(
        connection, sprint_id, {str(card.id) for card in cards}
    )
    missing = sorted(
        reference(card) for card in cards if str(card.id) in missing_ids
    )
    latest: dict[str, dict[str, object]] = {}
    for snapshot in fetch_execution_snapshots(connection, sprint_id):
        card_id = str(snapshot["work_item_id"])
        if card_id not in latest or parse_timestamp(
            snapshot["captured_at"]
        ) > parse_timestamp(str(latest[card_id]["captured_at"])):
            latest[card_id] = snapshot
    open_timers = [
        {
            "card": snapshot["card_reference"],
            "category": snapshot["stats"]["open_timer"]["category"],
        }
        for snapshot in latest.values()
        if snapshot["stats"].get("open_timer") is not None
    ]
    metrics = board.sprint_cycle_metrics(sprint.cycle_id)
    derived = derive_close_accounting(sprint, now, metrics)
    return {
        "sprint_id": sprint_id,
        "cycle_id": sprint.cycle_id,
        "started": sprint.started,
        "as_of": now,
        "ready": not (nonterminal or missing or open_timers),
        "nonterminal_cards": nonterminal,
        "missing_final_snapshots": missing,
        "open_timers": open_timers,
        "derived": derived,
    }


def format_velocity(value: float) -> str:
    """Up to two decimal digits, without trailing zeros."""
    text = f"{float(value):.2f}".rstrip("0").rstrip(".")
    return text or "0"


def format_number(value: int | float) -> str:
    return str(int(value)) if isinstance(value, int) or value.is_integer() else repr(value)


def format_elapsed(hours: float) -> str:
    minutes = int((Decimal(str(hours)) * 60).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    whole_hours, remaining = divmod(minutes, 60)
    return f"{whole_hours}h {remaining:02d}m"


def current_velocity(started: str, done_points: int, *, now: datetime | None = None) -> float:
    """Return completed points per elapsed hour for an active sprint."""
    began = parse_timestamp(started)
    observed = now or datetime.now(tz=began.tzinfo)
    elapsed_hours = (observed - began).total_seconds() / 3600
    return 0.0 if elapsed_hours <= 0 else done_points / elapsed_hours


def format_duration(hours: float) -> str:
    """Render decimal hours at whole-second precision for statistics."""
    total_seconds = int(
        (Decimal(str(hours)) * Decimal(3600)).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    )
    whole_hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{whole_hours:02d}h {minutes:02d}m {seconds:02d}s"


def format_timestamp(value: str | None) -> str:
    """Render an instant in the machine timezone, including its UTC offset."""
    if value is None:
        return "—"
    return parse_timestamp(value).astimezone().isoformat(
        sep=" ", timespec="seconds"
    )


def _field_table(fields: tuple[tuple[str, object], ...]) -> Table:
    table = Table(box=box.ROUNDED, padding=(0, 1), show_header=False, highlight=False)
    table.add_column("Field", style="bold", no_wrap=True)
    table.add_column("Value", overflow="fold")
    for label, value in fields:
        table.add_row(label, "—" if value in {None, ""} else str(value))
    return table


def _render_active(
    console: Console,
    found: list[Sprint],
    *,
    include_all: bool,
    include_estimates: bool,
    current_metrics: dict[int, dict[str, int | float]],
    timing_fields: dict[int, tuple[tuple[str, object], ...]],
) -> None:
    if not found:
        console.print("No current sprint")
        return
    for index, sprint in enumerate(found):
        if index:
            console.print()
        metrics = current_metrics.get(sprint.sprint_id, {})
        fields = (
            ("Sprint", sprint.sprint_id), ("Alias", sprint.alias),
            ("Title", sprint.title),
            ("Started", format_timestamp(sprint.started)),
            ("Starting cards", sprint.cards_start),
            ("Current cards", metrics.get("cards_current")),
            ("Done cards", metrics.get("cards_done")),
            ("Cancelled cards", metrics.get("cards_cancelled")),
        )
        if include_estimates:
            fields += (
                ("Starting points", sprint.points_start),
                ("Current points", metrics.get("points_current")),
                ("Done points", metrics.get("points_done")),
                ("Cancelled points", metrics.get("points_cancelled")),
                (
                    "Current velocity",
                    f"{format_velocity(float(metrics['velocity']))}/h"
                    if "velocity" in metrics else None,
                ),
            )
        fields += (("Goal", sprint.goal),)
        if include_all:
            fields += (
                ("Execution", sprint.execution),
                ("Acceptance", sprint.acceptance),
            )
        table_fields = fields + timing_fields.get(sprint.sprint_id, ())
        console.print(_field_table(table_fields))


def _render_planned(
    console: Console,
    found: list[Sprint],
    *,
    include_all: bool,
    include_estimates: bool,
    planned_totals: dict[int, tuple[int, int]],
) -> None:
    if not found:
        console.print("No planned sprints")
        return
    if console.width < 100:
        for sprint in found:
            fields = (
                ("Position", sprint.position), ("Sprint", sprint.sprint_id),
                ("Alias", sprint.alias), ("Title", sprint.title),
                ("Cards", planned_totals.get(sprint.sprint_id, (None, None))[0]),
            )
            if include_estimates:
                fields += ((
                    "Points",
                    planned_totals.get(sprint.sprint_id, (None, None))[1],
                ),)
            fields += (("Goal", sprint.goal),)
            if include_all:
                fields += (("Execution", sprint.execution), ("Acceptance", sprint.acceptance))
            console.print(_field_table(fields))
        return
    table = Table(box=box.ROUNDED, header_style="bold", padding=(0, 0), highlight=False)
    table.add_column("Order", justify="right", no_wrap=True)
    table.add_column("Sprint", justify="right", no_wrap=True)
    table.add_column("Alias", no_wrap=True)
    table.add_column("Title", overflow="fold")
    table.add_column("Cards", justify="right", no_wrap=True)
    if include_estimates:
        table.add_column("Points", justify="right", no_wrap=True)
    table.add_column("Goal", overflow="fold")
    if include_all:
        table.add_column("Execution", overflow="fold")
        table.add_column("Acceptance", overflow="fold")
    for sprint in found:
        cards, points = planned_totals.get(sprint.sprint_id, (None, None))
        values = [
            str(sprint.position), str(sprint.sprint_id),
            sprint.alias or "—", sprint.title,
            "—" if cards is None else str(cards),
        ]
        if include_estimates:
            values.append("—" if points is None else str(points))
        values.append(sprint.goal)
        if include_all:
            values.extend((sprint.execution, sprint.acceptance))
        table.add_row(*values)
    console.print(table)


def _render_past(
    console: Console, found: list[Sprint], *, include_all: bool,
    include_estimates: bool,
    timing_fields: dict[int, tuple[tuple[str, object], ...]],
) -> None:
    if not found:
        console.print("No past sprints")
        return
    if console.width < 100 or any(timing_fields.get(sprint.sprint_id) for sprint in found):
        for sprint in found:
            fields: tuple[tuple[str, object], ...] = (
                ("Sprint", sprint.sprint_id), ("Alias", sprint.alias),
                ("Title", sprint.title),
                ("Cards", f"{sprint.cards_start} → {sprint.cards_end}"),
            )
            if include_estimates:
                fields += (
                    ("Points", f"{sprint.points_start} → {sprint.points_end}"),
                    ("Velocity", f"{format_velocity(sprint.velocity or 0)} /h"),
                )
            fields += (("Time start", format_timestamp(sprint.started)),
                       ("Time end", format_timestamp(sprint.ended)),
                       ("Elapsed", format_elapsed(sprint.hours or 0)))
            if include_all:
                fields += (("Delivered", sprint.delivered),
                           ("Retrospective", sprint.retrospective))
            console.print(_field_table(fields + timing_fields.get(sprint.sprint_id, ())))
        console.print()
        console.print("Statistics", style="bold")
        summary = stats(found)
        if not include_estimates:
            summary = {
                name: values for name, values in summary.items()
                if name not in {"points_start", "points_end", "velocity"}
            }
        render_stats(summary, console=console)
        return
    table = Table(box=box.ROUNDED, header_style="bold", padding=(0, 0), highlight=False)
    table.add_column("Sprint", justify="right", no_wrap=True)
    table.add_column("Alias", no_wrap=True)
    table.add_column("Title", overflow="fold")
    table.add_column("Cards", justify="right", no_wrap=True)
    if include_estimates:
        table.add_column("Points", justify="right", no_wrap=True)
        table.add_column("Velocity", justify="right", no_wrap=True)
    table.add_column("Time start", overflow="fold")
    table.add_column("Time end", overflow="fold")
    table.add_column("Elapsed", justify="right", no_wrap=True)
    if include_all:
        table.add_column("Delivered", overflow="fold")
        table.add_column("Retrospective", overflow="fold")
    for sprint in found:
        values = [
            str(sprint.sprint_id), sprint.alias or "—", sprint.title,
            f"{sprint.cards_start} → {sprint.cards_end}",
        ]
        if include_estimates:
            values.extend((
                f"{sprint.points_start} → {sprint.points_end}",
                f"{format_velocity(sprint.velocity or 0)} /h",
            ))
        values.extend((
            format_timestamp(sprint.started),
            format_timestamp(sprint.ended),
            format_elapsed(sprint.hours or 0),
        ))
        if include_all:
            values.extend((sprint.delivered or "", sprint.retrospective))
        table.add_row(*values)
    console.print(table)
    console.print()
    console.print("Statistics", style="bold")
    summary = stats(found)
    if not include_estimates:
        summary = {
            name: values for name, values in summary.items()
            if name not in {"points_start", "points_end", "velocity"}
        }
    render_stats(summary, console=console)


def render_stats(
    payload: dict[str, dict[str, int | float]], *, console: Console | None = None
) -> None:
    """Render summary statistics without exposing Python dictionary syntax."""
    target = console or Console(highlight=False)
    if not payload:
        target.print("No sprint history")
        return
    labels = {
        "hours": "Hours", "cards_start": "Cards start", "cards_end": "Cards end",
        "points_start": "Points start", "points_end": "Points end", "velocity": "Velocity",
    }
    integer_metrics = {"cards_start", "cards_end", "points_start", "points_end"}

    def formatted(name: str, summary: dict[str, int | float]) -> list[str]:
        if name == "hours":
            return [format_duration(float(summary[key])) for key in (
                "average", "median", "min", "max", "sd"
            )]
        if name == "velocity":
            return [f"{format_velocity(float(summary[key]))}/h" for key in (
                "average", "median", "min", "max", "sd"
            )]
        values = [f"{float(summary['average']):.2f}"]
        for key in ("median", "min", "max"):
            value = summary[key]
            values.append(str(int(value)) if name in integer_metrics else format_number(value))
        values.append(f"{float(summary['sd']):.2f}")
        return values

    headings = ("Avg", "Med", "Min", "Max", "SD")
    table = Table(box=box.ROUNDED, header_style="bold", padding=(0, 0), highlight=False)
    table.add_column("Metric", no_wrap=True)
    for heading in headings:
        table.add_column(heading, justify="right", no_wrap=True)
    for name, summary in payload.items():
        table.add_row(labels[name], *formatted(name, summary))
    target.print(table)


def render_sprint_list(
    found: list[Sprint],
    *,
    include_all: bool,
    include_estimates: bool,
    statuses: frozenset[str] | None,
    planned_totals: dict[int, tuple[int, int]],
    current_metrics: dict[int, dict[str, int | float]],
    timing_fields: dict[int, tuple[tuple[str, object], ...]] | None = None,
) -> None:
    """Render lifecycle sections with the standalone tool's responsive Rich tables."""
    console = Console(highlight=False)
    current_count = len([s for s in found if s.status == STATUS_CURRENT])
    current_heading = (
        "Current sprints" if current_count > 1 else "Current sprint"
    )
    sections = (
        (current_heading, STATUS_CURRENT, _render_active),
        ("Planned sprints", STATUS_PLANNED, _render_planned),
        ("Past sprints", STATUS_COMPLETED, _render_past),
    )
    selected = sections if statuses is None else tuple(
        section for section in sections if section[1] in statuses
    )
    for index, (heading, status, renderer) in enumerate(selected):
        if index:
            console.print()
        console.print(heading, style="bold")
        section_sprints = [sprint for sprint in found if sprint.status == status]
        if status == STATUS_CURRENT:
            renderer(
                console,
                section_sprints,
                include_all=include_all,
                include_estimates=include_estimates,
                current_metrics=current_metrics,
                timing_fields=timing_fields or {},
            )
        elif status == STATUS_PLANNED:
            renderer(
                console,
                section_sprints,
                include_all=include_all,
                include_estimates=include_estimates,
                planned_totals=planned_totals,
            )
        else:
            renderer(
                console, section_sprints, include_all=include_all,
                include_estimates=include_estimates,
                timing_fields=timing_fields or {},
            )


def sprint_record(
    sprint: Sprint,
    *,
    include_all: bool,
    planned_total: tuple[int, int] | None = None,
    current_metrics: dict[str, int | float] | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "sprint": sprint.sprint_id, "status": sprint.status,
        "title": sprint.title, "alias": sprint.alias,
    }
    if sprint.status == STATUS_PLANNED:
        cards, points = planned_total if planned_total is not None else (None, None)
        record.update(position=sprint.position, cards=cards, points=points, goal=sprint.goal)
    elif sprint.status == STATUS_CURRENT:
        record.update(
            started=sprint.started,
            cards_start=sprint.cards_start,
            points_start=sprint.points_start,
            **(current_metrics or {}),
            goal=sprint.goal,
        )
    else:
        record.update(cards=f"{sprint.cards_start} → {sprint.cards_end}",
                      points=f"{sprint.points_start} → {sprint.points_end}",
                      velocity=sprint.velocity, elapsed=format_elapsed(sprint.hours or 0))
    if include_all:
        record.update({name: getattr(sprint, name) for name in Sprint.__dataclass_fields__})
    return record


def metric_summary(values: list[int | float]) -> dict[str, int | float]:
    return {
        "average": statistics.mean(values),
        "median": statistics.median_high(values),
        "min": min(values),
        "max": max(values),
        "sd": statistics.pstdev(values),
    }


def stats(sprints: list[Sprint]) -> dict[str, dict[str, int | float]]:
    completed = [sprint for sprint in sprints if sprint.status == STATUS_COMPLETED]
    if not completed:
        return {}
    names = ("hours", "cards_start", "cards_end", "points_start", "points_end", "velocity")
    return {name: metric_summary([getattr(sprint, name) for sprint in completed]) for name in names}
