"""Independent verdicts and the transition table, refused before any write."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from plane_proj import execution, verdicts
from plane_proj.board import Board
from plane_proj.cli import Context, cli
from plane_proj.config import load_config
from plane_proj.guards import (
    ConfigError,
    GuardViolation,
    MissingIndependentVerdict,
    TransitionNotAllowed,
)
from tests.conftest import AUTOMATION, Card, FakeClient

REV = "0123abc"
OTHER_REV = "4567def"
OP = "verdict-op-1"


def entered_verifying(when: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"activity-{when}", created_at=when, field="state",
        old_value="In Progress", new_value="Verifying", actor="worker-id",
    )


def verdict(
    when: str, role: str, result: str = "pass", revision: str = REV,
    author: str = "reviewer", operation_id: str = "",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"comment-{when}-{role}", created_at=when, actor="worker-id",
        comment_html=verdicts.verdict_html(verdicts.verdict_fields(
            role=role, result=result, revision=revision, author=author,
            note="", operation_id=operation_id or f"op-{when}-{role}",
        )),
    )


def with_rules(board: Board, **rules: bool) -> Board:
    board.project = replace(
        board.project, rules=replace(board.project.rules, **rules)
    )
    return board


def board_writes(client: FakeClient) -> list[str]:
    return [
        name for name in client.write_calls
        if name in {"work_items._patch", "comments.create"}
    ]


def invoke(config_path, board, monkeypatch, *args: str):
    monkeypatch.setattr(Context, "board", property(lambda self: board))
    return CliRunner().invoke(cli, ["--conf", str(config_path), *args])


# ---- the verdict command ---------------------------------------------


def verdict_args(*extra: str) -> list[str]:
    return [
        "card", "verdict", "DEMO-12", "--role", "qa", "--result", "pass",
        "--revision", REV, "--author", "qa-agent", "--note", "suite green",
        "--operation-id", OP, *extra,
    ]


def test_verdict_posts_one_structured_comment(
    config_path, board, client, monkeypatch,
):
    client.cards = [Card(sequence_id=12, state="state-verifying")]

    result = invoke(
        config_path, board, monkeypatch, "--json", *verdict_args()
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["posted"] is True
    assert len(client.named("comments.create")) == 1
    body = client.comments[0].comment_html
    assert body == (
        "<p>plane-proj-verdict/v1 "
        '{"author":"qa-agent","note":"suite green","operation_id":"'
        f'{OP}","result":"pass","revision":"{REV}","role":"qa"}}</p>'
    )
    [parsed] = verdicts.verdicts(client.comments)
    assert (parsed.role, parsed.result, parsed.author) == (
        "qa", "pass", "qa-agent",
    )


def test_a_verdict_note_with_markup_round_trips(board, client):
    client.cards = [Card(sequence_id=12, state="state-verifying")]
    note = "*not* <b>bold</b> & spaced"
    fields = verdicts.verdict_fields(
        role="qa", result="fail", revision=REV, author="qa-agent",
        note=note, operation_id=OP,
    )
    board.comment(client.cards[0], verdicts.verdict_html(fields))

    assert verdicts.verdicts(client.comments)[0].note == note


def test_a_verdict_retry_with_the_same_operation_posts_nothing(
    config_path, board, client, monkeypatch,
):
    client.cards = [Card(sequence_id=12, state="state-verifying")]

    first = invoke(config_path, board, monkeypatch, *verdict_args())
    second = invoke(config_path, board, monkeypatch, *verdict_args())

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert "already recorded" in second.output
    assert len(client.named("comments.create")) == 1


def test_a_retry_with_spacing_in_the_note_is_still_the_same_verdict(
    config_path, board, client, monkeypatch,
):
    # The stored comment reads back with whitespace runs collapsed, so a
    # lost-response retry must compare the same normalized text.
    client.cards = [Card(sequence_id=12, state="state-verifying")]
    spaced = ("--note", "suite  green\n ok", "--author", "qa  agent")

    first = invoke(config_path, board, monkeypatch, *verdict_args(*spaced))
    second = invoke(config_path, board, monkeypatch, *verdict_args(*spaced))

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert "already recorded" in second.output
    assert len(client.named("comments.create")) == 1


def test_a_verdict_outside_verifying_is_refused_before_any_write(
    config_path, board, client, monkeypatch,
):
    client.cards = [Card(sequence_id=12, state="state-progress")]

    result = invoke(config_path, board, monkeypatch, *verdict_args())

    assert isinstance(result.exception, GuardViolation)
    assert "Verdict rule" in str(result.exception)
    assert board_writes(client) == []


@pytest.mark.parametrize("revision", [
    "abc12", "ABCDEF1", "0123abg", "a" * 41, "main",
])
def test_a_verdict_with_a_bad_revision_sends_no_request(
    config_path, board, client, monkeypatch, revision,
):
    client.cards = [Card(sequence_id=12, state="state-verifying")]
    args = verdict_args()
    args[args.index(REV)] = revision

    result = invoke(config_path, board, monkeypatch, *args)

    assert isinstance(result.exception, GuardViolation)
    assert "Verdict rule" in str(result.exception)
    assert client.calls == []


def test_a_verdict_with_a_blank_author_is_refused():
    with pytest.raises(GuardViolation, match="Verdict rule"):
        verdicts.verdict_fields(
            role="qa", result="pass", revision=REV, author="  ", note="",
            operation_id=OP,
        )


@pytest.mark.parametrize("payload", [
    "{not json", "[]",
    '{"author":"a","note":"","operation_id":"o","result":"maybe",'
    f'"revision":"{REV}","role":"qa"}}',
    '{"author":"a","note":"","operation_id":"o","result":"pass",'
    '"revision":"HEAD","role":"qa"}',
])
def test_a_corrupt_verdict_comment_refuses_naming_the_rule(payload):
    corrupt = SimpleNamespace(
        id="c", created_at="2026-09-16T01:00:00Z",
        comment_html=f"<p>{verdicts.VERDICT_PREFIX}{payload}</p>",
    )
    with pytest.raises(GuardViolation, match="Verdict rule"):
        verdicts.verdicts([corrupt])


# ---- IndependentVerdictRule ------------------------------------------


ENTRY = [entered_verifying("2026-09-16T00:00:00Z")]
QA = verdict("2026-09-16T01:00:00Z", "qa", author="qa-agent")
LEAD = verdict("2026-09-16T02:00:00Z", "tech-lead", author="lead-agent")


@pytest.mark.parametrize("comments,expected", [
    ([LEAD], "no qa verdict"),
    ([QA], "no tech-lead verdict"),
    ([verdict("2026-09-16T01:00:00Z", "qa", "fail", author="qa-agent"),
      LEAD], "the latest qa verdict is fail"),
    ([QA, verdict("2026-09-16T02:00:00Z", "tech-lead",
                  revision=OTHER_REV, author="lead-agent")],
     f"qa names revision {REV} but tech-lead names {OTHER_REV}"),
    ([QA, verdict("2026-09-16T02:00:00Z", "tech-lead", author="QA-agent")],
     "both by qa-agent"),
    # A later fail supersedes an earlier pass for the same role.
    ([QA, LEAD, verdict("2026-09-16T03:00:00Z", "qa", "fail",
                        author="qa-agent")],
     "the latest qa verdict is fail"),
])
def test_verdicts_that_do_not_agree_are_refused(comments, expected):
    with pytest.raises(MissingIndependentVerdict, match=expected) as error:
        verdicts.require_independent_verdicts("DEMO-12", ENTRY, comments)
    assert "Independent verdict rule" in str(error.value)


def test_a_verdict_older_than_the_last_return_to_verifying_does_not_count():
    activities = [*ENTRY, entered_verifying("2026-09-16T01:30:00Z")]

    with pytest.raises(
        MissingIndependentVerdict,
        match="no qa verdict since it last entered Verifying at "
              r"2026-09-16T01:30:00\+00:00 \(1 older qa verdict",
    ):
        verdicts.require_independent_verdicts(
            "DEMO-12", activities, [QA, LEAD]
        )


def test_current_independent_passes_on_one_revision_are_accepted():
    verdicts.require_independent_verdicts("DEMO-12", ENTRY, [QA, LEAD])


def verifying_card(client: FakeClient, comments: list) -> Card:
    card = Card(sequence_id=12, state="state-verifying")
    client.cards = [card]
    client.activities = list(ENTRY)
    client.comments = list(comments)
    return card


@pytest.mark.parametrize("command", [
    ["card", "move", "DEMO-12", "Done"],
    ["card", "move-many", "DEMO-12", "--from", "Verifying", "--to", "Done"],
])
def test_done_without_independent_verdicts_is_refused_before_any_write(
    config_path, board, client, monkeypatch, command,
):
    verifying_card(client, [QA])
    with_rules(board, require_independent_verdicts=True)

    result = invoke(config_path, board, monkeypatch, *command)

    assert isinstance(result.exception, MissingIndependentVerdict)
    assert "no tech-lead verdict" in str(result.exception)
    assert board_writes(client) == []


def test_done_with_independent_verdicts_moves(
    config_path, board, client, monkeypatch,
):
    card = verifying_card(client, [QA, LEAD])
    with_rules(board, require_independent_verdicts=True)

    result = invoke(
        config_path, board, monkeypatch, "card", "move", "DEMO-12", "Done"
    )

    assert result.exit_code == 0, result.output
    assert card.state == "state-done"


def test_with_both_rules_off_done_needs_no_verdict_or_table(
    config_path, board, client, monkeypatch,
):
    card = Card(sequence_id=12, state="state-todo")
    client.cards = [card]

    result = invoke(
        config_path, board, monkeypatch, "card", "move", "DEMO-12", "Done"
    )

    assert result.exit_code == 0, result.output
    assert card.state == "state-done"
    assert client.named("activities.list") == []


# ---- TransitionTableRule ---------------------------------------------


@pytest.mark.parametrize("command,state", [
    (["card", "move", "DEMO-12", "Done"], "state-progress"),
    (["card", "move", "DEMO-12", "Verifying"], "state-todo"),
    (["card", "move", "DEMO-12", "In Progress"], "state-done"),
    (["card", "move-many", "DEMO-12", "--from", "Todo", "--to", "Done"],
     "state-todo"),
])
def test_a_move_outside_the_table_is_refused_before_any_write(
    config_path, board, client, monkeypatch, command, state,
):
    client.cards = [Card(sequence_id=12, state=state)]
    with_rules(board, require_transition_table=True)

    result = invoke(config_path, board, monkeypatch, *command)

    assert isinstance(result.exception, TransitionNotAllowed)
    assert "Transition table rule" in str(result.exception)
    assert board_writes(client) == []


def test_a_move_in_the_table_is_allowed_whatever_the_case(board, client):
    card = Card(sequence_id=12, state="state-progress")
    client.cards = [card]
    with_rules(board, require_transition_table=True)

    board.check_transition(card, "IN PROGRESS", "verifying")
    board.move_state(card, "Verifying")

    assert card.state == "state-verifying"


def test_the_refusal_names_where_the_card_may_go(board):
    with_rules(board, require_transition_table=True)

    with pytest.raises(
        TransitionNotAllowed, match="From Done a card may move only to: todo",
    ):
        board.check_transition(Card(), "Done", "Verifying")


# ---- configuration ---------------------------------------------------


@pytest.mark.parametrize("name", [
    "require_independent_verdicts", "require_transition_table",
])
def test_the_new_rules_default_off_and_accept_booleans(config_path, name):
    rules = load_config(config_path).project("DEMO").rules
    assert getattr(rules, name) is False
    document = json.loads(config_path.read_text(encoding="utf-8"))
    document["rules"] = {name: True}
    config_path.write_text(json.dumps(document), encoding="utf-8")
    assert getattr(load_config(config_path).project("DEMO").rules, name)

    document["rules"] = {name: "yes"}
    config_path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ConfigError, match=f"rules.{name} must be a boolean"):
        load_config(config_path)


# ---- review follow-ups -------------------------------------------------


@pytest.mark.parametrize("rule,error", [
    ("require_transition_table", TransitionNotAllowed),
    ("require_independent_verdicts", MissingIndependentVerdict),
])
@pytest.mark.parametrize("state", ["Done", "In Progress", "Verifying"])
def test_a_create_past_todo_is_refused_before_any_write(
    board, client, rule, error, state,
):
    with_rules(board, **{rule: True})

    with pytest.raises(error, match="Backlog or Todo"):
        board.create_card(
            title="Shortcut", description_html="<p>x</p>",
            assignee_id=AUTOMATION, module_name="pipeline",
            cycle_name="Sprint 1", estimate=3, state_name=state,
        )

    assert client.write_calls == []


def test_a_create_in_todo_is_allowed_under_both_rules(board, client):
    with_rules(board, require_transition_table=True,
               require_independent_verdicts=True)

    board.create_card(
        title="Admitted", description_html="<p>x</p>",
        assignee_id=AUTOMATION, module_name="pipeline",
        cycle_name="Sprint 1", estimate=3, state_name="Todo",
    )

    assert client.named("work_items.create")


def test_card_new_into_done_is_refused_before_any_write(
    config_path, board, client, monkeypatch,
):
    with_rules(board, require_transition_table=True)

    result = invoke(
        config_path, board, monkeypatch, "card", "new",
        "--title", "Shortcut",
        "--description", "Build it.", "--module", "pipeline",
        "--cycle", "Sprint 1", "--assignee", AUTOMATION, "--estimate", "3",
        "--state", "Done",
    )

    assert isinstance(result.exception, TransitionNotAllowed)
    assert client.write_calls == []


@pytest.mark.parametrize("state,activities", [
    # Left Verifying after both verdicts: they judged an older candidate.
    ("state-progress", [
        *ENTRY,
        SimpleNamespace(
            id="back", created_at="2026-09-16T03:00:00Z", field="state",
            old_value="Verifying", new_value="In Progress",
            actor="worker-id",
        ),
    ]),
    # Done → Todo reopened the card; old verdicts must not re-close it.
    ("state-todo", [*ENTRY]),
])
def test_done_from_outside_verifying_is_refused_under_the_verdict_rule(
    config_path, board, client, monkeypatch, state, activities,
):
    client.cards = [Card(sequence_id=12, state=state)]
    client.activities = activities
    client.comments = [QA, LEAD]
    with_rules(board, require_independent_verdicts=True)

    result = invoke(
        config_path, board, monkeypatch, "card", "move", "DEMO-12", "Done"
    )

    assert isinstance(result.exception, MissingIndependentVerdict)
    assert "only from Verifying" in str(result.exception)
    assert board_writes(client) == []


@pytest.mark.parametrize("body", [
    verdicts.VERDICT_PREFIX + "{}",
    "plane-proj-verdict/v1",
    execution.EVENT_PREFIX + '{"action":"start","category":"x"}',
    execution.EVENT_PREFIX_V2 + "{}",
    execution.REWORK_PREFIX + '{"reason":"spec"}',
])
def test_card_comment_refuses_a_structured_prefix_before_any_write(
    config_path, board, client, monkeypatch, body,
):
    client.cards = [Card(sequence_id=12, state="state-verifying")]

    result = invoke(
        config_path, board, monkeypatch, "card", "comment", "DEMO-12",
        "--body", body,
    )

    assert isinstance(result.exception, GuardViolation)
    assert "Structured comment rule" in str(result.exception)
    assert client.calls == []


def test_card_comment_still_posts_free_text_mentioning_a_verdict(
    config_path, board, client, monkeypatch,
):
    client.cards = [Card(sequence_id=12, state="state-verifying")]

    result = invoke(
        config_path, board, monkeypatch, "card", "comment", "DEMO-12",
        "--body", "See the plane-proj-verdict/v1 records above.",
    )

    assert result.exit_code == 0, result.output
    assert len(client.named("comments.create")) == 1


def test_a_corrupt_verdict_names_the_comment_to_remove():
    corrupt = SimpleNamespace(
        id="comment-77", created_at="2026-09-16T01:00:00Z",
        comment_html=f"<p>{verdicts.VERDICT_PREFIX}human note</p>",
    )

    with pytest.raises(GuardViolation, match="comment comment-77"):
        verdicts.verdicts([corrupt])


def test_a_verdict_without_created_at_refuses_naming_the_rule():
    undated = SimpleNamespace(id="comment-9", comment_html=QA.comment_html)

    with pytest.raises(
        GuardViolation, match="Verdict rule.*comment-9.*created_at",
    ):
        verdicts.verdicts([undated])


def test_a_verdict_retry_with_different_fields_conflicts(
    config_path, board, client, monkeypatch,
):
    client.cards = [Card(sequence_id=12, state="state-verifying")]
    first = invoke(config_path, board, monkeypatch, *verdict_args())
    args = verdict_args()
    args[args.index("pass")] = "fail"

    second = invoke(config_path, board, monkeypatch, *args)

    assert first.exit_code == 0, first.output
    assert isinstance(second.exception, GuardViolation)
    assert "already recorded a different verdict" in str(second.exception)
    assert len(client.named("comments.create")) == 1


def test_move_many_refuses_the_whole_batch_when_one_card_lacks_verdicts(
    config_path, board, client, monkeypatch,
):
    ready = Card(id="ready", sequence_id=1, state="state-verifying")
    lacking = Card(id="lacking", sequence_id=2, state="state-verifying")
    client.cards = [ready, lacking]
    comments = {"ready": [QA, LEAD], "lacking": [QA]}
    monkeypatch.setattr(board, "comments", lambda card: comments[card.id])
    monkeypatch.setattr(board, "activities", lambda card: list(ENTRY))
    with_rules(board, require_independent_verdicts=True)

    result = invoke(
        config_path, board, monkeypatch, "card", "move-many",
        "DEMO-1", "DEMO-2", "--from", "Verifying", "--to", "Done",
    )

    assert isinstance(result.exception, MissingIndependentVerdict)
    assert "DEMO-2" in str(result.exception)
    assert board_writes(client) == []
    assert ready.state == "state-verifying"


def test_move_many_outside_the_table_writes_nothing(
    config_path, board, client, monkeypatch,
):
    client.cards = [
        Card(id="a", sequence_id=1, state="state-todo"),
        Card(id="b", sequence_id=2, state="state-todo"),
    ]
    with_rules(board, require_transition_table=True)

    result = invoke(
        config_path, board, monkeypatch, "card", "move-many",
        "DEMO-1", "DEMO-2", "--from", "Todo", "--to", "Verifying",
    )

    assert isinstance(result.exception, TransitionNotAllowed)
    assert board_writes(client) == []
