"""The command surface.

One tool, so that the rules live in one place and every project reaches the
board the same way. The shape follows from who calls it: an agent that has
never read the config file, and a person who does not want to look one up.

- **Names, never UUIDs.** `--state Todo`, `--module pipeline`, `--assignee
  alice`. Plane supplies UUIDs; the config only retains estimate mappings.
- **`-` means stdin** on every long-text option.
- **A guard refuses before the first request**, and says which rule and what
  to do instead — the caller is usually an agent that cannot go and read
  `TASK-SIZING.md`.
- **`--json` for programs**, aligned columns for people.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click
from plane.errors import HttpError

from plane_proj import board as board_module
from plane_proj import dependencies as dependencies_module
from plane_proj import execution as execution_module
from plane_proj import operations as operations_module
from plane_proj import register as register_module
from plane_proj import sprint_timing
from plane_proj import sprints as sprints_module
from plane_proj import text as text_module
from plane_proj import verdicts as verdicts_module
from plane_proj import web as web_module
from plane_proj.config import (
    DEFAULT_CONFIG,
    IDLE_STATES,
    Config,
    bootstrap_document,
    load_config,
    merge_scale,
)
from plane_proj.credentials import (
    create_credentials_file,
    load_connection_target,
    load_credentials,
)
from plane_proj.guards import (
    GuardViolation,
    PlaneProjError,
    SprintCycleByHand,
)
from plane_proj.output import emit, records_table, table

BLANK_ESTIMATE = "blank"
SPRINT_ESTIMATE_FIELDS = frozenset({
    "points",
    "points_cancelled",
    "points_current",
    "points_done",
    "points_end",
    "points_start",
    "velocity",
})


class Context:
    """Everything a command needs, built once and only when a command needs it.

    Lazy because `--help` and a malformed config file must not need
    credentials, and because a person running `plane-proj` with no arguments
    should get usage rather than a complaint about an environment variable.
    """

    def __init__(self, config_path: str | None, project: str | None, as_json: bool,
                 env_file: Path | None = None) -> None:
        self.env_file = env_file
        self.config_path = config_path
        self.project_key = project
        self.as_json = as_json
        self._config: Config | None = None
        self._board: board_module.Board | None = None

    @property
    def config(self) -> Config:
        if self._config is None:
            self._config = load_config(self.config_path)
        return self._config

    @property
    def board(self) -> board_module.Board:
        if self._board is None:
            config = self.config
            self._board = board_module.connect(
                config,
                load_credentials(config.workspace_slug, config_path=config.path,
                                 env_file=self.env_file),
                self.project_key,
            )
        return self._board


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(None, "-v", "--version", package_name="plane-proj", prog_name="plane-proj")
@click.option("--conf", "config", envvar="PLANE_PROJ_CONFIG",
              help="Config file (default: plane/plane-proj.json).")
@click.option("--project", envvar="PLANE_PROJ_PROJECT", help="Project key, e.g. DEMO.")
@click.option("--env-file", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="File of PLANE_* credentials to load before connecting.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.pass_context
def cli(ctx: click.Context, config: str | None, project: str | None,
        env_file: Path | None, as_json: bool) -> None:
    """Run visible, governed multi-agent delivery through Plane."""
    ctx.obj = Context(config, project, as_json, env_file)


# ---- sprints ------------------------------------------------------------

@cli.group("sprints")
@click.option("--database", type=click.Path(dir_okay=False, path_type=Path),
              help="SQLite state file; overrides plane-proj.json state_file.")
@click.pass_context
def sprints(ctx: click.Context, database: Path | None) -> None:
    """Maintain planned, current, and completed sprints in SQLite."""
    ctx.ensure_object(dict)
    root = ctx.find_root().obj
    configured = None if database is not None else root.config.state_file
    if database is None and configured is None and ctx.invoked_subcommand == "start":
        raise click.ClickException(
            "sprints start requires state_file in plane-proj.json or --database PATH."
        )
    selected_database = database or configured or sprints_module.default_database_path()
    project = root.project_key or root.config.default_project
    if project is None:
        raise sprints_module.SprintError(
            "Sprint register binding requires --project or defaults.project."
        )
    host, workspace = load_connection_target(
        root.config.workspace_slug, config_path=root.config.path, env_file=root.env_file,
    )
    binding = (host, workspace, project)
    # bind and migrate must accept registers that predate the binding
    # table; every other subcommand requires the matching binding first.
    if ctx.invoked_subcommand not in {None, "bind", "migrate"}:
        sprints_module.require_binding(selected_database, binding)
    ctx.obj = {"root": root, "database": selected_database, "binding": binding}


@sprints.command("migrate")
@click.pass_obj
def sprints_migrate(obj: dict[str, Any]) -> None:
    """Migrate the register to the current schema, explicitly.

    Write commands migrate implicitly; this does only the migration and
    reports the version change. Registers older than the binding table
    still need `sprints bind` afterwards.
    """
    database = obj["database"]
    before = sprints_module.schema_version(database)
    sprints_module.connect_database(database, writable=True).close()
    after = sprints_module.schema_version(database)
    if before == after:
        click.echo(f"{database}: already at schema v{after}")
    else:
        click.echo(f"{database}: schema v{before} → v{after}")


@sprints.command("bind")
@click.pass_obj
def sprints_bind(obj: dict[str, Any]) -> None:
    """Bind a legacy register to the selected target after checking its cycles."""
    database, binding = obj["database"], obj["binding"]
    existing = sprints_module.read_binding(database)
    if existing is not None:
        sprints_module.require_binding(database, binding)
    else:
        for sprint_id, cycle_id in sprints_module.binding_cycles(database):
            found_id, _ = obj["root"].board.cycle_sprint_id(cycle_id)
            if found_id != sprint_id:
                raise sprints_module.SprintError(
                    f"Sprint register binding rule: cycle {cycle_id} is Sprint {found_id}, "
                    f"but the register records Sprint {sprint_id}."
                )
        sprints_module.bind_database(database, binding)
    click.echo(f"Bound {database} to {binding[1]} / {binding[2]}.")


@sprints.command("plan")
@click.option("--id", "sprint_id", required=True,
              help="Numeric ID or existing alias; new sprints need a number.")
@click.option("--alias", help="Optional unique sprint alias, e.g. DBT-CM1.")
@click.option("--title", required=True)
@click.option("--position", required=True, type=click.IntRange(min=1))
@click.option("--goal", required=True)
@click.option("--execution", default="", help="Ordering and execution guidance.")
@click.option("--acceptance", multiple=True, required=True,
              help="Acceptance criterion; repeat for each criterion.")
@click.option("--adopt-cycle", is_flag=True,
              help="Take over an existing `Sprint N` cycle and overwrite "
                   "its description with this plan.")
@click.pass_obj
def sprints_plan(
    obj: dict[str, Any], sprint_id: str, title: str, position: int,
    goal: str, execution: str, acceptance: tuple[str, ...], alias: str | None,
    adopt_cycle: bool,
) -> None:
    """Create or replace one future sprint plan and its `Sprint N` cycle.

    The cycle's description mirrors the plan. The register row is written
    after the cycle, so a retry reuses the cycle rather than adding one.
    The register's write lock is taken only for that final write; the
    checks are repeated under it so nothing changed in the meantime.
    """
    with sprints_module.connect_database(obj["database"], writable=True) as connection:
        sprint_id = sprints_module.resolve_sprint_id(connection, sprint_id)
        description = sprints_module.plan_description(
            title, goal, execution, acceptance,
        )
        sprints_module.validate_plan(
            connection, sprint_id, title, goal, acceptance, alias,
        )
        known = sprints_module.fetch_sprint(connection, sprint_id) is not None
        board = obj["root"].board
        cycle_id = board.sprint_cycles({sprint_id}).get(sprint_id)
        if cycle_id is not None:
            sprints_module.require_unbound_cycle(
                connection, sprint_id, cycle_id,
            )
            if not adopt_cycle:
                sprints_module.require_adoptable_cycle(
                    connection, sprint_id, cycle_id,
                    board.cycle_description(cycle_id), description,
                )
        cycle_id = board.write_sprint_cycle(sprint_id, cycle_id, description)
        connection.execute("BEGIN IMMEDIATE")
        sprints_module.require_unbound_cycle(connection, sprint_id, cycle_id)
        if not known and not adopt_cycle:
            sprints_module.require_unplanned(connection, sprint_id, cycle_id)
        sprints_module.plan_sprint(
            connection, sprint_id, title, position, goal, execution,
            acceptance, alias,
        )
    click.echo(f"Planned sprint {sprint_id}.")


@sprints.command("alias")
@click.argument("sprint_id")
@click.argument("alias", required=False)
@click.option("--clear", is_flag=True, help="Remove the alias.")
@click.pass_obj
def sprints_alias(
    obj: dict[str, Any], sprint_id: str, alias: str | None, clear: bool,
) -> None:
    """Assign or replace an alias for any sprint; use --clear to remove it."""
    if (alias is None) != clear:
        raise click.UsageError("Provide ALIAS or --clear, exclusively.")
    with sprints_module.connect_database(
        obj["database"], writable=True
    ) as connection:
        resolved = sprints_module.resolve_sprint_id(connection, sprint_id)
        sprints_module.set_alias(connection, resolved, alias)
    click.echo(f"Sprint {resolved} alias: {alias or 'none'}.")


@sprints.command("reorder")
@click.argument(
    "sprint_ids",
    nargs=-1,
    required=True,
    type=str,
)
@click.pass_obj
def sprints_reorder(obj: dict[str, Any], sprint_ids: tuple[str, ...]) -> None:
    """Set the complete planned-sprint order from first to last."""
    with sprints_module.connect_database(
        obj["database"], writable=True
    ) as connection:
        resolved = tuple(
            sprints_module.resolve_sprint_id(connection, ref)
            for ref in sprint_ids
        )
        sprints_module.reorder_planned_sprints(connection, resolved)
    click.echo(f"Reordered {len(sprint_ids)} planned sprints.")


@sprints.command("start")
@click.argument("cycle_id")
@click.option("--started", required=True, help="RFC 3339 timestamp with numeric offset.")
@click.pass_obj
def sprints_start(obj: dict[str, Any], cycle_id: str, started: str) -> None:
    """Start a sprint by numeric ID, alias, or explicit Plane cycle UUID."""
    requested_id = None
    with sprints_module.connect_database(
        obj["database"], writable=True
    ) as connection:
        try:
            uuid.UUID(cycle_id)
        except ValueError:
            sprint_id = sprints_module.resolve_sprint_id(connection, cycle_id)
            requested_id = sprint_id
            sprint = sprints_module.fetch_sprint(connection, sprint_id)
            if sprint is None:
                raise sprints_module.SprintError(
                    f"sprint {sprint_id} not found"
                ) from None
            board = obj["root"].board
            cycle_id = sprint.cycle_id or board.sprint_cycles(
                {sprint_id}
            ).get(sprint_id)
            if cycle_id is None:
                raise sprints_module.SprintError(
                    f"sprint {sprint_id} has no matching Plane cycle"
                ) from None
        board = obj["root"].board
        sprint_id, cycle = board.cycle_sprint_id(cycle_id)
        if requested_id is not None and sprint_id != requested_id:
            raise sprints_module.SprintError(
                "Sprint reference rule: Plane cycle no longer matches "
                f"sprint {requested_id}"
            )
        sprints_module.validate_sprint_start(connection, sprint_id, started, cycle_id)
        board.require_no_orphans(*_open_sprints(connection))
        cycle_name, admitted, backlogged = board.start_sprint_cycle(cycle_id, started)
        metrics = board.sprint_cycle_metrics(cycle_id)
        # Cancelled members are not admitted scope; opening totals
        # exclude them.
        sprints_module.start_sprint(
            connection,
            sprint_id,
            started,
            cycle_id,
            metrics["cards_current"] - metrics["cards_cancelled"],
            metrics["points_current"] - metrics["points_cancelled"],
        )
    click.echo(
        f"Started sprint {sprint_id} ({cycle_name or cycle.name}): "
        f"{len(admitted)} → Todo, {len(backlogged)} → Backlog."
    )


def _open_sprints(
    connection: sqlite3.Connection,
) -> tuple[dict[int, str], set[int]]:
    """Current sprints with their bound cycle ids, and planned sprint ids."""
    current: dict[int, str] = {}
    planned: set[int] = set()
    for sprint in sprints_module.fetch_sprints(connection):
        if sprint.status == sprints_module.STATUS_CURRENT and sprint.cycle_id:
            current[sprint.sprint_id] = sprint.cycle_id
        elif sprint.status == sprints_module.STATUS_PLANNED:
            planned.add(sprint.sprint_id)
    return current, planned


@sprints.command("check")
@click.pass_obj
def sprints_check(obj: dict[str, Any]) -> None:
    """Audit sprint membership and hygiene; writes nothing, exits 1 on findings.

    With cycles on, every card not Done or Cancelled belongs to a planned or
    current sprint. Also reported: Backlog cards inside a running sprint,
    active cards in a sprint that has not started, open timers on settled
    cards in a running sprint, and planned sprints with no Plane cycle.
    """
    root = obj["root"]
    with sprints_module.connect_database(
        obj["database"], writable=False
    ) as connection:
        current, planned = _open_sprints(connection)
    findings = root.board.sprint_findings(current, planned)
    emit(findings, as_json=root.as_json, render=lambda r: records_table(r, [
        ("card", "CARD"), ("finding", "FINDING"), ("detail", "DETAIL"),
    ]))
    if findings and not root.as_json:
        click.echo(f"\n{len(findings)} finding(s)", err=True)
    if findings:
        raise SystemExit(1)


@sprints.command("add")
@click.option("--id", "sprint_id", required=True,
              help="Numeric ID or existing alias; new sprints need a number.")
@click.option("--alias", help="Optional unique sprint alias, e.g. DBT-CM1.")
@click.option("--title", required=True)
@click.option("--started", required=True)
@click.option("--ended", required=True)
@click.option("--hours", required=True, type=float)
@click.option("--cards-start", required=True, type=click.IntRange(min=0))
@click.option("--cards-end", required=True, type=click.IntRange(min=0))
@click.option("--points-start", required=True, type=click.IntRange(min=0))
@click.option("--points-end", required=True, type=click.IntRange(min=0))
@click.option("--velocity", required=True, type=float)
@click.option("--delivered", required=True)
@click.option("--retrospective", default="")
@click.pass_obj
def sprints_add(
    obj: dict[str, Any], sprint_id: str, title: str, started: str, ended: str,
    hours: float, cards_start: int, cards_end: int, points_start: int,
    points_end: int, velocity: float, delivered: str, retrospective: str,
    alias: str | None,
) -> None:
    """Import one completed sprint (normally use start then close)."""
    with sprints_module.connect_database(
        obj["database"], writable=True
    ) as connection:
        sprint_id = sprints_module.resolve_sprint_id(connection, sprint_id)
        sprint = sprints_module.Sprint(
            sprint_id, title, sprints_module.STATUS_COMPLETED,
            started=started, ended=ended, hours=hours,
            cards_start=cards_start, cards_end=cards_end,
            points_start=points_start, points_end=points_end, velocity=velocity,
            delivered=delivered, retrospective=retrospective, alias=alias,
        )
        sprints_module.add_completed_sprint(connection, sprint)
    click.echo(f"Added sprint {sprint_id}.")


@sprints.command("preflight")
@click.argument("sprint_id")
@click.pass_obj
def sprints_preflight(obj: dict[str, Any], sprint_id: str) -> None:
    """Report closure readiness and derived accounting; writes nothing."""
    with sprints_module.connect_database(
        obj["database"], writable=False
    ) as connection:
        sprint_id = sprints_module.resolve_sprint_id(connection, sprint_id)
        payload = sprints_module.preflight_report(
            connection, obj["root"].board, sprint_id,
            now=datetime.now(UTC).isoformat(timespec="seconds"),
        )

    def render(data: dict[str, Any]) -> None:
        click.echo(
            f"sprint {data['sprint_id']}  cycle {data['cycle_id']}  "
            + ("READY" if data["ready"] else "NOT READY")
        )
        for entry in data["nonterminal_cards"]:
            click.echo(f"  nonterminal: {entry['card']} ({entry['state']})")
        for card in data["missing_final_snapshots"]:
            click.echo(f"  missing final snapshot: {card}")
        for timer in data["open_timers"]:
            click.echo(
                f"  open timer: {timer['card']} ({timer['category']})"
            )
        derived = data["derived"]
        click.echo(
            f"  derived: hours {derived['hours']:.2f}  cards "
            f"{derived['cards_start']}→{derived['cards_end']}  points "
            f"{derived['points_start']}→{derived['points_end']}  done "
            f"{derived['points_done']}  velocity "
            f"{sprints_module.format_velocity(derived['velocity'])}/h"
        )

    emit(payload, as_json=obj["root"].as_json, render=render)


@sprints.command("close")
@click.argument("sprint_id")
@click.option("--ended", required=True)
@click.option("--delivered", required=True)
@click.option("--retrospective", default="")
@click.pass_obj
def sprints_close(obj: dict[str, Any], sprint_id: str, ended: str,
                  delivered: str, retrospective: str) -> None:
    """Close the current sprint; accounting is derived, never hand-typed.

    Hours come from start/end, closing totals from the live cycle, and
    velocity from Done points over elapsed hours. Opening totals stay as
    recorded at start. Legacy imports use sprints add.
    """
    with sprints_module.connect_database(obj["database"], writable=True) as connection:
        sprint_id = sprints_module.resolve_sprint_id(connection, sprint_id)
        current = sprints_module.fetch_sprint(connection, sprint_id)
        if (
            current is None
            or current.status != sprints_module.STATUS_CURRENT
            or current.cycle_id is None
        ):
            raise sprints_module.SprintError(f"sprint {sprint_id} is not current")
        board = obj["root"].board
        metrics = board.sprint_cycle_metrics(current.cycle_id)
        derived = sprints_module.derive_close_accounting(current, ended, metrics)
        sprint = sprints_module.validate_sprint_close(
            connection, sprint_id, ended, derived["hours"],
            derived["cards_start"], derived["cards_end"],
            derived["points_start"], derived["points_end"],
            derived["velocity"], delivered,
        )
        cycle_cards = board.cycle_cards(sprint.cycle_id)
        missing = sprints_module.missing_final_snapshots(
            connection, sprint_id, {str(card.id) for card in cycle_cards}
        )
        if missing:
            references = sorted(
                f"{board.project.key}-{getattr(card, 'sequence_id', '?')}"
                for card in cycle_cards if str(card.id) in missing
            )
            raise sprints_module.SprintError(
                "final execution snapshot missing; run sprints collect for: "
                + ", ".join(references)
            )
        cycle_name = board.close_sprint_cycle(sprint.cycle_id, ended)
        sprints_module.close_sprint(
            connection, sprint_id, ended, derived["hours"],
            derived["cards_start"], derived["cards_end"],
            derived["points_start"], derived["points_end"],
            derived["velocity"], delivered, retrospective,
        )
    click.echo(
        f"Closed sprint {sprint_id} and ended Plane cycle {cycle_name!r}: "
        f"{derived['hours']:.2f}h, {derived['points_done']} done points, "
        f"velocity {sprints_module.format_velocity(derived['velocity'])}/h."
    )


@sprints.command("collect")
@click.argument("references", nargs=-1)
@click.option(
    "--sprint",
    "sprint_id",
    type=str,
    help=(
        "Sprint ID (required if multiple sprints are current "
        "without references)."
    ),
)
@click.pass_obj
def sprints_collect(
    obj: dict[str, Any],
    references: tuple[str, ...],
    sprint_id: str | None = None,
) -> None:
    """Persist execution snapshots for selected or every current-sprint card."""
    with sprints_module.connect_database(obj["database"], writable=True) as connection:
        if sprint_id is not None:
            sprint_id = sprints_module.resolve_sprint_id(connection, sprint_id)
        current_sprints = [
            sprint for sprint in sprints_module.fetch_sprints(connection)
            if sprint.status == sprints_module.STATUS_CURRENT
        ]
        if not current_sprints:
            raise sprints_module.SprintError("there is no current sprint with a Plane cycle")

        if sprint_id is not None:
            matching = [s for s in current_sprints if s.sprint_id == sprint_id]
            if not matching:
                raise sprints_module.SprintError(
                    f"sprint {sprint_id} is not current"
                )
            target_sprints = matching
        elif len(current_sprints) == 1:
            target_sprints = current_sprints
        elif not references:
            ids = ", ".join(str(s.sprint_id) for s in current_sprints)
            raise sprints_module.SprintError(
                f"Multiple sprints are current ({ids}); specify --sprint "
                "or pass references."
            )
        else:
            target_sprints = current_sprints

        board = obj["root"].board
        by_reference: dict[str, tuple[Any, int]] = {}
        for sprint in target_sprints:
            if sprint.cycle_id is None:
                continue
            for card in board.cycle_cards(sprint.cycle_id):
                ref = f"{board.project.key}-{getattr(card, 'sequence_id', '?')}"
                by_reference[ref] = (card, sprint.sprint_id)

        if references:
            unknown = [
                ref for ref in references if ref not in by_reference
            ]
            if unknown:
                target_desc = (
                    f"current sprint {target_sprints[0].sprint_id}"
                    if len(target_sprints) == 1
                    else "current sprints"
                )
                raise sprints_module.SprintError(
                    f"cards are not in {target_desc}: {', '.join(unknown)}"
                )
            selected = [
                (
                    by_reference[reference][0],
                    reference,
                    by_reference[reference][1],
                )
                for reference in references
            ]
        else:
            selected = [
                (card, reference, sid)
                for reference, (card, sid) in by_reference.items()
            ]

        if len(selected) > board_module.BATCH_CARD_MAXIMUM:
            raise sprints_module.SprintError(
                f"collection selects {len(selected)} cards; maximum is "
                f"{board_module.BATCH_CARD_MAXIMUM} within Plane's request budget"
            )
        telemetry = [
            (board.activities(card), board.comments(card))
            for card, _, _ in selected
        ]
        # The cutoff follows every read, at full precision; see
        # operations._collect_snapshot.
        as_of = datetime.now(UTC)
        captured_at = as_of.isoformat(timespec="seconds")
        snapshots = []
        for (card, reference, sid), (activities, comments) in zip(
            selected, telemetry, strict=True
        ):
            stats = execution_module.execution_stats(
                activities, comments, as_of=as_of
            )
            state_name = _state_name(board.project, getattr(card, "state", None))
            snapshots.append((card, reference, state_name, stats, sid))
        for card, reference, state_name, stats, sid in snapshots:
            sprints_module.record_execution_snapshot(
                connection,
                sprint_id=sid,
                work_item_id=str(card.id),
                card_reference=reference,
                captured_at=captured_at,
                is_final=state_name.casefold() in {"done", "cancelled"},
                stats=stats,
            )
    emit(
        [{"card": reference, "state": state_name, "final": state_name.casefold()
          in {"done", "cancelled"}, "captured_at": captured_at}
         for _, reference, state_name, _, _ in snapshots],
        as_json=obj["root"].as_json,
        render=lambda rows: records_table(rows, [
            ("card", "CARD"), ("state", "STATE"), ("final", "FINAL"),
            ("captured_at", "CAPTURED"),
        ]),
    )


@sprints.command("telemetry")
@click.argument("sprint_id", required=False)
@click.option("--all", "include_all", is_flag=True, help="Include superseded snapshots.")
@click.pass_obj
def sprints_telemetry(
    obj: dict[str, Any], sprint_id: str | None, include_all: bool
) -> None:
    """Read persisted per-card execution statistics."""
    with sprints_module.connect_database(obj["database"], writable=False) as connection:
        if sprint_id is not None:
            sprint_id = sprints_module.resolve_sprint_id(connection, sprint_id)
        if sprint_id is None:
            currents = [
                sprint for sprint in sprints_module.fetch_sprints(connection)
                if sprint.status == sprints_module.STATUS_CURRENT
            ]
            if len(currents) > 1:
                ids = ", ".join(str(s.sprint_id) for s in currents)
                raise sprints_module.SprintError(
                    f"Multiple sprints are current ({ids}); specify SPRINT_ID"
                )
            if not currents:
                raise sprints_module.SprintError("there is no current sprint; pass SPRINT_ID")
            sprint_id = currents[0].sprint_id
        snapshots = sprints_module.fetch_execution_snapshots(connection, sprint_id)
    if not include_all:
        latest = {}
        for snapshot in snapshots:
            latest[str(snapshot["work_item_id"])] = snapshot
        snapshots = list(latest.values())
    emit(
        snapshots,
        as_json=obj["root"].as_json,
        render=lambda rows: records_table(rows, [
            ("card_reference", "CARD"), ("captured_at", "CAPTURED"),
            ("is_final", "FINAL"), ("stats", "STATS"),
        ]),
    )


def _list_sprints(
    obj: dict[str, Any], *, include_all: bool, statuses: frozenset[str] | None = None,
    as_json: bool = False,
) -> None:
    """Read and emit sprint records, optionally limited by lifecycle status."""
    listing = _sprint_listing(obj, include_all=include_all, statuses=statuses)
    payload = listing["payload"]
    emit(
        payload,
        as_json=as_json or obj["root"].as_json,
        render=lambda _: _render_sprint_listing(
            payload, listing["timings"],
            listing["selected"],
            include_all=include_all,
            statuses=statuses,
            planned_totals=listing["planned_totals"],
            current_metrics=listing["current_metrics"],
            include_estimates=listing["estimates_enabled"],
        ),
    )


def _sprint_listing(
    obj: dict[str, Any], *, include_all: bool, statuses: frozenset[str] | None = None,
    local: bool = False,
) -> dict[str, Any]:
    """Build the `sprints list` payload and the facts its renderer needs.

    `local` reads only the register: planned card and point totals and
    current live counts, which come from Plane, are left out.
    """
    with sprints_module.connect_database(obj["database"], writable=False) as connection:
        found = sprints_module.fetch_sprints(connection)
        timings = sprint_timing.summaries(connection, found)
    selected = [
        sprint for sprint in found if statuses is None or sprint.status in statuses
    ]
    planned = [sprint for sprint in selected if sprint.status == sprints_module.STATUS_PLANNED]
    current = [
        sprint for sprint in selected
        if sprint.status == sprints_module.STATUS_CURRENT
    ]
    board = obj["root"].board if (planned or current) and not local else None
    planned_totals = (
        board.planned_sprint_totals({sprint.sprint_id for sprint in planned})
        if planned and board is not None else {}
    )
    estimates_enabled = (
        board.project.estimates_enabled
        if board is not None
        else _configured_estimates_enabled(obj["root"])
    )
    current_metrics: dict[int, dict[str, int | float]] = {}
    for sprint in selected:
        if sprint.status != sprints_module.STATUS_CURRENT or sprint.cycle_id is None:
            continue
        if board is None:
            if local:
                continue
            raise sprints_module.SprintError(
                "current sprint requires a Plane board"
            )
        metrics = board.sprint_cycle_metrics(sprint.cycle_id)
        if estimates_enabled:
            metrics["velocity"] = sprints_module.current_velocity(
                sprint.started or "", metrics["points_done"]
            )
        current_metrics[sprint.sprint_id] = metrics
    by_status = {
        sprints_module.STATUS_CURRENT: "current",
        sprints_module.STATUS_PLANNED: "planned",
        sprints_module.STATUS_COMPLETED: "past",
    }
    payload = {
        label: [
            sprints_module.sprint_record(
                sprint,
                include_all=include_all,
                planned_total=planned_totals.get(sprint.sprint_id),
                current_metrics=current_metrics.get(sprint.sprint_id),
            ) | {"timing": timings[sprint.sprint_id]}
            for sprint in selected if sprint.status == status
        ]
        for status, label in by_status.items()
        if statuses is None or status in statuses
    }
    if statuses is None or sprints_module.STATUS_COMPLETED in statuses:
        payload["stats"] = sprints_module.stats(selected) | {
            "timing": sprint_timing.statistics(selected, timings)
        }
    if not estimates_enabled:
        payload = _without_sprint_estimates(payload)
    return {
        "payload": payload,
        "timings": timings,
        "selected": selected,
        "planned_totals": planned_totals,
        "current_metrics": current_metrics,
        "estimates_enabled": estimates_enabled,
        "board": board,
    }


def _render_sprint_listing(
    payload: dict[str, Any], timings: dict[int, dict[str, Any] | None],
    found: list[sprints_module.Sprint], **kwargs: Any,
) -> None:
    sprints_module.render_sprint_list(
        found, timing_fields={
            key: sprint_timing.fields(value) for key, value in timings.items()
            if value is not None and value["timed_cards"] > 0
        },
        **kwargs,
    )
    if "stats" in payload:
        sprint_timing.render_statistics(payload["stats"]["timing"])


def _without_sprint_estimates(value: Any) -> Any:
    """Remove estimate-derived output while retaining stored accounting."""
    if isinstance(value, dict):
        return {
            key: _without_sprint_estimates(item)
            for key, item in value.items()
            if key not in SPRINT_ESTIMATE_FIELDS
        }
    if isinstance(value, list):
        return [
            _without_sprint_estimates(item) for item in value
        ]
    return value


def _configured_estimates_enabled(root: Context) -> bool:
    """Use the saved scale when a local report deliberately avoids Plane."""
    return bool(root.config.project(root.project_key).estimate_points)


@sprints.group("list", invoke_without_command=True)
@click.option("--all", "include_all", is_flag=True, help="Include every stored field.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.pass_context
def sprints_list(ctx: click.Context, include_all: bool, as_json: bool) -> None:
    """List all sprints, or select current, planned, or past."""
    ctx.obj["list_include_all"] = include_all
    ctx.obj["list_json"] = as_json
    if ctx.invoked_subcommand is None:
        _list_sprints(ctx.obj, include_all=include_all, as_json=as_json)


@sprints_list.command("current")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.pass_obj
def sprints_list_current(obj: dict[str, Any], as_json: bool) -> None:
    """List the current sprint."""
    _list_sprints(
        obj, include_all=obj["list_include_all"],
        statuses=frozenset({sprints_module.STATUS_CURRENT}),
        as_json=as_json or obj["list_json"],
    )


@sprints_list.command("planned")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.pass_obj
def sprints_list_planned(obj: dict[str, Any], as_json: bool) -> None:
    """List future sprints in execution order."""
    _list_sprints(
        obj, include_all=obj["list_include_all"],
        statuses=frozenset({sprints_module.STATUS_PLANNED}),
        as_json=as_json or obj["list_json"],
    )


@sprints_list.command("past")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.pass_obj
def sprints_list_past(obj: dict[str, Any], as_json: bool) -> None:
    """List completed sprints chronologically."""
    _list_sprints(
        obj, include_all=obj["list_include_all"],
        statuses=frozenset({sprints_module.STATUS_COMPLETED}),
        as_json=as_json or obj["list_json"],
    )


cli.add_command(sprints, "sprint")


def _open_sprint_cycles(obj: dict[str, Any]) -> dict[int, str]:
    """Cycle ids of current sprints, then planned sprints in order."""
    with sprints_module.connect_database(
        obj["database"], writable=False
    ) as connection:
        found = sprints_module.fetch_sprints(connection)
    cycles = {
        sprint.sprint_id: sprint.cycle_id for sprint in found
        if sprint.status == sprints_module.STATUS_CURRENT and sprint.cycle_id
    }
    planned = sorted(
        (s for s in found if s.status == sprints_module.STATUS_PLANNED),
        key=lambda sprint: sprint.position or 0,
    )
    named = obj["root"].board.sprint_cycles({s.sprint_id for s in planned})
    cycles.update(
        (sprint.sprint_id, named[sprint.sprint_id])
        for sprint in planned if sprint.sprint_id in named
    )
    return cycles


@sprints.command("ready")
@click.pass_obj
def sprints_ready(obj: dict[str, Any]) -> None:
    """List waiting cards whose blockers are all settled, in any sprint."""
    board = obj["root"].board
    payload = dependencies_module.ready(
        board.dependency_facts(_open_sprint_cycles(obj))
    )

    def render(data: dict[str, Any]) -> None:
        click.echo("Ready")
        records_table(data["ready"], [
            ("ref", "CARD"), ("sprint", "SPRINT"), ("state", "STATE"),
            ("points", "POINTS"), ("title", "TITLE"),
        ])
        click.echo("\nBlocked")
        records_table(
            [card | {"waiting_on": ", ".join(card["blocked_by"])}
             for card in data["blocked"]],
            [("ref", "CARD"), ("sprint", "SPRINT"),
             ("waiting_on", "WAITING ON"), ("title", "TITLE")],
        )

    emit(payload, as_json=obj["root"].as_json, render=render)


@sprints.command("critical-path")
@click.pass_obj
def sprints_critical_path(obj: dict[str, Any]) -> None:
    """Show the longest dependency chain and the parallel speedup ceiling."""
    board = obj["root"].board
    payload = dependencies_module.critical_path(
        board.dependency_facts(_open_sprint_cycles(obj)),
        use_points=board.project.estimates_enabled,
    )

    def render(data: dict[str, Any]) -> None:
        unit = data["unit"]
        click.echo(f"Open work: {data['total']} {unit}")
        click.echo(f"Critical path: {data['critical_path']} {unit}")
        ceiling = data["speedup_ceiling"]
        click.echo(
            "Speedup ceiling: "
            + ("—" if ceiling is None else f"{ceiling}x")
            + " (open work ÷ critical path; more agents cannot beat it)"
        )
        records_table(data["chain"], [
            ("ref", "CARD"), ("sprint", "SPRINT"), ("state", "STATE"),
            ("points", "POINTS"), ("title", "TITLE"),
        ])

    emit(payload, as_json=obj["root"].as_json, render=render)


@sprints.command("web")
@click.option("--host", default="127.0.0.1", show_default=True,
              help="Address to listen on.")
@click.option("--port", default=8765, type=int, show_default=True,
              help="Port to listen on.")
@click.option("--plane-url",
              help="Plane web app address for links; overrides "
                   "defaults.web_url in plane-proj.json.")
@click.pass_obj
def sprints_web(
    obj: dict[str, Any], host: str, port: int, plane_url: str | None,
) -> None:
    """Serve a read-only web view of current, planned, and past sprints."""
    plane_url = plane_url or obj["root"].config.web_url
    def load() -> dict[str, Any]:
        listing = _sprint_listing(obj, include_all=True)
        board = obj["root"].board
        project = board.project
        current = {
            sprint.sprint_id: sprint.cycle_id
            for sprint in listing["selected"]
            if sprint.status == sprints_module.STATUS_CURRENT
            and sprint.cycle_id is not None
        }
        facts = board.dependency_facts(current)
        cards = {
            sprint_id: web_module.with_blockers(members, facts)
            for sprint_id, members in facts["members"].items()
        }
        return web_module.build_payload(
            listing["payload"],
            project={"key": project.key, "name": project.name},
            board=web_module.board_url(plane_url, board.slug, project.id),
            cards=cards,
            estimates=listing["estimates_enabled"],
        )

    def load_local() -> dict[str, Any]:
        """The register alone, in milliseconds, for the page's first paint."""
        listing = _sprint_listing(obj, include_all=True, local=True)
        _, _, project = obj["binding"]
        return web_module.build_payload(
            listing["payload"], project={"key": project, "name": ""},
            board=None, cards={}, estimates=listing["estimates_enabled"],
        ) | {"partial": True}

    def dependencies() -> dict[str, Any]:
        board = obj["root"].board
        facts = board.dependency_facts(_open_sprint_cycles(obj))
        return dependencies_module.ready(facts) | {
            "critical_path": dependencies_module.critical_path(
                facts, use_points=board.project.estimates_enabled
            ),
        }

    server = web_module.make_server(host, port, {
        "/api/sprints": load,
        "/api/sprints/local": load_local,
        "/api/version": lambda: {
            "version": web_module.register_version(obj["database"])
        },
        "/api/dependencies": dependencies,
    })
    click.echo(f"Serving sprints on http://{host}:{server.server_port}/ (Ctrl+C stops)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


@sprints.command("show")
@click.argument("sprint_id", required=False)
@click.pass_obj
def sprints_show(obj: dict[str, Any], sprint_id: str | None) -> None:
    """Show every fact for a sprint; defaults to all current sprints."""
    with (
        sprints_module.connect_database(
            obj["database"], writable=False
        ) as connection
    ):
        if sprint_id is not None:
            sprint_id = sprints_module.resolve_sprint_id(connection, sprint_id)
        if sprint_id is None:
            target_sprints = [
                item
                for item in sprints_module.fetch_sprints(connection)
                if item.status == sprints_module.STATUS_CURRENT
            ]
        else:
            sprint = sprints_module.fetch_sprint(connection, sprint_id)
            if sprint is None:
                raise sprints_module.SprintError(
                    f"sprint {sprint_id} not found"
                )
            target_sprints = [sprint]
        timings = sprint_timing.summaries(connection, target_sprints)

    if not target_sprints and sprint_id is None:
        emit(
            None,
            as_json=obj["root"].as_json,
            render=lambda _: click.echo("No current sprint"),
        )
        return

    items: list[tuple[dict[str, Any], Any]] = []
    for s in target_sprints:
        timing = timings.get(s.sprint_id)
        estimates_enabled = _configured_estimates_enabled(obj["root"])
        if s.status == sprints_module.STATUS_CURRENT:
            if s.cycle_id is None:
                raise sprints_module.SprintError(
                    f"current sprint {s.sprint_id} has no Plane cycle ID"
                )
            estimates_enabled = obj["root"].board.project.estimates_enabled
            metrics = obj["root"].board.sprint_cycle_metrics(s.cycle_id)
            record = sprints_module.current_sprint_detail(s, metrics)
        else:
            record = sprints_module.sprint_record(s, include_all=True)
        record["timing"] = timing
        if not estimates_enabled:
            record = _without_sprint_estimates(record)
        items.append((record, timing))

    if len(items) == 1:
        single_record, single_timing = items[0]
        emit(
            single_record,
            as_json=obj["root"].as_json,
            render=lambda data: sprints_module.render_sprint_detail(
                data, timing_fields=sprint_timing.fields(single_timing)
            ),
        )
    else:
        def _render_multiple(_: list[dict[str, Any]]) -> None:
            for index, (record, timing) in enumerate(items):
                if index > 0:
                    click.echo()
                sprints_module.render_sprint_detail(
                    record, timing_fields=sprint_timing.fields(timing)
                )

        emit(
            [record for record, _ in items],
            as_json=obj["root"].as_json,
            render=_render_multiple,
        )


@sprints.command("stats")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.pass_obj
def sprints_stats(obj: dict[str, Any], as_json: bool) -> None:
    """Summarize completed sprint facts."""
    with sprints_module.connect_database(obj["database"], writable=False) as connection:
        found = sprints_module.fetch_sprints(connection)
        timings = sprint_timing.summaries(connection, found)
        payload = sprints_module.stats(found) | {"timing": sprint_timing.statistics(found, timings)}
    if not _configured_estimates_enabled(obj["root"]):
        payload = _without_sprint_estimates(payload)
    emit(payload, as_json=as_json or obj["root"].as_json,
         render=_render_sprint_statistics)


def _render_sprint_statistics(payload: dict[str, Any]) -> None:
    sprints_module.render_stats({key: value for key, value in payload.items() if key != "timing"})
    sprint_timing.render_statistics(payload["timing"])


# ---- register -----------------------------------------------------------

@cli.group("register")
def register() -> None:
    """Git merge driver and diff view for the sprint register.

    No credentials, server, or binding: git runs these during merges.
    """


@register.command("merge")
@click.argument("base", type=click.Path(dir_okay=False, path_type=Path))
@click.argument("ours", type=click.Path(exists=True, dir_okay=False,
                                        path_type=Path))
@click.argument("theirs", type=click.Path(exists=True, dir_okay=False,
                                          path_type=Path))
def register_merge(base: Path, ours: Path, theirs: Path) -> None:
    """Merge THEIRS into OURS (git passes %O %A %B); exit 1 on conflict."""
    register_module.merge_registers(base, ours, theirs)


@register.command("dump")
@click.argument("file", type=click.Path(exists=True, dir_okay=False,
                                        path_type=Path))
def register_dump(file: Path) -> None:
    """Print the register as sorted text for git diff textconv."""
    click.echo(register_module.dump_register(file), nl=False)


@register.command("git-setup")
@click.pass_obj
def register_git_setup(obj: Context) -> None:
    """Wire the merge driver and textconv into this git work tree."""
    configured = (
        obj.config.state_file
        if obj.config_path is not None or DEFAULT_CONFIG.is_file()
        else None
    )
    database = configured or sprints_module.default_database_path()
    _report_git_setup(register_module.setup_git(Path.cwd(), database))


def _report_git_setup(changes: list[str]) -> None:
    if not changes:
        click.echo("Register git integration already set up.")
    for change in changes:
        click.echo(f"Set {change}")


# ---- card ---------------------------------------------------------------

@cli.group()
def card() -> None:
    """Work items. Work item = card; 'card' is this CLI's shorthand."""


@card.command("list")
@click.option("--state", help="Only work items in this state.")
@click.option("--module", "module_name", help="Only work items in this module.")
@click.option("--assignee", help="Only work items assigned to this member.")
@click.option("--unestimated", is_flag=True, help="Only work items carrying no estimate.")
@click.option("--all", "show_all", is_flag=True,
              help="Include Done, Backlog, Cancelled, and archived work items.")
@click.pass_obj
def card_list(obj: Context, state: str | None, module_name: str | None,
              assignee: str | None, unestimated: bool, show_all: bool) -> None:
    """List work items; Done, Backlog, and Cancelled need --all or --state."""
    board = obj.board
    project, config = board.project, board.config
    wanted_state = project.state_id(state) if state else None
    wanted_assignee = config.member_id(assignee) if assignee else None
    module_members = (
        {str(getattr(c, "id", "")) for c in board.client.modules.list_work_items(
            board.slug, project.id, project.module_id(module_name)).results}
        if module_name else None
    )

    records = []
    for item in board.cards(include_archived=show_all):
        state_name = _state_name(project, getattr(item, "state", None))
        if not show_all and (
            getattr(item, "archived_at", None)
            # An explicit --state shows that state even when hidden by default.
            or (state is None
                and state_name.casefold() in {"done", "backlog", "cancelled"})
        ):
            continue
        estimate = (
            project.estimate_value(getattr(item, "estimate_point", None))
            if project.estimates_enabled
            else None
        )
        if wanted_state and str(getattr(item, "state", "")) != wanted_state:
            continue
        if wanted_assignee and wanted_assignee not in (getattr(item, "assignees", None) or []):
            continue
        if module_members is not None and str(item.id) not in module_members:
            continue
        if unestimated and estimate is not None:
            continue
        record = {
            "card": f"{project.key}-{getattr(item, 'sequence_id', '?')}",
            "work_item_id": str(item.id),
            "state": state_name,
            "owner": config.member_name(_first(getattr(item, "assignees", None))),
            "title": text_module.to_plain(getattr(item, "name", ""), limit=60),
            "cycles": [],
        }
        if project.estimates_enabled:
            record["estimate"] = estimate
        records.append(record)

    by_id = {row["work_item_id"]: row for row in records}
    if by_id:
        for cycle_name, cycle_id in sorted(project.cycles.items()):
            for item_id in board.cycle_card_ids(cycle_id) & by_id.keys():
                by_id[item_id]["cycles"].append(cycle_name)

    columns = [("card", "CARD"), ("state", "STATE")]
    if project.estimates_enabled:
        columns.append(("estimate", "EST"))
    columns.extend([
        ("owner", "OWNER"), ("cycle", "CYCLE"), ("title", "TITLE"),
    ])
    emit(records, as_json=obj.as_json, render=lambda r: records_table([
        {**row, "cycle": ", ".join(row["cycles"]) or None} for row in r
    ], columns))


@card.command("show")
@click.argument("reference")
@click.pass_obj
def card_show(obj: Context, reference: str) -> None:
    """Show one work item in full, including its attachments."""
    board = obj.board
    item = board.find(reference)
    project, config = board.project, board.config
    payload = {
        "card": f"{project.key}-{getattr(item, 'sequence_id', '?')}",
        "work_item_id": item.id,
        "title": getattr(item, "name", ""),
        "state": _state_name(project, getattr(item, "state", None)),
        "assignees": [config.member_name(a) for a in (getattr(item, "assignees", None) or [])],
        "priority": getattr(item, "priority", None),
        "description_html": getattr(item, "description_html", ""),
        "relations": _named_relations(board, board.relations(item.id)),
        "attachments": board.attachments(item.id),
    }
    if project.estimates_enabled:
        payload["estimate"] = project.estimate_value(
            getattr(item, "estimate_point", None)
        )

    def render(data: dict[str, Any]) -> None:
        def field(label: str, value: str) -> None:
            # Wider than the longest relation name, so a 10-character
            # `blocked_by` cannot run into its own value.
            click.echo(f"{label:<13}{value}")

        click.echo(click.style(f"{data['card']}  {data['title']}", bold=True))
        field("state", data["state"])
        if "estimate" in data:
            field(
                "estimate",
                str(data["estimate"]) if data["estimate"] is not None else "—",
            )
        field("owner", ", ".join(data["assignees"]) or "—")
        for name, refs in data["relations"].items():
            field(name, ", ".join(refs))
        if data["description_html"]:
            click.echo()
            click.echo("description_html:")
            click.echo(data["description_html"])
        _render_attachments(data["attachments"])

    emit(payload, as_json=obj.as_json, render=render)


@card.command("new")
@click.option("--title", required=True, help="One bounded instruction.")
@click.option("--description", default="-", show_default="stdin",
              help="Markdown; '-' reads stdin.")
@click.option("--html", "is_html", is_flag=True, help="Description is already HTML.")
@click.option("--module", "module_name", help="Module the work is in.")
@click.option("--cycle", "cycle_name",
              help="Cycle of the current or a planned sprint the work item joins.")
@click.option("--assignee", required=True, help="The one owner.")
@click.option("--estimate", help=f"Scale value, or '{BLANK_ESTIMATE}' for an owner who takes none.")
@click.option("--state", "state_name", help="Starting state.")
@click.option("--label", "labels", multiple=True, help="Label; repeatable.")
@click.option("--priority", help="urgent, high, medium, low or none.")
@click.pass_obj
def card_new(obj: Context, title: str, description: str, is_html: bool, module_name: str | None,
             cycle_name: str | None, assignee: str, estimate: str | None,
             state_name: str | None, labels: tuple[str, ...], priority: str | None) -> None:
    """Create a work item, place it in its cycle and module, and verify all three."""
    board = obj.board
    body = text_module.read_text(description) or ""
    written = board.create_card(
        title=title,
        description_html=text_module.to_html(body, already_html=is_html),
        assignee_id=board.config.member_id(assignee),
        module_name=module_name,
        cycle_name=cycle_name,
        estimate=_parse_estimate(estimate),
        blank_estimate=estimate == BLANK_ESTIMATE,
        state_name=state_name,
        label_names=labels,
        priority=priority,
    )
    emit({"card": f"{board.project.key}-{written.reference}",
          "work_item_id": written.card_id,
          "steps": list(written.steps)},
         as_json=obj.as_json,
         render=lambda d: click.echo(f"{d['card']}  {', '.join(d['steps'])}"))


@card.command("move")
@click.argument("reference")
@click.argument("state")
@click.pass_obj
def card_move(obj: Context, reference: str, state: str) -> None:
    """Move a work item to a state."""
    board = obj.board
    item = board.find(reference)
    names = {state_id: name for name, state_id in board.project.states.items()}
    current = names.get(str(getattr(item, "state", "")), "")
    _refuse_untracked_sendback(current, state)
    board.move_state(item, state)
    click.echo(f"{reference} → {state}")


def _refuse_untracked_sendback(from_state: str, to_state: str) -> None:
    if execution_module.is_rework(from_state, to_state):
        raise GuardViolation(
            "Rework reason rule: a send-back (Verifying → In Progress) goes "
            "through `card transition --from Verifying --to 'In Progress' "
            "--reason REASON`, so its reason and telemetry are recorded."
        )


@card.command("move-many")
@click.argument("references", nargs=-1)
@click.option("--from", "from_state", required=True, help="Required current state.")
@click.option("--to", "to_state", required=True, help="Target state.")
@click.pass_obj
def card_move_many(
    obj: Context,
    references: tuple[str, ...],
    from_state: str,
    to_state: str,
) -> None:
    """Move up to 20 cards; omit CARD references to select the whole source state."""
    _refuse_untracked_sendback(from_state, to_state)
    board = obj.board
    moved = board.move_states(references, from_state=from_state, to_state=to_state)
    payload = {
        "from": from_state,
        "to": to_state,
        "cards": [
            f"{board.project.key}-{getattr(item, 'sequence_id', '?')}" for item in moved
        ],
    }
    emit(
        payload,
        as_json=obj.as_json,
        render=lambda data: click.echo(
            f"{len(data['cards'])} cards: {data['from']} → {data['to']}"
        ),
    )


@card.command("estimate")
@click.argument("reference")
@click.argument("value")
@click.pass_obj
def card_estimate(obj: Context, reference: str, value: str) -> None:
    """Set a work item's estimate, or blank it."""
    board = obj.board
    board.set_estimate(board.find(reference), _parse_estimate(value),
                       blank_estimate=value == BLANK_ESTIMATE)
    click.echo(f"{reference} estimate {value}")


@card.command("comment")
@click.argument("reference")
@click.option("--body", default="-", show_default="stdin", help="Markdown; '-' reads stdin.")
@click.option("--html", "is_html", is_flag=True, help="Body is already HTML.")
@click.pass_obj
def card_comment(obj: Context, reference: str, body: str, is_html: bool) -> None:
    """Post a comment."""
    board = obj.board
    text = text_module.read_text(body) or ""
    comment_html = text_module.to_html(text, already_html=is_html)
    verdicts_module.require_free_text(comment_html)
    board.comment(board.find(reference), comment_html)
    click.echo(f"{reference} commented")


@card.command("verdict")
@click.argument("reference")
@click.option("--role", required=True,
              type=click.Choice(verdicts_module.ROLES))
@click.option("--result", required=True,
              type=click.Choice(verdicts_module.RESULTS))
@click.option("--revision", required=True,
              help="Git commit hash the verdict covers.")
@click.option("--author", required=True, help="Who issued the verdict.")
@click.option("--note", default="", help="Short free-text note.")
@click.option("--operation-id",
              help="Stable id; a retry finds the earlier verdict and "
                   "posts nothing.")
@click.pass_obj
def card_verdict(obj: Context, reference: str, role: str, result: str,
                 revision: str, author: str, note: str,
                 operation_id: str | None) -> None:
    """Record a qa or tech-lead verdict on a card in Verifying."""
    board = obj.board
    operation = operation_id or str(uuid.uuid4())
    fields = verdicts_module.verdict_fields(
        role=role, result=result, revision=revision, author=author,
        note=note, operation_id=operation,
    )
    item = board.find(reference)
    names = {state_id: name for name, state_id in board.project.states.items()}
    current = names.get(str(getattr(item, "state", "")), "unknown")
    if current.casefold() != "verifying":
        raise GuardViolation(
            f"Verdict rule: {reference} is {current}; a verdict is "
            "recorded only on a card in Verifying."
        )
    posted = not verdicts_module.already_recorded(
        board.comments(item), fields
    )
    if posted:
        board.comment(item, verdicts_module.verdict_html(fields))
    payload = {
        "card": f"{board.project.key}-{getattr(item, 'sequence_id', '?')}",
        "role": role, "result": result, "revision": revision,
        "author": author, "operation_id": operation, "posted": posted,
    }
    emit(payload, as_json=obj.as_json, render=lambda data: click.echo(
        f"{data['card']} {data['role']} {data['result']} on "
        f"{data['revision']} by {data['author']}  operation "
        f"{data['operation_id']}"
        + ("" if data["posted"] else "  (already recorded)")
    ))


@card.command("comments")
@click.argument("reference")
@click.pass_obj
def card_comments(obj: Context, reference: str) -> None:
    """Read a work item's comments."""
    board = obj.board
    records = [
        {"created_at": str(getattr(c, "created_at", ""))[:16],
         "author": board.config.member_name(getattr(c, "actor", None)),
         "comment": text_module.to_plain(getattr(c, "comment_html", ""))}
        for c in board.comments(board.find(reference))
    ]
    emit(records, as_json=obj.as_json, render=lambda r: records_table(r, [
        ("created_at", "WHEN"), ("author", "WHO"), ("comment", "COMMENT"),
    ]))


def _register_database(root: Context) -> Path:
    """The bound sprint register the journaled card operations use."""
    database = root.config.state_file or sprints_module.default_database_path()
    project = root.project_key or root.config.default_project
    if project is None:
        raise sprints_module.SprintError(
            "Sprint register binding requires --project or defaults.project."
        )
    host, workspace = load_connection_target(
        root.config.workspace_slug, config_path=root.config.path,
        env_file=root.env_file,
    )
    sprints_module.require_binding(database, (host, workspace, project))
    return database


@card.command("transition")
@click.argument("reference")
@click.option("--from", "from_state", required=True,
              help="Required current state.")
@click.option("--to", "to_state", required=True, help="Target state.")
@click.option("--stop-activity", is_flag=True,
              help="Stop the open activity timer before moving.")
@click.option("--operation-id",
              help="Stable id; a retry resumes incomplete steps and "
                   "never replays completed ones.")
@click.option("--reason", type=click.Choice(execution_module.REWORK_REASONS),
              help="Why a send-back (Verifying → In Progress) happened; "
                   "required for that move only.")
@click.pass_obj
def card_transition(obj: Context, reference: str, from_state: str,
                    to_state: str, stop_activity: bool,
                    operation_id: str | None, reason: str | None) -> None:
    """Move a card through one journaled, resumable transition.

    Sequence: inspect, stop activity if requested, move with readback,
    collect telemetry, receipt. A retry with the same --operation-id
    resumes incomplete steps without replaying moves or timer events; an
    unexpected source state conflicts. Not a distributed transaction:
    competing writers are detected, not prevented.
    """
    board = obj.board
    database = _register_database(obj)
    with sprints_module.connect_database(
        database, writable=True
    ) as connection:
        receipt = operations_module.run_transition(
            connection, board,
            reference=reference,
            from_state=from_state,
            to_state=to_state,
            stop_activity=stop_activity,
            operation_id=operation_id or str(uuid.uuid4()),
            reason=reason,
        )
    emit(receipt, as_json=obj.as_json,
         render=lambda data: click.echo(
             f"{data['card']} {data['from']} → {data['to']}  "
             f"collected {data['collected_at']}  "
             f"operation {data['operation_id']}"
         ))


@card.command("activity")
@click.argument("reference")
@click.argument("category")
@click.option("--operation-id",
              help="Stable id; a retry never writes duplicate events.")
@click.option("--started-at",
              help="Delayed interval start, RFC 3339 with offset.")
@click.option("--ended-at",
              help="Delayed interval end, RFC 3339 with offset.")
@click.option("--evidence-ref",
              help="Provenance for a delayed interval, e.g. a log path "
                   "or evidence attachment id.")
@click.pass_obj
def card_activity(obj: Context, reference: str, category: str,
                  operation_id: str | None, started_at: str | None,
                  ended_at: str | None,
                  evidence_ref: str | None) -> None:
    """Switch the open activity timer, or record a delayed interval.

    Without the delayed options: stops whatever timer is open and starts
    CATEGORY, exactly once — a timer already open under CATEGORY is
    success with no new event, so retries cannot duplicate boundaries.

    With --started-at, --ended-at, and --evidence-ref (all three
    required together): records one provenance-backed delayed interval.
    Future or inverted times, missing evidence, and overlap with
    anything already timed are refused; occurrence and receipt times are
    kept separately, and missing intervals stay missing.
    """
    delayed = (started_at, ended_at, evidence_ref)
    if any(value is not None for value in delayed) and None in delayed:
        raise click.ClickException(
            "A delayed interval requires --started-at, --ended-at, and "
            "--evidence-ref together."
        )
    board = obj.board
    database = _register_database(obj)
    with sprints_module.connect_database(
        database, writable=True
    ) as connection:
        if started_at is not None:
            receipt = operations_module.run_delayed_activity(
                connection, board,
                reference=reference,
                category=category,
                started_at=started_at,
                ended_at=ended_at or "",
                evidence_ref=evidence_ref or "",
                operation_id=operation_id or str(uuid.uuid4()),
            )
            emit(receipt, as_json=obj.as_json,
                 render=lambda data: click.echo(
                     f"{data['card']} delayed {data['category']} "
                     f"{data['started_at']} → {data['ended_at']}  "
                     + ("recorded" if data["recorded"]
                        else "already recorded")
                 ))
            return
        receipt = operations_module.run_activity_switch(
            connection, board,
            reference=reference,
            category=category,
            operation_id=operation_id or str(uuid.uuid4()),
        )
    emit(receipt, as_json=obj.as_json,
         render=lambda data: click.echo(
             f"{data['card']} activity {data['category']}  "
             + (", ".join(data["events"]) or "already running")
         ))


@card.group("timer")
def card_timer() -> None:
    """Measure active work on a card without Plane Pro worklogs."""


def _write_timer_event(obj: Context, reference: str, action: str, category: str | None) -> None:
    board = obj.board
    item = board.find(reference)
    resolved_action, resolved_category = execution_module.timer_event(
        board.comments(item), action=action, category=category
    )
    body = execution_module.event_text(resolved_action, resolved_category)
    created = board.comment(item, text_module.to_html(body))
    emit(
        {
            "card": reference,
            "action": resolved_action,
            "category": resolved_category,
            "recorded_at": getattr(created, "created_at", None),
        },
        as_json=obj.as_json,
        render=lambda data: click.echo(
            f"{data['card']} timer {data['action']} {data['category']}"
        ),
    )


@card_timer.command("start")
@click.argument("reference")
@click.argument("category")
@click.pass_obj
def card_timer_start(obj: Context, reference: str, category: str) -> None:
    """Start one categorized timer; CATEGORY is a lowercase slug."""
    _write_timer_event(obj, reference, "start", category)


@card_timer.command("stop")
@click.argument("reference")
@click.pass_obj
def card_timer_stop(obj: Context, reference: str) -> None:
    """Stop the card's currently open timer."""
    _write_timer_event(obj, reference, "stop", None)


@card.command("timeline")
@click.argument("reference")
@click.pass_obj
def card_timeline(obj: Context, reference: str) -> None:
    """Show state transitions and CE-compatible timer boundaries."""
    board = obj.board
    item = board.find(reference)
    records = execution_module.timeline(board.activities(item), board.comments(item))
    for record in records:
        record["actor"] = board.config.member_name(record["actor"])
    emit(records, as_json=obj.as_json, render=lambda rows: records_table(rows, [
        ("when", "WHEN"), ("kind", "KIND"), ("actor", "WHO"), ("detail", "DETAIL"),
    ]))


@card.command("stats")
@click.argument("reference")
@click.pass_obj
def card_stats(obj: Context, reference: str) -> None:
    """Show wall-clock state residence and active execution time."""
    board = obj.board
    item = board.find(reference)
    payload = execution_module.execution_stats(
        board.activities(item), board.comments(item), as_of=datetime.now(UTC)
    )

    def render(data: dict[str, Any]) -> None:
        rows = [
            {"metric": f"state:{name}", "minutes": minutes}
            for name, minutes in data["state_minutes"].items()
        ]
        rows.extend(
            {"metric": f"active:{name}", "minutes": minutes}
            for name, minutes in data["execution_minutes"].items()
        )
        current = data["current_state"]
        if current is not None:
            rows.append({
                "metric": f"current:{current['name']}",
                "minutes": current["elapsed_minutes"],
            })
        records_table(rows, [("metric", "METRIC"), ("minutes", "MINUTES")])
        click.echo(f"Rework count: {data['rework_count']}")
        click.echo(f"Rework time: {data['rework_minutes']} minutes")
        for name, count in data["rework_reasons"].items():
            click.echo(f"Rework reason {name}: {count}")
        if data["open_timer"] is not None:
            click.echo(f"open timer: {data['open_timer']['category']}")

    emit(payload, as_json=obj.as_json, render=render)


@card.command("set")
@click.argument("reference")
@click.option("--title", help="New title.")
@click.option("--description", help="Markdown; '-' reads stdin.")
@click.option("--html", "is_html", is_flag=True, help="Description is already HTML.")
@click.option("--assignee", help="New single owner.")
@click.option("--priority", help="urgent, high, medium, low or none.")
@click.pass_obj
def card_set(obj: Context, reference: str, title: str | None, description: str | None,
             is_html: bool, assignee: str | None, priority: str | None) -> None:
    """Change a work item's fields."""
    board = obj.board
    fields: dict[str, Any] = {}
    if title:
        fields["name"] = title
    body = text_module.read_text(description)
    if body is not None:
        fields["description_html"] = text_module.to_html(body, already_html=is_html)
    if assignee:
        fields["assignees"] = [board.config.member_id(assignee)]
    if priority:
        fields["priority"] = priority
    board.update_card(board.find(reference), fields)
    click.echo(f"{reference} updated: {', '.join(sorted(fields))}")


@card.command("set-cycle")
@click.argument("reference")
@click.argument("cycle_name")
@click.pass_obj
def card_set_cycle(obj: Context, reference: str, cycle_name: str) -> None:
    """Put one card in a named cycle and verify membership."""
    board = obj.board
    board.set_card_cycle(board.find(reference), _grouping_id(board, "cycle", cycle_name))
    click.echo(f"{reference} → cycle {cycle_name}")


@card.command("rm-cycle")
@click.argument("reference")
@click.argument("cycle_name")
@click.pass_obj
def card_rm_cycle(obj: Context, reference: str, cycle_name: str) -> None:
    """Remove one card from a named cycle and verify membership."""
    board = obj.board
    board.clear_card_cycle(board.find(reference), _grouping_id(board, "cycle", cycle_name))
    click.echo(f"{reference} removed from cycle {cycle_name}")


# ---- intake -------------------------------------------------------------

@cli.group()
def intake() -> None:
    """Project Intake records."""


@intake.command("new")
@click.option("--title", required=True,
              help="Short bug report or suggestion title.")
@click.option("--description", default="-", show_default="stdin",
              help="Markdown; '-' reads stdin.")
@click.option("--html", "is_html", is_flag=True,
              help="Description is already HTML.")
@click.pass_obj
def intake_new(
    obj: Context, title: str, description: str, is_html: bool
) -> None:
    """File a pending Intake item without placing a card in a cycle."""
    body = text_module.read_text(description) or ""
    item = obj.board.create_intake(
        title, text_module.to_html(body, already_html=is_html)
    )
    detail = item.issue_detail
    emit(
        _intake_payload(obj.board, item, detail),
        as_json=obj.as_json,
        render=lambda data: click.echo(
            f"{data['card'] or data['intake_record_id']} pending"
        ),
    )


@intake.command("list")
@click.option("--all", "show_all", is_flag=True, help="Include non-pending Intake records.")
@click.pass_obj
def intake_list(obj: Context, show_all: bool) -> None:
    """List pending Intake records."""
    board = obj.board
    records = []
    for item in board.intake_items():
        if not show_all and getattr(item, "status", None) != -2:
            continue
        detail = getattr(item, "issue_detail", None)
        records.append({
            "intake_record_id": getattr(item, "id", None),
            "work_item_id": getattr(item, "issue", None),
            "card": _intake_card_reference(board, detail),
            "status": _intake_status(getattr(item, "status", None)),
            "source": getattr(item, "source", None),
            "title": text_module.to_plain(
                getattr(detail, "name", None) or getattr(item, "name", ""), limit=60
            ),
        })
    emit(records, as_json=obj.as_json, render=lambda r: records_table(r, [
        ("intake_record_id", "INTAKE"), ("card", "CARD"), ("status", "STATUS"),
        ("source", "SOURCE"), ("title", "TITLE"),
    ]))


@intake.command("show")
@click.argument("reference")
@click.pass_obj
def intake_show(obj: Context, reference: str) -> None:
    """Show one Intake record by Intake id, work-item id, or card reference."""
    board = obj.board
    item = board.find_intake(reference)
    detail = board.intake_work_item(item)
    payload = _intake_payload(board, item, detail)
    attachments = board.intake_attachments(item)
    payload["attachments"] = attachments
    emit(payload, as_json=obj.as_json, render=_render_intake)


@card.command("attachment")
@click.argument("reference")
@click.argument("attachment_id")
@click.option("--out", "destination", required=True,
              type=click.Path(dir_okay=False, path_type=Path), help="New local output file.")
@click.pass_obj
def card_attachment(obj: Context, reference: str, attachment_id: str, destination: Path) -> None:
    """Download an attachment using the IDs shown by card show."""
    if destination.exists() or destination.is_symlink():
        raise click.ClickException(f"{destination} already exists; choose a new output path.")
    board = obj.board
    item = board.find(reference)
    downloaded = board.download_attachment(item.id, attachment_id, destination)
    emit(downloaded, as_json=obj.as_json,
         render=lambda data: click.echo(f"{data['path']}  {data['size']} bytes"))


@card.group("evidence")
def card_evidence() -> None:
    """Byte-verified evidence archives on a card."""


@card_evidence.command("add")
@click.argument("reference")
@click.argument("file", type=click.Path(exists=True, dir_okay=False,
                                        path_type=Path))
@click.option("--kind", required=True,
              help="Evidence kind slug, e.g. performance.")
@click.option("--revision",
              help="Source revision the evidence was captured at.")
@click.pass_obj
def card_evidence_add(obj: Context, reference: str, file: Path,
                      kind: str, revision: str | None) -> None:
    """Archive FILE on a card and return a byte-verified receipt.

    The exact bytes are uploaded, read back from storage, and compared by
    SHA-256 and size before success is reported. A matching archive
    already on the card is reused, which makes a retry after a lost
    response safe. The local file is never deleted.
    """
    board = obj.board
    receipt = board.upload_evidence(
        board.find(reference), file, kind=kind, revision=revision,
    )
    receipt["card"] = reference

    def render(data: dict[str, Any]) -> None:
        verb = "reused" if data["reused"] else "archived"
        click.echo(
            f"{data['card']} {verb} {data['name']}  {data['size']} bytes  "
            f"sha256 {data['sha256']}  attachment {data['attachment_id']}"
        )

    emit(receipt, as_json=obj.as_json, render=render)


@card_evidence.command("verify")
@click.argument("reference")
@click.argument("attachment_id")
@click.option("--digest", "expected_digest",
              help="Expected SHA-256; verification fails on a mismatch.")
@click.pass_obj
def card_evidence_verify(obj: Context, reference: str, attachment_id: str,
                         expected_digest: str | None) -> None:
    """Re-verify an archived attachment's bytes against storage."""
    board = obj.board
    result = board.verify_evidence(
        board.find(reference), attachment_id,
        expected_digest=expected_digest,
    )
    result["card"] = reference
    emit(result, as_json=obj.as_json,
         render=lambda data: click.echo(
             f"{data['card']} {data['name']}  {data['size']} bytes  "
             f"sha256 {data['sha256']}  verified"
         ))


@intake.command("attachment")
@click.argument("reference")
@click.argument("attachment_id")
@click.option("--out", "destination", required=True,
              type=click.Path(dir_okay=False, path_type=Path), help="New local output file.")
@click.pass_obj
def intake_attachment(obj: Context, reference: str, attachment_id: str, destination: Path) -> None:
    """Download an attachment using the IDs shown by intake show."""
    if destination.exists() or destination.is_symlink():
        raise click.ClickException(f"{destination} already exists; choose a new output path.")
    board = obj.board
    item = board.find_intake(reference)
    downloaded = board.download_attachment(getattr(item, "issue", None), attachment_id, destination)
    emit(downloaded, as_json=obj.as_json,
         render=lambda data: click.echo(f"{data['path']}  {data['size']} bytes"))


@intake.command("accept")
@click.argument("reference")
@click.pass_obj
def intake_accept(obj: Context, reference: str) -> None:
    """Accept one pending Intake item into the project."""
    board = obj.board
    accepted = board.accept_intake(board.find_intake(reference))
    detail = board.intake_work_item(accepted)
    emit(
        _intake_payload(board, accepted, detail),
        as_json=obj.as_json,
        render=lambda data: click.echo(f"{data['card'] or reference} accepted"),
    )


@intake.command("reject")
@click.argument("reference")
@click.pass_obj
def intake_reject(obj: Context, reference: str) -> None:
    """Reject one pending Intake item."""
    board = obj.board
    rejected = board.reject_intake(board.find_intake(reference))
    detail = board.intake_work_item(rejected)
    emit(
        _intake_payload(board, rejected, detail),
        as_json=obj.as_json,
        render=lambda data: click.echo(f"{data['card'] or reference} rejected"),
    )


# ---- relations ----------------------------------------------------------

@cli.group("rel")
def relation_group() -> None:
    """Dependencies between work items."""


@relation_group.command("add")
@click.argument("reference")
@click.argument("relation_type")
@click.argument("others", nargs=-1, required=True)
@click.pass_obj
def relation_add(obj: Context, reference: str, relation_type: str, others: tuple[str, ...]) -> None:
    """Relate a work item to others, e.g. `rel add DEMO-3 blocked_by DEMO-2`."""
    board = obj.board
    board.add_relation(board.find(reference), relation_type,
                       [board.find(other) for other in others])
    click.echo(f"{reference} {relation_type} {', '.join(others)}")


@relation_group.command("remove")
@click.argument("reference")
@click.argument("relation_type")
@click.argument("others", nargs=-1, required=True)
@click.pass_obj
def relation_remove(
    obj: Context, reference: str, relation_type: str, others: tuple[str, ...]
) -> None:
    """Remove a relation edge, e.g. `rel remove DEMO-3 blocked_by DEMO-2`.

    Removes only the named edges and verifies by readback that every other
    edge survived. An already-absent edge is idempotent success.
    """
    board = obj.board
    result = board.remove_relation(
        board.find(reference),
        relation_type,
        [board.find(other) for other in others],
    )
    index = board.card_index()
    payload = {
        "card": reference,
        "relation": relation_type,
        "removed": [
            _reference(board, index, card_id)
            for card_id in result["removed"]
        ],
        "already_absent": [
            _reference(board, index, card_id)
            for card_id in result["already_absent"]
        ],
    }

    def render(data: dict[str, Any]) -> None:
        if data["removed"]:
            click.echo(
                f"{data['card']} {data['relation']} removed: "
                + ", ".join(data["removed"])
            )
        if data["already_absent"]:
            click.echo(
                "already absent: " + ", ".join(data["already_absent"])
            )

    emit(payload, as_json=obj.as_json, render=render)


@relation_group.command("list")
@click.argument("reference")
@click.pass_obj
def relation_list(obj: Context, reference: str) -> None:
    """A work item's relations."""
    board = obj.board
    found = board.relations(board.find(reference).id)
    index = board.card_index()
    records = [
        {"relation": name, "work_item_id": card_id,
         "card": _reference(board, index, card_id),
         "title": text_module.to_plain(getattr(index.get(card_id), "name", ""), limit=60)}
        for name, ids in found.items() for card_id in ids
    ]
    emit(records, as_json=obj.as_json, render=lambda r: records_table(r, [
        ("relation", "RELATION"), ("card", "CARD"), ("title", "TITLE"),
    ]))


# ---- cycles and modules -------------------------------------------------
#
# One implementation, two groups. A cycle and a module differ in what they
# mean — a cycle is when, a module is where — but their lifecycles are the
# same shape, and writing it twice is how the two drift apart.


def _grouping_id(
    board: Any, kind: str, name: str, *, archived: bool = False,
) -> str:
    """Resolve a cycle or module name, refreshing from the project before failing.

    Retry the live lookup if the name was created after connection startup.
    """
    recorded = board.project.cycles if kind == "cycle" else board.project.modules
    if not archived and name in recorded:
        return recorded[name]
    live = (
        board.refresh_grouping(kind, archived=True)
        if archived else board.refresh_grouping(kind)
    )
    if name in live:
        return live[name]
    known_names = set(live) if archived else set(recorded) | set(live)
    known = ", ".join(sorted(known_names)) or "none"
    raise click.ClickException(f"No {kind} named {name!r} on {board.project.key}. Known: {known}.")


def _refuse_sprint_cycle(name: str) -> None:
    """Refuse a hand-made `Sprint N` cycle before any request."""
    sprint_id = board_module.sprint_cycle_number(name)
    if sprint_id is not None:
        raise SprintCycleByHand(
            f"Sprint cycle rule: {name!r} is a sprint cycle, and sprint "
            "cycles are created with their plan. Use `plane-proj sprints "
            f"plan --id {sprint_id}`."
        )


def _refuse_sprint_rename(old: str, new_name: str) -> None:
    """Refuse a rename that moves a cycle into or out of a sprint's name."""
    was = board_module.sprint_cycle_number(old)
    sprint_id = board_module.sprint_cycle_number(new_name)
    if was is not None and sprint_id != was:
        raise SprintCycleByHand(
            f"Sprint cycle rule: {old!r} is sprint {was}'s cycle, and "
            f"{new_name!r} is not named for sprint {was}, so sprint {was} "
            f"would lose its cycle. Keep the `Sprint {was}` prefix, e.g. "
            f"'Sprint {was} — Title'."
        )
    if sprint_id is not None and was != sprint_id:
        raise SprintCycleByHand(
            f"Sprint cycle rule: {new_name!r} is a sprint cycle name, and "
            f"{old!r} is not sprint {sprint_id}'s cycle. Sprint cycles are "
            f"created with their plan: use `plane-proj sprints plan --id "
            f"{sprint_id}`."
        )


def _refuse_sprint_description(name: str) -> None:
    """Refuse a hand-written description on a sprint's cycle."""
    sprint_id = board_module.sprint_cycle_number(name)
    if sprint_id is not None:
        raise SprintCycleByHand(
            f"Sprint cycle rule: {name!r} describes sprint {sprint_id}'s "
            "plan, and the plan owns that description. Change it with "
            f"`plane-proj sprints plan --id {sprint_id}`."
        )


def _grouping_commands(group: click.Group, kind: str) -> None:
    """Attach the full lifecycle for `kind` to `group`."""
    plural = f"{kind}s"

    @group.command("list", help=f"Every {kind} on the project, read live.")
    @click.option("--archived", is_flag=True, help="List archived ones instead.")
    @click.pass_obj
    def list_(obj: Context, archived: bool) -> None:
        board = obj.board
        id_field = f"{kind}_id"
        records = [
            {"name": getattr(g, "name", ""),
             "start_date": getattr(g, "start_date", None),
             "end_date": getattr(g, "end_date", None) or getattr(g, "target_date", None),
             "card_count": getattr(g, "total_issues", None),
             id_field: g.id}
            for g in board.groupings(kind, archived=archived)
        ]
        emit(records, as_json=obj.as_json, render=lambda r: records_table(r, [
            ("name", "NAME"), ("start_date", "START"), ("end_date", "END"),
            ("card_count", "CARDS"), (id_field, "UUID"),
        ]))

    @group.command(
        "new", help=f"Create a {kind}.", hidden=kind == "cycle",
    )
    @click.argument("name")
    @click.option("--start", help="Start date, YYYY-MM-DD.")
    @click.option("--end", help="End date, YYYY-MM-DD (cycles) or target date (modules).")
    @click.option("--description", help="Markdown; '-' reads stdin.")
    @click.option("--lead", help="Module lead. Modules only.")
    @click.pass_obj
    def new(obj: Context, name: str, start: str | None, end: str | None,
            description: str | None, lead: str | None) -> None:
        if kind == "cycle" and lead:
            raise click.UsageError("--lead is for modules; a cycle has an owner, not a lead.")
        if kind == "cycle":
            _refuse_sprint_cycle(name)
        board = obj.board
        fields: dict[str, Any] = {
            "start_date": start, "description": text_module.read_text(description)
        }
        if kind == "cycle":
            fields["end_date"] = end
            click.echo(
                "plane-proj: `cycle new` is deprecated; sprints are created "
                "with `plane-proj sprints plan`.",
                err=True,
            )
            created = board.create_cycle(name, fields)
        else:
            fields["target_date"] = end
            fields["lead"] = board.config.member_id(lead) if lead else None
            created = board.create_module(name, fields)
        board.verify_grouping(kind, name=name, target_id=str(getattr(created, "id", "")))
        click.echo(f"{kind} {getattr(created, 'name', name)!r} created")

    @group.command("rename", help=f"Rename a {kind}.")
    @click.argument("old")
    @click.argument("new_name")
    @click.pass_obj
    def rename(obj: Context, old: str, new_name: str) -> None:
        if kind == "cycle":
            _refuse_sprint_rename(old, new_name)
        board = obj.board
        target = _grouping_id(board, kind, old)
        setter = board.update_cycle if kind == "cycle" else board.update_module
        setter(target, {"name": new_name})
        board.verify_grouping(kind, name=new_name, target_id=target)
        click.echo(f"{kind} {old!r} → {new_name!r}")

    @group.command("set", help=f"Change a {kind}'s dates, description or lead.")
    @click.argument("name")
    @click.option("--start", help="Start date, YYYY-MM-DD.")
    @click.option("--end", help="End or target date, YYYY-MM-DD.")
    @click.option("--description", help="Markdown; '-' reads stdin.")
    @click.option("--lead", help="Module lead. Modules only.")
    @click.option("--status", help="Module status. Modules only.")
    @click.pass_obj
    def set_(obj: Context, name: str, start: str | None, end: str | None,
             description: str | None, lead: str | None, status: str | None) -> None:
        if kind == "cycle" and (lead or status):
            raise click.UsageError("--lead and --status are for modules.")
        if kind == "cycle" and description is not None:
            _refuse_sprint_description(name)
        board = obj.board
        fields: dict[str, Any] = {
            "start_date": start, "description": text_module.read_text(description)
        }
        if kind == "cycle":
            fields["end_date"] = end
            board.update_cycle(_grouping_id(board, kind, name), fields)
        else:
            fields["target_date"] = end
            fields["status"] = status
            fields["lead"] = board.config.member_id(lead) if lead else None
            board.update_module(_grouping_id(board, kind, name), fields)
        changed = ", ".join(sorted(k for k, v in fields.items() if v is not None)) or "nothing"
        click.echo(f"{kind} {name!r} updated: {changed}")

    @group.command("cards", help=f"The work items in a {kind}.")
    @click.argument("name")
    @click.pass_obj
    def cards(obj: Context, name: str) -> None:
        board = obj.board
        target = _grouping_id(board, kind, name)
        members = (board.cycle_card_ids(target) if kind == "cycle"
                   else board.module_card_ids(target))
        records = [
            {"card": f"{board.project.key}-{getattr(c, 'sequence_id', '?')}",
             "work_item_id": c.id,
             "state": _state_name(board.project, getattr(c, "state", None)),
             "title": text_module.to_plain(getattr(c, "name", ""), limit=60)}
            for c in board.cards() if str(c.id) in members
        ]
        emit(records, as_json=obj.as_json, render=lambda r: records_table(r, [
            ("card", "CARD"), ("state", "STATE"), ("title", "TITLE"),
        ]))

    def add(obj: Context, name: str, references: tuple[str, ...]) -> None:
        board = obj.board
        target = _grouping_id(board, kind, name)
        cards = [board.find(reference) for reference in references]
        if kind == "cycle":
            for card in cards:
                board.check_cycle_entry(card)
        for card in cards:
            if kind == "cycle":
                board.add_to_cycle(card.id, target)
            else:
                board.add_to_module(card.id, target)
        click.echo(f"{', '.join(references)} → {kind} {name}")

    def remove(obj: Context, name: str, references: tuple[str, ...]) -> None:
        board = obj.board
        target = _grouping_id(board, kind, name)
        cards = [board.find(reference) for reference in references]
        if kind == "cycle":
            for card in cards:
                board.check_cycle_exit(card)
        for card in cards:
            if kind == "cycle":
                board.remove_from_cycle(card.id, target)
            else:
                board.remove_from_module(card.id, target)
        click.echo(f"{', '.join(references)} removed from {kind} {name}")

    if kind == "module":
        @group.command("archive", help="Archive a finished module, or restore one.")
        @click.argument("name")
        @click.option("--undo", is_flag=True, help="Unarchive instead.")
        @click.pass_obj
        def archive(obj: Context, name: str, undo: bool) -> None:
            board = obj.board
            board.archive_module(
                _grouping_id(board, kind, name, archived=undo),
                archived=not undo,
            )
            click.echo(f"module {name!r} {'unarchived' if undo else 'archived'}")

        @group.command("delete", help="Delete a module. Prefer archive.")
        @click.argument("name")
        @click.option("--force", is_flag=True, help="Required. Deleting is not reversible.")
        @click.pass_obj
        def delete(obj: Context, name: str, force: bool) -> None:
            if not force:
                raise click.UsageError(
                    "Deleting a module is not reversible and loses what it recorded. "
                    "Use `archive` to hide it, or pass --force."
                )
            board = obj.board
            target = _grouping_id(board, kind, name)
            board.delete_module(target)
            board.verify_grouping(kind, name=name, target_id=target, deleted=True)
            click.echo(f"module {name!r} deleted")

        group.command("add", help="Put existing work items in a module.")(
            click.argument("references", nargs=-1, required=True)(
                click.argument("name")(click.pass_obj(add))
            )
        )
        group.command("rm", help="Take work items out of a module.")(
            click.argument("references", nargs=-1, required=True)(
                click.argument("name")(click.pass_obj(remove))
            )
        )

    if kind == "cycle":
        @group.command("restore")
        @click.argument("name")
        @click.pass_obj
        def restore(obj: Context, name: str) -> None:
            """Restore an archived cycle; cycles cannot be archived here."""
            board = obj.board
            board.restore_cycle(_grouping_id(board, kind, name, archived=True))
            click.echo(f"cycle {name!r} restored")

        @group.command("transfer")
        @click.argument("source")
        @click.argument("destination")
        @click.pass_obj
        def transfer(obj: Context, source: str, destination: str) -> None:
            """Move a cycle's unfinished work items to another cycle.

            Completed work items stay where they were completed, so the finished
            cycle keeps an honest record of what it delivered. This is Plane's
            own rule, not a choice made here.
            """
            board = obj.board
            board.transfer_cycle_work_items(
                _grouping_id(board, kind, source), _grouping_id(board, kind, destination)
            )
            click.echo(f"unfinished work items moved from {source!r} to {destination!r}")

    _ = (list_, new, rename, set_, cards, add, remove, plural)


@cli.group("cycle")
def cycle_group() -> None:
    """Cycles: when work is committed to happen."""


@cli.group("module")
def module_group() -> None:
    """Modules: which part of the work something belongs to."""


_grouping_commands(cycle_group, "cycle")
_grouping_commands(module_group, "module")


# ---- project ------------------------------------------------------------

@cli.group("project")
def project_group() -> None:
    """Project metadata, estimate scale, and rules checks."""


@project_group.command("states")
@click.pass_obj
def project_states(obj: Context) -> None:
    """States, read live from Plane."""
    records = [{"state": name, "state_id": value}
               for name, value in sorted(obj.board.project.states.items())]
    emit(records, as_json=obj.as_json, render=lambda r: records_table(r, [
        ("state", "STATE"), ("state_id", "UUID"),
    ]))


@project_group.command("modules")
@click.pass_obj
def project_modules(obj: Context) -> None:
    """Modules, read live from Plane."""
    records = [{"module": name, "module_id": value}
               for name, value in sorted(obj.board.project.modules.items())]
    emit(records, as_json=obj.as_json, render=lambda r: records_table(r, [
        ("module", "MODULE"), ("module_id", "UUID"),
    ]))


@project_group.command("cycles")
@click.pass_obj
def project_cycles(obj: Context) -> None:
    """Cycles, read live from Plane."""
    records = [{"cycle": name, "cycle_id": value}
               for name, value in sorted(obj.board.project.cycles.items())]
    emit(records, as_json=obj.as_json, render=lambda r: records_table(r, [
        ("cycle", "CYCLE"), ("cycle_id", "UUID"),
    ]))


@project_group.command("members")
@click.pass_obj
def project_members(obj: Context) -> None:
    """Members, and which of them take no estimate."""
    config = obj.board.config
    records = [
        {"name": name, "member_id": member_id,
         "sizing": "no estimate" if member_id in config.unestimated_assignees else ""}
        for member_id, name in sorted(config.members.items(), key=lambda kv: kv[1])
    ]
    emit(records, as_json=obj.as_json, render=lambda r: records_table(r, [
        ("name", "NAME"), ("member_id", "UUID"), ("sizing", "SIZING"),
    ]))


@project_group.command("capture")
@click.option("--write", is_flag=True, help="Save only the recovered estimate mapping.")
@click.pass_obj
def project_capture(obj: Context, write: bool) -> None:
    """Inspect live metadata; --write saves only estimate_points."""
    board = obj.board
    facts = board.capture_facts()
    if not write:
        emit(facts, as_json=True)
        click.echo("(not written — pass --write)", err=True)
        return

    _write_facts(board, facts)
    click.echo(f"{board.config.path} updated for {board.project.key}")
    if not facts["estimate_points"]:
        click.echo(
            "warning: no estimate scale recovered. Only values that some work item carries can be "
            "read back; set each value on one work item once, then capture again.", err=True,
        )


@project_group.command("scale")
@click.option("--write", is_flag=True, help="Write the recovered scale into the config file.")
@click.option("--replace", is_flag=True,
              help="Discard values the project cannot confirm. Destructive; needs --write.")
@click.pass_obj
def project_scale(obj: Context, write: bool, replace: bool) -> None:
    """Recover the estimate scale from the project and print it ready to paste.

    Self-hosted Plane exposes no estimates endpoint, so a scale point's value
    is readable only from a work item that carries it. This reads every work item with
    the point expanded, and reports which work item proved each value so the map
    can be checked rather than trusted.
    """
    if replace and not write:
        raise click.UsageError("--replace only means something with --write.")

    board = obj.board
    evidence = board.scale_evidence()
    fragment = {str(entry["value"]): entry["uuid"] for entry in evidence}

    if obj.as_json:
        emit({"estimate_points": fragment, "evidence": evidence}, as_json=True)
    else:
        table([[str(e["value"]), e["uuid"], e["seen_on"],
                text_module.to_plain(e["card_name"], limit=40)] for e in evidence],
              ["VALUE", "UUID", "SEEN ON", "CARD"])

    if not evidence:
        click.echo(
            "\nNo estimate points found on any work item. Only values in use can be read back.\n"
            "To publish the scale: create one work item per value, set each work item's estimate "
            "in the Plane UI,\nthen run this again.", err=True,
        )
        raise SystemExit(1)

    if write:
        if replace:
            dropped = sorted(
                {str(v) for v in board.project.estimate_points} - fragment.keys(),
                key=int,
            )
            if dropped:
                click.echo(
                    f"\nreplacing: dropping {len(dropped)} recorded value(s) not in use — "
                    f"{', '.join(dropped)}.\nTheir UUIDs are not recorded anywhere else and "
                    f"cannot be recovered.", err=True,
                )
        _write_facts(board, {"estimate_points": fragment}, replace_scale=replace)
        click.echo(f"\n{board.config.path} updated: {len(fragment)} scale point(s) confirmed.")
        return

    missing_note(board, fragment)
    if not obj.as_json:
        click.echo(f"\nPaste at the top level of {board.config.path},")
        click.echo("or re-run with --write to have it done for you:\n")
        click.echo(_indent(json.dumps({"estimate_points": fragment}, indent=2)[1:-1].strip()))


def missing_note(board: Any, fragment: dict[str, str]) -> None:
    """Say which values the config file expects and the project did not show."""
    expected = {str(value) for value in board.project.estimate_points}
    missing = sorted(expected - fragment.keys(), key=int)
    if missing:
        click.echo(
            f"\nwarning: the config file has {', '.join(missing)} and no work item carries "
            f"{'them' if len(missing) > 1 else 'it'}; those entries were kept, not confirmed.",
            err=True,
        )


def _indent(text: str) -> str:
    return "\n".join("  " + line for line in text.splitlines())


def _write_facts(board: Any, facts: dict[str, Any], *, replace_scale: bool = False) -> None:
    """Persist only the selected project's estimate map."""
    document = json.loads(board.config.path.read_text(encoding="utf-8"))
    if document.get("defaults", {}).get("project") != board.project.key:
        raise click.ClickException(
            "The selected project differs from defaults.project. "
            "Run init --project KEY --out PATH for a separate config before saving its scale."
        )
    entry = document
    facts = {"estimate_points": facts["estimate_points"]}

    if not replace_scale:
        merged, kept, changed = merge_scale(entry.get("estimate_points") or {},
                                            facts["estimate_points"])
        facts["estimate_points"] = merged
        if kept:
            click.echo(
                f"kept {len(kept)} scale point(s) no work item carries today: {', '.join(kept)}. "
                f"A capture only sees values in use, so these were preserved, not confirmed.",
                err=True,
            )
        if changed:
            click.echo(
                f"warning: the project disagrees with the recorded UUID for {', '.join(changed)}. "
                f"The project's value was taken. If the estimate set was rebuilt this is expected; "
                f"otherwise check before trusting it.", err=True,
            )

    entry.update(facts)
    board.config.path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


@project_group.command("rules-check")
@click.pass_obj
def project_rules_check(obj: Context) -> None:
    """Audit the project against this project's rules.

    Reports rather than fixes. Every finding here is something a person has to
    decide about — a work item is unestimated because nobody sized it, and choosing
    a number for them is exactly the miss the estimate guards exist to stop.
    """
    board = obj.board
    project, config = board.project, board.config
    in_cycle: set[str] = set()
    for cycle_id in project.cycles.values():
        in_cycle |= board.cycle_card_ids(cycle_id)
    in_module: set[str] = set()
    if project.rules.require_module:
        for module_id in project.modules.values():
            in_module |= board.module_card_ids(module_id)

    findings: list[dict[str, str]] = []
    wip: dict[str, int] = {}
    for item in board.cards():
        reference = f"{project.key}-{getattr(item, 'sequence_id', '?')}"
        state = _state_name(project, getattr(item, "state", None))
        estimate = project.estimate_value(getattr(item, "estimate_point", None))
        assignees = getattr(item, "assignees", None) or []
        owner = _first(assignees)
        settled = state in project.states_outside_cycles

        if state in project.rules.wip_states:
            for assignee in assignees:
                wip[assignee] = wip.get(assignee, 0) + 1
        if not assignees:
            findings.append({
                "card": reference,
                "finding": "no owner",
                "detail": "an issue without an owner is not work in flight",
            })
        has_estimate = getattr(item, "estimate_point", None) is not None
        if project.estimates_enabled and has_estimate and estimate is None:
            findings.append({"card": reference, "finding": "unknown estimate",
                             "detail": "refresh the scale with project scale --write"})
        if (
            project.estimates_enabled
            and owner
            and config.takes_no_estimate(owner)
            and has_estimate
        ):
            findings.append({"card": reference, "finding": "sized, owner takes none",
                             "detail": f"estimate {estimate}"})
        elif project.rules.require_estimate and not has_estimate and not settled \
                and not (owner and config.takes_no_estimate(owner)):
            findings.append({"card": reference, "finding": "unestimated",
                             "detail": "counts in no total or velocity"})
        if project.rules.require_cycle and not settled and str(item.id) not in in_cycle:
            findings.append({"card": reference, "finding": "outside every cycle",
                             "detail": f"state {state}"})
        if project.rules.require_module and not settled and str(item.id) not in in_module:
            findings.append({"card": reference, "finding": "outside every module",
                             "detail": f"state {state}"})
        ceiling = (
            project.rules.cycle_estimate_max
            if project.estimates_enabled
            else None
        )
        if ceiling is not None and estimate is not None and estimate > ceiling \
                and state.casefold() not in IDLE_STATES:
            findings.append({"card": reference, "finding": "too large to admit",
                             "detail": f"estimate {estimate} > {ceiling} in {state}"})

    limit = project.rules.wip_limit
    if limit is not None:
        for assignee, count in sorted(wip.items()):
            if count > limit:
                findings.append({"card": "—", "finding": "WIP over limit",
                                 "detail": f"{config.member_name(assignee)}: "
                                           f"{count} in progress, limit {limit}"})

    emit(findings, as_json=obj.as_json, render=lambda r: records_table(r, [
        ("card", "CARD"), ("finding", "FINDING"), ("detail", "DETAIL"),
    ]))
    if findings and not obj.as_json:
        click.echo(f"\n{len(findings)} finding(s)", err=True)
    if findings:
        raise SystemExit(1)


@cli.command("init")
@click.option("--project", "project_key", required=True, help="Project key, e.g. DEMO.")
@click.option("--slug", help="Workspace slug. Defaults to PLANE_WORKSPACE_SLUG.")
@click.option("--out", type=click.Path(dir_okay=False, path_type=Path),
              default=str(DEFAULT_CONFIG),
              show_default=True, help="Where to write the config file.")
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite the config; preserve a matching sprint register.",
)
@click.pass_obj
def init(obj: Context, project_key: str, slug: str | None, out: Path, force: bool) -> None:
    """Create credentials, project configuration, and the sprint register."""
    if out.exists() and not force:
        raise click.ClickException(f"{out} exists. Pass --force to overwrite it.")
    env_path = Path(".env_plane")
    database_path = out.parent / "SPRINTS.sqlite"
    if env_path.exists() and not env_path.is_file():
        raise click.ClickException(f"{env_path} exists and is not a file.")
    env_created = False
    database_created = False
    previous_config = out.read_bytes() if out.exists() else None
    config_write_started = False
    try:
        if env_path.exists():
            credentials = load_credentials(slug, config_path=out, env_file=obj.env_file)
        else:
            credentials = create_credentials_file(env_path, slug, env_file=obj.env_file)
            env_created = True
        binding = (credentials.host, credentials.workspace_slug, project_key)
        if database_path.exists():
            sprints_module.require_binding(database_path, binding)

        client, projects, _ = board_module.discover(credentials)
        if project_key not in projects:
            raise click.ClickException(
                f"No project {project_key!r}. Known: {', '.join(sorted(projects))}."
            )
        board = board_module.bootstrap_board(client, credentials, projects[project_key])
        points = (
            {str(value): uuid for value, uuid in board._capture_scale().items()}
            if board.project.estimates_enabled
            else {}
        )
        document = bootstrap_document(
            slug=credentials.workspace_slug,
            project_key=project_key,
            estimate_points=points,
            rules=board.project.rules,
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        if not database_path.exists():
            sprints_module.create_database(database_path, binding)
            database_created = True
        config_write_started = True
        out.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    except Exception:
        if config_write_started:
            if previous_config is None and out.exists():
                out.unlink()
            elif previous_config is not None:
                out.write_bytes(previous_config)
        if database_created and database_path.exists():
            database_path.unlink()
        if env_created and env_path.exists():
            env_path.unlink()
        raise
    estimate_status = (
        f"{len(points)} estimate point(s)"
        if board.project.estimates_enabled
        else "estimates disabled"
    )
    click.echo(f"Initialized {project_key}: {estimate_status}.")
    click.echo(f"  {env_path} (secret; do not commit)")
    click.echo(f"  {out} (commit)")
    click.echo(f"  {database_path} (commit)")
    # The project is initialized; git wiring is optional and retryable.
    try:
        changes = register_module.setup_git(
            database_path.parent, database_path
        )
    except (register_module.RegisterSetupError, OSError) as error:
        click.echo(
            f"Skipped register git setup: {error}; run plane-proj "
            "register git-setup once that is fixed."
        )
    else:
        _report_git_setup(changes)
    if board.project.estimates_enabled:
        click.echo(
            "To fill the scale, create work items named Estimate 1, "
            "Estimate 2, etc., set their estimates in Plane, then run "
            "plane-proj project scale --write."
        )


@cli.command("projects")
@click.option("--all", "show_all", is_flag=True, help="Include archived projects.")
@click.pass_obj
def projects(obj: Context, show_all: bool) -> None:
    """List active projects; --all includes archived projects. No config required."""
    path = Path(obj.config_path or DEFAULT_CONFIG)
    config = load_config(path) if path.is_file() or obj.config_path is not None else None
    credentials = load_credentials(
        config.workspace_slug if config is not None else None,
        config_path=config.path if config is not None else None, env_file=obj.env_file,
    )
    _, live, _ = board_module.discover(credentials)
    records = [{"project_key": key, "name": p.name, "project_id": p.id,
                "status": "Archived" if getattr(p, "archived_at", None) else "Active"}
               for key, p in sorted(live.items())
               if show_all or not getattr(p, "archived_at", None)]
    emit(records, as_json=obj.as_json, render=lambda r: records_table(r, [
        ("project_key", "KEY"), ("name", "NAME"), ("status", "STATUS"), ("project_id", "ID"),
    ]))


# ---- helpers ------------------------------------------------------------

def _parse_estimate(value: str | None) -> int | None:
    """An estimate argument: a number, `blank`, or absent.

    `blank` is a word rather than an empty string because an empty string is
    what a shell produces when a variable is unset, and "the variable was
    empty" must not silently mean "this work item is deliberately unsized".
    """
    if value is None or value == "":
        return None
    if value.lower() == BLANK_ESTIMATE:
        return None
    if not value.lstrip("-").isdigit():
        raise click.BadParameter(f"{value!r} is not a number or {BLANK_ESTIMATE!r}.")
    return int(value)


def _intake_card_reference(board: Any, detail: Any) -> str | None:
    """The optional work-item reference carried by an Intake record."""
    sequence = getattr(detail, "sequence_id", None)
    return f"{board.project.key}-{sequence}" if sequence is not None else None


def _intake_status(status: Any) -> str:
    """Plane's integer Intake status as a stable human-readable name."""
    return board_module.INTAKE_STATUS_NAMES.get(
        status, str(status) if status is not None else "—"
    )


def _intake_payload(board: Any, item: Any, detail: Any) -> dict[str, Any]:
    """One Intake record together with the work item detail needed for review."""
    state = getattr(detail, "state", None)
    state_name = getattr(state, "name", None) or _state_name(
        board.project, getattr(state, "id", state)
    )
    estimate = getattr(detail, "estimate_point", None)
    estimate_id = getattr(estimate, "id", estimate)
    assignees = [
        getattr(member, "display_name", None)
        or getattr(member, "name", None)
        or board.config.member_name(str(getattr(member, "id", member)))
        for member in (getattr(detail, "assignees", None) or [])
    ]
    labels = [
        getattr(label, "name", None) or str(getattr(label, "id", label))
        for label in (getattr(detail, "labels", None) or [])
    ]
    return {
        "intake_record_id": getattr(item, "id", None),
        "status": _intake_status(getattr(item, "status", None)),
        "source": getattr(item, "source", None),
        "created_at": getattr(item, "created_at", None),
        "snoozed_till": getattr(item, "snoozed_till", None),
        "work_item_id": getattr(item, "issue", None),
        "card": _intake_card_reference(board, detail),
        "title": getattr(detail, "name", None),
        "state": state_name,
        "priority": getattr(detail, "priority", None),
        "estimate": board.project.estimate_value(estimate_id),
        "assignees": assignees,
        "labels": labels,
        "start_date": getattr(detail, "start_date", None),
        "target_date": getattr(detail, "target_date", None),
        "description_html": getattr(detail, "description_html", ""),
    }


def _render_intake(data: dict[str, Any]) -> None:
    """Render Intake and card detail for a person."""
    click.echo(click.style(
        data["title"] or data["intake_record_id"] or "Intake item", bold=True
    ))
    for label, value in data.items():
        if label in {"title", "description_html", "attachments"} or value is None:
            continue
        shown = ", ".join(value) if isinstance(value, list) else value
        click.echo(f"{label:<18}{shown}")
    if data["description_html"]:
        click.echo()
        click.echo("description_html:")
        click.echo(data["description_html"])
    if "attachments" in data:
        _render_attachments(data["attachments"])


def _render_attachments(attachments: list[dict[str, Any]]) -> None:
    """Render the same named attachment fields on cards and Intake items."""
    click.echo()
    click.echo("attachments:" if attachments else "attachments: []")
    for attachment in attachments:
        click.echo(f"  - attachment_id: {attachment['attachment_id']}")
        click.echo(f"    name: {attachment['name']}")
        click.echo(f"    content_type: {attachment['content_type'] or 'unknown'}")
        size = attachment["size"]
        click.echo(f"    size: {str(size) + ' bytes' if size is not None else 'unknown'}")


def _reference(board: Any, index: dict[str, Any], card_id: str) -> str:
    """A related card's `KEY-12`, or its bare id when it is on another project.

    A relation may cross projects, and the index only holds this one. Printing
    the id is honest; inventing a sequence number for it would not be.
    """
    card = index.get(card_id)
    if card is None:
        return card_id
    return f"{board.project.key}-{getattr(card, 'sequence_id', '?')}"


def _named_relations(board: Any, found: dict[str, list[str]]) -> dict[str, list[str]]:
    """Relations with each id turned into a reference, dropping empty buckets."""
    if not any(found.values()):
        return {}
    index = board.card_index()
    return {
        name: [_reference(board, index, card_id) for card_id in ids]
        for name, ids in found.items() if ids
    }


def _state_name(project: Any, state_id: Any) -> str:
    for name, state_uuid in project.states.items():
        if state_uuid == str(state_id):
            return name
    return str(state_id or "—")


def _first(values: Any) -> Any:
    return values[0] if values else None


def main() -> None:
    """Entry point, and the only error boundary.

    A guard prints its rule and stops. An HTTP failure prints the status,
    because a 404 from this API usually means "your server does not have that
    endpoint" and not "the thing you asked for is missing".
    """
    try:
        cli.main(standalone_mode=False)
    except click.ClickException as error:
        error.show()
        raise SystemExit(error.exit_code) from None
    except click.Abort:
        click.echo("aborted", err=True)
        raise SystemExit(130) from None
    except PlaneProjError as error:
        click.echo(f"{type(error).__name__}: {error}", err=True)
        raise SystemExit(1) from None
    except (sprints_module.SprintError, sqlite3.Error) as error:
        click.echo(f"sprints: {error}", err=True)
        raise SystemExit(1) from None
    except HttpError as error:
        click.echo(f"plane: HTTP {error.status_code}: {error}", err=True)
        raise SystemExit(1) from None
