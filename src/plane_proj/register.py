"""Git integration for the committed sprint register.

The register stays SQLite; git sees it through a merge driver that merges
rows three ways and a textconv that prints rows as sorted JSON lines.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from collections import defaultdict
from contextlib import closing, suppress
from pathlib import Path

from plane_proj.sprints import (
    SCHEMA_SQL,
    SCHEMA_VERSION,
    SprintError,
    allow_register_writes,
    database_uri,
    to_utc_timestamp,
)

DRIVER = "plane-proj-register"
GIT_CONFIG = (
    (f"merge.{DRIVER}.name", "plane-proj sprint register merge"),
    (f"merge.{DRIVER}.driver", "plane-proj register merge %O %A %B"),
    (f"diff.{DRIVER}.textconv", "plane-proj register dump"),
)
# Insertion order satisfies the snapshot foreign key on sprints.
TABLE_KEYS = {
    "register_binding": ("singleton",),
    "sprints": ("sprint_id",),
    "card_execution_snapshots": ("sprint_id", "work_item_id", "captured_at"),
    "operation_journal": ("operation_id",),
}
TIMESTAMP_COLUMNS = {
    "sprints": ("started", "ended"),
    "card_execution_snapshots": ("captured_at",),
    "operation_journal": ("created_at", "updated_at"),
}
# Characters with meaning in a .gitattributes pattern line.
PATTERN_SPECIALS = frozenset('*?[\\!#"')
TEMPORARY_SUFFIX = ".plane-proj-merge.tmp"
REORDER_HINT = (
    "planned order changed on both branches; run "
    "`plane-proj sprints reorder` after resolving"
)

type Row = dict[str, object]
type Table = dict[tuple[object, ...], Row]
type Register = dict[str, Table]


class RegisterMergeError(SprintError):
    """Register merge rule: a merge that cannot be made without a person."""


class RegisterCommitError(SprintError):
    """Register commit rule: every register write is committed at once."""


class RegisterSetupError(SprintError):
    """Register git setup rule: git integration needs a git work tree."""


def _connect_read_only(path: Path) -> sqlite3.Connection:
    try:
        connection = sqlite3.connect(
            database_uri(path, mode="ro"), uri=True
        )
    except sqlite3.Error as error:
        raise SprintError(f"cannot open database {path}: {error}") from error
    connection.row_factory = sqlite3.Row
    return connection


def _shape(connection: sqlite3.Connection) -> dict[str, frozenset[str]]:
    tables = [
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%'"
        )
    ]
    return {
        table: frozenset(
            row[1] for row in connection.execute(f"PRAGMA table_info({table})")
        )
        for table in tables
    }


def _expected_shape() -> dict[str, frozenset[str]]:
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.executescript(SCHEMA_SQL)
        return _shape(connection)


def _read_register(path: Path, label: str) -> Register:
    try:
        with closing(_connect_read_only(path)) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in {9, SCHEMA_VERSION}:
                raise RegisterMergeError(
                    f"Register merge rule: {label} register {path} is schema "
                    f"version {version}, not {SCHEMA_VERSION}; run "
                    "`plane-proj sprints migrate` on each branch, commit, "
                    "and merge again."
                )
            if _shape(connection) != _expected_shape():
                raise RegisterMergeError(
                    f"Register merge rule: {label} register {path} has "
                    "tables or columns outside the version "
                    f"{SCHEMA_VERSION} schema; the driver merges only that "
                    "schema, so remove the extras or merge by hand."
                )
            register: Register = {}
            for table, key in TABLE_KEYS.items():
                rows: Table = {}
                time_cols = TIMESTAMP_COLUMNS.get(table, ())
                for db_row in connection.execute(f"SELECT * FROM {table}"):
                    row = dict(db_row)
                    for col in time_cols:
                        if row.get(col) is not None:
                            with suppress(SprintError):
                                row[col] = to_utc_timestamp(str(row[col]))
                    row_key = tuple(row[column] for column in key)
                    rows[row_key] = row
                register[table] = rows
            return register
    except sqlite3.DatabaseError as error:
        raise RegisterMergeError(
            f"Register merge rule: {label} register {path} is not a "
            f"readable SQLite register ({error}); check that the file "
            "itself, not a pointer to it, is committed."
        ) from error


def _differences(ours: Row | None, theirs: Row | None) -> str:
    if ours is None or theirs is None:
        side = "ours" if ours is None else "theirs"
        return f"removed on {side}, changed on the other side"
    fields = sorted(name for name in ours if ours[name] != theirs.get(name))
    return "; ".join(
        f"{name}: ours {ours[name]!r}, theirs {theirs.get(name)!r}"
        for name in fields
    )


def _three_way(
    table: str, base: Table, ours: Table, theirs: Table,
    conflicts: list[str],
) -> Table:
    merged: Table = {}
    for key in sorted(base.keys() | ours.keys() | theirs.keys()):
        old, mine, other = base.get(key), ours.get(key), theirs.get(key)
        if mine == other:
            chosen = mine
        elif old == mine:
            chosen = other
        elif old == other:
            chosen = mine
        else:
            conflicts.append(
                f"{table} {_key_text(key)}: changed on both branches: "
                + _differences(mine, other)
            )
            continue
        if chosen is not None:
            merged[key] = chosen
    return merged


def _key_text(key: tuple[object, ...]) -> str:
    return "/".join(str(part) for part in key)


def _collisions(
    sprints: Table, field: str, status: str | None
) -> dict[object, frozenset[int]]:
    groups: dict[object, set[int]] = defaultdict(set)
    for row in sprints.values():
        if row[field] is not None and status in (None, row["status"]):
            groups[row[field]].add(int(row["sprint_id"]))
    return {
        value: frozenset(ids) for value, ids in groups.items() if len(ids) > 1
    }


def _invariant_conflicts(
    merged: Table, ours: Table, theirs: Table
) -> list[str]:
    conflicts = [
        f"sprints alias {alias}: used by sprints {_ids(ids)}"
        for alias, ids in sorted(_collisions(merged, "alias", None).items())
    ]
    conflicts.extend(
        f"sprints {row['sprint_id']}: numeric alias {row['alias']} names "
        f"sprint {row['alias']}"
        for row in merged.values()
        if str(row["alias"] or "").isdigit()
        and int(str(row["alias"])) != row["sprint_id"]
        and (int(str(row["alias"])),) in merged
    )
    # A collision already on one branch is that branch's state, not a merge.
    existing = set(_collisions(ours, "position", "planned").values()) | set(
        _collisions(theirs, "position", "planned").values()
    )
    conflicts.extend(
        f"sprints position {position}: planned sprints {_ids(ids)}; "
        + REORDER_HINT
        for position, ids in sorted(
            _collisions(merged, "position", "planned").items()
        )
        if ids not in existing
    )
    conflicts.extend(
        f"sprints cycle {cycle}: current sprints {_ids(ids)} share one cycle"
        for cycle, ids in sorted(
            _collisions(merged, "cycle_id", "current").items()
        )
    )
    return conflicts


def _ids(ids: frozenset[int]) -> str:
    return ", ".join(str(item) for item in sorted(ids))


def merge_registers(base: Path, ours: Path, theirs: Path) -> None:
    """Merge THEIRS into OURS, or raise leaving OURS byte-for-byte intact."""
    mine = _read_register(ours, "ours")
    other = _read_register(theirs, "theirs")
    empty: Register = {table: {} for table in TABLE_KEYS}
    has_base = base.is_file() and base.stat().st_size > 0
    old = _read_register(base, "base") if has_base else empty
    merged: Register = {"register_binding": _merged_binding(
        {"base": old, "ours": mine, "theirs": other}
    )}
    conflicts: list[str] = []
    for table in list(TABLE_KEYS)[1:]:
        merged[table] = _three_way(
            table, old[table], mine[table], other[table], conflicts
        )
    conflicts.extend(
        _invariant_conflicts(
            merged["sprints"], mine["sprints"], other["sprints"]
        )
    )
    _refuse(conflicts)
    _write_atomically(ours, merged)


def _merged_binding(registers: dict[str, Register]) -> Table:
    """One project binding, which a branch may add but never change."""
    bound = {
        label: register["register_binding"]
        for label, register in registers.items()
        if register["register_binding"]
    }
    if len({json.dumps(list(table.values()), sort_keys=True)
            for table in bound.values()}) > 1:
        described = "; ".join(
            f"{label} {_binding_text(registers[label]['register_binding'])}"
            for label in registers
        )
        raise RegisterMergeError(
            f"Register merge rule: register_binding differs ({described}); "
            "a register belongs to one workspace project, so these "
            "registers cannot be merged."
        )
    return next(iter(bound.values()), {})


def _binding_text(table: Table) -> str:
    rows = list(table.values())
    if not rows:
        return "unbound"
    return "/".join(str(rows[0][name]) for name in ("workspace", "project"))


def _refuse(conflicts: list[str]) -> None:
    if conflicts:
        raise RegisterMergeError(
            f"Register merge rule: {len(conflicts)} conflict(s); OURS is "
            "unchanged. Resolve them with plane-proj sprints commands on "
            "one branch, then merge again.\n"
            + "\n".join(f"  {conflict}" for conflict in conflicts)
        )


def _write_atomically(ours: Path, merged: Register) -> None:
    # A fixed name beside OURS, so a crash leftover is easy to spot.
    temporary = ours.with_name(ours.name + TEMPORARY_SUFFIX)
    temporary.unlink(missing_ok=True)
    try:
        _build(temporary, merged)
        shutil.copymode(ours, temporary)
        descriptor = os.open(temporary, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        temporary.replace(ours)
    finally:
        temporary.unlink(missing_ok=True)


def _build(path: Path, merged: Register) -> None:
    conflicts: list[str] = []
    with closing(sqlite3.connect(path)) as connection:
        allow_register_writes(connection)
        connection.executescript(SCHEMA_SQL)
        for table, rows in merged.items():
            for key, row in sorted(rows.items()):
                columns = ", ".join(row)
                marks = ", ".join("?" for _ in row)
                try:
                    connection.execute(
                        f"INSERT INTO {table} ({columns}) VALUES ({marks})",
                        tuple(row.values()),
                    )
                except sqlite3.IntegrityError as error:
                    conflicts.append(f"{table} {_key_text(key)}: {error}")
        connection.commit()
        conflicts.extend(
            _foreign_key_conflict(connection, table, rowid, parent)
            for table, rowid, parent, _ in connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
        )
        integrity = [
            row[0] for row in connection.execute("PRAGMA integrity_check")
        ]
        if integrity != ["ok"]:
            conflicts.append(f"integrity_check: {'; '.join(integrity)}")
    _refuse(conflicts)
    if _read_register(path, "merged") != merged:
        raise RegisterMergeError(
            "Register merge rule: merged register readback failed; OURS is "
            "unchanged."
        )


def _foreign_key_conflict(
    connection: sqlite3.Connection, table: str, rowid: int, parent: str
) -> str:
    columns = ", ".join(TABLE_KEYS[table])
    key = connection.execute(
        f"SELECT {columns} FROM {table} WHERE rowid = ?", (rowid,)
    ).fetchone()
    return (
        f"{table} {_key_text(tuple(key))}: references a row missing from "
        f"{parent} (removed on one branch, used on the other)"
    )


def dump_register(path: Path) -> str:
    """Render every table as sorted compact JSON lines for `git diff`."""
    with closing(_connect_read_only(path)) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        lines = [f"schema_version {version}"]
        tables = [
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        for table in tables:
            info = connection.execute(f"PRAGMA table_info({table})").fetchall()
            key = [
                row["name"] for row in sorted(info, key=lambda row: row["pk"])
                if row["pk"] > 0
            ] or [row["name"] for row in info]
            rows = [
                dict(row)
                for row in connection.execute(f"SELECT * FROM {table}")
            ]
            rows.sort(key=lambda row: [(row[name] is None, row[name])
                                       for name in key])
            lines.append(f"[{table}]")
            lines.extend(
                json.dumps(row, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":"))
                for row in rows
            )
    return "\n".join(lines) + "\n"


def _git(directory: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments], cwd=directory, capture_output=True, text=True,
        check=False,
    )


def commit_register(register: Path, command: str) -> bool:
    """Commit only the register when this command changed it.

    An uncommitted register is one `git checkout` away from losing writes
    Plane has already accepted, so each write command commits it at once.
    A register outside git, or untracked, is left alone. Returns whether
    a commit was made.
    """
    directory = register.parent
    if shutil.which("git") is None or not directory.is_dir():
        return False
    if _git(directory, "ls-files", "--error-unmatch", "--",
            register.name).returncode != 0:
        return False
    status = _git(directory, "status", "--porcelain", "--", register.name)
    if status.returncode != 0 or not status.stdout.strip():
        return False
    committed = _git(
        directory, "commit", "--only",
        "-m", f"chore(plane): record `{command}` in sprint register",
        "-m", "plane-proj commits each register write immediately so no "
        "uncommitted register change can be reverted out from under the "
        "board. Verification: plane-proj read back its Plane writes before "
        "this commit.",
        "--", register.name,
    )
    if committed.returncode != 0:
        detail = (committed.stderr or committed.stdout).strip()
        raise RegisterCommitError(
            f"Register commit rule: {register} was written but git refused "
            f"to commit it: {detail}. Commit it now with `git commit --only "
            f"{register}`; never checkout, restore or stash it."
        )
    return True


def _work_tree(directory: Path) -> Path:
    if shutil.which("git") is None:
        raise RegisterSetupError(
            "Register git setup rule: git is not available on PATH; install "
            "it, then run `plane-proj register git-setup`."
        )
    inside = _git(directory, "rev-parse", "--is-inside-work-tree")
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        raise RegisterSetupError(
            f"Register git setup rule: {directory} is not inside a git work "
            "tree; run `git init` first, then `plane-proj register git-setup`."
        )
    top = _git(directory, "rev-parse", "--show-toplevel").stdout.strip()
    return Path(top).resolve()


def _pattern(root: Path, register: Path) -> str:
    if not register.is_file():
        raise RegisterSetupError(
            f"Register git setup rule: no register at {register}; run from "
            "the project directory, or set state_file, so the path names "
            "the committed register."
        )
    absolute = register.resolve()
    if not absolute.is_relative_to(root):
        raise RegisterSetupError(
            f"Register git setup rule: {register} is outside the work tree "
            f"{root}."
        )
    pattern = absolute.relative_to(root).as_posix()
    if any(c.isspace() or c in PATTERN_SPECIALS for c in pattern):
        raise RegisterSetupError(
            f"Register git setup rule: {pattern!r} contains whitespace or a "
            ".gitattributes pattern character; add its line by hand."
        )
    return pattern


def _attributes_wired(root: Path, pattern: str) -> bool:
    result = _git(root, "check-attr", "merge", "diff", "--", pattern)
    values = {
        line.rsplit(": ", 1)[-1] for line in result.stdout.splitlines()
    }
    return result.returncode == 0 and values == {DRIVER}


def setup_git(directory: Path, register: Path) -> list[str]:
    """Wire the driver and textconv for REGISTER; return what changed."""
    root = _work_tree(directory)
    pattern = _pattern(root, register)
    changes: list[str] = []
    line = f"{pattern} merge={DRIVER} diff={DRIVER}"
    gitattributes = root / ".gitattributes"
    existing = (
        gitattributes.read_text(encoding="utf-8")
        if gitattributes.exists() else ""
    )
    if line not in existing.splitlines() and not _attributes_wired(
        root, pattern
    ):
        separator = "\n" if existing and not existing.endswith("\n") else ""
        gitattributes.write_text(
            f"{existing}{separator}{line}\n", encoding="utf-8"
        )
        changes.append(f".gitattributes: {pattern}")
    for name, value in GIT_CONFIG:
        if _git(root, "config", "--local", "--get", name).stdout.strip() \
                != value:
            _git(root, "config", "--local", name, value)
            changes.append(f"git config {name}")
    _verify_setup(root, pattern)
    return changes


def _verify_setup(root: Path, pattern: str) -> None:
    problems = [
        name for name, value in GIT_CONFIG
        if _git(root, "config", "--local", "--get", name).stdout.strip()
        != value
    ]
    if not _attributes_wired(root, pattern):
        problems.append(
            f"attributes for {pattern} (check .git/info/attributes and "
            ".gitattributes for an overriding line such as `binary`)"
        )
    if problems:
        raise RegisterSetupError(
            "Register git setup rule: readback failed for "
            + "; ".join(problems)
        )
