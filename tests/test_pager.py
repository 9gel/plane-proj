"""Human terminal output pages itself; JSON and pipes never do."""

from __future__ import annotations

import io
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from plane_proj import output


class Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


class FakePager:
    def __init__(self) -> None:
        self.stdin = io.StringIO()
        self.waited = False

    def wait(self) -> int:
        self.waited = True
        return 0


@pytest.fixture
def pager_env(monkeypatch):
    """A tty stdout, a recorded Popen, and captured atexit finalizers.

    pytest rebinds sys.stdout between fixture setup and the test call,
    so the tty swap happens through attach(), called inside the test.
    """
    state = SimpleNamespace(
        popen_calls=[], pager=FakePager(), finalizers=[], tty=Tty(),
    )
    monkeypatch.setattr(output, "_pager", None)

    def attach(stream: Any | None = None) -> None:
        monkeypatch.setattr(sys, "stdout", stream or state.tty)

    state.attach = attach

    def popen(command: list[str], **kwargs: Any) -> FakePager:
        state.popen_calls.append((command, kwargs))
        return state.pager

    monkeypatch.setattr(output.subprocess, "Popen", popen)
    monkeypatch.setattr(
        output.atexit, "register", lambda f: state.finalizers.append(f)
    )
    return state


def test_tty_output_goes_through_the_pager(pager_env):
    pager_env.attach()
    output.emit({"card": "DEMO-1"}, as_json=False, render=None)

    assert len(pager_env.popen_calls) == 1
    assert "DEMO-1" in pager_env.pager.stdin.getvalue()
    assert pager_env.tty.getvalue() == ""


def test_json_output_never_pages(pager_env):
    pager_env.attach()
    output.emit({"card": "DEMO-1"}, as_json=True, render=None)

    assert pager_env.popen_calls == []
    assert "DEMO-1" in pager_env.tty.getvalue()


def test_non_tty_output_never_pages(pager_env):
    pipe = io.StringIO()
    pager_env.attach(pipe)

    output.emit({"card": "DEMO-1"}, as_json=False, render=None)

    assert pager_env.popen_calls == []
    assert "DEMO-1" in pipe.getvalue()


def test_the_pager_starts_once_and_the_finalizer_restores_stdout(pager_env):
    pager_env.attach()
    output.emit("one", as_json=False, render=None)
    output.emit("two", as_json=False, render=None)

    assert len(pager_env.popen_calls) == 1
    assert len(pager_env.finalizers) == 1
    pager_env.finalizers[0]()
    assert sys.stdout is pager_env.tty
    assert pager_env.pager.waited


def test_less_gets_frx_and_pager_env_is_respected(pager_env, monkeypatch):
    monkeypatch.delenv("LESS", raising=False)
    monkeypatch.delenv("PAGER", raising=False)
    pager_env.attach()

    output.emit("body", as_json=False, render=None)

    command, kwargs = pager_env.popen_calls[0]
    assert command == ["less"]
    assert kwargs["env"]["LESS"] == "FRX"


def test_a_custom_pager_command_is_used_verbatim(pager_env, monkeypatch):
    monkeypatch.setenv("PAGER", "more -s")
    pager_env.attach()

    output.emit("body", as_json=False, render=None)

    assert pager_env.popen_calls[0][0] == ["more", "-s"]


def test_a_missing_pager_degrades_to_direct_output(pager_env, monkeypatch):
    def popen(command: list[str], **kwargs: Any) -> None:
        raise FileNotFoundError(command[0])

    monkeypatch.setattr(output.subprocess, "Popen", popen)
    pager_env.attach()

    output.emit("body", as_json=False, render=None)

    assert "body" in pager_env.tty.getvalue()


def test_quitting_the_pager_early_is_not_an_error():
    class Closed:
        def write(self, text: str) -> int:
            raise BrokenPipeError

        def flush(self) -> None:
            raise BrokenPipeError

    stream = output._PagerStream(Closed())

    assert stream.write("late") == 4
    stream.flush()
    assert stream.closed_early
