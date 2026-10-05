"""Acceptance verdicts as visible structured card comments.

A verdict is one comment: `plane-proj-verdict/v1 ` followed by compact
JSON naming the role, result, git revision, author, note, and operation
id. Comments give each verdict a server timestamp and an append-only
trail; Plane's state history says which verdicts are current. Pure: the
caller fetches comments and activities.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from plane_proj import execution as execution_module
from plane_proj import text as text_module
from plane_proj.guards import GuardViolation, MissingIndependentVerdict

VERDICT_PREFIX = "plane-proj-verdict/v1 "
ROLES = ("qa", "tech-lead")
RESULTS = ("pass", "fail")
_REVISION = re.compile(r"^[0-9a-f]{7,40}$")


@dataclass(frozen=True)
class Verdict:
    role: str
    result: str
    revision: str
    author: str
    note: str
    operation_id: str
    when: datetime
    source_id: str


def _validated(payload: dict[str, Any], where: str = "") -> dict[str, str]:
    """The six verdict fields, or a Verdict rule refusal.

    `where` names the comment a parsed payload came from, so a corrupt
    verdict can be found and removed in Plane.
    """
    if payload.get("role") not in ROLES:
        raise GuardViolation(
            f"Verdict rule{where}: role must be one of {', '.join(ROLES)}; "
            f"got {payload.get('role')!r}."
        )
    if payload.get("result") not in RESULTS:
        raise GuardViolation(
            f"Verdict rule{where}: result must be one of "
            f"{', '.join(RESULTS)}; got {payload.get('result')!r}."
        )
    revision = payload.get("revision")
    if not isinstance(revision, str) or _REVISION.fullmatch(revision) is None:
        raise GuardViolation(
            f"Verdict rule{where}: revision must be a git commit hash of 7 "
            f"to 40 lowercase hex characters; got {revision!r}."
        )
    author = payload.get("author")
    if not isinstance(author, str) or not author.strip():
        raise GuardViolation(
            f"Verdict rule{where}: a verdict names its author; got "
            f"{author!r}."
        )
    note, operation_id = payload.get("note"), payload.get("operation_id")
    if not isinstance(note, str) or not isinstance(operation_id, str):
        raise GuardViolation(
            f"Verdict rule{where}: note and operation_id must be strings."
        )
    return {
        "author": author, "note": note, "operation_id": operation_id,
        "result": payload["result"], "revision": revision,
        "role": payload["role"],
    }


def verdict_fields(
    *, role: str, result: str, revision: str, author: str, note: str,
    operation_id: str,
) -> dict[str, str]:
    """One verdict's validated fields; refuses invalid ones.

    Author and note have whitespace runs collapsed, as the comment reads
    back from Plane that way; a retry then compares equal text.
    """
    return _validated({
        "author": " ".join(author.split()), "note": " ".join(note.split()),
        "operation_id": operation_id,
        "result": result, "revision": revision, "role": role,
    })


def verdict_html(fields: dict[str, str]) -> str:
    """The comment HTML for one verdict's validated fields."""
    body = VERDICT_PREFIX + json.dumps(
        _validated(fields), separators=(",", ":"), sort_keys=True
    )
    return f"<p>{html.escape(body, quote=False)}</p>"


def _plain(comment_html: str | None) -> str:
    return html.unescape(text_module.to_plain(comment_html))


def verdicts(comments: Iterable[Any]) -> list[Verdict]:
    """Every verdict comment, oldest first; a corrupt one refuses."""
    found = []
    for comment in comments:
        plain = _plain(getattr(comment, "comment_html", ""))
        if not plain.startswith(VERDICT_PREFIX):
            continue
        source_id = str(getattr(comment, "id", ""))
        where = (
            f" (comment {source_id}; remove or correct it in Plane)"
        )
        try:
            payload = json.loads(plain.removeprefix(VERDICT_PREFIX))
        except json.JSONDecodeError as error:
            raise GuardViolation(
                f"Verdict rule{where}: the verdict contains invalid JSON."
            ) from error
        if not isinstance(payload, dict):
            raise GuardViolation(
                f"Verdict rule{where}: the verdict must be a JSON object."
            )
        fields = _validated(payload, where)
        try:
            when = execution_module.comment_time(comment)
        except GuardViolation as error:
            raise GuardViolation(
                f"Verdict rule{where}: the verdict has no valid created_at "
                "timestamp, so it cannot be ordered against Verifying."
            ) from error
        found.append(Verdict(**fields, when=when, source_id=source_id))
    return sorted(found, key=lambda verdict: (verdict.when, verdict.source_id))


