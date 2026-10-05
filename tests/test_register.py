"""The register merge driver and dump, on temporary registers (no git)."""

from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from click.testing import CliRunner

from plane_proj import register, sprints
from plane_proj.cli import cli

BINDING = ("https://plane.test", "test", "DEMO")
STARTED = "2026-10-01T09:00:00Z"


def new_register(path: Path, binding=BINDING) -> Path:
    sprints.create_database(path, binding)
    return path


def plan(path: Path, sprint_id: int, position: int = 1, *,
         title: str | None = None, alias: str | None = None) -> None:
    with closing(sprints.connect_database(path, writable=True)) as connection:
        sprints.plan_sprint(
            connection, sprint_id, title or f"Sprint {sprint_id}", position,
            "Ship the outcome", "", ("The gate passes",), alias,
        )


def start(path: Path, sprint_id: int, cycle_id: str) -> None:
    with closing(sprints.connect_database(path, writable=True)) as connection:
        sprints.start_sprint(connection, sprint_id, STARTED, cycle_id, 3, 8)


def snapshot(path: Path, work_item_id: str, *, done: int = 1) -> None:
    with closing(sprints.connect_database(path, writable=True)) as connection:
        sprints.record_execution_snapshot(
            connection, sprint_id=1, work_item_id=work_item_id,
            card_reference=f"DEMO-{work_item_id}",
            captured_at="2026-10-01T10:00:00Z", is_final=False,
            stats={"done": done},
        )


def set_alias(path: Path, sprint_id: int, alias: str) -> None:
    with closing(sprints.connect_database(path, writable=True)) as connection:
        sprints.set_alias(connection, sprint_id, alias)


def branches(tmp_path: Path) -> tuple[Path, Path, Path]:
    base = tmp_path / "base.sqlite"
    ours = tmp_path / "ours.sqlite"
    theirs = tmp_path / "theirs.sqlite"
    shutil.copy(base, ours)
    shutil.copy(base, theirs)
    return base, ours, theirs


def merge(base: Path, ours: Path, theirs: Path):
    return CliRunner().invoke(
        cli, ["register", "merge", str(base), str(ours), str(theirs)]
    )


def titles(path: Path) -> dict[int, str]:
    with closing(sprints.connect_database(path, writable=False)) as connection:
        return {
            sprint.sprint_id: sprint.title
            for sprint in sprints.fetch_sprints(connection)
        }


def refused(result, ours: Path, before: bytes, *needles: str) -> None:
    assert result.exit_code == 1, result.output
    assert "Register merge rule" in result.stderr
    for needle in needles:
        assert needle in result.stderr
    assert ours.read_bytes() == before
    leftovers = {path.name for path in ours.parent.iterdir()}
    assert leftovers <= {"base.sqlite", "ours.sqlite", "theirs.sqlite"}


def test_different_sprints_changed_on_each_branch_merge_cleanly(tmp_path):
    base = new_register(tmp_path / "base.sqlite")
    plan(base, 1, 1)
    plan(base, 2, 2)
    base, ours, theirs = branches(tmp_path)
    plan(ours, 1, 1, title="Ours")
    plan(theirs, 2, 2, title="Theirs")
    plan(theirs, 3, 3)
    with closing(sprints.connect_database(theirs, writable=True)) as connection:
        sprints.claim_operation(connection, "op-1", "transition", "DEMO-1", {})

    result = merge(base, ours, theirs)

    assert result.exit_code == 0, result.output
    assert titles(ours) == {1: "Ours", 2: "Theirs", 3: "Sprint 3"}
    with closing(sprints.connect_database(ours, writable=False)) as connection:
        assert sprints.claim_operation(
            connection, "op-1", "transition", "DEMO-1", {}
        )["fresh"] is False
    assert sprints.read_binding(ours) == ("unused", "test", "DEMO")
    assert {path.name for path in tmp_path.iterdir()} == {
        "base.sqlite", "ours.sqlite", "theirs.sqlite",
    }


