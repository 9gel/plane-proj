"""Relation removal: only the named edge, verified, idempotent when absent.

Plane's removal endpoint addresses the pair, not the edge, so the defects
worth testing are the ones where obeying the caller would delete an edge
nobody named — and the ones where a refusal must leave the board unchanged.
"""

from __future__ import annotations

from typing import Any

import pytest

from plane_proj.board import Board
from plane_proj.guards import ConfigError, GuardViolation, ReadbackFailed
from tests.conftest import Card, FakeClient


class RelationStore:
    """A stateful relations double: reads reflect prior deletes.

    A static result would make every readback test meaningless — removal
    must be observed changing what the next read returns.
    """

    def __init__(
        self,
        log: list[tuple[str, tuple, dict]],
        buckets: dict[str, list[str]],
        *,
        delete_removes: bool = True,
        collateral: dict[str, list[str]] | None = None,
    ) -> None:
        self._log = log
        self.buckets = {name: list(ids) for name, ids in buckets.items()}
        self._delete_removes = delete_removes
        self._collateral = collateral or {}

    def _get(self, endpoint: str) -> dict[str, Any]:
        self._log.append(("relations._get", (endpoint,), {}))
        return {
            name: [{"issue_id": card_id} for card_id in ids]
            for name, ids in self.buckets.items()
        }

    def delete(self, *args: Any, **kwargs: Any) -> None:
        self._log.append(("relations.delete", args, kwargs))
        if not self._delete_removes:
            return
        removed = kwargs["data"].related_issue
        for name, ids in self.buckets.items():
            self.buckets[name] = [
                card_id for card_id in ids if card_id != removed
            ]
        for name, ids in self._collateral.items():
            self.buckets[name] = [
                card_id for card_id in self.buckets[name]
                if card_id not in ids
            ]


def _with_relations(client: FakeClient, store: RelationStore) -> None:
    client.work_items.relations = store


def test_remove_deletes_only_the_named_edge_and_verifies(
    board: Board, client: FakeClient
):
    store = RelationStore(
        client.calls,
        {"blocked_by": ["other"], "relates_to": ["third"]},
    )
    _with_relations(client, store)

    result = board.remove_relation(
        Card(id="card-uuid"), "blocked_by", [Card(id="other")]
    )

    assert result == {"removed": ["other"], "already_absent": []}
    assert len(client.named("relations.delete")) == 1
    assert store.buckets["relates_to"] == ["third"]


def test_an_already_absent_edge_is_idempotent_success(
    board: Board, client: FakeClient
):
    _with_relations(client, RelationStore(client.calls, {}))

    result = board.remove_relation(
        Card(id="card-uuid"), "blocked_by", [Card(id="other")]
    )

    assert result == {"removed": [], "already_absent": ["other"]}
    assert client.named("relations.delete") == []


def test_a_pair_related_under_another_type_is_refused_before_any_write(
    board: Board, client: FakeClient
):
    _with_relations(
        client, RelationStore(client.calls, {"relates_to": ["other"]})
    )

    with pytest.raises(GuardViolation, match="by pair"):
        board.remove_relation(
            Card(id="card-uuid"), "blocked_by", [Card(id="other")]
        )

    assert client.named("relations.delete") == []


def test_an_unknown_relation_type_is_refused_before_any_request(
    board: Board, client: FakeClient
):
    with pytest.raises(ConfigError, match="not a relation"):
        board.remove_relation(
            Card(id="card-uuid"), "depends_on", [Card(id="other")]
        )

    assert client.calls == []


def test_a_removal_the_readback_does_not_show_is_a_failure(
    board: Board, client: FakeClient
):
    _with_relations(
        client,
        RelationStore(
            client.calls, {"blocked_by": ["other"]}, delete_removes=False
        ),
    )

    with pytest.raises(ReadbackFailed, match="still on the card"):
        board.remove_relation(
            Card(id="card-uuid"), "blocked_by", [Card(id="other")]
        )


def test_collateral_loss_of_another_edge_is_a_failure(
    board: Board, client: FakeClient
):
    """The pair-addressed endpoint must not silently take other edges."""
    _with_relations(
        client,
        RelationStore(
            client.calls,
            {"blocked_by": ["other"], "relates_to": ["third"]},
            collateral={"relates_to": ["third"]},
        ),
    )

    with pytest.raises(ReadbackFailed, match="also removed"):
        board.remove_relation(
            Card(id="card-uuid"), "blocked_by", [Card(id="other")]
        )


def test_a_mixed_batch_removes_present_edges_and_reports_absent_ones(
    board: Board, client: FakeClient
):
    _with_relations(
        client, RelationStore(client.calls, {"blocked_by": ["other"]})
    )

    result = board.remove_relation(
        Card(id="card-uuid"),
        "blocked_by",
        [Card(id="other"), Card(id="gone")],
    )

    assert result == {"removed": ["other"], "already_absent": ["gone"]}
    assert len(client.named("relations.delete")) == 1
