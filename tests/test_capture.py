"""Recovering the board's identifiers, including the scale the API will not state.

The scale is recovered by asking the API to expand each card's estimate point.
The route that looks obvious — pairing the legacy `point` field with
`estimate_point` — does not work: an estimate set through the UI leaves `point`
null, so it recovers nothing from exactly the cards a person creates to publish
a scale. These tests pin the working route and both of its traps.
"""

from __future__ import annotations

import pytest
from plane.errors import HttpError

from plane_proj.board import Board
from plane_proj.guards import ScaleContradiction
from tests.conftest import Card, FakeClient


def expanded(*pairs: tuple[str, str, int | None]) -> list[dict]:
    """Raw cards as `?expand=estimate_point` returns them."""
    return [
        {"id": f"card-{index}", "estimate_point": {"id": uuid, "value": value, "key": key}}
        for index, (uuid, value, key) in enumerate(pairs)
    ]


def test_the_scale_is_recovered_from_expanded_cards(board: Board, client: FakeClient):
    client.expanded = expanded(("uuid-1", "1", 1), ("uuid-5", "5", 4), ("uuid-22", "22", 6))

    points, unnamed = board._scale_from_cards()

    assert points == {1: "uuid-1", 5: "uuid-5", 22: "uuid-22"}
    assert unnamed == set()


def test_the_value_is_read_and_never_the_key(board: Board, client: FakeClient):
    """`key` is the ordinal: on a 1,2,3,5,10,22 scale the fourth point is value 5."""
    client.expanded = expanded(("uuid-5", "5", 4))

    points, _ = board._scale_from_cards()

    assert points == {5: "uuid-5"}
    assert 4 not in points


def test_a_ui_set_estimate_is_recovered_although_point_is_null(board: Board, client: FakeClient):
    """The whole reason this route exists. `point` is absent from these payloads."""
    client.expanded = [
        {"id": "c", "point": None, "estimate_point": {"id": "uuid-3", "value": "3", "key": 3}}
    ]

    assert board._scale_from_cards()[0] == {3: "uuid-3"}


def test_a_card_with_no_estimate_returns_a_stub_that_is_ignored(board: Board, client: FakeClient):
    """The API answers with a dict either way; the stub has no id and an empty value."""
    client.expanded = [
        {"id": "a", "estimate_point": {"key": None, "value": "", "description": ""}},
        {"id": "b", "estimate_point": {"id": "uuid-2", "value": "2", "key": 2}},
    ]

    assert board._scale_from_cards()[0] == {2: "uuid-2"}


def test_a_non_numeric_value_does_not_enter_the_scale(board: Board, client: FakeClient):
    """A t-shirt-sized scale has no integer to record; it is skipped, not guessed."""
    client.expanded = expanded(("uuid-m", "M", 2))

    assert board._scale_from_cards()[0] == {}


def test_one_value_with_two_uuids_is_a_contradiction(board: Board, client: FakeClient):
    client.expanded = expanded(("uuid-2", "2", 2), ("uuid-other", "2", 2))

    with pytest.raises(ScaleContradiction, match="two UUIDs"):
        board._scale_from_cards()


def test_one_uuid_under_two_values_is_a_contradiction(board: Board, client: FakeClient):
    client.expanded = expanded(("uuid-2", "2", 2), ("uuid-2", "3", 3))

    with pytest.raises(ScaleContradiction, match="two point values"):
        board._scale_from_cards()


def test_a_missing_estimates_endpoint_falls_back_rather_than_failing(
    board: Board, client: FakeClient
):
    """404 means this server does not have the endpoint — not that the call went wrong."""
    def absent(*args, **kwargs):
        raise HttpError("Page not found.", status_code=404)

    client.estimates.retrieve = absent
    client.expanded = expanded(("uuid-1", "1", 1))

    assert board._capture_scale() == {1: "uuid-1"}