def test_snapshots_union(tmp_path):
    base = new_register(tmp_path / "base.sqlite")
    plan(base, 1)
    start(base, 1, "cycle-1")
    snapshot(base, "shared")
    base, ours, theirs = branches(tmp_path)
    snapshot(ours, "ours")
    snapshot(theirs, "theirs")

    assert merge(base, ours, theirs).exit_code == 0

    with closing(sprints.connect_database(ours, writable=False)) as connection:
        stored = sprints.fetch_execution_snapshots(connection, 1)
    assert sorted(row["work_item_id"] for row in stored) == [
        "ours", "shared", "theirs",
    ]


def test_same_snapshot_key_with_different_content_conflicts(tmp_path):
    base = new_register(tmp_path / "base.sqlite")
    plan(base, 1)
    start(base, 1, "cycle-1")
    base, ours, theirs = branches(tmp_path)
    snapshot(ours, "card", done=1)
    snapshot(theirs, "card", done=2)
    before = ours.read_bytes()

    refused(merge(base, ours, theirs), ours, before,
            "card_execution_snapshots 1/card/", "stats_json")


def test_same_sprint_changed_on_both_sides_conflicts(tmp_path):
    base = new_register(tmp_path / "base.sqlite")
    plan(base, 1)
    base, ours, theirs = branches(tmp_path)
    plan(ours, 1, title="Ours")
    plan(theirs, 1, title="Theirs")
    plan(theirs, 2, 2)
    before = ours.read_bytes()

    refused(merge(base, ours, theirs), ours, before,
            "sprints 1", "title: ours 'Ours', theirs 'Theirs'")


def test_alias_collision_conflicts(tmp_path):
    base = new_register(tmp_path / "base.sqlite")
    plan(base, 1, 1)
    plan(base, 2, 2)
    base, ours, theirs = branches(tmp_path)
    set_alias(ours, 1, "AB")
    set_alias(theirs, 2, "AB")
    before = ours.read_bytes()

    refused(merge(base, ours, theirs), ours, before,
            "alias AB: used by sprints 1, 2")


def test_numeric_alias_naming_another_sprint_conflicts(tmp_path):
    base = new_register(tmp_path / "base.sqlite")
    plan(base, 1, 1)
    base, ours, theirs = branches(tmp_path)
    set_alias(ours, 1, "12")
    plan(theirs, 12, 2)
    before = ours.read_bytes()

    refused(merge(base, ours, theirs), ours, before,
            "numeric alias 12 names sprint 12")


def test_planned_position_collision_conflicts(tmp_path):
    base = new_register(tmp_path / "base.sqlite")
    plan(base, 1, 1)
    base, ours, theirs = branches(tmp_path)
    plan(ours, 2, 2)
    plan(theirs, 3, 2)
    before = ours.read_bytes()

    refused(merge(base, ours, theirs), ours, before,
            "position 2: planned sprints 2, 3", "sprints reorder")


def test_position_collision_already_on_a_branch_does_not_block(tmp_path):
    base = new_register(tmp_path / "base.sqlite")
    plan(base, 1, 1)
    plan(base, 2, 1)
    base, ours, theirs = branches(tmp_path)
    plan(theirs, 3, 2)

    assert merge(base, ours, theirs).exit_code == 0
    assert set(titles(ours)) == {1, 2, 3}


def test_one_cycle_per_current_sprint_conflicts(tmp_path):
    base = new_register(tmp_path / "base.sqlite")
    plan(base, 1, 1)
    plan(base, 2, 2)
    base, ours, theirs = branches(tmp_path)
    start(ours, 1, "cycle-1")
    start(theirs, 2, "cycle-1")
    before = ours.read_bytes()

    refused(merge(base, ours, theirs), ours, before,
            "cycle cycle-1: current sprints 1, 2")


@pytest.mark.parametrize("which", ["base", "theirs"])
def test_schema_version_mismatch_is_refused(tmp_path, which):
    base = new_register(tmp_path / "base.sqlite")
    plan(base, 1)
    base, ours, theirs = branches(tmp_path)
    target = {"base": base, "theirs": theirs}[which]
    with closing(sqlite3.connect(target)) as db:
        db.execute("PRAGMA user_version = 8")
    before = ours.read_bytes()

    refused(merge(base, ours, theirs), ours, before,
            f"{which} register", "version 8", "plane-proj sprints migrate")


