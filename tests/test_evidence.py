"""Evidence archives: exact bytes in, verified digest back, honest receipts.

The defect these tests hunt is the plausible lie: an upload that was
truncated, altered, or lost in transit being reported as a successful
archive. The attachment double therefore stores real bytes and the
readback digest is computed from what storage actually holds, never from
what the caller sent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from plane.errors import HttpError

from plane_proj import board as board_module
from plane_proj.board import Board
from plane_proj.guards import ConfigError, GuardViolation, ReadbackFailed
from tests.conftest import Card, FakeClient


class AttachmentStore:
    """An attachments double whose storage serves the bytes it holds.

    `mangle` corrupts bytes on the way into storage, the way a broken
    upload would, while the attachment record keeps the claimed size —
    exactly the case a digest readback exists to catch.
    """

    def __init__(self, log: list[tuple[str, tuple, dict]]) -> None:
        self._log = log
        self.stored: dict[str, dict[str, Any]] = {}
        self.mangle = None
        self.upload_error: Exception | None = None
        self._counter = 0

    def seed(self, name: str, payload: bytes) -> str:
        self._counter += 1
        attachment_id = f"att-{self._counter}"
        self.stored[attachment_id] = {
            "name": name, "bytes": payload, "claimed_size": len(payload),
        }
        return attachment_id

    def list(self, *args: Any, **kwargs: Any) -> list[Any]:
        self._log.append(("attachments.list", args, kwargs))
        return [
            type("Attachment", (), {
                "id": attachment_id,
                "attributes": {
                    "name": record["name"],
                    "type": "application/octet-stream",
                    "size": record["claimed_size"],
                },
                "size": record["claimed_size"],
            })()
            for attachment_id, record in self.stored.items()
        ]

    def upload_from_bytes(self, *args: Any, **kwargs: Any) -> Any:
        self._log.append(("attachments.upload_from_bytes", args, kwargs))
        if self.upload_error is not None:
            raise self.upload_error
        _, _, _, payload, name, _ = args
        stored = self.mangle(payload) if self.mangle else payload
        self._counter += 1
        attachment_id = f"att-{self._counter}"
        self.stored[attachment_id] = {
            "name": name, "bytes": stored, "claimed_size": len(payload),
        }
        return type("Attachment", (), {"id": attachment_id})()

    def get_download_url(self, *args: Any, **kwargs: Any) -> str:
        self._log.append(("attachments.get_download_url", args, kwargs))
        return f"https://files.example/{args[3]}"


@pytest.fixture
def store(client: FakeClient, monkeypatch: pytest.MonkeyPatch):
    attachments = AttachmentStore(client.calls)
    client.work_items.attachments = attachments

    def digest_from_store(url: str):
        import hashlib
        attachment_id = url.rsplit("/", 1)[-1]
        payload = attachments.stored[attachment_id]["bytes"]
        return hashlib.sha256(payload).hexdigest(), len(payload)

    monkeypatch.setattr(board_module, "_digest_from_url", digest_from_store)
    return attachments


def _evidence_file(tmp_path: Path, name: str = "result.json",
                   payload: bytes = b'{"p95_ms": 41}') -> Path:
    path = tmp_path / name
    path.write_bytes(payload)
    return path


def test_an_upload_is_read_back_and_receipted(
    board: Board, client: FakeClient, store: AttachmentStore, tmp_path: Path
):
    path = _evidence_file(tmp_path)

    receipt = board.upload_evidence(
        Card(id="card-uuid"), path, kind="performance", revision="abc123",
    )

    assert receipt["verified"] is True
    assert receipt["reused"] is False
    assert receipt["size"] == len(path.read_bytes())
    assert receipt["revision"] == "abc123"
    assert receipt["attachment_id"] in store.stored
    assert len(client.named("attachments.upload_from_bytes")) == 1
    assert path.exists(), "the local input must never be deleted"


def test_a_truncated_archive_is_a_failure_not_a_success(
    board: Board, client: FakeClient, store: AttachmentStore, tmp_path: Path
):
    store.mangle = lambda payload: payload[:-3]

    with pytest.raises(ReadbackFailed, match="not a faithful copy"):
        board.upload_evidence(
            Card(id="card-uuid"), _evidence_file(tmp_path),
            kind="performance", revision=None,
        )


def test_altered_bytes_of_the_same_size_are_caught_by_the_digest(
    board: Board, client: FakeClient, store: AttachmentStore, tmp_path: Path
):
    store.mangle = lambda payload: b"X" + payload[1:]

    with pytest.raises(ReadbackFailed, match="not a faithful copy"):
        board.upload_evidence(
            Card(id="card-uuid"), _evidence_file(tmp_path),
            kind="performance", revision=None,
        )


def test_unicode_names_and_content_round_trip(
    board: Board, client: FakeClient, store: AttachmentStore, tmp_path: Path
):
    path = _evidence_file(
        tmp_path, name="результат-测量.json",
        payload='{"note": "旗艦門店 – ±5µs"}'.encode(),
    )

    receipt = board.upload_evidence(
        Card(id="card-uuid"), path, kind="performance", revision=None,
    )

    assert receipt["verified"] is True
    assert receipt["name"] == "результат-测量.json"


def test_a_retry_after_a_lost_response_reuses_the_verified_archive(
    board: Board, client: FakeClient, store: AttachmentStore, tmp_path: Path
):
    """The first upload landed; only its response was lost."""
    path = _evidence_file(tmp_path)
    existing = store.seed(path.name, path.read_bytes())

    receipt = board.upload_evidence(
        Card(id="card-uuid"), path, kind="performance", revision=None,
    )

    assert receipt["reused"] is True
    assert receipt["attachment_id"] == existing
    assert client.named("attachments.upload_from_bytes") == []


def test_a_same_name_archive_with_different_bytes_is_not_reused(
    board: Board, client: FakeClient, store: AttachmentStore, tmp_path: Path
):
    path = _evidence_file(tmp_path)
    store.seed(path.name, b"0" * len(path.read_bytes()))

    receipt = board.upload_evidence(
        Card(id="card-uuid"), path, kind="performance", revision=None,
    )

    assert receipt["reused"] is False
    assert len(store.stored) == 2, "evidence is append-only"


def test_an_empty_file_is_refused_before_any_request(
    board: Board, client: FakeClient, store: AttachmentStore, tmp_path: Path
):
    path = tmp_path / "empty.json"
    path.write_bytes(b"")

    with pytest.raises(GuardViolation, match="empty"):
        board.upload_evidence(
            Card(id="card-uuid"), path, kind="performance", revision=None,
        )

    assert client.calls == []


def test_a_bad_kind_slug_is_refused_before_any_request(
    board: Board, client: FakeClient, store: AttachmentStore, tmp_path: Path
):
    with pytest.raises(GuardViolation, match="lowercase slug"):
        board.upload_evidence(
            Card(id="card-uuid"), _evidence_file(tmp_path),
            kind="Performance Results", revision=None,
        )

    assert client.calls == []


def test_a_permission_denial_propagates_and_claims_nothing(
    board: Board, client: FakeClient, store: AttachmentStore, tmp_path: Path
):
    store.upload_error = HttpError(status_code=403, message="forbidden")

    with pytest.raises(HttpError):
        board.upload_evidence(
            Card(id="card-uuid"), _evidence_file(tmp_path),
            kind="performance", revision=None,
        )


def test_verify_reports_the_stored_digest(
    board: Board, client: FakeClient, store: AttachmentStore
):
    import hashlib
    attachment_id = store.seed("result.json", b'{"p95_ms": 41}')

    result = board.verify_evidence(
        Card(id="card-uuid"), attachment_id, expected_digest=None,
    )

    assert result["sha256"] == hashlib.sha256(b'{"p95_ms": 41}').hexdigest()
    assert result["verified"] is True


def test_verify_fails_on_an_expected_digest_mismatch(
    board: Board, client: FakeClient, store: AttachmentStore
):
    attachment_id = store.seed("result.json", b'{"p95_ms": 41}')

    with pytest.raises(ReadbackFailed, match="does not hold"):
        board.verify_evidence(
            Card(id="card-uuid"), attachment_id, expected_digest="0" * 64,
        )


def test_verify_refuses_an_unknown_attachment(
    board: Board, client: FakeClient, store: AttachmentStore
):
    with pytest.raises(ConfigError, match="No attachment"):
        board.verify_evidence(
            Card(id="card-uuid"), "att-nope", expected_digest=None,
        )
