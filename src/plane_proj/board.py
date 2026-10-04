"""The live board: name resolution, the guarded writes, and readback.

Everything that touches the network is here. Two shapes of the API drive the
design and are easy to get wrong:

- **Creating a card puts it in neither a cycle nor a module.** The create call
  has no field for either; both are separate requests needing the id of a card
  that does not exist until the create returns. `create_card` therefore does
  all three and reports what it did, and refuses to start without both.
- **`point` is not what the board reads.** The UI shows `estimate_point`, a
  UUID into the project's scale. A card carrying `point: 2` and no
  `estimate_point` displays as unestimated. `Project.estimate_fields` writes
  both from one number and no caller here handles a scale UUID.

**Two SDK defects are worked around, both measured against a self-hosted
instance and both narrowly scoped.** They are worked around rather than
avoided because the alternative is hand-rolling the whole client.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import requests
from plane.api.base_resource import BaseResource
from plane.client.plane_client import PlaneClient
from plane.errors import HttpError
from plane.models.cycles import CreateCycle, TransferCycleWorkItemsRequest, UpdateCycle
from plane.models.intake import CreateIntakeWorkItem, UpdateIntakeWorkItem
from plane.models.modules import CreateModule, UpdateModule
from plane.models.projects import ProjectFeature
from plane.models.query_params import PaginatedQueryParams
from plane.models.work_items import (
    CreateWorkItem,
    CreateWorkItemComment,
    CreateWorkItemRelation,
    RemoveWorkItemRelation,
    WorkItemForIntakeRequest,
)

from plane_proj.config import PROJECT_ENV_VAR, Config, Project, Rules, project_from_facts
from plane_proj.credentials import Credentials
from plane_proj.execution import open_timer
from plane_proj.guards import (
    ConfigError,
    EmptyCycle,
    EstimateOnUnestimatedAssignee,
    GuardViolation,
    MissingCycle,
    MissingEstimate,
    MissingModule,
    OrphanedCard,
    ReadbackFailed,
    ScaleContradiction,
)
from plane_proj.sprints import timestamps_equal

RELATION_TYPES = (
    "blocking",
    "blocked_by",
    "duplicate",
    "relates_to",
    "start_before",
    "start_after",
    "finish_before",
    "finish_after",
)

INTAKE_STATUS_NAMES = {
    -2: "Pending",
    -1: "Rejected",
    0: "Snoozed",
    1: "Accepted",
    2: "Duplicate",
}

BATCH_CARD_MAXIMUM = 100
_SPRINT_NAME = re.compile(r"^Sprint ([1-9][0-9]*)(?:\b|\s*[-—:])")
SETTLED_STATES = frozenset({"done", "cancelled"})

_EVIDENCE_KIND = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")


def _digest_from_url(url: str) -> tuple[str, int]:
    """Stream a signed storage URL and return its SHA-256 and byte size.

    A separate session has no Plane authentication headers, and ambient
    authentication is disabled so .netrc credentials never reach storage.
    Request errors may contain the signed URL; they are never printed.
    """
    digest = hashlib.sha256()
    size = 0
    try:
        with requests.Session() as session:
            session.trust_env = False
            with session.get(url, stream=True, timeout=120) as response:
                response.raise_for_status()
                for chunk in response.iter_content(chunk_size=65536):
                    digest.update(chunk)
                    size += len(chunk)
    except requests.RequestException as error:
        raise ConfigError(
            "Attachment readback failed; the archive is unverified."
        ) from error
    return digest.hexdigest(), size


@dataclass(frozen=True)
class CardWrite:
    """What a write actually did, so the caller reports facts and not intentions."""

    card_id: str
    sequence_id: int | None
    steps: tuple[str, ...]

    @property
    def reference(self) -> str:
        return str(self.sequence_id) if self.sequence_id is not None else self.card_id


class Board:
    """One project on one workspace, with this project's rules applied to every write."""

    def __init__(
        self, client: PlaneClient, config: Config, project: Project, slug: str
    ) -> None:
        self.client = client
        self.config = config
        self.project = project
        # From the resolved credentials, never the config file: the file's slug
        # is optional and the environment supplies it when absent.
        self.slug = slug

    # ---- reads ---------------------------------------------------------

    def cards(self, *, include_archived: bool = False) -> list[Any]:
        """Every work item on the project, following the cursor to the end.

        Paged rather than trusting one response: a default page size that
        happens to exceed today's card count is a guard that stops working on
        the day the board grows, and silently.
        """
        cards = list(self._paged(lambda cursor: self.client.work_items.list(
            self.slug, self.project.id, params=_cursor_params(cursor)
        )))
        if not include_archived:
            return cards
        try:
            archived = list(self._paged(lambda cursor: self.client.work_items.list_archived(
                self.slug, self.project.id, params=_cursor_params(cursor)
            )))
        except HttpError as error:
            if error.status_code != 404:
                raise
            raise ConfigError(
                "card list --all requires the archived-work-items API, but Plane returned 404. "
                "This server cannot provide a complete archived-card listing through that "
                "endpoint; refusing to report a partial list as all cards."
            ) from error
        return list({str(card.id): card for card in [*cards, *archived]}.values())

    def intake_items(self) -> list[Any]:
        """Every Intake record, retaining its optional underlying work item."""
        return list(self._paged(lambda cursor: self.client.intake.list(
            self.slug, self.project.id, params=_intake_params(cursor)
        )))

    def intake_attachments(self, item: Any) -> list[dict[str, Any]]:
        """List attachments using the Intake item's work-item ID, including Pending items."""
        issue_id = getattr(item, "issue", None)
        if not issue_id:
            raise ConfigError("Intake item has no work-item ID for attachment lookup.")
        return self.attachments(issue_id)

    def attachments(self, issue_id: str) -> list[dict[str, Any]]:
        """List regular attachments for an ordinary or Pending work item."""
        attachments = self.client.work_items.attachments.list(self.slug, self.project.id, issue_id)
        records = []
        for attachment in attachments:
            attachment_id = getattr(attachment, "id", None)
            if not attachment_id:
                raise ConfigError("Plane returned an attachment without a download identifier.")
            attributes = getattr(attachment, "attributes", None) or {}
            size = getattr(attachment, "size", None)
            records.append({
                "attachment_id": str(attachment_id),
                "name": attributes.get("name") or str(attachment_id),
                "content_type": attributes.get("type"),
                "size": size if size is not None else attributes.get("size"),
            })
        return records

    def download_attachment(
        self, issue_id: str, attachment_id: str, destination: Path,
    ) -> dict[str, Any]:
        """Stream an attachment to a new local file without sending the Plane key to storage."""
        if destination.exists() or destination.is_symlink():
            raise ConfigError(f"{destination} already exists; choose a new output path.")
        if not destination.parent.is_dir():
            raise ConfigError(f"Output directory {destination.parent} does not exist.")
        if not issue_id:
            raise ConfigError("No work-item ID for attachment download.")
        url = self.client.work_items.attachments.get_download_url(
            self.slug, self.project.id, issue_id, attachment_id,
        )
        if not isinstance(url, str):
            raise ConfigError("Plane did not provide an attachment download URL.")
        url = urljoin(self.client.config.base_path, url)
        if urlsplit(url).scheme not in {"http", "https"}:
            raise ConfigError("Plane returned an unsupported attachment download URL scheme.")
        size = 0
        with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".plane-download-") as work:
            temporary = Path(work) / "attachment"
            try:
                # A separate session has no Plane authentication headers. Disabling
                # ambient authentication prevents .netrc credentials reaching storage.
                with requests.Session() as session:
                    session.trust_env = False
                    with session.get(url, stream=True, timeout=120) as response:
                        response.raise_for_status()
                        content_type = response.headers.get("Content-Type")
                        with temporary.open("wb") as stream:
                            for chunk in response.iter_content(chunk_size=65536):
                                stream.write(chunk)
                                size += len(chunk)
            except requests.RequestException as error:
                # Request errors may contain a signed URL; do not print it.
                raise ConfigError(
                    "Attachment download failed; no output file was saved."
                ) from error
            # Atomic, no-overwrite publication. The temporary file is on the same filesystem.
            try:
                os.link(temporary, destination)
            except FileExistsError as error:
                raise ConfigError(
                    f"{destination} already exists; it was not overwritten."
                ) from error
        return {"attachment_id": attachment_id, "path": str(destination),
                "size": size, "content_type": content_type}

    def _attachment_digest(
        self, issue_id: str, attachment_id: str
    ) -> tuple[str, int]:
        """Stream an attachment back and return its SHA-256 and byte size.

        The same credential rules as `download_attachment`: the signed
        storage URL is fetched without Plane headers or ambient .netrc
        authentication, and request errors never print the URL.
        """
        url = self.client.work_items.attachments.get_download_url(
            self.slug, self.project.id, issue_id, attachment_id,
        )
        if not isinstance(url, str):
            raise ConfigError(
                "Plane did not provide an attachment download URL."
            )
        url = urljoin(self.client.config.base_path, url)
        if urlsplit(url).scheme not in {"http", "https"}:
            raise ConfigError(
                "Plane returned an unsupported attachment download "
                "URL scheme."
            )
        return _digest_from_url(url)

    def upload_evidence(
        self, card: Any, path: Path, *, kind: str, revision: str | None,
    ) -> dict[str, Any]:
        """Archive one evidence file with a byte-verified receipt.

        Uploads the exact bytes, reads them back from storage, and compares
        SHA-256 and size before reporting success. An existing attachment
        with the same name, size, and digest is reused rather than
        duplicated — which is also what makes a retry after a lost upload
        response safe. The local input is never deleted here; removal
        eligibility is the caller's decision after a verified receipt.
        """
        if _EVIDENCE_KIND.fullmatch(kind) is None:
            raise GuardViolation(
                f"Evidence kind {kind!r} is not a lowercase slug."
            )
        if not path.is_file():
            raise ConfigError(f"{path} is not a readable file.")
        payload = path.read_bytes()
        if not payload:
            raise GuardViolation(
                f"{path} is empty; an empty evidence archive proves "
                "nothing and is refused."
            )
        digest = hashlib.sha256(payload).hexdigest()
        name = path.name
        existing = self._matching_evidence(
            card, name=name, size=len(payload), digest=digest
        )
        if existing is not None:
            return self._evidence_receipt(
                card, existing, name=name, digest=digest,
                size=len(payload), kind=kind, revision=revision,
                reused=True,
            )
        content_type = (
            mimetypes.guess_type(name)[0] or "application/octet-stream"
        )
        uploaded = self.client.work_items.attachments.upload_from_bytes(
            self.slug, self.project.id, str(card.id),
            payload, name, content_type,
        )
        attachment_id = str(getattr(uploaded, "id", "") or "")
        if not attachment_id:
            raise ReadbackFailed(
                "Plane accepted the evidence upload but returned no "
                "attachment id; the archive cannot be verified."
            )
        written_digest, written_size = self._attachment_digest(
            str(card.id), attachment_id
        )
        if (written_digest, written_size) != (digest, len(payload)):
            raise ReadbackFailed(
                f"Evidence {name} was uploaded but reads back as "
                f"{written_size} bytes, sha256 {written_digest}; expected "
                f"{len(payload)} bytes, sha256 {digest}. The archive is "
                "not a faithful copy."
            )
        return self._evidence_receipt(
            card, attachment_id, name=name, digest=digest,
            size=len(payload), kind=kind, revision=revision, reused=False,
        )

    def _matching_evidence(
        self, card: Any, *, name: str, size: int, digest: str
    ) -> str | None:
        """An existing attachment carrying exactly these bytes, or None.

        Name and size narrow the candidates cheaply; the digest decides.
        A same-name attachment with different bytes is not a match and is
        left alone — evidence is append-only, never silently replaced.
        """
        for record in self.attachments(str(card.id)):
            if record["name"] != name or record["size"] != size:
                continue
            found_digest, found_size = self._attachment_digest(
                str(card.id), record["attachment_id"]
            )
            if (found_digest, found_size) == (digest, size):
                return record["attachment_id"]
        return None

    def verify_evidence(
        self, card: Any, attachment_id: str, *, expected_digest: str | None,
    ) -> dict[str, Any]:
        """Re-verify an archived attachment's bytes against storage."""
        records = {
            record["attachment_id"]: record
            for record in self.attachments(str(card.id))
        }
        if attachment_id not in records:
            raise ConfigError(
                f"No attachment {attachment_id} on this card. "
                "Use the IDs shown by card show."
            )
        digest, size = self._attachment_digest(str(card.id), attachment_id)
        if expected_digest is not None and digest != expected_digest:
            raise ReadbackFailed(
                f"Attachment {attachment_id} reads back as sha256 "
                f"{digest}, not the expected {expected_digest}; the "
                "archive does not hold the claimed bytes."
            )
        return {
            "card_id": str(card.id),
            "attachment_id": attachment_id,
            "name": records[attachment_id]["name"],
            "sha256": digest,
            "size": size,
            "verified": True,
        }

    @staticmethod
    def _evidence_receipt(
        card: Any, attachment_id: str, *, name: str, digest: str,
        size: int, kind: str, revision: str | None, reused: bool,
    ) -> dict[str, Any]:
        return {
            "card_id": str(card.id),
            "attachment_id": attachment_id,
            "name": name,
            "sha256": digest,
            "size": size,
            "kind": kind,
            "revision": revision,
            "verified": True,
            "reused": reused,
        }

    def find_intake(self, reference: str) -> Any:
        """One Intake record by Intake id, work-item id, or card reference."""
        wanted = reference.strip()
        _, _, tail = wanted.rpartition("-")
        sequence = int(tail) if tail.isdigit() else None
        for item in self.intake_items():
            detail = getattr(item, "issue_detail", None)
            identifiers = {
                str(value)
                for value in (getattr(item, "id", None), getattr(item, "issue", None))
                if value is not None
            }
            if wanted and wanted in identifiers:
                return item
            if sequence is not None and getattr(detail, "sequence_id", None) == sequence:
                return item
        raise ConfigError(
            f"No Intake item {reference!r} on {self.project.key}. Use an Intake id, "
            f"work-item id, or {self.project.key}-12."
        )

    def intake_work_item(self, item: Any) -> Any | None:
        """The Intake record's work item, retrieving it when expansion was omitted."""
        detail = getattr(item, "issue_detail", None)
        if detail is not None:
            return detail
        work_item_id = getattr(item, "issue", None)
        if work_item_id is None:
            return None
        refreshed = self.client.intake.retrieve(
            self.slug, self.project.id, str(work_item_id)
        )
        detail = getattr(refreshed, "issue_detail", None)
        if detail is None:
            raise ConfigError(
                f"Plane returned no work-item details for Intake record "
                f"{getattr(item, 'id', None)!r} (work item {work_item_id!r})."
            )
        return detail

    def find(self, reference: str) -> Any:
        """One card, by `DEMO-12`, by bare sequence number, or by UUID."""
        wanted = reference.strip()
        if _looks_like_uuid(wanted):
            return self.client.work_items.retrieve(self.slug, self.project.id, wanted)

        _, _, tail = wanted.rpartition("-")
        if not tail.isdigit():
            raise ConfigError(
                f"{reference!r} is not a card reference. Use {self.project.key}-12, 12, or a UUID."
            )
        sequence = int(tail)
        for card in self.cards():
            if getattr(card, "sequence_id", None) == sequence:
                return card
        raise ConfigError(f"No card {self.project.key}-{sequence} on {self.project.key}.")

    def relations(self, card_id: str) -> dict[str, list[str]]:
        """A card's relations, as related-card ids per relation type.

        **Two traps, both measured.** `client.work_items.relations.list`
        declares each bucket as `list[str]` while this server returns objects,
        so the typed call raises `ValidationError` on any card that has a
        relation — including the readback straight after a successful write.
        The request itself is correct, so this reuses the SDK's own
        authenticated, retrying session and stops one layer short of the model.

        And the object it returns is `{"project_id", "issue_id"}` — **no `id`,
        no `sequence_id`, no `name`.** A readback keyed on `id` therefore finds
        nothing and reports a write that in fact landed. Only the related
        card's id is available here; anything else a caller wants to display
        comes from `card_index`.
        """
        raw = self.client.work_items.relations._get(  # noqa: SLF001 — see docstring
            f"{self.slug}/projects/{self.project.id}/work-items/{card_id}/relations"
        )
        return {
            name: [str(entry["issue_id"]) for entry in (raw.get(name) or []) if "issue_id" in entry]
            for name in RELATION_TYPES
        }

    def card_index(self) -> dict[str, Any]:
        """Every card keyed by id, for turning a relation's bare id into a reference."""
        return {str(card.id): card for card in self.cards()}

    def cycle_card_ids(self, cycle_id: str) -> set[str]:
        """Every card id in a cycle."""
        return {str(getattr(item, "id", "")) for item in self._cycle_cards(cycle_id)}

    def cycle_cards(self, cycle_id: str) -> list[Any]:
        """Return every work item in one cycle."""
        return self._cycle_cards(cycle_id)

    def _cycle_cards(self, cycle_id: str) -> list[Any]:
        return list(self._paged(lambda cursor: self.client.cycles.list_work_items(
            self.slug, self.project.id, cycle_id, params=_cursor_params(cursor)
        )))

    def _sprint_card_points(self, card: Any) -> int:
        """Distinguish genuinely unestimated cards from an outdated local scale."""
        if not self.project.estimates_enabled:
            return 0
        estimate = getattr(card, "estimate_point", None)
        if estimate is None:
            return 0
        points = self.project.estimate_value(estimate)
        if points is None:
            raise ConfigError(
                f"Sprint totals require a known estimate scale: card "
                f"{self.project.key}-{getattr(card, 'sequence_id', '?')} has an unknown "
                "estimate ID. Run `plane-proj project scale --write` with the same config."
            )
        return points

    def sprint_cycle_metrics(self, cycle_id: str) -> dict[str, int]:
        """Count all, Done, and Cancelled cards and points in one live cycle."""
        metrics = {
            "cards_current": 0,
            "points_current": 0,
            "cards_done": 0,
            "points_done": 0,
            "cards_cancelled": 0,
            "points_cancelled": 0,
        }
        for card in self._cycle_cards(cycle_id):
            points = self._sprint_card_points(card)
            metrics["cards_current"] += 1
            metrics["points_current"] += points
            state = self._state_name(getattr(card, "state", None)).casefold()
            if state in {"done", "cancelled"}:
                metrics[f"cards_{state}"] += 1
                metrics[f"points_{state}"] += points
        return metrics

    def sprint_cycle_cards(self, cycle_id: str) -> list[dict[str, Any]]:
        """Reference, title, state name, and points of every live cycle card."""
        return [
            {
                "id": str(card.id),
                "ref": f"{self.project.key}-{getattr(card, 'sequence_id', '?')}",
                "title": str(getattr(card, "name", "") or ""),
                "state": self._state_name(getattr(card, "state", None)),
                "points": self._sprint_card_points(card),
            }
            for card in self._cycle_cards(cycle_id)
        ]

    def dependency_facts(self, cycles: Mapping[int, str]) -> dict[str, Any]:
        """Open cards of the given sprint cycles, with blockers and states.

        Each cycle is read once (`members` keeps every card, settled ones
        too) and each open card's relations once. A blocker outside these
        cycles is retrieved by id; one the server no longer returns (404)
        is archived, which Plane allows only for Completed or Cancelled
        work items. The archived-items listing is not used: some servers
        answer it with 404 (DESIGN.md §7c).
        """
        members: dict[int, list[dict[str, Any]]] = {}
        cards: list[dict[str, Any]] = []
        blocked_by: dict[str, list[str]] = {}
        for sprint_id, cycle_id in cycles.items():
            members[sprint_id] = self.sprint_cycle_cards(cycle_id)
            for card in members[sprint_id]:
                if card["state"].casefold() in {"done", "cancelled"}:
                    continue
                cards.append(card | {"sprint": sprint_id})
                relations = self.relations(card["id"])
                blocked_by[card["id"]] = relations["blocked_by"]
        states = {
            card["id"]: (card["ref"], card["state"])
            for found in members.values() for card in found
        }
        outside = {
            blocker for ids in blocked_by.values() for blocker in ids
        } - set(states)
        for blocker in sorted(outside):
            states[blocker] = self._blocker_state(blocker)
        return {"cards": cards, "blocked_by": blocked_by,
                "states": states, "members": members}

    def _blocker_state(self, card_id: str) -> tuple[str, str]:
        try:
            card = self.client.work_items.retrieve(
                self.slug, self.project.id, card_id
            )
        except HttpError as error:
            if error.status_code != 404:
                raise
            return (f"archived {card_id[:8]}", "Archived")
        return (
            f"{self.project.key}-{getattr(card, 'sequence_id', '?')}",
            self._state_name(getattr(card, "state", None)),
        )

    def sprint_cycles(self, sprint_ids: set[int]) -> dict[int, str]:
        """Live cycle ids named `Sprint N` for each wanted N that has one."""
        found: dict[int, str] = {}
        for cycle in self.groupings("cycle"):
            match = _SPRINT_NAME.match(str(cycle.name))
            if match is None or int(match.group(1)) not in sprint_ids:
                continue
            sprint_id = int(match.group(1))
            if sprint_id in found:
                raise ConfigError(f"More than one Plane cycle is named for Sprint {sprint_id}.")
            found[sprint_id] = str(cycle.id)
        return found

    def sprint_membership(
        self, current: Mapping[int, str], planned: set[int],
    ) -> tuple[dict[str, tuple[int, bool]], list[int]]:
        """Which open sprint holds each card, and planned sprints with no cycle.

        `current` maps a running sprint to its bound cycle id; a planned
        sprint joins its cycle by the `Sprint N` name. A card maps to
        `(sprint_id, running)`. Closed sprints are open to no card.
        """
        planned_cycles = self.sprint_cycles(planned)
        members: dict[str, tuple[int, bool]] = {}
        for running, cycles in ((True, current), (False, planned_cycles)):
            for sprint_id, cycle_id in cycles.items():
                for card_id in self.cycle_card_ids(cycle_id):
                    members[card_id] = (sprint_id, running)
        return members, sorted(planned - planned_cycles.keys())

    def orphaned_cards(
        self, members: Mapping[str, tuple[int, bool]], cards: Sequence[Any],
    ) -> list[Any]:
        """Open cards in no planned or current sprint.

        With cycles on, every card that is not Done or Cancelled belongs to
        a sprint. A backlog of cards in no sprint is where work goes to be
        forgotten: nothing plans it, nothing closes it, and it is never
        finished or cancelled.
        """
        if not self.project.rules.require_cycle:
            return []
        return [
            card for card in cards
            if str(card.id) not in members
            and self._state_name(getattr(card, "state", None)).casefold()
            not in SETTLED_STATES
        ]

    def require_no_orphans(
        self, current: Mapping[int, str], planned: set[int],
    ) -> None:
        """Refuse while any open card belongs to no planned or current sprint."""
        if not self.project.rules.require_cycle:
            return
        members, _ = self.sprint_membership(current, planned)
        orphans = self.orphaned_cards(members, self.cards())
        if orphans:
            names = ", ".join(self._reference(card) for card in orphans)
            raise OrphanedCard(
                f"{len(orphans)} open card(s) belong to no planned or current "
                f"sprint: {names}. Every card not Done or Cancelled belongs to "
                "a sprint; set-cycle each to a planned sprint's cycle (plan a "
                "one-card sprint if none fits) or cancel it. `sprints check` "
                "lists them."
            )

    def sprint_findings(
        self, current: Mapping[int, str], planned: set[int],
    ) -> list[dict[str, str]]:
        """Audit sprint membership, card states, and timers; writes nothing."""
        members, cycleless = self.sprint_membership(current, planned)
        cards = self.cards()
        findings = [
            {"card": self._reference(card), "finding": "in no sprint",
             "detail": f"state {self._state_name(getattr(card, 'state', None))}; "
                       "set-cycle to a planned sprint or cancel"}
            for card in self.orphaned_cards(members, cards)
        ]
        for card in cards:
            if str(card.id) not in members:
                continue
            sprint_id, running = members[str(card.id)]
            state = self._state_name(getattr(card, "state", None))
            if running and state.casefold() == "backlog":
                findings.append({
                    "card": self._reference(card),
                    "finding": "backlog in running sprint",
                    "detail": f"Sprint {sprint_id} cannot close; move to Todo "
                              "or set-cycle to a planned sprint",
                })
            elif not running and state.casefold() not in SETTLED_STATES | {"backlog"}:
                findings.append({
                    "card": self._reference(card),
                    "finding": "active in unstarted sprint",
                    "detail": f"{state} in Sprint {sprint_id}, which has not started",
                })
            elif running and state.casefold() in SETTLED_STATES:
                opened = open_timer(self.comments(card))
                if opened is not None:
                    findings.append({
                        "card": self._reference(card),
                        "finding": "open timer on settled card",
                        "detail": f"{opened['category']} still running; card timer stop",
                    })
        findings.extend(
            {"card": "—", "finding": "planned sprint has no cycle",
             "detail": f"Sprint {sprint_id}: cycle new 'Sprint {sprint_id} …'"}
            for sprint_id in cycleless
        )
        return findings

    def _reference(self, card: Any) -> str:
        return f"{self.project.key}-{getattr(card, 'sequence_id', '?')}"

    def planned_sprint_totals(self, sprint_ids: set[int]) -> dict[int, tuple[int, int]]:
        """Return live card and point totals for matching `Sprint N` cycles."""
        totals: dict[int, tuple[int, int]] = {}
        for sprint_id, cycle_id in self.sprint_cycles(sprint_ids).items():
            # Cancelled members are not future work; they never count.
            cards = [
                card for card in self._cycle_cards(cycle_id)
                if self._state_name(getattr(card, "state", None)).casefold()
                != "cancelled"
            ]
            points = sum(
                self._sprint_card_points(card)
                for card in cards
            )
            totals[sprint_id] = (len(cards), points)
        return totals

    # ---- writes --------------------------------------------------------

    def create_intake(self, title: str, description_html: str) -> Any:
        """File a pending Intake item and verify its stored content."""
        if not title.strip():
            raise GuardViolation("An Intake item needs a title.")
        if not description_html.strip():
            raise GuardViolation("An Intake item needs a description.")

        created = self.client.intake.create(
            self.slug,
            self.project.id,
            data=CreateIntakeWorkItem(issue=WorkItemForIntakeRequest(
                name=title, description_html=description_html,
            )),
        )
        issue_id = getattr(created, "issue", None)
        if not getattr(created, "id", None) or not issue_id:
            raise ReadbackFailed("Plane created Intake without both resource IDs.")
        stored = self.client.intake.retrieve(
            self.slug, self.project.id, str(issue_id)
        )
        detail = getattr(stored, "issue_detail", None)
        if (
            getattr(stored, "id", None) != getattr(created, "id", None)
            or getattr(stored, "issue", None) != issue_id
            or getattr(stored, "status", None) != -2
            or getattr(detail, "name", None) != title
            or getattr(detail, "description_html", None) != description_html
        ):
            raise ReadbackFailed(
                f"Intake item {issue_id} did not read back as pending "
                "with the submitted title and description."
            )
        return stored

    def create_card(
        self,
        *,
        title: str,
        description_html: str,
        assignee_id: str,
        module_name: str | None,
        cycle_name: str | None,
        estimate: int | None,
        state_name: str | None = None,
        label_names: Sequence[str] = (),
        priority: str | None = None,
        blank_estimate: bool = False,
    ) -> CardWrite:
        """Create a card and place it, or refuse before sending anything.

        Every check runs before the first request. A card created and then
        rejected for its module is worse than no card: it exists, it is
        unplaced, and nobody is looking for it.
        """
        rules = self.project.rules
        if not title.strip():
            raise GuardViolation("A card needs a title.")
        if not description_html.strip():
            raise GuardViolation(
                f"A card needs a description — it is where the instruction lives, and a card "
                f"nobody can act on without asking is not a card. Title: {title!r}."
            )
        if rules.require_module and not module_name:
            raise MissingModule(
                f"No module for {title!r}. {self.project.key} requires one on every card: it "
                f"says which part of the repository the work is in, and a card without one is "
                f"in no module rollup. Known: {', '.join(sorted(self.project.modules)) or 'none'}."
            )
        if rules.require_cycle and not cycle_name:
            raise MissingCycle(
                f"No cycle for {title!r}. Every open card belongs to a sprint, and creating "
                f"a card does not put it in one — the create call has no cycle field. Name "
                f"the cycle of the current or a planned sprint; if none fits, plan a "
                f"one-card sprint and grow its scope later."
            )

        self._check_estimate(assignee_id=assignee_id, estimate=estimate,
                             blank_estimate=blank_estimate)
        self.project.check_admission_estimate(estimate, state_name)


        module_id = self.project.module_id(module_name) if module_name else None
        cycle_id = self.project.cycle_id(cycle_name) if cycle_name else None

        payload: dict[str, Any] = {
            "name": title,
            "description_html": description_html,
            "assignees": [assignee_id],
        }
        if estimate is not None:
            payload.update(self.project.estimate_fields(estimate))
        if state_name:
            payload["state"] = self.project.state_id(state_name)
        if label_names:
            payload["labels"] = [self.project.label_id(name) for name in label_names]
        if priority:
            payload["priority"] = priority

        created = self.client.work_items.create(
            self.slug, self.project.id, data=CreateWorkItem(**payload)
        )
        steps = ["created"]

        if cycle_id:
            self.add_to_cycle(created.id, cycle_id)
            steps.append(f"cycle={cycle_name}")
        if module_id:
            self.add_to_module(created.id, module_id)
            steps.append(f"module={module_name}")

        self._verify_card(created.id, estimate=estimate, state_name=state_name)
        steps.append("verified")
        return CardWrite(created.id, getattr(created, "sequence_id", None), tuple(steps))

    def accept_intake(self, item: Any) -> Any:
        """Accept one pending Intake item and confirm it became a project card."""
        accepted = self._transition_intake(item, status=1, action="accepted")
        issue_id = accepted.issue
        card = self.client.work_items.retrieve(
            self.slug, self.project.id, str(issue_id)
        )
        if (
            str(getattr(card, "id", "")) != str(issue_id)
            or getattr(card, "sequence_id", None) is None
        ):
            raise ReadbackFailed(
                f"Accepted Intake item {issue_id} did not become project work item {issue_id}."
            )
        return accepted

    def reject_intake(self, item: Any) -> Any:
        """Reject one pending Intake item and confirm its triage status."""
        return self._transition_intake(item, status=-1, action="rejected")

    def _transition_intake(
        self, item: Any, *, status: int, action: str
    ) -> Any:
        """Apply one terminal Intake decision and require an exact readback."""
        issue_id = getattr(item, "issue", None)
        if not issue_id:
            raise ConfigError(
                f"Intake item {getattr(item, 'id', None)!r} has no work-item id, so Plane's "
                "Intake status endpoint cannot address it."
            )
        current_status = getattr(item, "status", None)
        if current_status != -2:
            name = INTAKE_STATUS_NAMES.get(
                current_status,
                str(current_status) if current_status is not None else "unknown",
            )
            raise GuardViolation(
                f"Only a pending Intake item can be {action}; {issue_id} is {name}."
            )

        # The SDK also exposes `update_status`, but that targets a `/status`
        # subroute which Plane's PAT API does not serve (404). Triage status is
        # updated through the ordinary Intake PATCH endpoint.
        self.client.intake.update(
            self.slug,
            self.project.id,
            str(issue_id),
            data=UpdateIntakeWorkItem(status=status),
        )
        transitioned = self.client.intake.retrieve(
            self.slug,
            self.project.id,
            str(issue_id),
        )
        written_status = getattr(transitioned, "status", None)
        if written_status != status:
            name = INTAKE_STATUS_NAMES.get(
                written_status,
                str(written_status) if written_status is not None else "unknown",
            )
            raise ReadbackFailed(
                f"Plane did not mark Intake item {issue_id} as "
                f"{INTAKE_STATUS_NAMES[status]}; it still reads as {name}. "
                "On this self-hosted Plane API, the API user must be a project Admin "
                "(role greater than 15) for Intake decisions."
            )
        return transitioned

    def update_card(self, card: Any, fields: Mapping[str, Any]) -> None:
        """Patch a card.

        **Sent raw, not through the SDK's typed update.** That method
        serialises with `model_dump(exclude_none=True)`, so **every field set
        to `None` is silently dropped** — the request goes out without them,
        the server answers 200, and nothing changed. Clearing a field is
        therefore impossible through it, which is exactly what blanking an
        estimate is. Measured: a card left `point: 0` after a blank the tool
        reported as done.

        The server itself accepts `{"point": null}` and clears the field, so
        this is the SDK's limit and not Plane's.
        """
        if not fields:
            raise GuardViolation("Nothing to update.")
        self.client.work_items._patch(  # noqa: SLF001 — see docstring
            f"{self.slug}/projects/{self.project.id}/work-items/{card.id}", dict(fields)
        )

    def set_estimate(
        self, card: Any, estimate: int | None, *, blank_estimate: bool = False,
    ) -> None:
        """Set or blank a card's estimate, both fields together.

        Blank is `None` on both, never `point: 0` — zero reads as an estimate
        of nothing rather than as no estimate, and it counts in totals.
        """
        assignee_id = _first_assignee(card)
        self._check_estimate(
            assignee_id=assignee_id, estimate=estimate, allow_missing_assignee=True,
            blank_estimate=blank_estimate,
        )
        self.project.check_admission_estimate(
            estimate, self._state_name(getattr(card, "state", None))
        )
        if estimate is None:
            self.update_card(card, {"estimate_point": None, "point": None})
            self._verify_blank(card.id)
            return
        self.update_card(card, self.project.estimate_fields(estimate))
        self._verify_card(card.id, estimate=estimate, state_name=None)

    def move_state(self, card: Any, state_name: str) -> None:
        self.check_admission(card, state_name)
        self.update_card(card, {"state": self.project.state_id(state_name)})
        self._verify_card(card.id, estimate=None, state_name=state_name)

    def move_states(
        self,
        references: Sequence[str],
        *,
        from_state: str,
        to_state: str,
    ) -> list[Any]:
        """Move a bounded card set after validating the complete selection.

        Plane's public API has no generic bulk-update endpoint. This command
        therefore shares one list lookup across the batch, but keeps the
        supported per-card patch and readback contract.
        """
        source_id = self.project.state_id(from_state)
        target_id = self.project.state_id(to_state)
        if source_id == target_id:
            raise GuardViolation("Batch source and target states must differ.")

        available = self.cards()
        selected = self._select_batch_cards(available, references, source_id)
        if not selected:
            raise GuardViolation(f"No cards are in {from_state!r}; nothing to update.")
        if len(selected) > BATCH_CARD_MAXIMUM:
            raise GuardViolation(
                f"Batch selects {len(selected)} cards; the maximum is {BATCH_CARD_MAXIMUM} "
                "so one invocation attempts to stay below Plane's rate limit. "
                "Split the selection and wait for the next rate-limit window."
            )
        for card in selected:
            self.check_admission(card, to_state)

        stale = [
            card for card in selected if str(getattr(card, "state", "")) != source_id
        ]
        if stale:
            details = ", ".join(
                f"{self.project.key}-{getattr(card, 'sequence_id', '?')} is "
                f"{self._state_name(getattr(card, 'state', None))}"
                for card in stale
            )
            raise GuardViolation(
                f"Batch selection changed: {details}; expected {from_state}. "
                "No cards were updated."
            )

        for card in selected:
            self.update_card(card, {"state": target_id})
            self._verify_card(card.id, estimate=None, state_name=to_state)
        return selected

    def _select_batch_cards(
        self,
        available: Sequence[Any],
        references: Sequence[str],
        source_id: str,
    ) -> list[Any]:
        """Resolve a batch from one card listing, refusing ambiguous input."""
        if not references:
            return [
                card for card in available
                if str(getattr(card, "state", "")) == source_id
            ]

        by_id = {str(card.id): card for card in available}
        by_sequence = {getattr(card, "sequence_id", None): card for card in available}
        selected = []
        seen = set()
        for reference in references:
            wanted = reference.strip()
            if _looks_like_uuid(wanted):
                card = by_id.get(wanted)
            else:
                _, _, tail = wanted.rpartition("-")
                if not tail.isdigit():
                    raise ConfigError(
                        f"{reference!r} is not a card reference. Use "
                        f"{self.project.key}-12, 12, or a UUID."
                    )
                card = by_sequence.get(int(tail))
            if card is None:
                raise ConfigError(f"No card {reference!r} on {self.project.key}.")
            card_id = str(card.id)
            if card_id in seen:
                raise GuardViolation(f"Card {reference!r} appears more than once in the batch.")
            seen.add(card_id)
            selected.append(card)
        return selected

    def _state_name(self, state_id: Any) -> str:
        for name, known_id in self.project.states.items():
            if known_id == str(state_id):
                return name
        return str(state_id or "unknown")

    def add_to_cycle(self, card_id: str, cycle_id: str) -> None:
        """Put a card in a cycle.

        `issue_ids` is a positional list, not a `data=` body. Both membership
        calls took `data={"issues": [...]}` here for a while and would have
        raised `TypeError` on the first real cycle — the guard that places a
        created card was, itself, broken. Nothing caught it because the test
        double accepted any signature; it does not any more.
        """
        self._cycle_write(
            lambda: self.client.cycles.add_work_items(
                self.slug,
                self.project.id,
                cycle_id,
                [card_id],
            )
        )

    def remove_from_cycle(self, card_id: str, cycle_id: str) -> None:
        self._cycle_write(
            lambda: self.client.cycles.remove_work_item(
                self.slug,
                self.project.id,
                cycle_id,
                card_id,
            )
        )

    def check_admission(self, card: Any, state_name: str) -> None:
        """Refuse moving an oversized card into an execution state."""
        self.project.check_admission_estimate(
            self.project.estimate_value(getattr(card, "estimate_point", None)),
            state_name,
        )

    def check_cycle_entry(self, card: Any) -> None:
        """A card may join a cycle in any state it could hold there."""
        self.check_admission(card, self._state_name(getattr(card, "state", None)))

    def check_cycle_exit(self, card: Any) -> None:
        """With cycles on, an open card leaves its sprint only for another.

        `set_card_cycle` moves it; removal alone would orphan it.
        """
        state = self._state_name(getattr(card, "state", None))
        if self.project.rules.require_cycle and state.casefold() not in SETTLED_STATES:
            raise OrphanedCard(
                f"{self._reference(card)} is {state}; removing it from its cycle leaves "
                "an open card in no sprint. Move it with set-cycle to another sprint's "
                "cycle, or cancel it."
            )

    def set_card_cycle(self, card: Any, cycle_id: str) -> None:
        """Put one card in a cycle and require membership readback."""
        self.check_cycle_entry(card)
        self.add_to_cycle(str(card.id), cycle_id)
        if str(card.id) not in self.cycle_card_ids(cycle_id):
            raise ReadbackFailed(f"Plane did not put card {card.id} in cycle {cycle_id}.")

    def clear_card_cycle(self, card: Any, cycle_id: str) -> None:
        """Remove one settled card from a cycle and require membership readback."""
        self.check_cycle_exit(card)
        self.remove_from_cycle(str(card.id), cycle_id)
        if str(card.id) in self.cycle_card_ids(cycle_id):
            raise ReadbackFailed(f"Plane did not remove card {card.id} from cycle {cycle_id}.")

    def add_to_module(self, card_id: str, module_id: str) -> None:
        self.client.modules.add_work_items(self.slug, self.project.id, module_id, [card_id])

    def remove_from_module(self, card_id: str, module_id: str) -> None:
        self.client.modules.remove_work_item(self.slug, self.project.id, module_id, card_id)

    # ---- cycles and modules -------------------------------------------
    #
    # Names are resolved afresh on every invocation; these operations do not
    # cache their results in the config.

    def create_cycle(self, name: str, fields: Mapping[str, Any]) -> Any:
        """Create a cycle. `owned_by` and `project_id` are required by the API."""
        payload = {"name": name, "project_id": self.project.id,
                   "owned_by": self.me(), **_present(fields)}
        return self._cycle_write(
            lambda: self.client.cycles.create(
                self.slug,
                self.project.id,
                data=CreateCycle(**payload),
            )
        )

    def update_cycle(self, cycle_id: str, fields: Mapping[str, Any]) -> Any:
        return self._cycle_write(
            lambda: self.client.cycles.update(
                self.slug,
                self.project.id,
                cycle_id,
                data=UpdateCycle(**_present(fields)),
            )
        )

    def restore_cycle(self, cycle_id: str) -> None:
        """Restore an archived cycle and require active-cycle readback."""
        member_ids = self.cycle_card_ids(cycle_id)
        self._cycle_write(
            lambda: self.client.cycles.unarchive(
                self.slug,
                self.project.id,
                cycle_id,
            )
        )
        active_ids = {str(item.id) for item in self.groupings("cycle")}
        if cycle_id not in active_ids:
            raise ReadbackFailed(f"Plane did not restore cycle {cycle_id}.")
        missing_ids = member_ids - self.cycle_card_ids(cycle_id)
        if missing_ids:
            missing = ", ".join(sorted(missing_ids))
            raise ReadbackFailed(
                f"Restoring cycle {cycle_id} lost card membership: {missing}."
            )

    def _cycle_write[T](self, write: Callable[[], T]) -> T:
        """Enable Cycles after Plane's feature error, then retry once."""
        try:
            return write()
        except HttpError as error:
            if not _cycles_disabled(error):
                raise
            self.client.projects.update_features(
                self.slug,
                self.project.id,
                ProjectFeature(cycles=True),
            )
            features = self.client.projects.get_features(
                self.slug,
                self.project.id,
            )
            if features.cycles is not True:
                raise ReadbackFailed(
                    "Plane accepted the Cycles feature update but readback "
                    "still reports Cycles disabled."
                ) from error
            return write()

    def cycle_sprint_id(self, cycle_id: str) -> tuple[int, Any]:
        """Read a cycle by UUID and extract its `Sprint N` register identity."""
        cycle = self.client.cycles.retrieve(self.slug, self.project.id, cycle_id)
        name = str(getattr(cycle, "name", ""))
        match = _SPRINT_NAME.match(name)
        if match is None:
            raise ConfigError(
                f"Plane cycle {cycle_id} is named {name!r}; expected a name beginning `Sprint N`."
            )
        return int(match.group(1)), cycle

    def start_sprint_cycle(self, cycle_id: str, started: str) -> tuple[str, list[str], list[str]]:
        """Prepare card states, start the matching cycle, and read every write back."""
        sprint_id, cycle = self.cycle_sprint_id(cycle_id)
        cards = self.cards()
        target_ids = self.cycle_card_ids(cycle_id)
        if not any(
            str(card.id) in target_ids
            and self._state_name(getattr(card, "state", None)).casefold()
            != "cancelled"
            for card in cards
        ):
            raise EmptyCycle(
                f"Cycle {cycle.name!r} has no card that is not Cancelled; "
                "a sprint starts only with planned scope in its cycle, "
                "because opening totals are read at start."
            )
        live_cycle_ids: set[str] = set()
        for live_cycle in self.groupings("cycle"):
            live_cycle_ids.update(self.cycle_card_ids(str(live_cycle.id)))

        todo_id = self.project.state_id("Todo")
        backlog_id = self.project.state_id("Backlog")
        settled = {"done", "cancelled"}
        admitted = [
            card for card in cards
            if str(card.id) in target_ids
            and str(getattr(card, "state", "")) != todo_id
            # Never resurrect settled members: a cancelled card in the
            # cycle stays cancelled.
            and self._state_name(getattr(card, "state", None)).casefold()
            not in settled
        ]
        outside = [
            card for card in cards
            if str(card.id) not in live_cycle_ids
            and str(getattr(card, "state", "")) != backlog_id
            and self._state_name(getattr(card, "state", None)).casefold() not in settled
        ]
        for card in admitted:
            self.check_admission(card, "Todo")
        changes = [*admitted, *outside]
        if len(changes) > BATCH_CARD_MAXIMUM:
            raise GuardViolation(
                f"Starting Sprint {sprint_id} would move {len(changes)} cards; the maximum is "
                f"{BATCH_CARD_MAXIMUM} so writes and readbacks remain within Plane's rate limit."
            )

        for card in admitted:
            self.update_card(card, {"state": todo_id})
            self._verify_card(card.id, estimate=None, state_name="Todo")
        for card in outside:
            self.update_card(card, {"state": backlog_id})
            self._verify_card(card.id, estimate=None, state_name="Backlog")
        if not timestamps_equal(getattr(cycle, "start_date", None), started):
            self.update_cycle(cycle_id, {"start_date": started})
            self._verify_cycle_field(cycle_id, "start_date", started)
        return (
            str(cycle.name),
            [str(card.id) for card in admitted],
            [str(card.id) for card in outside],
        )

    def close_sprint_cycle(self, cycle_id: str, ended: str) -> str:
        """End the matching cycle without changing its archive or membership.

        An already-archived cycle with the same end date is idempotent
        success. A different end date on an archived cycle is a conflict,
        not a silent overwrite.
        """
        _, cycle = self.cycle_sprint_id(cycle_id)
        archived_ids = {str(item.id) for item in self.groupings("cycle", archived=True)}
        if cycle_id in archived_ids:
            if not timestamps_equal(getattr(cycle, "end_date", None), ended):
                raise GuardViolation(
                    f"Cycle {cycle.name!r} is already archived with end "
                    f"date {getattr(cycle, 'end_date', None)!r}, not "
                    f"{ended!r}; refusing to change an archived record."
                )
            return str(cycle.name)
        member_ids = self.cycle_card_ids(cycle_id)
        unsettled = [
            card for card in self.cards()
            if str(card.id) in member_ids
            and self._state_name(getattr(card, "state", None)).casefold()
            not in {"done", "cancelled"}
        ]
        if unsettled:
            details = ", ".join(
                f"{self.project.key}-{getattr(card, 'sequence_id', '?')} "
                f"({self._state_name(getattr(card, 'state', None))})"
                for card in unsettled
            )
            raise GuardViolation(
                f"Cycle {cycle.name!r} still has cards outside Done or Cancelled: {details}."
            )
        if not timestamps_equal(getattr(cycle, "end_date", None), ended):
            self.update_cycle(cycle_id, {"end_date": ended})
            self._verify_cycle_field(cycle_id, "end_date", ended)
        return str(cycle.name)

    def _verify_cycle_field(self, cycle_id: str, field: str, expected: str) -> None:
        cycle = self.client.cycles.retrieve(self.slug, self.project.id, cycle_id)
        if not timestamps_equal(getattr(cycle, field, None), expected):
            raise ReadbackFailed(
                f"Plane accepted {field}={expected!r} for cycle {cycle_id}, but read back "
                f"{getattr(cycle, field, None)!r}."
            )

    def transfer_cycle_work_items(self, cycle_id: str, new_cycle_id: str) -> None:
        """Move a cycle's *incomplete* work items to another cycle.

        Plane's own semantics: completed work stays where it was done, so the
        finished cycle keeps an honest record. This is not a bulk move.
        """
        self._cycle_write(
            lambda: self.client.cycles.transfer_work_items(
                self.slug,
                self.project.id,
                cycle_id,
                data=TransferCycleWorkItemsRequest(
                    new_cycle_id=new_cycle_id,
                ),
            )
        )

    def create_module(self, name: str, fields: Mapping[str, Any]) -> Any:
        return self.client.modules.create(
            self.slug, self.project.id, data=CreateModule(name=name, **_present(fields))
        )

    def update_module(self, module_id: str, fields: Mapping[str, Any]) -> Any:
        return self.client.modules.update(
            self.slug, self.project.id, module_id, data=UpdateModule(**_present(fields))
        )

    def delete_module(self, module_id: str) -> None:
        self.client.modules.delete(self.slug, self.project.id, module_id)

    def archive_module(self, module_id: str, *, archived: bool) -> None:
        call = self.client.modules.archive if archived else self.client.modules.unarchive
        call(self.slug, self.project.id, module_id)

    def module_card_ids(self, module_id: str) -> set[str]:
        return {
            str(getattr(item, "id", ""))
            for item in self._paged(lambda cursor: self.client.modules.list_work_items(
                self.slug, self.project.id, module_id, params=_cursor_params(cursor)
            ))
        }

    def groupings(self, kind: str, *, archived: bool = False) -> list[Any]:
        """Every cycle or module on the project, live rather than from the file."""
        resource = self.client.cycles if kind == "cycle" else self.client.modules
        lister = resource.list_archived if archived else resource.list
        return list(self._paged(lambda cursor: lister(
            self.slug, self.project.id, params=_cursor_params(cursor)
        )))

    def refresh_grouping(self, kind: str, *, archived: bool = False) -> dict[str, str]:
        """The live name-to-UUID map, retained only in memory."""
        return _unique_names(self.groupings(kind, archived=archived), kind)

    def verify_grouping(
        self, kind: str, *, name: str, target_id: str, deleted: bool = False,
    ) -> None:
        """Confirm a lifecycle write without saving the server's metadata."""
        live = self.refresh_grouping(kind)
        verified = target_id not in live.values() if deleted else live.get(name) == target_id
        if not target_id or not verified:
            raise ReadbackFailed(f"Plane did not confirm the {kind} change for {name!r}.")

    def me(self) -> str:
        """The acting user's id, which creating a cycle requires."""
        return str(self.client.users.get_me().id)

    def add_relation(self, card: Any, relation_type: str, others: Sequence[Any]) -> None:
        """Relate a card to others; a blocker may sit in any sprint."""
        if relation_type not in RELATION_TYPES:
            raise ConfigError(
                f"{relation_type!r} is not a relation. Known: {', '.join(RELATION_TYPES)}."
            )
        self.client.work_items.relations.create(
            self.slug,
            self.project.id,
            card.id,
            data=CreateWorkItemRelation(
                relation_type=relation_type, issues=[other.id for other in others]
            ),
        )
        landed = set(self.relations(card.id)[relation_type])
        missing = [other.id for other in others if str(other.id) not in landed]
        if missing:
            raise ReadbackFailed(
                f"{relation_type} was accepted but is not on the card: {', '.join(missing)}."
            )

    def remove_relation(
        self, card: Any, relation_type: str, others: Sequence[Any]
    ) -> dict[str, list[str]]:
        """Remove exactly the named relation edges, and no other edge.

        Plane's removal endpoint addresses the *pair*, not the edge: the
        request carries only the related card's id. Every check therefore
        runs before the first request — a pair related under a different
        type than the caller named is a refusal, because deleting it would
        remove an edge nobody asked about. An edge already absent under the
        requested type, and under every other type, is idempotent success.
        """
        if relation_type not in RELATION_TYPES:
            raise ConfigError(
                f"{relation_type!r} is not a relation. "
                f"Known: {', '.join(RELATION_TYPES)}."
            )
        before = self.relations(card.id)
        present = set(before[relation_type])
        to_remove: list[str] = []
        already_absent: list[str] = []
        for other in others:
            other_id = str(other.id)
            if other_id in present:
                to_remove.append(other_id)
                continue
            elsewhere = [
                name for name, ids in before.items() if other_id in ids
            ]
            if elsewhere:
                raise GuardViolation(
                    f"Relation-removal rule: the cards are related as "
                    f"{elsewhere[0]!r}, not {relation_type!r}, and Plane "
                    f"removes relations by pair. Removing here would delete "
                    f"the {elsewhere[0]!r} edge nobody named; ask for that "
                    f"type instead."
                )
            already_absent.append(other_id)
        preserved = {
            name: set(ids)
            for name, ids in before.items()
            if name != relation_type
        }
        for other_id in to_remove:
            self.client.work_items.relations.delete(
                self.slug,
                self.project.id,
                card.id,
                data=RemoveWorkItemRelation(related_issue=other_id),
            )
        if not to_remove:
            return {"removed": [], "already_absent": already_absent}
        after = self.relations(card.id)
        still_there = [
            other_id for other_id in to_remove
            if other_id in set(after[relation_type])
        ]
        if still_there:
            raise ReadbackFailed(
                f"{relation_type} removal was accepted but the edges are "
                f"still on the card: {', '.join(still_there)}."
            )
        lost = {
            name: sorted(ids - set(after[name]))
            for name, ids in preserved.items()
            if ids - set(after[name])
        }
        if lost:
            details = "; ".join(
                f"{name}: {', '.join(ids)}" for name, ids in lost.items()
            )
            raise ReadbackFailed(
                f"Removing {relation_type} also removed other edges — the "
                f"board no longer matches what was asked: {details}."
            )
        return {"removed": to_remove, "already_absent": already_absent}

    def comment(self, card: Any, html: str) -> Any:
        if not html.strip():
            raise GuardViolation("An empty comment says nothing; write one or post none.")
        created = self.client.work_items.comments.create(
            self.slug, self.project.id, card.id, data=CreateWorkItemComment(comment_html=html)
        )
        created_id = str(getattr(created, "id", ""))
        if not created_id or all(
            str(getattr(comment, "id", "")) != created_id for comment in self.comments(card)
        ):
            raise ReadbackFailed("Plane accepted the comment but its readback did not contain it.")
        return created

    def comments(self, card: Any) -> list[Any]:
        return list(self._paged(lambda cursor: self.client.work_items.comments.list(
            self.slug, self.project.id, card.id, params=_cursor_params(cursor)
        )))

    def activities(self, card: Any) -> list[Any]:
        """Return the complete immutable activity stream for one work item."""
        return list(self._paged(lambda cursor: self.client.work_items.activities.list(
            self.slug, self.project.id, card.id, params=_cursor_params(cursor)
        )))

    # ---- checks --------------------------------------------------------

    def _check_estimate(
        self, *, assignee_id: str | None, estimate: int | None,
        allow_missing_assignee: bool = False, blank_estimate: bool = False,
    ) -> None:
        """Whether this assignee may carry an estimate, and whether one is required."""
        # Order matters, and both orderings are defensible until you read the
        # message the loser produces. Whether the assignee takes an estimate at
        # all comes first: for someone who does not, the value is beside the
        # point, and "4 is not on the scale" sends the caller off to pick a
        # better number for a card that must carry none.
        if assignee_id and self.config.takes_no_estimate(assignee_id):
            if estimate is not None:
                raise EstimateOnUnestimatedAssignee(
                    f"{self.config.member_name(assignee_id)} takes no estimate, and {estimate} "
                    f"was given. Their cards are sized blank — never 0, never 1 — and count in "
                    f"no total, no velocity and no ETA."
                )
            return
        # On-scale before the cycle ceiling, for the same reason: an off-scale
        # value is one nobody can have chosen, and reporting it as "too large
        # for a cycle" sends the caller off to split a card whose number was
        # simply wrong.
        if estimate is not None:
            self.project.estimate_uuid(estimate)
        if estimate is None and self.project.rules.require_estimate and not blank_estimate:
            if assignee_id is None and allow_missing_assignee:
                return
            scale = ", ".join(str(v) for v in sorted(self.project.estimate_points))
            raise MissingEstimate(
                f"No estimate, and {self.project.key} requires one on every card whose assignee "
                f"is on the team. A card with none counts in no total and is invisible in the "
                f"burndown. Scale: {scale}."
            )

    def _verify_blank(self, card_id: str) -> None:
        """Confirm both estimate fields are actually clear.

        Its own check because `_verify_card` skips a `None` estimate as
        "nothing asked for", which is precisely the case that silently did
        nothing for as long as the write went through the SDK.
        """
        card = self.client.work_items.retrieve(self.slug, self.project.id, card_id)
        left = {
            name: value
            for name in ("estimate_point", "point")
            if (value := getattr(card, name, None)) is not None
        }
        if left:
            raise ReadbackFailed(
                f"The estimate was blanked and the card still carries "
                f"{', '.join(f'{k}={v!r}' for k, v in left.items())}."
            )

    def _verify_card(self, card_id: str, *, estimate: int | None, state_name: str | None) -> None:
        """Read the card back and confirm what was asked for is on it."""
        card = self.client.work_items.retrieve(self.slug, self.project.id, card_id)
        if estimate is not None:
            written = self.project.estimate_value(getattr(card, "estimate_point", None))
            if written != estimate:
                raise ReadbackFailed(
                    f"Estimate {estimate} was accepted and the card reads back as "
                    f"{written if written is not None else 'unestimated'}."
                )
        if state_name is not None:
            expected = self.project.state_id(state_name)
            if str(getattr(card, "state", "")) != expected:
                raise ReadbackFailed(
                    f"State {state_name!r} was accepted and the card reads back in a "
                    f"different state."
                )

    # ---- capture -------------------------------------------------------

    def metadata(self) -> dict[str, Any]:
        """Resolve project metadata without persisting it or scanning cards."""
        states = list(self._paged(
            lambda c: self.client.states.list(
                self.slug, self.project.id, params=_cursor_params(c))
        ))
        facts: dict[str, Any] = {
            "id": self.project.id,
            "name": self.project.name,
            "estimates_enabled": self.project.estimates_enabled,
            "states": _unique_names(states, "state"),
            "states_outside_cycles": [
                state.name for state in states
                if getattr(state, "group", None) in {"backlog", "cancelled", "completed"}
            ],
            "cycles": _unique_names(self._paged(
                lambda c: self.client.cycles.list(
                    self.slug, self.project.id, params=_cursor_params(c))
            ), "cycle"),
            "modules": _unique_names(self._paged(
                lambda c: self.client.modules.list(
                    self.slug, self.project.id, params=_cursor_params(c))
            ), "module"),
            "labels": _unique_names(self._paged(
                lambda c: self.client.labels.list(
                    self.slug, self.project.id, params=_cursor_params(c))
            ), "label"),
        }
        return facts

    def capture_facts(self) -> dict[str, Any]:
        """Inspect live metadata and recover the estimate scale."""
        facts = self.metadata()
        points = self._capture_scale()
        facts["estimate_points"] = {str(value): uuid for value, uuid in sorted(points.items())}
        return facts

    def _capture_scale(self) -> dict[int, str]:
        """The value-to-UUID map, from the endpoint if it exists, else from cards.

        Only points that some card carries are recoverable from cards. To
        publish a whole scale, set each value on one card once — which is what
        the endpoint would otherwise save you.
        """
        from_endpoint = self._scale_from_endpoint()
        if from_endpoint is not None:
            return from_endpoint
        return self._scale_from_cards()[0]

    def _scale_from_endpoint(self) -> dict[int, str] | None:
        """The scale as the server states it, or None where the endpoint is absent.

        Measured absent on self-hosted Plane CE: `…/estimates/` answers 404
        even for a project whose `estimate` field names a configured estimate.
        A 404 is therefore "this server does not have it" and not an error;
        anything else is a real failure and is left to the caller.
        """
        try:
            estimate = self.client.estimates.retrieve(self.slug, self.project.id)
            points = self.client.estimates.list_points(self.slug, self.project.id, estimate.id)
        except HttpError as error:
            if error.status_code == 404:
                return None
            raise
        recovered: dict[int, str] = {}
        for point in points:
            value = getattr(point, "value", None)
            if value is None or not str(value).strip().lstrip("-").isdigit():
                continue
            recovered[int(str(value).strip())] = point.id
        return recovered

    def _scale_from_cards(self) -> tuple[dict[int, str], set[str]]:
        """Rebuild the scale by asking the API to expand each card's estimate point.

        **This is the only route that works, and finding it took some doing.**
        The obvious one does not: an estimate set through the UI leaves the
        legacy `point` field **null**, so pairing `point` with `estimate_point`
        recovers nothing from exactly the cards a person creates to publish the
        scale. Measured on the Testing project — six cards, one per scale
        value, every `point` null.

        `?expand=estimate_point` replaces the bare UUID with the estimate point
        object, which carries its `value`. Two traps in that object:

        - **Use `value`, never `key`.** `key` is the ordinal — on a 1, 2, 3, 5,
          10, 22 scale the fourth point has `key` 4 and `value` 5. Reading
          `key` would record five of the six values wrongly, and plausibly.
        - **A card with no estimate still returns a dict**, a stub with no
          `id` and `value: ""`. Guard on both, or the stub enters the scale.

        The SDK cannot make this call: it types `estimate_point` as
        `str | None`, so the expanded object fails validation. Hence the raw
        request, as with relations.

        The second return value is empty and kept for the caller's shape: with
        expansion there is no such thing as a point whose value the API will
        not report.
        """
        points: dict[int, str] = {}
        by_uuid: dict[str, int] = {}

        for card in self._expanded_cards():
            point = card.get("estimate_point")
            if not isinstance(point, dict):
                continue
            uuid, raw = point.get("id"), str(point.get("value", "")).strip()
            if not uuid or not raw.lstrip("-").isdigit():
                continue
            value = int(raw)
            if points.get(value, uuid) != uuid:
                raise ScaleContradiction(
                    f"Point {value} appears with two UUIDs: {points[value]} and {uuid}."
                )
            if by_uuid.get(uuid, value) != value:
                raise ScaleContradiction(
                    f"UUID {uuid} appears under two point values: {by_uuid[uuid]} and {value}."
                )
            points[value] = uuid
            by_uuid[uuid] = value
        return points, set()

    def scale_evidence(self) -> list[dict[str, Any]]:
        """Every scale point in use, with the card that proves each value.

        The evidence matters because this map is inferred from the board
        rather than read from an endpoint: a reader deciding whether to trust
        it should be able to open the card and look.
        """
        found: dict[str, dict[str, Any]] = {}
        by_value: dict[int, str] = {}
        for card in self._expanded_cards():
            point = card.get("estimate_point")
            if not isinstance(point, dict):
                continue
            uuid, raw = point.get("id"), str(point.get("value", "")).strip()
            if not uuid or not raw.lstrip("-").isdigit():
                continue
            value = int(raw)
            if value in by_value and by_value[value] != uuid:
                raise ScaleContradiction(f"Point {value} appears with two UUIDs.")
            if uuid in found:
                if found[uuid]["value"] != value:
                    raise ScaleContradiction(f"UUID {uuid} appears under two point values.")
                continue
            by_value[value] = uuid
            found[uuid] = {
                "value": int(raw),
                "uuid": uuid,
                "seen_on": f"{self.project.key}-{card.get('sequence_id')}",
                "card_name": card.get("name") or "",
            }
        return sorted(found.values(), key=lambda entry: entry["value"])

    def _expanded_cards(self) -> Iterator[dict[str, Any]]:
        """Every card as raw JSON, with `estimate_point` expanded. Paged to the end."""
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            params: dict[str, Any] = {"per_page": 100, "expand": "estimate_point"}
            if cursor:
                params["cursor"] = cursor
            page = self.client.work_items._get(  # noqa: SLF001 — see _scale_from_cards
                f"{self.slug}/projects/{self.project.id}/issues", params=params
            )
            yield from page.get("results") or []
            if not page.get("next_page_results"):
                return
            cursor = page.get("next_cursor")
            if not cursor or cursor in seen:
                return
            seen.add(cursor)

    # ---- paging --------------------------------------------------------

    @staticmethod
    def _paged(fetch: Any) -> Iterator[Any]:
        """Walk a cursor-paginated endpoint to exhaustion."""
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            page = fetch(cursor)
            results = getattr(page, "results", None)
            if results is None:
                yield from page if isinstance(page, list) else []
                return
            yield from results
            if not getattr(page, "next_page_results", False):
                return
            cursor = getattr(page, "next_cursor", None)
            if not cursor or cursor in seen:
                return
            seen.add(cursor)


