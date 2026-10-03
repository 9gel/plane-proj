"""Plane Intake remains distinct from accepted project work items."""

import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from plane_proj.board import Board
from plane_proj.cli import Context, cli
from plane_proj.guards import ConfigError, GuardViolation, ReadbackFailed
from tests.conftest import Card, FakeClient


def intake_item(**fields):
    return SimpleNamespace(
        id=fields.get("id", "intake-id"),
        issue=fields.get("issue", "issue-id"),
        issue_detail=fields.get("issue_detail"),
        status=fields.get("status", -2),
        source=fields.get("source", "in_app"),
    )


def test_intake_new_creates_pending_item_without_board_placement(
    board: Board, client: FakeClient, config_path, monkeypatch
):
    monkeypatch.setattr(Context, "board", property(lambda self: board))

    result = CliRunner().invoke(
        cli,
        ["--conf", str(config_path), "--json", "intake", "new",
         "--title", "Broken export"],
        input="Export fails on empty data.\n",
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["status"] == "Pending"
    assert data["title"] == "Broken export"
    assert data["description_html"] == "<p>Export fails on empty data.</p>"
    assert data["intake_record_id"] == "new-intake"
    assert data["card"] == "DEMO-14"
    sent = client.named("intake.create")[0][2]["data"]
    assert sent.model_dump(exclude_none=True) == {"issue": {
        "name": "Broken export",
        "description_html": data["description_html"],
    }}
    assert client.named("intake.retrieve")
    assert not client.named("work_items.create")
    assert not client.named("cycles.add_work_items")
    assert not client.named("modules.add_work_items")


@pytest.mark.parametrize(
    ("title", "description", "message"),
    [(" ", "<p>body</p>", "needs a title"),
     ("Bug", " ", "needs a description")],
)
def test_intake_new_guards_before_write(
    board: Board, client: FakeClient, title: str,
    description: str, message: str,
):
    with pytest.raises(GuardViolation, match=message):
        board.create_intake(title, description)
    assert client.write_calls == []


def test_intake_new_rejects_incorrect_readback(
    board: Board, client: FakeClient
):
    client.intake_retrieved = intake_item(
        id="new-intake", issue="new-intake-issue", status=-2,
        issue_detail=Card(
            name="Different title", description_html="<p>body</p>"
        ),
    )

    with pytest.raises(ReadbackFailed, match="did not read back"):
        board.create_intake("Bug", "<p>body</p>")


def test_project_cards_do_not_include_intake(board: Board, client: FakeClient):
    project_card = Card(id="project-card")
    client.cards = [project_card]
    client.intake_records = [intake_item(issue_detail=Card(id="intake-card"))]

    assert board.cards() == [project_card]
    assert not client.named("intake.list")


def test_intake_records_are_returned_even_without_a_work_item(
    board: Board, client: FakeClient
):
    bare_intake = intake_item(issue_detail=None)
    client.intake_records = [bare_intake]

    assert board.intake_items() == [bare_intake]

    _, _, kwargs = client.named("intake.list")[0]
    assert kwargs["params"].expand is None
    assert kwargs["params"].per_page == 100


def test_find_intake_accepts_the_optional_card_reference(board: Board, client: FakeClient):
    found = intake_item(issue_detail=Card(id="intake-card", sequence_id=42))
    client.intake_records = [found]

    assert board.find_intake("DEMO-42") is found


def test_find_intake_accepts_both_resource_ids(board: Board, client: FakeClient):
    found = intake_item(id="intake-id", issue="issue-id")
    client.intake_records = [found]

    assert board.find_intake("intake-id") is found
    assert board.find_intake("issue-id") is found


@pytest.mark.parametrize("reference", ["DEMO-99", ""])
def test_find_intake_rejects_an_unknown_reference(board: Board, reference: str):
    with pytest.raises(ConfigError, match="No Intake item"):
        board.find_intake(reference)


def test_intake_list_keeps_a_record_without_card_details(
    board: Board, client: FakeClient, config_path, monkeypatch
):
    client.intake_records = [intake_item(id="intake-only", issue=None)]
    monkeypatch.setattr(Context, "board", property(lambda self: board))

    result = CliRunner().invoke(
        cli, ["--conf", str(config_path), "--json", "intake", "list"]
    )

    assert result.exit_code == 0
    assert json.loads(result.output) == [{
        "intake_record_id": "intake-only",
        "work_item_id": None,
        "card": None,
        "status": "Pending",
        "source": "in_app",
        "title": "",
    }]


def test_intake_list_defaults_to_pending_and_all_includes_every_status(
    board: Board, client: FakeClient, config_path, monkeypatch
):
    client.intake_records = [
        intake_item(id="pending", status=-2),
        intake_item(id="accepted", status=1),
        intake_item(id="rejected", status=-1),
    ]
    monkeypatch.setattr(Context, "board", property(lambda self: board))
    runner = CliRunner()

    pending = runner.invoke(
        cli, ["--conf", str(config_path), "--json", "intake", "list"]
    )
    all_items = runner.invoke(
        cli, ["--conf", str(config_path), "--json", "intake", "list", "--all"]
    )

    assert pending.exit_code == 0
    assert [row["intake_record_id"] for row in json.loads(pending.output)] == ["pending"]
    assert all_items.exit_code == 0
    assert [row["intake_record_id"] for row in json.loads(all_items.output)] == [
        "pending",
        "accepted",
        "rejected",
    ]


def test_intake_list_json_names_both_resource_ids(
    board: Board, client: FakeClient, config_path, monkeypatch
):
    client.intake_records = [intake_item(
        id="intake-record-id",
        issue="work-item-id",
        issue_detail=Card(sequence_id=42, name="Pending request"),
    )]
    monkeypatch.setattr(Context, "board", property(lambda self: board))

    result = CliRunner().invoke(
        cli, ["--conf", str(config_path), "--json", "intake", "list"]
    )

    assert result.exit_code == 0
    assert json.loads(result.output) == [{
        "intake_record_id": "intake-record-id",
        "work_item_id": "work-item-id",
        "card": "DEMO-42",
        "status": "Pending",
        "source": "in_app",
        "title": "Pending request",
    }]


def test_intake_show_retrieves_card_when_plane_omits_expanded_detail(
    board: Board, client: FakeClient, config_path, monkeypatch
):
    client.intake_records = [intake_item(
        id="intake-record-id", issue="work-item-id", issue_detail=None, status=1
    )]
    client.intake_retrieved = intake_item(
        id="intake-record-id",
        issue="work-item-id",
        issue_detail=Card(
            id="work-item-id",
            sequence_id=83,
            name="Metric scale ruler",
            description_html="<p>Show distance on the map.</p>",
        ),
        status=1,
    )
    monkeypatch.setattr(Context, "board", property(lambda self: board))

    result = CliRunner().invoke(
        cli, ["--conf", str(config_path), "--json", "intake", "show", "intake-record-id"]
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["work_item_id"] == "work-item-id"
    assert payload["card"] == "DEMO-83"
    assert payload["title"] == "Metric scale ruler"
    assert payload["state"] == "Todo"
    assert payload["priority"] == "none"
    assert payload["assignees"] == ["automation"]
    assert payload["description_html"] == "<p>Show distance on the map.</p>"
    assert "description" not in payload
    _, args, _ = client.named("intake.retrieve")[0]
    assert args == ("example-workspace", "project-uuid", "work-item-id")
    assert not client.named("work_items.retrieve")


def test_intake_show_preserves_an_image_between_description_blocks(
    board: Board, client: FakeClient, config_path, monkeypatch
):
    description_html = (
        '<h1 class="editor-heading-block">What the heck can you see the image below this</h1>'
        '<image-component id="image-component-id" src="asset-id" width="299px" '
        'height="368px" status="uploaded"></image-component>'
        '<p class="editor-paragraph-block">The image above this?</p>'
    )
    client.intake_records = [intake_item(
        id="intake-record-id",
        issue="work-item-id",
        issue_detail=Card(sequence_id=218, description_html=description_html),
    )]
    monkeypatch.setattr(Context, "board", property(lambda self: board))

    result = CliRunner().invoke(
        cli, ["--conf", str(config_path), "--json", "intake", "show", "intake-record-id"]
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["description_html"] == description_html
    assert "description" not in payload

    human = CliRunner().invoke(
        cli, ["--conf", str(config_path), "intake", "show", "intake-record-id"]
    )
    assert human.exit_code == 0
    assert description_html in human.output


def test_intake_show_uses_expanded_detail_without_an_extra_request(
    board: Board, client: FakeClient
):
    detail = Card(id="work-item-id", sequence_id=83)
    item = intake_item(issue="work-item-id", issue_detail=detail)

    assert board.intake_work_item(item) is detail
    assert not client.named("intake.retrieve")


def test_intake_show_refuses_to_report_blank_details_after_intake_reread(
    board: Board, client: FakeClient
):
    item = intake_item(id="intake-record-id", issue="work-item-id", issue_detail=None)
    client.intake_retrieved = item

    with pytest.raises(ConfigError, match="returned no work-item details"):
        board.intake_work_item(item)

    assert not client.named("work_items.retrieve")


def test_pending_intake_is_accepted_by_its_issue_id_and_read_back(
    board: Board, client: FakeClient
):
    pending = intake_item(id="intake-id", issue="issue-id", status=-2)
    client.intake_records = [pending]
    client.retrieved = Card(id="issue-id", sequence_id=42)

    accepted = board.accept_intake(pending)

    assert accepted.status == 1
    assert not client.named("intake.update_status"), "Plane's PAT API has no /status subroute"
    _, args, kwargs = client.named("intake.update")[0]
    assert args == ("example-workspace", "project-uuid", "issue-id")
    assert kwargs["data"].model_dump(exclude_none=True) == {"status": 1}
    assert client.named("intake.retrieve"), "an acceptance is not claimed without a readback"
    _, args, kwargs = client.named("intake.retrieve")[0]
    assert args == ("example-workspace", "project-uuid", "issue-id")
    assert kwargs == {}
    _, args, _ = client.named("work_items.retrieve")[0]
    assert args == ("example-workspace", "project-uuid", "issue-id")


@pytest.mark.parametrize(
    ("item", "message"),
    [
        (intake_item(issue=None), "has no work-item id"),
        (intake_item(status=1), "Only a pending Intake item"),
    ],
)
def test_invalid_intake_is_refused_without_a_write(
    board: Board, client: FakeClient, item, message: str
):
    with pytest.raises((ConfigError, GuardViolation), match=message):
        board.accept_intake(item)

    assert client.write_calls == []


def test_acceptance_readback_must_report_accepted(board: Board, client: FakeClient):
    pending = intake_item(issue="issue-id", status=-2)
    client.intake_records = [pending]
    client.intake_retrieved = intake_item(issue="issue-id", status=-2)

    with pytest.raises(ReadbackFailed, match="project Admin.*role greater than 15"):
        board.accept_intake(pending)

    assert not client.named("work_items.retrieve")


def test_acceptance_readback_must_be_the_same_project_card(
    board: Board, client: FakeClient
):
    pending = intake_item(issue="issue-id", status=-2)
    client.intake_records = [pending]
    client.retrieved = Card(id="different-id")

    with pytest.raises(ReadbackFailed, match="did not become project work item"):
        board.accept_intake(pending)


def test_intake_accept_command_accepts_a_card_reference(
    board: Board, client: FakeClient, config_path, monkeypatch
):
    client.intake_records = [
        intake_item(issue="issue-id", issue_detail=Card(sequence_id=42), status=-2)
    ]
    client.retrieved = Card(id="issue-id", sequence_id=42)
    monkeypatch.setattr(Context, "board", property(lambda self: board))

    result = CliRunner().invoke(
        cli, ["--conf", str(config_path), "intake", "accept", "DEMO-42"]
    )

    assert result.exit_code == 0
    assert result.output == "DEMO-42 accepted\n"


def test_pending_intake_can_be_rejected_and_read_back(
    board: Board, client: FakeClient, config_path, monkeypatch
):
    client.intake_records = [
        intake_item(issue="issue-id", issue_detail=Card(sequence_id=42), status=-2)
    ]
    monkeypatch.setattr(Context, "board", property(lambda self: board))

    result = CliRunner().invoke(
        cli,
        ["--conf", str(config_path), "--json", "intake", "reject", "DEMO-42"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["status"] == "Rejected"
    assert payload["work_item_id"] == "issue-id"
    assert payload["card"] == "DEMO-42"
    _, args, kwargs = client.named("intake.update")[0]
    assert args == ("example-workspace", "project-uuid", "issue-id")
    assert kwargs["data"].model_dump(exclude_none=True) == {"status": -1}
    assert not client.named("work_items.retrieve")
