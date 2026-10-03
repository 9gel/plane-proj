"""Long text in, rendered text out.

Descriptions and comments are the two fields worth typing more than a line
into, and both are HTML on the wire. Two consequences shape this module:

- **Long text comes from stdin.** A description written as a shell argument is
  quoted, escaped, and truncated by whatever assembled the command line. `-`
  means "read stdin", the same convention every unix filter uses.
- **Markdown is the input language.** The field is `description_html`, so text
  written straight into it renders as one unbroken paragraph with visible
  asterisks. Callers write markdown and this converts it; `--html` passes text
  through for the caller who already has HTML.
"""

from __future__ import annotations

import re
import sys

import markdown

STDIN_SENTINEL = "-"

# Plane's editor understands a subset of HTML. These extensions produce only
# tags inside it; `nl2br` matters because the editor treats a single newline as
# a line break and markdown by default does not.
MARKDOWN_EXTENSIONS = ("extra", "sane_lists", "nl2br")

_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")


def read_text(value: str | None, *, stream: object = None) -> str | None:
    """Resolve an argument that may be `-`, meaning stdin.

    Returns None only when the caller passed nothing, so "absent" and "empty"
    stay distinguishable — an omitted `--description` leaves the field alone,
    an empty one is a caller trying to erase it.
    """
    if value is None:
        return None
    if value != STDIN_SENTINEL:
        return value
    source = stream if stream is not None else sys.stdin
    return source.read()  # type: ignore[attr-defined]


def to_html(text: str, *, already_html: bool = False) -> str:
    """Markdown to the HTML Plane stores, or the text unchanged when it is HTML."""
    if already_html:
        return text
    return markdown.markdown(text, extensions=list(MARKDOWN_EXTENSIONS))


def to_plain(html: str | None, *, limit: int | None = None) -> str:
    """HTML back to one line, for listings.

    Deliberately crude: this is for a terminal column, never for round-tripping
    a description back into a write.
    """
    if not html:
        return ""
    text = _WHITESPACE.sub(" ", _TAG.sub(" ", html)).strip()
    if limit is not None and len(text) > limit:
        return text[: limit - 1] + "…"
    return text