def already_recorded(
    comments: Iterable[Any], fields: dict[str, str]
) -> bool:
    """Whether this operation already posted this verdict (a lost response).

    The same operation id with different fields is a conflict, never a
    silent success: the board would hold a verdict the caller did not
    ask for.
    """
    for verdict in verdicts(comments):
        if verdict.operation_id != fields["operation_id"]:
            continue
        recorded = {
            "author": verdict.author, "note": verdict.note,
            "operation_id": verdict.operation_id, "result": verdict.result,
            "revision": verdict.revision, "role": verdict.role,
        }
        if recorded == fields:
            return True
        raise GuardViolation(
            f"Verdict rule: operation {fields['operation_id']} already "
            f"recorded a different verdict ({verdict.role} "
            f"{verdict.result} on {verdict.revision} by {verdict.author}, "
            f"comment {verdict.source_id}); use a new operation id."
        )
    return False


STRUCTURED_PREFIXES = (
    execution_module.EVENT_PREFIX,
    execution_module.EVENT_PREFIX_V2,
    execution_module.REWORK_PREFIX,
    VERDICT_PREFIX,
)


def require_free_text(comment_html: str) -> None:
    """Refuse a free comment that would read as a structured record."""
    plain = _plain(comment_html)
    for prefix in STRUCTURED_PREFIXES:
        if plain.startswith(prefix.strip()):
            raise GuardViolation(
                f"Structured comment rule: a comment starting with "
                f"{prefix.strip()!r} is a plane-proj record, and a free "
                "comment must not forge or corrupt one. Use `card verdict`, "
                "`card timer`, `card activity`, or `card transition`, or "
                "reword the comment."
            )


def require_independent_verdicts(
    reference: str, activities: Iterable[Any], comments: Iterable[Any]
) -> None:
    """Refuse Done unless current qa and tech-lead verdicts agree.

    Current means posted after the card last entered Verifying; a card
    with no recorded entry counts every verdict.
    """
    cutoff = execution_module.last_entered(activities, "Verifying")
    every = verdicts(comments)
    current = [v for v in every if cutoff is None or v.when > cutoff]
    since = "" if cutoff is None else (
        f" since it last entered Verifying at {cutoff.isoformat()}"
    )
    problems = []
    latest: dict[str, Verdict] = {}
    for role in ROLES:
        mine = [v for v in current if v.role == role]
        if not mine:
            stale = sum(v.role == role for v in every)
            problems.append(
                f"no {role} verdict{since}"
                + (f" ({stale} older {role} verdict(s) predate that return "
                   "to Verifying and do not count)" if stale else "")
            )
            continue
        latest[role] = mine[-1]
        if mine[-1].result != "pass":
            problems.append(
                f"the latest {role} verdict is {mine[-1].result} "
                f"(by {mine[-1].author} on {mine[-1].revision})"
            )
    if len(latest) == len(ROLES):
        qa, lead = latest["qa"], latest["tech-lead"]
        if qa.revision != lead.revision:
            problems.append(
                f"qa names revision {qa.revision} but tech-lead names "
                f"{lead.revision}"
            )
        if qa.author.strip().casefold() == (
                lead.author.strip().casefold()):
            problems.append(
                f"qa and tech-lead verdicts are both by {qa.author}; an "
                "author must not issue its own acceptance verdict"
            )
    if problems:
        raise MissingIndependentVerdict(
            f"Independent verdict rule: {reference} enters Done only with "
            "passing qa and tech-lead verdicts on one revision from "
            f"different authors{since}. Missing: {'; '.join(problems)}. "
            "Post verdicts with `card verdict`."
        )
