"""Long text in, one line out."""

from __future__ import annotations

import io

import pytest

from plane_proj.text import read_text, to_html, to_plain


def test_a_dash_reads_stdin():
    assert read_text("-", stream=io.StringIO("from a heredoc")) == "from a heredoc"


def test_a_value_is_used_as_given():
    assert read_text("inline", stream=io.StringIO("unused")) == "inline"


def test_absent_stays_absent_so_it_can_be_told_from_empty():
    """An omitted --description leaves the field alone; an empty one clears it."""
    assert read_text(None) is None


@pytest.mark.parametrize(
    ("markdown_text", "expected"),
    [
        ("**bold**", "<strong>bold</strong>"),
        ("## Build", "<h2>Build</h2>"),
        ("- one\n- two", "<li>one</li>"),
        ("`code`", "<code>code</code>"),
    ],
)
def test_markdown_becomes_the_html_plane_stores(markdown_text, expected):
    assert expected in to_html(markdown_text)


def test_html_passes_through_untouched():
    assert to_html("<p>already</p>", already_html=True) == "<p>already</p>"


def test_html_collapses_to_one_line_for_a_listing():
    assert to_plain("<p>one</p>\n<p>two</p>") == "one two"


def test_a_long_line_is_truncated_with_a_marker():
    assert to_plain("<p>" + "x" * 100 + "</p>", limit=10).endswith("…")
    assert len(to_plain("<p>" + "x" * 100 + "</p>", limit=10)) == 10
