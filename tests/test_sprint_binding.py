"""Sprint registers must reject the wrong target before reads or writes reach Plane."""

import json
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from plane_proj import sprints
from plane_proj.cli import Context, cli


@pytest.fixture
def target(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # credentials files are read from cwd
    config = tmp_path / "plane-proj.json"
    config.write_text(json.dumps({"defaults": {"workspace": "workspace", "project": "DEMO"}}))
    monkeypatch.setenv("PLANE_PROJ_CONFIG", str(config))
    monkeypatch.setenv("PLANE_API_HOST_URL", "https://plane.example.test")
    monkeypatch.delenv("PLANE_PROJ_PROJECT", raising=False)
    monkeypatch.delenv("PLANE_API_KEY", raising=False)
    return config


def seed_binding(database: Path) -> None:
    sprints.create_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute("""CREATE TABLE IF NOT EXISTS register_binding (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            host TEXT NOT NULL, workspace TEXT NOT NULL, project TEXT NOT NULL
        )""")
        connection.execute(
            "INSERT INTO register_binding VALUES (1, ?, ?, ?)",
            ("https://plane.example.test", "workspace", "DEMO"),
        )


@pytest.mark.parametrize(
    "command",
    [
        ("list", "planned"),
        ("show",),
        (
            "plan",
            "--id",
            "9",
            "--title",
            "Wrong",
            "--position",
            "1",
            "--goal",
            "G",
            "--execution",
            "E",
            "--acceptance",
            "A",
        ),
    ],
)
def test_mismatched_project_refuses_before_requests_and_database_writes(
    target, tmp_path, monkeypatch, command
):
    database = tmp_path / "SPRINTS.sqlite"
    seed_binding(database)
    before = database.read_bytes()
    requests = []
    monkeypatch.setattr(Context, "board", property(lambda self: requests.append("Plane")))
    result = CliRunner().invoke(
        cli, ["--project", "MCPTEST", "sprint", "--database", str(database), *command]
    )
    assert result.exit_code != 0
    assert "binding" in result.output.lower()
    assert "DEMO" in result.output
    assert "MCPTEST" in result.output
    assert requests == []
    assert database.read_bytes() == before


def test_unbound_register_requires_explicit_binding(target, tmp_path):
    database = tmp_path / "SPRINTS.sqlite"
    sprints.create_database(database)
    before = database.read_bytes()
    result = CliRunner().invoke(cli, ["sprint", "--database", str(database), "stats"])
    assert result.exit_code != 0
    assert "bind" in result.output
    assert database.read_bytes() == before


@pytest.mark.parametrize("dimension", ["workspace", "project-env"])
def test_effective_target_mismatch_is_rejected(target, tmp_path, monkeypatch, dimension):
    database = tmp_path / "SPRINTS.sqlite"
    seed_binding(database)
    before = database.read_bytes()
    args = []
    if dimension == "workspace":
        target.write_text(
            json.dumps({"defaults": {"workspace": "another", "project": "DEMO"}}), encoding="utf-8"
        )
    else:
        monkeypatch.setenv("PLANE_PROJ_PROJECT", "OTHER")
    requests = []
    monkeypatch.setattr(Context, "board", property(lambda self: requests.append("Plane")))
    result = CliRunner().invoke(cli, [*args, "sprint", "--database", str(database), "stats"])
    assert result.exit_code != 0
    assert "binding rule" in result.output
    assert requests == []
    assert database.read_bytes() == before


@pytest.mark.parametrize("returned_sprint", [7, 99])
def test_legacy_binding_verifies_cycles_before_migration(
    target, tmp_path, monkeypatch, returned_sprint
):
    database = tmp_path / "SPRINTS.sqlite"
    sprints.create_database(database)
    with sprints.connect_database(database, writable=True) as connection:
        sprints.plan_sprint(connection, 7, "Sprint 7", 1, "Goal", "Work", "Accept")
        sprints.start_sprint(connection, 7, "2026-01-01T00:00:00Z", "cycle-7", 2, 5)
        sprints.record_execution_snapshot(
            connection, sprint_id=7, work_item_id="card-1", card_reference="DEMO-1",
            captured_at="2026-01-01T01:00:00Z", is_final=False,
            stats={"rework_count": 2, "execution_minutes": {"coding": 15}},
        )
        connection.execute("DROP TABLE register_binding")
        connection.execute("PRAGMA user_version = 4")
    before = database.read_bytes()
    requests = []

    class Board:
        def cycle_sprint_id(self, cycle_id):
            requests.append(cycle_id)
            return returned_sprint, None

    monkeypatch.setattr(Context, "board", property(lambda self: Board()))
    result = CliRunner().invoke(cli, ["sprint", "--database", str(database), "bind"])
    assert requests == ["cycle-7"]
    if returned_sprint != 7:
        assert result.exit_code != 0
        assert "binding rule" in result.output
        assert database.read_bytes() == before
    else:
        assert result.exit_code == 0, result.output
        assert sprints.read_binding(database) == ("unused", "workspace", "DEMO")
        with sprints.connect_database(database, writable=False) as connection:
            stored = sprints.fetch_sprint(connection, 7)
            assert stored.cycle_id == "cycle-7"
            assert stored.points_start == 5
            snapshot = sprints.fetch_execution_snapshots(connection, 7)[0]
            assert snapshot["stats"] == {"rework_count": 2, "execution_minutes": {"coding": 15}}
            assert (
                connection.execute("PRAGMA user_version").fetchone()[0]
                == sprints.SCHEMA_VERSION
            )


def test_bind_cannot_replace_existing_owner(target, tmp_path, monkeypatch):
    database = tmp_path / "SPRINTS.sqlite"
    seed_binding(database)
    before = database.read_bytes()
    requests = []
    monkeypatch.setattr(Context, "board", property(lambda self: requests.append("Plane")))
    result = CliRunner().invoke(
        cli, ["--project", "OTHER", "sprint", "--database", str(database), "bind"]
    )
    assert result.exit_code != 0
    assert "binding rule" in result.output
    assert requests == []
    assert database.read_bytes() == before


@pytest.mark.parametrize("endpoint", ["changed", "absent", "env-file"])
def test_endpoint_is_not_register_identity(target, tmp_path, monkeypatch, endpoint):
    database = tmp_path / "SPRINTS.sqlite"
    seed_binding(database)
    with sprints.connect_database(database, writable=True) as connection:
        sprints.plan_sprint(connection, 7, "Retained", 1, "Goal", "Work", "Accept")
    before = database.read_bytes()
    args = []
    if endpoint == "absent":
        monkeypatch.delenv("PLANE_API_HOST_URL")
    elif endpoint == "env-file":
        env_file = tmp_path / "changed.env"
        env_file.write_text("PLANE_API_HOST_URL=https://new.example.test\n")
        args = ["--env-file", str(env_file)]
    else:
        monkeypatch.setenv("PLANE_API_HOST_URL", "https://new.example.test")

    def unexpected_board(self):
        pytest.fail("Local register access constructed Board")

    monkeypatch.setattr(Context, "board", property(unexpected_board))
    for command in ("stats", "bind"):
        result = CliRunner().invoke(cli, [*args, "sprint", "--database", str(database), command])
        assert result.exit_code == 0, result.output
        if command == "bind":
            assert result.output.endswith("to workspace / DEMO.\n")
        assert database.read_bytes() == before
    assert sprints.read_binding(database) == ("https://plane.example.test", "workspace", "DEMO")


@pytest.mark.parametrize(
    "owner", [("workspace", "DEMO"), ("other", "DEMO"), ("workspace", "OTHER")]
)
def test_store_binding_preserves_legacy_owner_and_bytes(tmp_path, owner):
    database = tmp_path / "SPRINTS.sqlite"
    seed_binding(database)
    before = database.read_bytes()
    with sprints.connect_database(database, writable=True) as connection:
        if owner == ("workspace", "DEMO"):
            sprints._store_binding(connection, ("https://new.example.test", *owner))
        else:
            with pytest.raises(sprints.SprintError, match="binding rule"):
                sprints._store_binding(connection, ("https://new.example.test", *owner))
    assert database.read_bytes() == before


@pytest.mark.parametrize("existing_unbound", [False, True])
def test_new_binding_does_not_store_endpoint(tmp_path, existing_unbound):
    database = tmp_path / "SPRINTS.sqlite"
    binding = ("https://new.example.test", "workspace", "DEMO")
    if existing_unbound:
        sprints.create_database(database)
        sprints.bind_database(database, binding)
    else:
        sprints.create_database(database, binding=binding)
    assert sprints.read_binding(database) == ("unused", "workspace", "DEMO")


@pytest.mark.parametrize("endpoint", ["https://new.example.test", None])
def test_bound_register_uses_current_network_credentials(target, tmp_path, monkeypatch, endpoint):
    from plane_proj import board

    database = tmp_path / "SPRINTS.sqlite"
    seed_binding(database)
    before = database.read_bytes()
    monkeypatch.setenv("PLANE_API_KEY", "test-key")
    if endpoint is None:
        monkeypatch.delenv("PLANE_API_HOST_URL")
    else:
        monkeypatch.setenv("PLANE_API_HOST_URL", endpoint)
    observed = []

    def connect(config, credentials, project):
        observed.append(credentials.host)
        raise RuntimeError("synthetic connection boundary")

    monkeypatch.setattr(board, "connect", connect)
    context = Context(str(target), None, False)
    sprints.require_binding(database, (endpoint or "", "workspace", "DEMO"))
    if endpoint is None:
        from plane_proj.guards import ConfigError

        with pytest.raises(ConfigError, match="PLANE_API_HOST_URL"):
            _ = context.board
        assert observed == []
    else:
        with pytest.raises(RuntimeError, match="synthetic connection"):
            _ = context.board
        assert observed == [endpoint]
    assert database.read_bytes() == before