def _cursor_params(cursor: str | None) -> Any:
    """Query params for one page, or None for the first."""
    if cursor is None:
        return None
    from plane.models.query_params import WorkItemQueryParams

    return WorkItemQueryParams(cursor=cursor)


def _cycles_disabled(error: HttpError) -> bool:
    """Whether Plane explicitly refused a write because Cycles is disabled."""
    return (
        error.status_code == 400
        and "cycles are not enabled for this project" in str(error).casefold()
    )


def _intake_params(cursor: str | None) -> PaginatedQueryParams:
    """Page Intake without an expansion that strips its default work-item detail."""
    return PaginatedQueryParams(cursor=cursor, per_page=100)


def _present(fields: Mapping[str, Any]) -> dict[str, Any]:
    """Drop the keys the caller did not set.

    An option nobody passed is absent, not null: sending null would clear a
    field the caller never mentioned.
    """
    return {name: value for name, value in fields.items() if value is not None}


def _unique_names(items: Any, kind: str) -> dict[str, str]:
    """A name-to-id map that refuses to silently drop a duplicate name.

    A name-keyed map cannot hold two things called the same, and the caller
    resolving that name would get whichever the endpoint listed last.
    """
    found: dict[str, str] = {}
    for item in items:
        name, item_id = getattr(item, "name", None), getattr(item, "id", None)
        if not isinstance(name, str) or not isinstance(item_id, str):
            continue
        if found.get(name, item_id) != item_id:
            raise ScaleContradiction(
                f"Two {kind}s are named {name!r}: {found[name]} and {item_id}."
            )
        found[name] = item_id
    return found


