"""Integration tests: these run the real git binary in temporary repositories.

Each test builds its own repository under tmp_path, sets user.name and
user.email locally, and ignores global and system git configuration.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from plane_proj import register as register_module
from plane_proj import sprints
from plane_proj.cli import cli
from tests.test_config_startup import Recorder, live_client  # noqa: F401
from tests.test_register import BINDING, plan, titles

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="integration tests need git"
)
ATTRIBUTE = (
    "plane/SPRINTS.sqlite merge=plane-proj-register diff=plane-proj-register"
)


def git(repo: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments], cwd=repo, capture_output=True, text=True,
        check=False,
    )


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.delenv("PLANE_PROJ_CONFIG", raising=False)
    # A hook-run pytest inherits variables that would redirect git to the
    # real repository.
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
                 "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
                 "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE",
                 "GIT_PREFIX", "GIT_CONFIG", "GIT_CONFIG_COUNT",
                 "GIT_CONFIG_PARAMETERS"):
        monkeypatch.delenv(name, raising=False)
    # The configured driver runs `plane-proj`; run this checkout's code.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "plane-proj"
    shim.write_text(
        f"#!{sys.executable}\nfrom plane_proj.cli import main\nmain()\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    path = tmp_path / "repo"
    path.mkdir()
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.name", "Register Test"),
        ("config", "user.email", "register@example.invalid"),
        ("config", "commit.gpgsign", "false"),
    ):
        assert git(path, *arguments).returncode == 0
    monkeypatch.chdir(path)
    return path


def git_setup():
    return CliRunner().invoke(cli, ["register", "git-setup"])


def make_register(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    sprints.create_database(path, BINDING)
    return path


def commit(repo: Path, message: str) -> None:
    for arguments in (("add", "--", ".gitattributes", "plane"),
                      ("commit", "-q", "-m", message)):
        result = git(repo, *arguments)
        assert result.returncode == 0, result.stderr


def test_git_setup_is_idempotent(repo):
    make_register(repo / "plane" / "SPRINTS.sqlite")
    first = git_setup()
    second = git_setup()

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert "already set up" in second.output
    lines = (repo / ".gitattributes").read_text(encoding="utf-8").splitlines()
    assert lines == [ATTRIBUTE]
    assert git(
        repo, "config", "--local", "merge.plane-proj-register.driver"
    ).stdout.strip() == "plane-proj register merge %O %A %B"
    assert git(
        repo, "config", "--local", "diff.plane-proj-register.textconv"
    ).stdout.strip() == "plane-proj register dump"


def test_git_setup_uses_configured_state_file(repo):
    (repo / "plane").mkdir()
    (repo / "plane" / "plane-proj.json").write_text(json.dumps({
        "defaults": {"workspace": "test", "project": "DEMO"},
        "state_file": "../state/REG.sqlite",
    }), encoding="utf-8")
    (repo / ".gitattributes").write_text("*.png binary", encoding="utf-8")
    make_register(repo / "state" / "REG.sqlite")

    result = git_setup()

    assert result.exit_code == 0, result.output
    assert (repo / ".gitattributes").read_text(encoding="utf-8") == (
        "*.png binary\nstate/REG.sqlite merge=plane-proj-register "
        "diff=plane-proj-register\n"
    )


def test_git_setup_refuses_a_missing_register(repo, monkeypatch):
    make_register(repo / "plane" / "SPRINTS.sqlite")
    (repo / "sub").mkdir()
    monkeypatch.chdir(repo / "sub")

    result = git_setup()

    assert result.exit_code == 1
    assert "no register at" in result.stderr
    assert not (repo / ".gitattributes").exists()
    assert git(repo, "config", "--local", "--get-regexp", "plane-proj")\
        .stdout == ""


def test_git_setup_refuses_pattern_characters(repo):
    (repo / "plane").mkdir()
    (repo / "plane" / "plane-proj.json").write_text(json.dumps({
        "defaults": {"workspace": "test", "project": "DEMO"},
        "state_file": "../st[1]/REG.sqlite",
    }), encoding="utf-8")
    make_register(repo / "st[1]" / "REG.sqlite")

    result = git_setup()

    assert result.exit_code == 1
    assert "pattern character" in result.stderr
    assert not (repo / ".gitattributes").exists()


def test_git_setup_reads_back_overridden_attributes(repo):
    make_register(repo / "plane" / "SPRINTS.sqlite")
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "attributes").write_text(
        "*.sqlite binary\n", encoding="utf-8"
    )

    results = [git_setup(), git_setup()]

    assert [result.exit_code for result in results] == [1, 1]
    assert "readback failed" in results[1].stderr
    assert ".git/info/attributes" in results[1].stderr
    lines = (repo / ".gitattributes").read_text(encoding="utf-8").splitlines()
    assert lines == [ATTRIBUTE]


def test_git_setup_refuses_outside_a_work_tree(repo, tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)

    result = git_setup()

    assert result.exit_code == 1
    assert "Register git setup rule" in result.stderr
    assert not (outside / ".gitattributes").exists()


def test_git_merge_merges_through_the_driver(repo):
    register = make_register(repo / "plane" / "SPRINTS.sqlite")
    assert git_setup().exit_code == 0
    plan(register, 1, 1)
    plan(register, 2, 2)
    commit(repo, "register")
    assert git(repo, "checkout", "-q", "-b", "feature").returncode == 0
    plan(register, 2, 2, title="Feature")
    commit(repo, "feature")
    assert git(repo, "checkout", "-q", "main").returncode == 0
    plan(register, 1, 1, title="Main")
    commit(repo, "main")

    diff = git(repo, "diff", "HEAD~1", "HEAD", "--", "plane/SPRINTS.sqlite")
    assert '"title":"Main"' in diff.stdout
    merged = git(repo, "merge", "--no-edit", "feature")

    assert merged.returncode == 0, merged.stdout + merged.stderr
    assert titles(register) == {1: "Main", 2: "Feature"}
    assert git(repo, "status", "--porcelain").stdout == ""


def test_git_merge_reports_a_register_conflict(repo):
    register = make_register(repo / "plane" / "SPRINTS.sqlite")
    assert git_setup().exit_code == 0
    plan(register, 1, 1)
    commit(repo, "register")
    assert git(repo, "checkout", "-q", "-b", "feature").returncode == 0
    plan(register, 1, 1, title="Feature")
    commit(repo, "feature")
    assert git(repo, "checkout", "-q", "main").returncode == 0
    plan(register, 1, 1, title="Main")
    commit(repo, "main")
    before = register.read_bytes()

    merged = git(repo, "merge", "--no-edit", "feature")

    assert merged.returncode != 0
    assert "CONFLICT" in merged.stdout
    assert "Register merge rule" in merged.stderr
    assert register.read_bytes() == before
    git(repo, "merge", "--abort")


def run_init(live_client, monkeypatch):  # noqa: F811
    for name, value in (("PLANE_API_HOST_URL", "http://plane"),
                        ("PLANE_API_KEY", "key"),
                        ("PLANE_WORKSPACE_SLUG", "w")):
        monkeypatch.setenv(name, value)
    live_client.projects = Recorder(live_client.calls, "projects", result=[
        SimpleNamespace(id="live-project", identifier="DEMO", name="Live",
                        cycle_view=True, module_view=True, estimate=None),
    ])
    return CliRunner().invoke(cli, ["init", "--project", "DEMO"])


def test_init_sets_up_git_inside_a_work_tree(
    repo, live_client, monkeypatch,  # noqa: F811
):
    result = run_init(live_client, monkeypatch)

    assert result.exit_code == 0, result.output
    assert (repo / ".gitattributes").read_text(encoding="utf-8") == (
        ATTRIBUTE + "\n"
    )


def test_init_outside_a_work_tree_explains_git_setup(
    repo, tmp_path, live_client, monkeypatch,  # noqa: F811
):
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)

    result = run_init(live_client, monkeypatch)

    assert result.exit_code == 0, result.output
    assert "plane-proj register git-setup" in result.output
    assert not (outside / ".gitattributes").exists()
    with closing(sprints.connect_database(
        outside / "plane" / "SPRINTS.sqlite", writable=False
    )):
        pass


@pytest.mark.parametrize("failure", ["override", "oserror"])
def test_init_survives_a_failing_git_setup(
    repo, live_client, monkeypatch, failure,  # noqa: F811
):
    if failure == "override":
        (repo / ".git" / "info").mkdir(exist_ok=True)
        (repo / ".git" / "info" / "attributes").write_text(
            "*.sqlite binary\n", encoding="utf-8"
        )
    else:
        def unwritable(directory, database):
            raise PermissionError(".gitattributes is read-only")

        monkeypatch.setattr(register_module, "setup_git", unwritable)

    result = run_init(live_client, monkeypatch)

    assert result.exit_code == 0, result.output
    assert "Skipped register git setup" in result.output
    assert "plane-proj register git-setup" in result.output
    assert (repo / "plane" / "plane-proj.json").is_file()
    assert sprints.read_binding(repo / "plane" / "SPRINTS.sqlite") == (
        "unused", "w", "DEMO",
    )


def committed_register(repo: Path) -> Path:
    path = make_register(repo / "plane" / "SPRINTS.sqlite")
    result = git(repo, "add", "--", "plane")
    assert result.returncode == 0, result.stderr
    assert git(repo, "commit", "-q", "-m", "register").returncode == 0
    sprints.take_written_registers()
    return path


def test_write_commits_only_the_register_at_command_end(repo):
    path = committed_register(repo)
    (repo / "other.txt").write_text("staged by someone else\n")
    assert git(repo, "add", "other.txt").returncode == 0

    plan(path, 7)
    from plane_proj.cli import _commit_registers
    _commit_registers("plane-proj sprints plan")

    assert git(repo, "status", "--porcelain", "--", "plane").stdout == ""
    assert git(repo, "diff", "--cached", "--name-only").stdout == "other.txt\n"
    assert "Sprint 7" in titles(repo / "plane" / "SPRINTS.sqlite").values()
    log = git(repo, "log", "-1", "--format=%s%n%n%b").stdout
    assert log.startswith("chore(plane): record `plane-proj sprints plan`")
    assert git(repo, "checkout", "--", "plane/SPRINTS.sqlite").returncode == 0
    assert "Sprint 7" in titles(path).values()


def test_unchanged_register_makes_no_commit(repo):
    path = committed_register(repo)
    sprints.connect_database(path, writable=True).close()
    head = git(repo, "rev-parse", "HEAD").stdout

    assert register_module.commit_register(path, "plane-proj x") is False
    assert git(repo, "rev-parse", "HEAD").stdout == head


def test_refused_commit_raises_and_keeps_the_write(repo):
    path = committed_register(repo)
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho refused-by-hook >&2\nexit 1\n")
    hook.chmod(0o755)
    plan(path, 8)

    with pytest.raises(register_module.RegisterCommitError,
                       match="Register commit rule.*refused-by-hook"):
        register_module.commit_register(path, "plane-proj sprints plan")
    assert "Sprint 8" in titles(path).values()
