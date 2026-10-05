from __future__ import annotations

from dataclasses import replace

from click.testing import CliRunner

from plane_proj.cli import Context, cli
from plane_proj.config import Rules
from plane_proj.delivery_plan import parse_delivery_plan
from plane_proj.guards import DeliveryPlanRule
from tests.conftest import Card, FakeClient


def test_card_new_and_plan_roundtrip_with_fake_client(board, client: FakeClient, monkeypatch):
    """pytest with FakeClient shows card new and card plan store a section that

    parses back to the requested paths and marker, and that card plan keeps
    the rest of the description unchanged.
    """
    monkeypatch.setattr(Context, "board", property(lambda self: board))

    # 1. card new with --touches and --deps-assessed
    runner = CliRunner()
    result = runner.invoke(cli, [
        "card", "new",
        "--title", "Add scoped pickup",
        "--description", "## What to build\nImplement pickup endpoint.\n",
        "--assignee", "automation",
        "--cycle", "Sprint 1",
        "--module", "pipeline",
        "--estimate", "2",
        "--touches", "src/api/members.ts",
        "--touches", "src/shop/pickup.ts (new)",
        "--deps-assessed",
    ])
    assert result.exit_code == 0, result.output
    assert client.created is not None

    stored_html = client.created.description_html
    plan = parse_delivery_plan(stored_html)
    assert plan.paths == ("src/api/members.ts", "src/shop/pickup.ts")
    assert [p.is_new for p in plan.touches] == [False, True]
    assert plan.dependencies_assessed is True
    assert "Implement pickup endpoint." in stored_html

    # Check prefix before Delivery plan
    prefix = stored_html[:stored_html.index("<h2>Delivery plan")]

    # 2. card plan replaces only the section, keeping the rest unchanged
    client.cards = [client.created]
    plan_result = runner.invoke(cli, [
        "card", "plan", "DEMO-13",
        "--touches", "src/api/members.ts",
        "--touches", "src/shop/pickup.ts",
        "--touches", "src/shop/extra.ts (new)",
        "--deps-assessed",
    ])
    assert plan_result.exit_code == 0, plan_result.output

    updated_html = client.created.description_html
    updated_plan = parse_delivery_plan(updated_html)
    assert updated_plan.paths == (
        "src/api/members.ts",
        "src/shop/pickup.ts",
        "src/shop/extra.ts",
    )
    assert [p.is_new for p in updated_plan.touches] == [False, False, True]
    assert updated_plan.dependencies_assessed is True
    # The rest of the description is byte-identical
    assert updated_html.startswith(prefix)
    assert "Implement pickup endpoint." in updated_html


def test_card_plan_touches_none(board, client: FakeClient, monkeypatch):
    """card plan supports --touches none."""
    monkeypatch.setattr(Context, "board", property(lambda self: board))
    card = Card(
        id="card-1",
        sequence_id=1,
        description_html=(
            "<div><h2>What to build</h2><p>Docs only.</p>"
            "<h2>Delivery plan</h2><p>Touches:<br>- old.py<br>Dependencies: assessed</p></div>"
        ),
    )
    client.cards = [card]

    runner = CliRunner()
    result = runner.invoke(cli, [
        "card", "plan", "DEMO-1",
        "--touches", "none",
        "--deps-assessed",
    ])
    assert result.exit_code == 0, result.output

    plan = parse_delivery_plan(client.created.description_html)
    assert plan.is_touches_none is True
    assert plan.dependencies_assessed is True
    assert "Docs only." in client.created.description_html


def test_require_delivery_plan_refuses_undeclared_card_new_with_no_board_write(
    board, client: FakeClient, monkeypatch
):
    """pytest shows require_delivery_plan refuses an undeclared card new under its

    named rule, with no board write.
    """
    board.project = replace(board.project, rules=Rules(
        require_cycle=True,
        require_module=True,
        require_estimate=True,
        require_delivery_plan=True,
    ))
    monkeypatch.setattr(Context, "board", property(lambda self: board))

    runner = CliRunner()
    result = runner.invoke(cli, [
        "card", "new",
        "--title", "Undeclared card",
        "--description", "No delivery plan declared.",
        "--assignee", "automation",
        "--cycle", "Sprint 1",
        "--module", "pipeline",
        "--estimate", "2",
    ])
    assert result.exit_code != 0
    assert isinstance(result.exception, DeliveryPlanRule)
    assert "Delivery plan rule" in str(result.exception)
    # No board write: work_items.create was never called
    assert not any(call[0] == "work_items.create" for call in client.calls)

    # Missing only deps-assessed
    result_missing_deps = runner.invoke(cli, [
        "card", "new",
        "--title", "Undeclared card 2",
        "--description", "Has touches but no deps.",
        "--assignee", "automation",
        "--cycle", "Sprint 1",
        "--module", "pipeline",
        "--estimate", "2",
        "--touches", "src/foo.py",
    ])
    assert result_missing_deps.exit_code != 0
    assert isinstance(result_missing_deps.exception, DeliveryPlanRule)
    assert "Delivery plan rule" in str(result_missing_deps.exception)
    assert not any(call[0] == "work_items.create" for call in client.calls)

    # Missing only touches
    result_missing_touches = runner.invoke(cli, [
        "card", "new",
        "--title", "Undeclared card 3",
        "--description", "Has deps but no touches.",
        "--assignee", "automation",
        "--cycle", "Sprint 1",
        "--module", "pipeline",
        "--estimate", "2",
        "--deps-assessed",
    ])
    assert result_missing_touches.exit_code != 0
    assert isinstance(result_missing_touches.exception, DeliveryPlanRule)
    assert "Delivery plan rule" in str(result_missing_touches.exception)
    assert not any(call[0] == "work_items.create" for call in client.calls)


def test_existing_config_without_require_delivery_plan_keeps_old_behaviour(
    board, client: FakeClient, monkeypatch
):
    """Existing config without require_delivery_plan keeps old behaviour (default off)."""
    assert board.project.rules.require_delivery_plan is False
    monkeypatch.setattr(Context, "board", property(lambda self: board))

    runner = CliRunner()
    result = runner.invoke(cli, [
        "card", "new",
        "--title", "Old behavior card",
        "--description", "No delivery plan here.",
        "--assignee", "automation",
        "--cycle", "Sprint 1",
        "--module", "pipeline",
        "--estimate", "2",
    ])
    assert result.exit_code == 0, result.output
    assert any(call[0] == "work_items.create" for call in client.calls)