def _first_assignee(card: Any) -> str | None:
    assignees = getattr(card, "assignees", None) or []
    return str(assignees[0]) if assignees else None


def _looks_like_uuid(value: str) -> bool:
    return len(value) == 36 and value.count("-") == 4


def _share_session(owner: Any, session: requests.Session) -> None:
    """Pool one keep-alive session across SDK resources.

    The SDK gives each of its ~80 resources its own session, so every
    resource touched paid a fresh DNS lookup and TLS handshake.
    """
    for value in vars(owner).values():
        if isinstance(value, BaseResource):
            value.session = session
            _share_session(value, session)


def discover(credentials: Credentials) -> tuple[PlaneClient, dict[str, Any], dict[str, str]]:
    """Connect and list projects and members. The only place the key is revealed."""
    client = PlaneClient(base_url=credentials.host, api_key=credentials.api_key.reveal())
    _share_session(client, client.projects.session)
    slug = credentials.workspace_slug
    projects = {
        str(project.identifier): project
        for project in Board._paged(
            lambda cursor: client.projects.list(slug, params=_cursor_params(cursor))
        )
    }
    members = {}
    for member in Board._paged(
        lambda cursor: client.workspaces.get_members(slug, params=_cursor_params(cursor))
    ):
        name = getattr(member, "display_name", None) or getattr(member, "email", None)
        if getattr(member, "id", None) and name:
            members[str(member.id)] = str(name)
    return client, projects, members


