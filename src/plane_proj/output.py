"""How results are printed.

Two audiences, one command. A person reads the default: aligned columns, names
where the board has UUIDs, nothing that needs a parser. A program passes
`--json` and gets the same data with no formatting applied at all.

The default is terse on purpose. The most frequent caller is an agent paying
for every token it reads back, and a listing that spends four lines per card
is a listing that gets truncated by whoever calls it.
"""

from __future__ import annotations

import atexit
import contextlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import PurePath
from typing import Any

import click

_pager: subprocess.Popen | None = None


class _PagerStream:
    """Stands in for stdout while the pager runs.

    Quitting the pager mid-stream closes its stdin; writes after that are
    swallowed rather than raised, because the person asked to stop
    reading — that is not an error the command failed with.
    """

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self.closed_early = False

    def write(self, text: str) -> int:
        if not self.closed_early:
            try:
                return self._stream.write(text)
            except BrokenPipeError:
                self.closed_early = True
        return len(text)

    def flush(self) -> None:
        if not self.closed_early:
            try:
                self._stream.flush()
            except BrokenPipeError:
                self.closed_early = True

    def isatty(self) -> bool:
        return False


def _start_pager() -> None:
    """Route human output through the user's pager, once per process.

    Only when stdout is a terminal: pipes and files keep raw output.
    `less -FRX` exits by itself when everything fits one screen, so short
    results read exactly as before. COLUMNS carries the real terminal
    width to renderers that would otherwise assume 80 on a pipe. A
    missing or unusable pager degrades to direct output.
    """
    global _pager
    if _pager is not None or not sys.stdout.isatty():
        return
    command = shlex.split(os.environ.get("PAGER") or "less")
    if not command:
        return
    environment = dict(os.environ)
    if PurePath(command[0]).name == "less":
        environment.setdefault("LESS", "FRX")
    os.environ.setdefault(
        "COLUMNS", str(shutil.get_terminal_size().columns)
    )
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            text=True,
            errors="backslashreplace",
            env=environment,
        )
    except OSError:
        return
    original = sys.stdout
    sys.stdout = _PagerStream(process.stdin)

    def finish() -> None:
        sys.stdout = original
        with contextlib.suppress(BrokenPipeError):
            process.stdin.close()
        process.wait()

    atexit.register(finish)
    _pager = process


def emit(payload: Any, *, as_json: bool, render: Any = None) -> None:
    """Print a result as JSON or through the human renderer.

    Human output pages itself on a terminal; `--json` and redirected
    output never touch a pager.
    """
    if as_json:
        if isinstance(payload, list) and any(not isinstance(item, dict) for item in payload):
            raise TypeError("JSON arrays of records must contain objects, not positional rows.")
        click.echo(json.dumps(payload, indent=2, default=str))
        return
    _start_pager()
    if render is None:
        click.echo(payload if isinstance(payload, str) else json.dumps(payload, default=str))
        return
    render(payload)


def table(rows: Sequence[Sequence[str]], headers: Sequence[str]) -> None:
    """Aligned columns, with the last one left to run on.

    Padding the final column would trail whitespace into anything that pipes
    this into a diff or a commit message.
    """
    if not rows:
        click.echo("(none)")
        return
    widths = [len(h) for h in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    def line(cells: Sequence[str]) -> str:
        padded = [
            cell.ljust(widths[index]) if index < len(cells) - 1 else cell
            for index, cell in enumerate(cells)
        ]
        return "  ".join(padded).rstrip()

    click.echo(click.style(line(headers), bold=True))
    for row in rows:
        click.echo(line(row))


def records_table(
    records: Sequence[Mapping[str, Any]], columns: Sequence[tuple[str, str]]
) -> None:
    """Render named JSON records as selected human-readable columns."""
    rows = [
        ["—" if record.get(field) is None else str(record.get(field)) for field, _ in columns]
        for record in records
    ]
    table(rows, [header for _, header in columns])
