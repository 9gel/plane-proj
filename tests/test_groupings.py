"""Cycle and module lifecycle.

Both groups are generated from one implementation, so these run against both
where the behaviour is shared. What differs — a cycle takes an owner and an
end date, a module takes a lead and a target date — is tested per kind.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner
from plane.errors import HttpError
from plane.models.projects import ProjectFeature

from plane_proj.cli import cli
from plane_proj.guards import ReadbackFailed

KINDS = ("cycle", "module")


@pytest.fixture(autouse=True)
def offline(monkeypatch, board):
    """Credentials present, network absent.

    These exercise the command surface and the config-file bookkeeping, not
    the transport; a real PlaneClient would try to reach a host.
    """
    monkeypatch.setenv("PLANE_API_HOST_URL", "https://plane.invalid")
    monkeypatch.setenv("PLANE_API_KEY", "not-a-key")
    monkeypatch.setenv("PLANE_WORKSPACE_SLUG", "example-workspace")
    monkeypatch.setattr("plane_proj.board.connect", lambda *args: board)


def run(config_path, *args):
    return CliRunner().invoke(cli, ["--conf", str(config_path), *args])


@pytest.mark.parametrize("kind", KINDS)
def test_every_lifecycle_verb_exists(config_path, kind):
    """The gap this closed: there was no way to make or rename either."""
    result = run(config_path, kind, "--help")

    verbs = ("new", "rename", "set", "list", "cards")
    for verb in verbs:
        assert verb in result.output, f"{kind} has no {verb}"
    if kind == "cycle":
        assert "restore" in result.output
        assert "\n  archive " not in result.output
        assert "\n  delete " not in result.output
    else:
        assert "add" in result.output
        assert "rm" in result.output
        assert "archive" in result.output
        assert "delete" in result.output


def test_cycle_membership_commands_are_card_centric(config_path):
    cycle_help = run(config_path, "cycle", "--help")
    card_help = run(config_path, "card", "--help")
    assert "  add " not in cycle_help.output
    assert "  rm " not in cycle_help.output
    assert "set-cycle" in card_help.output
    assert "rm-cycle" in card_help.output


def test_only_a_cycle_can_transfer(config_path):
    """Transfer means 'move the unfinished work on'; a module has no such notion."""
    assert "transfer" in run(config_path, "cycle", "--help").output
    assert "transfer" not in run(config_path, "module", "--help").output


def test_deleting_a_module_needs_force_and_names_archive(config_path):
    result = run(config_path, "module", "delete", "Module 1")

    assert result.exit_code != 0
    assert "archive" in result.output
    assert "not reversible" in result.output


def test_a_cycle_rejects_module_only_options(config_path):
    result = run(config_path, "cycle", "new", "Sprint 9", "--lead", "automation")

    assert result.exit_code != 0
    assert "lead" in result.output


def test_an_unknown_name_lists_what_exists(config_path, monkeypatch):
    """It re-reads the board before refusing, so the list is current."""
    monkeypatch.setattr("plane_proj.board.Board.refresh_grouping", lambda self, kind: {})
    result = run(config_path, "cycle", "cards", "Nope")

    assert result.exit_code != 0
    assert "Sprint 1" in result.output


def test_restore_resolves_the_archived_cycle(config_path, monkeypatch):
    restore_calls: list[str] = []
    monkeypatch.setattr(
        "plane_proj.board.Board.refresh_grouping",
        lambda self, kind, archived=False: (
            {"Sprint 1": "archived-cycle-1"} if archived else {}
        ),
    )
    monkeypatch.setattr(
        "plane_proj.board.Board.restore_cycle",
        lambda self, cycle_id: restore_calls.append(cycle_id),
    )

    result = run(config_path, "cycle", "restore", "Sprint 1")

    assert result.exit_code == 0
    assert restore_calls == ["archived-cycle-1"]


@pytest.mark.parametrize("readback", [True, False])
def test_restore_cycle_requires_active_readback(board, client, monkeypatch, readback):
    cycle = type("Cycle", (), {"id": "cycle-7"})()
    monkeypatch.setattr(client.cycles, "unarchive", lambda *args: None)
    monkeypatch.setattr(
        board,
        "groupings",
        lambda kind, archived=False: [cycle] if readback and not archived else [],
    )

    if readback:
        board.restore_cycle("cycle-7")
    else:
        with pytest.raises(ReadbackFailed, match="restore cycle"):
            board.restore_cycle("cycle-7")


def test_restore_cycle_requires_membership_readback(board, client, monkeypatch):
    memberships = iter(({"one", "two"}, {"one"}))
    cycle = type("Cycle", (), {"id": "cycle-7"})()
    monkeypatch.setattr(client.cycles, "unarchive", lambda *args: None)
    monkeypatch.setattr(board, "groupings", lambda kind: [cycle])
    monkeypatch.setattr(board, "cycle_card_ids", lambda cycle_id: next(memberships))

    with pytest.raises(ReadbackFailed, match="lost card membership.*two"):
        board.restore_cycle("cycle-7")


def test_disabled_cycles_are_enabled_and_the_write_retried(board, client):
    features = ProjectFeature(cycles=False)
    attempts = 0

    def create(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise HttpError(
                "HTTP 400: Bad Request",
                status_code=400,
                response={"non_field_errors": [
                    "Cycles are not enabled for this project",
                ]},
            )
        return type("Cycle", (), {"id": "cycle-2"})()

    class Projects:
        def get_features(self, *args):
            client.calls.append(("projects.get_features", args, {}))
            return features

        def update_features(self, *args, **kwargs):
            client.calls.append(("projects.update_features", args, kwargs))
            features.cycles = args[2].cycles
            return features

    client.projects = Projects()
    client.cycles.create = create
    board.me = lambda: "member-uuid"

    created = board.create_cycle("Sprint 2", {})

    assert created.id == "cycle-2"
    assert attempts == 2
    assert features.cycles is True
    assert len(client.named("projects.update_features")) == 1
    assert len(client.named("projects.get_features")) == 1


def test_an_unrelated_cycle_error_is_not_retried(board, client):
    attempts = 0

    def create(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        raise HttpError("HTTP 403: Forbidden", status_code=403)

    client.cycles.create = create
    board.me = lambda: "member-uuid"

    with pytest.raises(HttpError, match="Forbidden"):
        board.create_cycle("Sprint 2", {})

    assert attempts == 1


def test_cycle_enablement_must_pass_readback(board, client):
    attempts = 0

    def create(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        raise HttpError(
            "HTTP 400: Bad Request",
            status_code=400,
            response={"non_field_errors": [
                "Cycles are not enabled for this project",
            ]},
        )

    class Projects:
        def get_features(self, *args):
            return ProjectFeature(cycles=False)

        def update_features(self, *args, **kwargs):
            return ProjectFeature(cycles=True)

    client.projects = Projects()
    client.cycles.create = create
    board.me = lambda: "member-uuid"

    with pytest.raises(ReadbackFailed, match="Cycles feature"):
        board.create_cycle("Sprint 2", {})

    assert attempts == 1


class TestMetadataStaysOffDisk:
    """Lifecycle operations never cache server metadata in the config."""

    def test_creating_preserves_config(self, config_path, monkeypatch):
        before = config_path.read_bytes()
        monkeypatch.setattr("plane_proj.board.Board.me", lambda self: "member-uuid")
        monkeypatch.setattr("plane_proj.board.Board.create_cycle",
                            lambda self, name, fields:
                            type("C", (), {"name": name, "id": "cycle-2"})())
        monkeypatch.setattr("plane_proj.board.Board.refresh_grouping",
                            lambda self, kind: {"Sprint 1": "cycle-1", "Sprint 2": "cycle-2"})

        assert run(config_path, "cycle", "new", "Sprint 2").exit_code == 0

        assert config_path.read_bytes() == before

    def test_unapplied_rename_fails_readback(self, config_path, monkeypatch):
        before = config_path.read_bytes()
        monkeypatch.setattr("plane_proj.board.Board.update_cycle",
                            lambda self, cycle_id, fields: None)
        monkeypatch.setattr("plane_proj.board.Board.refresh_grouping",
                            lambda self, kind: {"Sprint 1": "cycle-1"})
        result = run(config_path, "cycle", "rename", "Sprint 1", "Sprint One")
        assert isinstance(result.exception, ReadbackFailed)
        assert config_path.read_bytes() == before

    @pytest.mark.parametrize("deleted", [False, True])
    def test_lifecycle_readback_checks_ids(self, board, monkeypatch, deleted):
        monkeypatch.setattr(board, "refresh_grouping", lambda kind: {"Same name": "other-id"})
        with pytest.raises(ReadbackFailed):
            board.verify_grouping(
                "module", name="Same name", target_id="other-id" if deleted else "expected-id",
                deleted=deleted,
            )

    def test_renaming_preserves_config(self, config_path, monkeypatch):
        before = config_path.read_bytes()
        monkeypatch.setattr("plane_proj.board.Board.update_cycle",
                            lambda self, cycle_id, fields: None)
        monkeypatch.setattr("plane_proj.board.Board.refresh_grouping",
                            lambda self, kind: {"Sprint One": "cycle-1"})

        assert run(config_path, "cycle", "rename", "Sprint 1", "Sprint One").exit_code == 0

        assert config_path.read_bytes() == before