def test_any_other_http_failure_is_not_swallowed(board: Board, client: FakeClient):
    """A 500 must not be reported as 'this server has no estimates'."""
    def broken(*args, **kwargs):
        raise HttpError("boom", status_code=500)

    client.estimates.retrieve = broken

    with pytest.raises(HttpError):
        board._capture_scale()


def test_the_endpoint_is_preferred_when_it_exists(board: Board, client: FakeClient):
    """The day the server grows the endpoint, capture reads instead of inferring."""
    class Point:
        def __init__(self, value, point_id):
            self.value, self.id = value, point_id

    client.estimates.retrieve = lambda *a, **k: type("E", (), {"id": "estimate-uuid"})()
    client.estimates.list_points = lambda *a, **k: [Point("1", "srv-1"), Point("8", "srv-8")]
    client.expanded = expanded(("uuid-1", "1", 1))

    assert board._capture_scale() == {1: "srv-1", 8: "srv-8"}


def test_the_expansion_is_actually_requested(board: Board, client: FakeClient):
    """Without `expand`, the field is a bare UUID and nothing above works."""
    client.expanded = expanded(("uuid-1", "1", 1))
    board._scale_from_cards()

    params = client.named("work_items._get")[0][2]["params"]
    assert params["expand"] == "estimate_point"


def test_two_modules_with_one_name_is_refused(board: Board, client: FakeClient):
    """A name-keyed map cannot hold both, and would silently keep the last."""
    from plane_proj.board import _unique_names

    duplicates = [Card(id="m1", name="pipeline"), Card(id="m2", name="pipeline")]

    with pytest.raises(ScaleContradiction, match="Two modules"):
        _unique_names(duplicates, "module")


class TestMergingACapturedScale:
    """A capture sees only values in use, so it must never replace the record.

    The card that proved a value can be closed or deleted and the UUID stays
    valid forever. Replacing would delete correct entries nothing can recover.
    """

    def test_a_value_no_card_carries_today_is_kept(self):
        from plane_proj.config import merge_scale

        merged, kept, changed = merge_scale({"1": "u1", "5": "u5", "22": "u22"}, {"1": "u1"})

        assert merged == {"1": "u1", "5": "u5", "22": "u22"}
        assert kept == ["5", "22"]
        assert changed == []

    def test_a_newly_seen_value_is_added(self):
        from plane_proj.config import merge_scale

        merged, kept, _ = merge_scale({"1": "u1"}, {"1": "u1", "3": "u3"})

        assert merged == {"1": "u1", "3": "u3"}
        assert kept == []

    def test_a_disagreement_is_reported_and_the_board_wins(self):
        from plane_proj.config import merge_scale

        merged, _, changed = merge_scale({"3": "old"}, {"3": "new"})

        assert merged == {"3": "new"}
        assert changed == ["3"]

    def test_the_result_is_ordered_by_value_not_by_string(self):
        from plane_proj.config import merge_scale

        merged, _, _ = merge_scale({"22": "u22", "3": "u3"}, {"10": "u10"})

        assert list(merged) == ["3", "10", "22"]


class TestReplacingTheScale:
    """Narrowing the scale is possible, but only when the caller says so."""

    def test_replace_without_write_is_refused(self, config_path):
        from click.testing import CliRunner

        from plane_proj.cli import cli

        result = CliRunner().invoke(
            cli, ["--conf", str(config_path), "project", "scale", "--replace"]
        )

        assert result.exit_code != 0
        assert "only means something with --write" in result.output

    def test_the_default_write_cannot_narrow_the_scale(self):
        """The reported bug: capture deleted the three values it could not confirm."""
        from plane_proj.config import merge_scale

        merged, kept, _ = merge_scale(
            {"1": "u1", "2": "u2", "3": "u3", "5": "u5", "10": "u10", "22": "u22"},
            {"1": "u1", "2": "u2", "3": "u3"},
        )

        assert list(merged) == ["1", "2", "3", "5", "10", "22"]
        assert kept == ["5", "10", "22"]