def test_binding_mismatch_is_refused(tmp_path):
    base = new_register(tmp_path / "base.sqlite")
    ours = tmp_path / "ours.sqlite"
    shutil.copy(base, ours)
    plan(ours, 1)
    theirs = new_register(tmp_path / "theirs.sqlite",
                          ("https://plane.test", "test", "OTHER"))
    before = ours.read_bytes()

    refused(merge(base, ours, theirs), ours, before,
            "register_binding differs", "test/OTHER")


@pytest.mark.parametrize("state", ["missing", "empty"])
def test_missing_or_empty_base_is_an_empty_register(tmp_path, state):
    base = tmp_path / "base.sqlite"
    if state == "empty":
        base.write_bytes(b"")
    ours = new_register(tmp_path / "ours.sqlite")
    theirs = new_register(tmp_path / "theirs.sqlite")
    plan(ours, 1, 1)
    plan(theirs, 2, 2)

    result = merge(base, ours, theirs)

    assert result.exit_code == 0, result.output
    assert titles(ours) == {1: "Sprint 1", 2: "Sprint 2"}


def test_dump_is_deterministic_for_equal_content(tmp_path):
    first = new_register(tmp_path / "first.sqlite")
    plan(first, 1, 1, alias="AB")
    plan(first, 2, 2)
    second = new_register(tmp_path / "second.sqlite")
    plan(second, 2, 2)
    plan(second, 1, 1, alias="AB")
    before = first.read_bytes()

    dumps = [
        CliRunner().invoke(cli, ["register", "dump", str(path)])
        for path in (first, first, second)
    ]

    assert all(result.exit_code == 0 for result in dumps)
    assert len({result.output for result in dumps}) == 1
    assert first.read_bytes() == before
    lines = dumps[0].output.splitlines()
    assert lines[0] == f"schema_version {sprints.SCHEMA_VERSION}"
    rows = lines[lines.index("[sprints]") + 1:][:2]
    assert [json.loads(row)["sprint_id"] for row in rows] == [1, 2]
    assert rows[0] == json.dumps(
        json.loads(rows[0]), sort_keys=True, separators=(",", ":"),
        ensure_ascii=False,
    )


def test_dump_differs_when_content_differs(tmp_path):
    first = new_register(tmp_path / "first.sqlite")
    plan(first, 1)
    second = new_register(tmp_path / "second.sqlite")
    plan(second, 1, title="Renamed")

    outputs = {
        CliRunner().invoke(cli, ["register", "dump", str(path)]).output
        for path in (first, second)
    }
    assert len(outputs) == 2


def raw(path: Path, *statements: str) -> None:
    with closing(sqlite3.connect(path)) as connection:
        for statement in statements:
            connection.execute(statement)
        connection.commit()


def test_snapshot_upserted_on_one_branch_takes_that_branch(tmp_path):
    base = new_register(tmp_path / "base.sqlite")
    plan(base, 1)
    start(base, 1, "cycle-1")
    snapshot(base, "card", done=1)
    base, ours, theirs = branches(tmp_path)
    snapshot(theirs, "card", done=5)

    assert merge(base, ours, theirs).exit_code == 0

    with closing(sprints.connect_database(ours, writable=False)) as connection:
        stored = sprints.fetch_execution_snapshots(connection, 1)
    assert [row["stats"] for row in stored] == [{"done": 5}]


def test_journal_row_changed_on_both_branches_conflicts(tmp_path):
    base = new_register(tmp_path / "base.sqlite")
    with closing(sprints.connect_database(base, writable=True)) as connection:
        sprints.claim_operation(connection, "op-1", "transition", "DEMO-1", {})
    base, ours, theirs = branches(tmp_path)
    for path, step in ((ours, "moved"), (theirs, "commented")):
        with closing(
            sprints.connect_database(path, writable=True)
        ) as connection:
            sprints.record_operation_step(connection, "op-1", step)
    before = ours.read_bytes()

    refused(merge(base, ours, theirs), ours, before,
            "operation_journal op-1: changed on both branches", "steps_json")


