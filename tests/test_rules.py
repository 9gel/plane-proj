"""Configured rules must affect checks without caching server metadata."""

import json
from dataclasses import replace

import pytest
from click.testing import CliRunner

from plane_proj.cli import Context, cli
from plane_proj.config import Rules, load_config
from plane_proj.guards import ConfigError
from tests.conftest import ALICE, AUTOMATION, Card


@pytest.mark.parametrize("rules", [
    None, [], {"wip_limit": True}, {"wip_limit": -1}, {"cycle_estimate_max": 2.5},
    {"require_estimate": "false"}, {"wip_states": "In Progress"}, {"wip_states": []},
    {"unestimated_assignees": [None]}, {"wip_limt": 1},
])
def test_invalid_rules_are_refused_before_connecting(config_path, rules):
    document = json.loads(config_path.read_text(encoding="utf-8"))
    document["rules"] = rules
    config_path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ConfigError, match="rules"):
        load_config(config_path)


def check(board, monkeypatch):
    monkeypatch.setattr(Context, "board", property(lambda self: board))
    return CliRunner().invoke(cli, ["--json", "project", "rules-check"])


def test_wip_is_counted_per_assignee(board, client, monkeypatch):
    board.project = replace(board.project, rules=Rules(
        require_cycle=False, require_module=False, require_estimate=False, wip_limit=1,
    ))
    client.cards = [
        Card(id="a", assignees=[AUTOMATION], state="state-progress"),
        Card(id="b", assignees=[ALICE], state="state-progress"),
    ]
    assert check(board, monkeypatch).exit_code == 0
    client.cards.append(Card(id="c", assignees=[AUTOMATION], state="state-progress"))
    result = check(board, monkeypatch)
    assert result.exit_code == 1
    assert json.loads(result.output) == [{
        "card": "—", "finding": "WIP over limit", "detail": "automation: 2 in progress, limit 1",
    }]
    assert all("list" in name for name, _, _ in client.calls)


def test_requirements_ceiling_and_unknown_scale_are_distinct(board, client, monkeypatch):
    client.cards = [
        Card(id="large", sequence_id=1, estimate_point="uuid-5"),
        Card(id="blank", sequence_id=2, estimate_point=None),
        Card(id="unknown", sequence_id=3, estimate_point="uncaptured-uuid"),
        # Oversized cards may wait in Backlog in a planned sprint.
        Card(id="waiting", sequence_id=4, estimate_point="uuid-5", state="state-backlog"),
    ]
    in_both = {"large", "unknown", "waiting"}
    monkeypatch.setattr(board, "cycle_card_ids", lambda cycle_id: in_both)
    monkeypatch.setattr(board, "module_card_ids", lambda module_id: in_both)
    result = check(board, monkeypatch)
    assert result.exit_code == 1
    findings = {(item["card"], item["finding"]) for item in json.loads(result.output)}
    assert findings == {
        ("DEMO-1", "too large to admit"), ("DEMO-2", "unestimated"),
        ("DEMO-2", "outside every cycle"), ("DEMO-2", "outside every module"),
        ("DEMO-3", "unknown estimate"),
    }


def test_exempt_assignee_can_be_blank_but_not_sized(board, client, monkeypatch):
    board.project = replace(board.project, rules=Rules(require_cycle=False, require_module=False))
    client.cards = [Card(assignees=[ALICE])]
    assert check(board, monkeypatch).exit_code == 0
    client.cards = [Card(assignees=[ALICE], estimate_point="uuid-3")]
    result = check(board, monkeypatch)
    assert result.exit_code == 1
    assert json.loads(result.output)[0]["finding"] == "sized, owner takes none"


def test_scale_writes_preserve_rules(board, client, config_path, monkeypatch):
    rules = {"wip_limit": 1, "unestimated_assignees": ["alice"]}
    document = json.loads(config_path.read_text(encoding="utf-8"))
    document["rules"] = rules
    config_path.write_text(json.dumps(document), encoding="utf-8")
    client.expanded = [{"estimate_point": {"id": "u8", "value": "8"}}]
    monkeypatch.setattr(Context, "board", property(lambda self: board))
    result = CliRunner().invoke(cli, ["project", "scale", "--write"])
    assert result.exit_code == 0, result.output
    written = json.loads(config_path.read_text(encoding="utf-8"))
    assert written["rules"] == rules
    assert written["estimate_points"]["8"] == "u8"
