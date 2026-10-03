"""Machine-readable output has named fields, never positional records."""

import json
from dataclasses import replace
from importlib.metadata import version

import pytest
from click.testing import CliRunner

from plane_proj.cli import Context, cli
from plane_proj.output import emit
from tests.conftest import Card, FakeClient


@pytest.mark.parametrize("flag", ["-v", "--version"])
def test_version_uses_package_metadata_without_startup(flag, monkeypatch, tmp_path):
    def unexpected_startup(*args, **kwargs):
        pytest.fail("Version output must not initialize configuration or connect to Plane")

    monkeypatch.setattr("plane_proj.cli.Context", unexpected_startup)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PLANE_PROJ_CONFIG", "missing-config.json")
    result = CliRunner().invoke(cli, [flag])
    assert result.exit_code == 0, result.output
    assert result.output == f"plane-proj, version {version('plane-proj')}\n"


def test_project_group_replaces_board_and_card_explains_work_items():
    runner = CliRunner()
    top = runner.invoke(cli, ["--help"])
    assert top.exit_code == 0
    assert "project" in cli.commands
    assert "board" not in cli.commands
    project = runner.invoke(cli, ["project", "--help"])
    assert project.exit_code == 0
    assert "rules-check" in project.output
    for args in (["card"], ["card", "--help"]):
        result = runner.invoke(cli, args)
        assert "Work item = card" in result.output


def test_json_rejects_positional_records():
    with pytest.raises(TypeError, match="objects, not positional"):
        emit([["DEMO-12", "Todo"]], as_json=True)


def test_card_list_json_names_the_work_item_id(
    board, client: FakeClient, config_path, monkeypatch
):
    client.cards = [Card(id="work-item-id", sequence_id=42, name="Named fields")]
    monkeypatch.setattr(Context, "board", property(lambda self: board))

    result = CliRunner().invoke(
        cli, ["--conf", str(config_path), "--json", "card", "list"]
    )

    assert result.exit_code == 0
    assert json.loads(result.output) == [{
        "card": "DEMO-42",
        "work_item_id": "work-item-id",
        "state": "Todo",
        "estimate": None,
        "owner": "automation",
        "title": "Named fields",
        "cycles": [],
    }]


def test_card_list_omits_estimates_when_the_feature_is_disabled(
    board, client: FakeClient, config_path, monkeypatch
):
    board.project = replace(board.project, estimates_enabled=False)
    client.cards = [Card(id="work-item-id", sequence_id=42)]
    monkeypatch.setattr(Context, "board", property(lambda self: board))

    result = CliRunner().invoke(
        cli, ["--conf", str(config_path), "--json", "card", "list"]
    )

    assert result.exit_code == 0
    assert "estimate" not in json.loads(result.output)[0]


def test_card_show_preserves_description_html(
    board, client: FakeClient, config_path, monkeypatch
):
    description_html = (
        "<p>Before</p>"
        '<image-component id="component-id" src="asset-id"></image-component>'
        "<p>After</p>"
    )
    client.cards = [Card(
        id="work-item-id",
        sequence_id=42,
        name="Lossless description",
        description_html=description_html,
    )]
    client.work_items.relations = type(client.work_items.relations)(
        client.calls, "relations", result={}
    )
    monkeypatch.setattr(Context, "board", property(lambda self: board))

    result = CliRunner().invoke(
        cli, ["--conf", str(config_path), "--json", "card", "show", "DEMO-42"]
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["description_html"] == description_html
    assert "description" not in payload


def test_board_register_json_names_each_identifier(config_path, board, monkeypatch):
    monkeypatch.setattr(Context, "board", property(lambda self: board))

    result = CliRunner().invoke(
        cli, ["--conf", str(config_path), "--json", "project", "modules"]
    )

    assert result.exit_code == 0
    assert json.loads(result.output) == [
        {"module": "pipeline", "module_id": "module-pipeline"},
        {"module": "viewer", "module_id": "module-viewer"},
    ]
