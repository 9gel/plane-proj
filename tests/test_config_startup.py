"""Startup must work with only caller defaults and credentials on disk."""

import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from plane.errors import HttpError

from plane_proj import sprints
from plane_proj.board import connect
from plane_proj.cli import Context, cli
from plane_proj.config import load_config
from plane_proj.credentials import (
    Credentials,
    Secret,
    create_credentials_file,
    load_credentials,
    load_env_file,
)
from plane_proj.guards import ConfigError, ScaleContradiction
from tests.conftest import AUTOMATION, Card, FakeClient, Recorder


@pytest.fixture(autouse=True)
def plane_directory(tmp_path):
    """The default layout keeps config and register under plane/."""
    (tmp_path / "plane").mkdir()


def test_default_config_does_not_require_server_metadata(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PLANE_PROJ_CONFIG", raising=False)
    (tmp_path / "plane" / "plane-proj.json").write_text(json.dumps({
        "defaults": {"workspace": "example-workspace", "project": "DEMO"},
        "estimate_points": {"3": "point-three"},
    }), encoding="utf-8")

    config = load_config()

    assert config.workspace_slug == "example-workspace"
    assert config.project(None).estimate_uuid(3) == "point-three"
    assert config.members == {}
    assert config.state_file is None


def test_sprints_init_subcommand_is_removed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "plane" / "plane-proj.json").write_text(
        json.dumps({"state_file": "history.sqlite",
                    "defaults": {"workspace": "test", "project": "DEMO"}}), encoding="utf-8"
    )

    monkeypatch.setenv("PLANE_API_HOST_URL", "https://plane.test")
    result = CliRunner().invoke(cli, ["sprints", "init"])

    assert result.exit_code == 2
    assert "No such command 'init'" in result.output
    assert not (tmp_path / "plane" / "history.sqlite").exists()
    assert not (tmp_path / "plane" / "SPRINTS.sqlite").exists()


