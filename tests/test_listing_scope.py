"""Listings hide inactive work unless --all explicitly includes it."""

import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from plane.errors import HttpError

from plane_proj.cli import Context, cli
from plane_proj.guards import ConfigError
from tests.conftest import Card


@pytest.mark.parametrize("show_all", [False, True])
def test_projects_filter_archived_and_report_status(board, monkeypatch, show_all):
    monkeypatch.setattr("plane_proj.cli.load_credentials", lambda *a, **k: None)
    monkeypatch.setattr("plane_proj.board.discover", lambda credentials: (None, {
        "ACTIVE": SimpleNamespace(id="active", name="Active project", archived_at=None),
        "OLD": SimpleNamespace(id="old", name="Old project", archived_at="2026-01-01"),
    }, {}))
    result = CliRunner().invoke(cli, [
        "--conf", str(board.config.path), "--json", "projects", *(["--all"] if show_all else []),
    ])
    assert result.exit_code == 0, result.output
    rows = {row["project_key"]: row for row in json.loads(result.output)}
    assert set(rows) == ({"ACTIVE", "OLD"} if show_all else {"ACTIVE"})
    assert rows["ACTIVE"]["status"] == "Active"
    if show_all:
        assert rows["OLD"]["status"] == "Archived"


@pytest.mark.parametrize("show_all", [False, True])
def test_cards_filter_inactive_states_and_fetch_archives(board, client, monkeypatch, show_all):
    client.cards = [
        Card(id="todo", state="state-todo"), Card(id="done", state="state-done"),
        Card(id="backlog", state="state-backlog"),
        Card(id="cancelled", state="state-cancelled"),
    ]
    archived = Card(id="archived", state="state-todo")
    archived.archived_at = "2026-01-01"
    # Test both archives included in normal listing and archive-only results.
    client.cards.append(archived)
    archive_only = Card(id="archive-only", state="state-done")
    archive_only.archived_at = "2026-01-02"
    calls = []
    def archives(slug, project_id, params=None):
        calls.append(params)
        return SimpleNamespace(results=[archived, archive_only], next_page_results=False)
    monkeypatch.setattr(client.work_items, "list_archived", archives)
    monkeypatch.setattr(Context, "board", property(lambda self: board))
    result = CliRunner().invoke(cli, ["--json", "card", "list", *(["--all"] if show_all else [])])
    assert result.exit_code == 0, result.output
    ids = [row["work_item_id"] for row in json.loads(result.output)]
    assert set(ids) == (
        {"todo", "done", "backlog", "cancelled", "archived", "archive-only"}
        if show_all else {"todo"}
    )
    assert len(ids) == len(set(ids))
    assert bool(calls) == show_all


def test_card_list_includes_live_cycle_names(board, client, monkeypatch):
    client.cards = [Card(id="scheduled", sequence_id=1), Card(id="unscheduled", sequence_id=2)]

    def members(slug, project_id, cycle_id, params=None):
        assert cycle_id == "cycle-1"
        return SimpleNamespace(results=[Card(id="scheduled")], next_page_results=False)

    client.cycles = SimpleNamespace(list_work_items=members)
    monkeypatch.setattr(Context, "board", property(lambda self: board))
    runner = CliRunner()
    result = runner.invoke(cli, ["--json", "card", "list"])
    assert result.exit_code == 0, result.output
    rows = {row["work_item_id"]: row for row in json.loads(result.output)}
    assert rows["scheduled"]["cycles"] == ["Sprint 1"]
    assert rows["unscheduled"]["cycles"] == []
    human = runner.invoke(cli, ["card", "list"])
    assert human.exit_code == 0, human.output
    assert "CYCLE" in human.output
    assert "Sprint 1" in human.output
    assert "['Sprint 1']" not in human.output


def test_an_explicit_state_filter_shows_a_hidden_state(board, client, monkeypatch):
    # `--all` also reads archived cards, whose route 404s on self-hosted CE,
    # so requiring it left no way to list Backlog cards there.
    archived = Card(id="archived", state="state-backlog")
    archived.archived_at = "2026-09-01T00:00:00Z"
    client.cards = [Card(id="backlog", state="state-backlog"), archived, Card(id="todo")]
    monkeypatch.setattr(Context, "board", property(lambda self: board))
    runner = CliRunner()
    default = runner.invoke(cli, ["--json", "card", "list", "--state", "Backlog"])
    assert default.exit_code == 0, default.output
    assert [row["work_item_id"] for row in json.loads(default.output)] == ["backlog"]
    client.cards.remove(archived)
    all_cards = runner.invoke(cli, ["--json", "card", "list", "--all", "--state", "Backlog"])
    assert all_cards.exit_code == 0, all_cards.output
    assert [row["work_item_id"] for row in json.loads(all_cards.output)] == ["backlog"]


@pytest.mark.parametrize("status", [404, 403, 500])
def test_archive_failures_never_return_a_partial_all_listing(board, client, monkeypatch, status):
    client.cards = [Card(id="active")]
    def unavailable(*args, **kwargs):
        raise HttpError("unavailable", status_code=status)
    monkeypatch.setattr(client.work_items, "list_archived", unavailable)
    with pytest.raises(ConfigError if status == 404 else HttpError):
        board.cards(include_archived=True)


def test_archived_cards_are_paged_to_the_end(board, client, monkeypatch):
    cursors = []
    def archives(slug, project_id, params=None):
        cursor = params.cursor if params is not None else None
        cursors.append(cursor)
        return SimpleNamespace(
            results=[Card(id="first" if cursor is None else "last")],
            next_page_results=cursor is None, next_cursor="second",
        )
    monkeypatch.setattr(client.work_items, "list_archived", archives)
    assert {card.id for card in board.cards(include_archived=True)} == {"first", "last"}
    assert cursors == [None, "second"]
