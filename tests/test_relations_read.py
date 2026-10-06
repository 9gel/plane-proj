"""Relation reads accept both shapes Plane servers return."""

from types import SimpleNamespace

from plane_proj.board import Board


def board_answering(raw):
    relations = SimpleNamespace(_get=lambda endpoint: raw)
    return SimpleNamespace(
        slug="ws",
        project=SimpleNamespace(id="project"),
        client=SimpleNamespace(work_items=SimpleNamespace(relations=relations)),
    )


def test_relation_buckets_of_objects_are_read_by_issue_id():
    # Self-hosted Plane, measured: each related card is {"issue_id", ...}.
    raw = {"blocked_by": [{"project_id": "project", "issue_id": "card-a"}]}
    found = Board.relations(board_answering(raw), "card-b")
    assert found["blocked_by"] == ["card-a"]


def test_relation_buckets_of_plain_ids_are_read_too():
    # plane.so, measured 2026-10-06: each related card is its bare id. Read
    # as objects these were dropped, so every new relation failed readback.
    raw = {"blocked_by": ["5dbe5a66-fc05-447a-b6cb-ce1425de049e"]}
    found = Board.relations(board_answering(raw), "card-b")
    assert found["blocked_by"] == ["5dbe5a66-fc05-447a-b6cb-ce1425de049e"]
    assert found["blocking"] == []