def test_delete_against_change_conflicts(tmp_path):
    base = new_register(tmp_path / "base.sqlite")
    plan(base, 1, 1)
    plan(base, 2, 2)
    base, ours, theirs = branches(tmp_path)
    raw(ours, "DELETE FROM sprints WHERE sprint_id = 2")
    plan(theirs, 2, 2, title="Changed")
    before = ours.read_bytes()

    refused(merge(base, ours, theirs), ours, before,
            "sprints 2: changed on both branches: removed on ours")


def test_snapshot_for_a_removed_sprint_fails_the_foreign_key(tmp_path):
    base = new_register(tmp_path / "base.sqlite")
    plan(base, 1)
    start(base, 1, "cycle-1")
    base, ours, theirs = branches(tmp_path)
    raw(ours, "DELETE FROM sprints WHERE sprint_id = 1")
    snapshot(theirs, "card")
    before = ours.read_bytes()

    refused(merge(base, ours, theirs), ours, before,
            "card_execution_snapshots 1/card/2026-10-01T10:00:00+00:00: "
            "references a row missing from sprints")


def test_check_constraint_failure_leaves_ours_unchanged(tmp_path):
    base = new_register(tmp_path / "base.sqlite")
    plan(base, 1)
    base, ours, theirs = branches(tmp_path)
    raw(theirs, "PRAGMA ignore_check_constraints = ON",
        "UPDATE sprints SET title = ' ' WHERE sprint_id = 1")
    before = ours.read_bytes()

    refused(merge(base, ours, theirs), ours, before,
            "sprints 1: CHECK constraint failed")


def test_readback_mismatch_leaves_ours_unchanged(tmp_path, monkeypatch):
    base = new_register(tmp_path / "base.sqlite")
    plan(base, 1)
    base, ours, theirs = branches(tmp_path)
    plan(theirs, 2, 2)
    before = ours.read_bytes()
    real = register._read_register

    def tampered(path, label):
        result = real(path, label)
        if label == "merged":
            result["sprints"].clear()
        return result

    monkeypatch.setattr(register, "_read_register", tampered)
    refused(merge(base, ours, theirs), ours, before, "readback failed")


def test_merge_preserves_the_file_mode(tmp_path):
    base = new_register(tmp_path / "base.sqlite")
    plan(base, 1)
    base, ours, theirs = branches(tmp_path)
    plan(theirs, 2, 2)
    ours.chmod(0o640)

    assert merge(base, ours, theirs).exit_code == 0
    assert ours.stat().st_mode & 0o777 == 0o640


@pytest.mark.parametrize("change", [
    "CREATE TABLE notes (body TEXT)",
    "ALTER TABLE sprints ADD COLUMN extra TEXT",
])
def test_tables_or_columns_outside_the_schema_are_refused(tmp_path, change):
    base = new_register(tmp_path / "base.sqlite")
    plan(base, 1)
    base, ours, theirs = branches(tmp_path)
    raw(theirs, change)
    before = ours.read_bytes()

    refused(merge(base, ours, theirs), ours, before,
            "theirs register", "tables or columns outside")


@pytest.mark.parametrize("content", [
    b"junk",
    b"version https://git-lfs.github.com/spec/v1\noid sha256:00\nsize 1\n",
])
def test_non_sqlite_input_names_the_input(tmp_path, content):
    base = new_register(tmp_path / "base.sqlite")
    base, ours, theirs = branches(tmp_path)
    theirs.write_bytes(content)
    before = ours.read_bytes()

    refused(merge(base, ours, theirs), ours, before,
            "theirs register", "not a readable SQLite register")


def test_binding_added_on_one_branch_is_kept(tmp_path):
    base = tmp_path / "base.sqlite"
    sprints.create_database(base)
    plan(base, 1)
    base, ours, theirs = branches(tmp_path)
    sprints.bind_database(ours, BINDING)
    plan(theirs, 2, 2)

    assert merge(base, ours, theirs).exit_code == 0
    assert sprints.read_binding(ours) == ("unused", "test", "DEMO")
    assert set(titles(ours)) == {1, 2}


def test_git_setup_says_when_git_is_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PLANE_PROJ_CONFIG", raising=False)
    monkeypatch.setattr(register.shutil, "which", lambda name: None)

    result = CliRunner().invoke(cli, ["register", "git-setup"])

    assert result.exit_code == 1
    assert "git is not available" in result.stderr
    assert not (tmp_path / ".gitattributes").exists()