def test_configured_state_file_controls_default_sprint_database(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "plane" / "plane-proj.json").write_text(
        json.dumps({
            "state_file": "history.sqlite",
            "defaults": {"workspace": "test", "project": "DEMO"},
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("PLANE_API_HOST_URL", "https://plane.test")
    sprints.create_database(
        tmp_path / "plane" / "history.sqlite", ("https://plane.test", "test", "DEMO")
    )

    result = CliRunner().invoke(cli, ["sprints", "stats"])

    assert result.exit_code == 0, result.output
    assert not (tmp_path / "plane" / "SPRINTS.sqlite").exists()


def test_init_rejects_a_register_bound_to_another_project_before_plane(
    tmp_path, monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PLANE_API_HOST_URL", "https://plane.test")
    monkeypatch.setenv("PLANE_API_KEY", "secret")
    monkeypatch.setenv("PLANE_WORKSPACE_SLUG", "workspace")
    database = tmp_path / "plane" / "SPRINTS.sqlite"
    sprints.create_database(database, ("https://plane.test", "workspace", "OTHER"))
    before = database.read_bytes()

    def unexpected_discovery(*args, **kwargs):
        pytest.fail("A binding conflict must refuse before the first Plane request")

    monkeypatch.setattr("plane_proj.cli.board_module.discover", unexpected_discovery)
    result = CliRunner().invoke(cli, ["init", "--project", "DEMO"])

    assert result.exit_code != 0
    assert "binding rule" in result.output
    assert database.read_bytes() == before
    assert not (tmp_path / "plane" / "plane-proj.json").exists()
    assert not (tmp_path / ".env_plane").exists()


def test_sprint_start_requires_an_explicit_state_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "plane" / "plane-proj.json").write_text("{}", encoding="utf-8")

    result = CliRunner().invoke(
        cli, ["sprints", "start", "cycle-7", "--started", "2026-01-02T03:04:05+08:00"]
    )

    assert result.exit_code == 1
    assert "state_file" in result.output
    assert "--database" in result.output


def test_plane_env_is_selected_instead_of_dotenv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in ("PLANE_API_HOST_URL", "PLANE_WORKSPACE_SLUG", "PLANE_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / ".env").write_text(
        "PLANE_API_HOST_URL=http://generic\nPLANE_WORKSPACE_SLUG=example-workspace\n"
        "PLANE_API_KEY=generic-key\n", encoding="utf-8",
    )
    (tmp_path / ".env_plane").write_text(
        "PLANE_API_HOST_URL='http://plane' # comment\nPLANE_API_KEY=plane-key\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="workspace"):
        load_credentials()
    credentials = load_credentials("specific-workspace")

    assert credentials.host == "http://plane"
    assert credentials.workspace_slug == "specific-workspace"
    assert credentials.api_key.reveal() == "plane-key"


def test_credentials_file_creation_never_deletes_an_existing_file(tmp_path, monkeypatch):
    target = tmp_path / ".env_plane"
    target.write_text("existing\n", encoding="utf-8")
    monkeypatch.setenv("PLANE_API_HOST_URL", "https://plane.test")
    monkeypatch.setenv("PLANE_API_KEY", "secret")
    monkeypatch.setenv("PLANE_WORKSPACE_SLUG", "workspace")

    with pytest.raises(FileExistsError):
        create_credentials_file(target)

    assert target.read_text(encoding="utf-8") == "existing\n"


@pytest.fixture
def live_client(monkeypatch):
    """The real connection code reads names through SDK-shaped resources."""
    client = FakeClient()
    client.projects = Recorder(client.calls, "projects", result=[
        SimpleNamespace(id="live-project", identifier="DEMO", name="Live project",
                        cycle_view=True, module_view=True),
        SimpleNamespace(id="other-project", identifier="OTHER", name="Another project"),
    ])
    client.workspaces = Recorder(client.calls, "workspaces", result=[
        SimpleNamespace(id=AUTOMATION, display_name="live-member"),
    ])
    for kind, name, uuid in [
        ("states", "Ready", "live-state"), ("cycles", "Sprint new", "live-cycle"),
        ("modules", "New module", "live-module"), ("labels", "New label", "live-label"),
    ]:
        setattr(client, kind, Recorder(client.calls, kind, result=[
            SimpleNamespace(id=uuid, name=name, group="backlog"),
        ]))
    monkeypatch.setattr("plane_proj.board.PlaneClient", lambda **kwargs: client)
    return client


def test_connection_resolves_all_metadata_live(config_path, live_client):
    before = config_path.read_bytes()
    board = connect(load_config(config_path), Credentials("http://plane", Secret("key"), "w"), None)
    assert board.project.id == "live-project"
    assert board.project.state_id("Ready") == "live-state"
    assert board.project.cycle_id("Sprint new") == "live-cycle"
    assert board.project.module_id("New module") == "live-module"
    assert board.project.label_id("New label") == "live-label"
    assert board.config.member_id("live-member") == AUTOMATION
    assert board.project.states_outside_cycles == {"Ready"}
    assert board.project.estimate_uuid(3) == "uuid-3"
    assert not live_client.named("work_items._get"), "ordinary startup must not scan scale cards"
    assert config_path.read_bytes() == before


def test_project_override_never_reuses_default_project_scale(config_path, live_client):
    live_client.estimates.retrieve = lambda *a, **k: SimpleNamespace(id="other-estimate")
    live_client.estimates.list_points = lambda *a, **k: [SimpleNamespace(id="other-3", value="3")]
    board = connect(
        load_config(config_path), Credentials("http://plane", Secret("key"), "w"), "OTHER",
    )
    assert board.project.id == "other-project"
    assert board.project.estimate_uuid(3) == "other-3"


def test_missing_project_cannot_write_to_any_board(config_path, live_client):
    with pytest.raises(ConfigError, match="No project 'NOPE'"):
        connect(load_config(config_path), Credentials("http://plane", Secret("key"), "w"), "NOPE")
    assert {name for name, _, _ in live_client.calls} <= {"projects.list", "workspaces.get_members"}


def test_cli_reads_credentials_beside_explicit_config(config_path, live_client, monkeypatch):
    for key in ("PLANE_API_HOST_URL", "PLANE_API_KEY", "PLANE_WORKSPACE_SLUG"):
        monkeypatch.delenv(key, raising=False)
    config_path.with_name(".env_plane").write_text(
        "PLANE_API_HOST_URL=http://plane\nPLANE_API_KEY=not-a-secret\n", encoding="utf-8",
    )
    result = CliRunner().invoke(cli, ["--conf", str(config_path), "--json", "project", "states"])
    assert result.exit_code == 0, result.output
    assert "live-state" in result.output
    assert "not-a-secret" not in result.output


def test_process_environment_and_explicit_env_file_precedence(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env_plane").write_text("PLANE_API_KEY=file-key\n", encoding="utf-8")
    monkeypatch.setenv("PLANE_API_KEY", "exported-key")
    monkeypatch.setenv("PLANE_API_HOST_URL", "http://exported")
    assert load_credentials("w").api_key.reveal() == "exported-key"
    explicit = tmp_path / "explicit.env"
    explicit.write_text("PLANE_API_KEY=explicit-key\n", encoding="utf-8")
    credentials = load_credentials("w", env_file=explicit)
    assert credentials.api_key.reveal() == "explicit-key"
    assert credentials.host == "http://exported"


def test_scale_write_merges_only_estimates(board, client, config_path, monkeypatch):
    client.expanded = [{
        "id": "dummy", "name": "Estimate 8", "sequence_id": 18, "point": None,
        "estimate_point": {"id": "point-eight", "value": "8", "key": 4},
    }]
    monkeypatch.setattr(Context, "board", property(lambda self: board))
    result = CliRunner().invoke(cli, ["project", "scale", "--write"])
    assert result.exit_code == 0, result.output
    document = json.loads(config_path.read_text(encoding="utf-8"))
    assert set(document) == {"defaults", "estimate_points"}
    assert document["estimate_points"]["8"] == "point-eight"
    assert document["estimate_points"]["22"] == "uuid-22"
    assert "Estimate 8" in result.output


def test_conflicting_scale_evidence_leaves_config_untouched(
    board, client, config_path, monkeypatch,
):
    before = config_path.read_bytes()
    client.expanded = [
        {"estimate_point": {"id": uuid, "value": "3"}} for uuid in ("first", "second")
    ]
    monkeypatch.setattr(Context, "board", property(lambda self: board))
    result = CliRunner().invoke(cli, ["project", "scale", "--write"])
    assert isinstance(result.exception, ScaleContradiction)
    assert config_path.read_bytes() == before


def test_scale_write_cannot_change_project_scope(board, client, config_path, monkeypatch):
    document = json.loads(config_path.read_text(encoding="utf-8"))
    document["defaults"]["project"] = "OTHER"
    config_path.write_text(json.dumps(document), encoding="utf-8")
    before = config_path.read_bytes()
    client.expanded = [{"estimate_point": {"id": "point-three", "value": "3"}}]
    monkeypatch.setattr(Context, "board", property(lambda self: board))
    result = CliRunner().invoke(cli, ["project", "scale", "--write"])
    assert result.exit_code != 0
    assert "differs from defaults.project" in result.output
    assert config_path.read_bytes() == before


@pytest.mark.parametrize("cycles,modules", [(True, True), (False, True), (True, False)])
def test_init_writes_explicit_rules(tmp_path, live_client, monkeypatch, cycles, modules):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PLANE_API_HOST_URL", "http://plane")
    monkeypatch.setenv("PLANE_API_KEY", "key")
    monkeypatch.setenv("PLANE_WORKSPACE_SLUG", "w")
    live_client.projects = Recorder(live_client.calls, "projects", result=[
        SimpleNamespace(id="live-project", identifier="DEMO", name="Live project",
                        cycle_view=cycles, module_view=modules),
    ])
    def absent(*args, **kwargs):
        raise HttpError("absent", status_code=404)
    live_client.estimates.retrieve = absent
    live_client.expanded = [{"estimate_point": {"id": "u3", "value": "3"}}]
    result = CliRunner().invoke(cli, ["init", "--project", "DEMO"])
    assert result.exit_code == 0, result.output
    assert load_env_file(tmp_path / ".env_plane") == {
        "PLANE_API_HOST_URL": "http://plane",
        "PLANE_API_KEY": "key",
        "PLANE_WORKSPACE_SLUG": "w",
    }
    assert (tmp_path / ".env_plane").stat().st_mode & 0o077 == 0
    assert json.loads((tmp_path / "plane" / "plane-proj.json").read_text(encoding="utf-8")) == {
        "defaults": {"workspace": "w", "project": "DEMO",
                     "web_url": "https://app.plane.so"},
        "state_file": "SPRINTS.sqlite",
        "estimate_points": {"3": "u3"},
        "rules": {
            "require_cycle": cycles,
            "require_module": modules,
            "require_estimate": True,
            "cycle_estimate_max": None,
            "wip_limit": None,
            "unestimated_assignees": [],
        },
    }
    assert sprints.read_binding(tmp_path / "plane" / "SPRINTS.sqlite") == (
        "unused", "w", "DEMO",
    )
    assert "key" not in result.output


def test_init_skips_scale_when_estimates_are_disabled(
    tmp_path, live_client, monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PLANE_API_HOST_URL", "http://plane")
    monkeypatch.setenv("PLANE_API_KEY", "key")
    monkeypatch.setenv("PLANE_WORKSPACE_SLUG", "w")
    live_client.projects = Recorder(live_client.calls, "projects", result=[
        SimpleNamespace(
            id="live-project",
            identifier="DEMO",
            name="Live project",
            cycle_view=True,
            module_view=False,
            estimate=None,
        ),
    ])
    live_client.expanded = [{
        "estimate_point": {"id": "stale-point", "value": "3"},
    }]

    result = CliRunner().invoke(cli, ["init", "--project", "DEMO"])

    assert result.exit_code == 0, result.output
    document = json.loads(
        (tmp_path / "plane" / "plane-proj.json").read_text(encoding="utf-8")
    )
    assert document["estimate_points"] == {}
    assert document["rules"]["require_estimate"] is False
    assert "estimates disabled" in result.output
    assert not live_client.named("work_items._get")


def test_explicit_blank_is_valid_without_member_config(board, client):
    client.cards = [Card(id="sized", estimate_point="uuid-3")]
    board.set_estimate(client.cards[0], None, blank_estimate=True)
    payload = client.named("work_items._patch")[0][2]["data"]
    assert payload == {"estimate_point": None, "point": None}
    assert client.named("work_items.retrieve"), "blanking must be read back"


def test_dotenv_is_used_when_plane_env_is_absent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in ("PLANE_API_HOST_URL", "PLANE_API_KEY", "PLANE_WORKSPACE_SLUG"):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / ".env").write_text(
        "PLANE_API_HOST_URL=http://fallback\nPLANE_API_KEY=fallback-key\n"
        "PLANE_WORKSPACE_SLUG=fallback-workspace\n", encoding="utf-8",
    )
    credentials = load_credentials()
    assert credentials.host == "http://fallback"
    assert credentials.workspace_slug == "fallback-workspace"
    assert credentials.api_key.reveal() == "fallback-key"


def test_default_invocation_observes_external_renames(tmp_path, live_client, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PLANE_PROJ_CONFIG", raising=False)
    monkeypatch.delenv("PLANE_PROJ_PROJECT", raising=False)
    (tmp_path / "plane" / "plane-proj.json").write_text(
        '{"defaults": {"workspace": "w", "project": "DEMO"}}', encoding="utf-8",
    )
    monkeypatch.setenv("PLANE_API_HOST_URL", "http://plane")
    monkeypatch.setenv("PLANE_API_KEY", "key")
    runner = CliRunner()
    first = runner.invoke(cli, ["--json", "project", "modules"])
    assert first.exit_code == 0, first.output
    assert json.loads(first.output) == [{"module": "New module", "module_id": "live-module"}]
    live_client.modules = Recorder(live_client.calls, "modules", result=[
        SimpleNamespace(id="live-module", name="Renamed externally"),
    ])
    second = runner.invoke(cli, ["--json", "project", "modules"])
    assert second.exit_code == 0, second.output
    assert json.loads(second.output) == [
        {"module": "Renamed externally", "module_id": "live-module"},
    ]


def test_project_feature_switches_control_assignment_requirements(config_path, live_client):
    live_client.projects = Recorder(live_client.calls, "projects", result=[
        SimpleNamespace(id="p", identifier="DEMO", name="P", cycle_view=False, module_view=False),
    ])
    board = connect(load_config(config_path), Credentials("http://plane", Secret("key"), "w"), None)
    assert not board.project.rules.require_cycle
    assert not board.project.rules.require_module


def test_capture_write_never_stores_metadata(board, config_path, monkeypatch):
    monkeypatch.setattr(Context, "board", property(lambda self: board))
    monkeypatch.setattr(board, "capture_facts", lambda: {
        "estimate_points": {"3": "uuid-3"}, "states": {"Ready": "live-state"},
        "cycles": {"Sprint new": "live-cycle"}, "modules": {"New module": "live-module"},
    })
    result = CliRunner().invoke(cli, ["project", "capture", "--write"])
    assert result.exit_code == 0, result.output
    document = json.loads(config_path.read_text(encoding="utf-8"))
    assert set(document) == {"defaults", "estimate_points"}
    assert document["estimate_points"]["22"] == "uuid-22"


def test_projects_lists_server_without_config(tmp_path, live_client, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PLANE_PROJ_CONFIG", raising=False)
    monkeypatch.setenv("PLANE_API_HOST_URL", "http://plane")
    monkeypatch.setenv("PLANE_API_KEY", "key")
    monkeypatch.setenv("PLANE_WORKSPACE_SLUG", "w")
    result = CliRunner().invoke(cli, ["--json", "projects"])
    assert result.exit_code == 0, result.output
    records = {record["project_key"]: record for record in json.loads(result.output)}
    assert records["OTHER"]["project_id"] == "other-project"
    assert records["DEMO"]["name"] == "Live project"


def test_connection_applies_rules_and_resolves_exemptions(config_path, live_client):
    document = json.loads(config_path.read_text(encoding="utf-8"))
    document["rules"] = {
        "wip_limit": 1, "wip_states": ["Ready"], "cycle_estimate_max": 3,
        "unestimated_assignees": ["live-member"], "require_module": False,
    }
    config_path.write_text(json.dumps(document), encoding="utf-8")
    board = connect(load_config(config_path), Credentials("http://plane", Secret("key"), "w"), None)
    assert board.project.rules.wip_limit == 1
    assert board.project.rules.cycle_estimate_max == 3
    assert not board.project.rules.require_module
    assert board.config.unestimated_assignees == {AUTOMATION}


@pytest.mark.parametrize("rules", [
    {"unestimated_assignees": ["unknown-member"]},
    {"wip_limit": 1, "wip_states": ["unknown-state"]},
])
def test_unknown_rule_references_fail_before_writes(config_path, live_client, rules):
    document = json.loads(config_path.read_text(encoding="utf-8"))
    document["rules"] = rules
    config_path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ConfigError):
        connect(load_config(config_path), Credentials("http://plane", Secret("key"), "w"), None)
    assert all("list" in name or name == "workspaces.get_members"
               for name, _, _ in live_client.calls)


def test_sdk_resources_share_one_keep_alive_session():
    from plane.api.base_resource import BaseResource
    from plane.client.plane_client import PlaneClient

    from plane_proj.board import _share_session

    client = PlaneClient(base_url="https://plane.test", api_key="synthetic")
    _share_session(client, client.projects.session)
    sessions = set()

    def collect(owner):
        for value in vars(owner).values():
            if isinstance(value, BaseResource):
                sessions.add(id(value.session))
                collect(value)

    collect(client)
    assert sessions == {id(client.projects.session)}
    assert client.work_items.comments.session is client.projects.session