def bootstrap_board(client: PlaneClient, credentials: Credentials, project: Any) -> Board:
    """A Board good enough to capture with, before any config file exists."""
    empty = Config(
        path=Path("<none>"),
        workspace_slug=credentials.workspace_slug,
        members={},
        unestimated_assignees=frozenset(),
        default_project=None,
        projects={},
    )
    return Board(
        client,
        empty,
        Project(
            key=str(project.identifier),
            id=str(project.id),
            name=str(getattr(project, "name", project.identifier)),
            estimates_enabled=(
                not hasattr(project, "estimate")
                or getattr(project, "estimate", None) is not None
            ),
            estimate_points={},
            cycles={},
            modules={},
            states={},
            labels={},
            states_outside_cycles=frozenset(),
            rules=Rules(
                require_cycle=getattr(project, "cycle_view", True) is not False,
                require_module=getattr(project, "module_view", True) is not False,
                require_estimate=(
                    not hasattr(project, "estimate")
                    or getattr(project, "estimate", None) is not None
                ),
            ),
        ),
        credentials.workspace_slug,
    )


def connect(config: Config, credentials: Credentials, project_key: str | None) -> Board:
    """Resolve the selected project and all name mappings from Plane on each invocation."""
    chosen = project_key or os.environ.get(PROJECT_ENV_VAR) or config.default_project
    if chosen is None:
        raise ConfigError("No project given. Set defaults.project or pass --project KEY.")
    client, projects, members = discover(credentials)
    if chosen not in projects:
        raise ConfigError(f"No project {chosen!r} on Plane. Known: {', '.join(sorted(projects))}.")
    board = bootstrap_board(client, credentials, projects[chosen])
    facts = board.metadata()
    if chosen == config.default_project:
        facts["estimate_points"] = config.document.get("estimate_points", {})
        rules = Rules.from_document(
            config.document.get("rules", {}),
            base=board.project.rules,
        )
        if not board.project.estimates_enabled:
            rules = replace(
                rules,
                require_estimate=False,
                cycle_estimate_max=None,
            )
        board.project = replace(board.project, rules=rules)
    else:
        # A project's scale UUIDs must never be reused on another project.
        facts["estimate_points"] = (
            {
                str(value): uuid
                for value, uuid in board._capture_scale().items()
            }
            if board.project.estimates_enabled
            else {}
        )
    board.project = replace(
        project_from_facts(chosen, facts, config.path), rules=board.project.rules,
    )
    board.config = replace(config, members=members, projects={chosen: board.project})
    exemptions = frozenset(
        board.config.member_id(name) for name in board.project.rules.unestimated_assignees
    )
    board.config = replace(board.config, unestimated_assignees=exemptions)
    if board.project.rules.wip_limit is not None:
        for state in board.project.rules.wip_states:
            board.project.state_id(state)
    return board
