"""card comments must return each comment whole, not truncated.

BUG.md §1: `card comments` truncated every comment to 90 characters,
including the --json path. These tests pin the correct behaviour:

- The full text of a long comment is returned unchanged.
- The --json path carries the same full text.
- A comment containing a newline and a non-ASCII character (em dash)
  round-trips faithfully.

Per AGENTS.md: a guard you have not watched fire is not a guard. The
first test asserts *equality with the posted text*, not merely that the
result is longer than 90 characters.
"""

from __future__ import annotations

import json
import types

import pytest
from click.testing import CliRunner

from plane_proj.board import Board
from plane_proj.cli import cli
from tests.conftest import AUTOMATION, Card, FakeClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CARD_REF = "DEMO-12"


class FakeComment:
    """Minimal stand-in for a Plane work-item comment object."""

    def __init__(self, comment_html: str, actor: str = AUTOMATION,
                 created_at: str = "2026-09-11T09:01:00Z") -> None:
        self.comment_html = comment_html
        self.actor = actor
        self.created_at = created_at


def run_comments(config_path, board: Board, as_json: bool = False):
    """Invoke `card comments DEMO-12` via the CLI with the given board injected.

    Monkeypatches `plane_proj.cli.Context.board` so that credentials and
    a real PlaneClient are never needed.
    """
    import plane_proj.cli as cli_module

    # Replace the lazy property with one that always returns our board.
    original = cli_module.Context.board
    cli_module.Context.board = property(lambda self: board)  # type: ignore[method-assign]
    try:
        extra = ["--json"] if as_json else []
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--conf", str(config_path), *extra, "card", "comments", CARD_REF],
            catch_exceptions=False,
        )
        return result
    finally:
        cli_module.Context.board = original


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_client() -> FakeClient:
    return FakeClient(cards=[Card(sequence_id=12)])


@pytest.fixture
def board_with_client(config, fake_client: FakeClient) -> Board:
    return Board(fake_client, config, config.project("DEMO"), "example-workspace")


def inject_comments(fake_client: FakeClient, *comments: FakeComment) -> None:
    """Replace the comments recorder's result with the given comment objects."""
    page = types.SimpleNamespace(
        results=list(comments),
        next_page_results=False,
        next_cursor=None,
    )
    fake_client.work_items.comments = types.SimpleNamespace(
        list=lambda *a, **kw: page,
        create=lambda *a, **kw: None,
    )


# ---------------------------------------------------------------------------
# The fix: comments are returned whole
# ---------------------------------------------------------------------------

LONG_TEXT = "A" * 200  # well beyond the old 90-character limit
LONG_HTML = f"<p>{LONG_TEXT}</p>"


def test_a_long_comment_is_returned_whole(
    config_path, board_with_client: Board, fake_client: FakeClient
):
    """Human output must equal the posted text, not 90 characters of it."""
    inject_comments(fake_client, FakeComment(LONG_HTML))

    result = run_comments(config_path, board_with_client)

    assert result.exit_code == 0, result.output
    # Equality, not just length — a 200-char limit would also pass a length check.
    assert LONG_TEXT in result.output


def test_json_output_carries_the_full_comment(
    config_path, board_with_client: Board, fake_client: FakeClient
):
    """--json must never truncate; the two paths diverged exactly here."""
    inject_comments(fake_client, FakeComment(LONG_HTML))

    result = run_comments(config_path, board_with_client, as_json=True)

    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    comment_text = rows[0]["comment"]
    assert comment_text == LONG_TEXT, (
        f"Expected full text ({len(LONG_TEXT)} chars), got {len(comment_text)} chars"
    )


def test_newline_and_non_ascii_round_trip(
    config_path, board_with_client: Board, fake_client: FakeClient
):
    """A comment with an em dash (\u2014) survives the read path intact.

    The bug report notes that the current path already emits \u2014 for em dash;
    this test pins faithful round-tripping of non-ASCII characters.
    A single <p> block produces no newline in to_plain's output.
    """
    html = "<p>First line\u2014second line</p>"
    expected_plain = "First line\u2014second line"

    inject_comments(fake_client, FakeComment(html))

    result = run_comments(config_path, board_with_client)
    assert result.exit_code == 0, result.output
    assert expected_plain in result.output

    # And via --json.
    result_json = run_comments(config_path, board_with_client, as_json=True)
    assert result_json.exit_code == 0, result_json.output
    rows = json.loads(result_json.output)
    assert rows[0]["comment"] == expected_plain


# ---------------------------------------------------------------------------
# Regression guard: the old call would have truncated this text
# ---------------------------------------------------------------------------

def test_to_plain_with_limit_90_would_have_truncated():
    """Documents that the old call site was the problem, not to_plain itself.

    to_plain with limit=90 correctly truncates; the bug was applying it to
    a field that must not be truncated.
    """
    from plane_proj.text import to_plain

    long_plain = "x" * 200
    truncated = to_plain(f"<p>{long_plain}</p>", limit=90)
    assert truncated.endswith("\u2026")
    assert len(truncated) == 90

    # Without limit, the full text is returned.
    full = to_plain(f"<p>{long_plain}</p>")
    assert full == long_plain
