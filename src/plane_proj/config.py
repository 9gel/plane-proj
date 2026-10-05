"""Local invocation defaults and estimate mappings; project metadata lives in memory."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from urllib.parse import urlparse

from plane_proj.guards import (
    ConfigError,
    EstimateTooLargeForCycle,
    UnknownEstimateValue,
    UnknownModule,
)

CONFIG_ENV_VAR = "PLANE_PROJ_CONFIG"
PROJECT_ENV_VAR = "PLANE_PROJ_PROJECT"

# `point` is declared by the API as an integer in [0, 12], so a scale value
# above that ceiling cannot be written there at all and `estimate_point`
# carries it alone. That is the mechanism behind cards whose UUID is on the
# scale but whose value the API can no longer tell you.
POINT_FIELD_MAXIMUM = 12
#: States in which a card of any size may sit: not yet admitted, or settled.
IDLE_STATES = frozenset({"backlog", "done", "cancelled"})


@dataclass(frozen=True)
class Rules:
    """What this project requires of a card. Every field is a decision someone made."""

    require_cycle: bool = True
    require_module: bool = True
    require_estimate: bool = True
    cycle_estimate_max: int | None = None
    wip_limit: int | None = None
    wip_states: tuple[str, ...] = ("In Progress",)
    unestimated_assignees: tuple[str, ...] = ()
    require_independent_verdicts: bool = False
    require_transition_table: bool = False
    require_delivery_plan: bool = False
    require_declared_scope: bool = False

    @classmethod
    def from_document(cls, document: Mapping[str, object], *, base: Rules | None = None) -> Rules:
        """Validate explicit rules; omitted values retain the project's defaults."""
        if not isinstance(document, dict):
            raise ConfigError("rules must be an object.")
        unknown = document.keys() - {item.name for item in fields(cls)}
        if unknown:
            raise ConfigError(f"Unknown rules: {', '.join(sorted(unknown))}.")
        values = dict(document)
        for name, value in values.items():
            if name.startswith("require_"):
                if not isinstance(value, bool):
                    raise ConfigError(f"rules.{name} must be a boolean.")
            elif name in {"cycle_estimate_max", "wip_limit"}:
                if value is not None and (type(value) is not int or value < 0):
                    raise ConfigError(f"rules.{name} must be a nonnegative integer or null.")
            else:
                if not isinstance(value, list) or any(
                    not isinstance(item, str) or not item.strip() for item in value
                ):
                    raise ConfigError(f"rules.{name} must be a list of nonempty names or IDs.")
                if name == "wip_states" and not value:
                    raise ConfigError("rules.wip_states must not be empty.")
                values[name] = tuple(value)
        return replace(base if base is not None else cls(), **values)


@dataclass(frozen=True)
class Project:
    """One project's identifiers and its rules."""

    key: str
    id: str
    name: str
    estimates_enabled: bool
    estimate_points: Mapping[int, str]
    cycles: Mapping[str, str]
    modules: Mapping[str, str]
    states: Mapping[str, str]
    labels: Mapping[str, str]
    states_outside_cycles: frozenset[str]
    rules: Rules

    def estimate_uuid(self, value: int) -> str:
        """The `estimate_point` UUID for a scale value, or raise.

        A value off the scale raises rather than rounding to a neighbour.
        """
        if value not in self.estimate_points:
            offered = ", ".join(str(point) for point in sorted(self.estimate_points))
            raise UnknownEstimateValue(
                f"{value} is not a point on the {self.key} estimate scale. "
                f"The scale is: {offered or 'empty — run `plane-proj project scale --write`'}."
            )
        return self.estimate_points[value]

    def estimate_value(self, estimate_point: str | None) -> int | None:
        """The scale value behind a UUID, or None where the scale does not name it."""
        if estimate_point is None:
            return None
        for value, uuid in self.estimate_points.items():
            if uuid == estimate_point:
                return value
        return None

    def estimate_fields(self, value: int) -> dict[str, object]:
        """Both estimate fields from one number, so no caller can write only one.

        The project displays `estimate_point`; `point` is the legacy integer and
        is invisible wherever an estimate set is configured. Writing only
        `point` shows the card as unestimated — a whole sprint of cards on this
        workspace did exactly that. Writing only `estimate_point` leaves the
        value unrecoverable by capture. Both, always, from one argument.
        """
        return {
            "estimate_point": self.estimate_uuid(value),
            # `None`, not omitted, above the ceiling. Omitting leaves whatever
            # `point` was there before: a card moved from 5 to 22 kept `point:
            # 5` beside the UUID for 22, and the two then disagree forever.
            "point": value if value <= POINT_FIELD_MAXIMUM else None,
        }

    def state_id(self, name: str) -> str:
        return _resolve(self.states, name, "state", self.key)

    def cycle_id(self, name: str) -> str:
        return _resolve(self.cycles, name, "cycle", self.key)

    def label_id(self, name: str) -> str:
        return _resolve(self.labels, name, "label", self.key)

    def module_id(self, name: str) -> str:
        """The module UUID for a module name, or raise.

        `UnknownModule` rather than the generic lookup error the other three
        raise: every card must carry a module, so an unresolvable name is a
        rule broken and not a lookup the caller chose to make.
        """
        if name not in self.modules:
            known = ", ".join(sorted(self.modules)) or "none"
            raise UnknownModule(
                f"{name!r} is not a module on {self.key}. Known: {known}. "
                f"Create the module on Plane, then retry."
            )
        return self.modules[name]

    def check_admission_estimate(self, value: int | None, state_name: str | None) -> None:
        """Refuse a card too large to enter `state_name` for execution.

        Backlog and settled states hold any size; every other state, and an
        unnamed one whose server default is unknown, is execution.
        """
        ceiling = self.rules.cycle_estimate_max
        if ceiling is None or value is None or value <= ceiling:
            return
        if not self.rules.require_cycle:
            return
        if state_name is not None and state_name.casefold() in IDLE_STATES:
            return
        raise EstimateTooLargeForCycle(
            f"An estimate of {value} is above {self.key}'s cycle ceiling of {ceiling}, "
            f"so this card may not enter {state_name or 'an unnamed state'}. Above the "
            f"ceiling the number is not a size — it says the size is unknown. It may "
            f"wait in Backlog in a planned sprint; split it before admission to Todo, "
            f"and do not raise the number."
        )


def _resolve(mapping: Mapping[str, str], name: str, kind: str, project_key: str) -> str:
    """One name-to-UUID lookup, with the alternatives named in the failure."""
    if name in mapping:
        return mapping[name]
    known = ", ".join(sorted(mapping)) or "none"
    raise ConfigError(
        f"No {kind} named {name!r} on {project_key}. Known: {known}. "
        f"Use the project listing commands to see the current names."
    )


DEFAULT_WEB_URL = "https://app.plane.so"


@dataclass(frozen=True)
class Config:
    """A whole config file, and the path it came from."""

    path: Path
    workspace_slug: str | None
    members: Mapping[str, str]
    unestimated_assignees: frozenset[str]
    default_project: str | None
    projects: Mapping[str, Project]
    state_file: Path | None = None
    # The Plane web app for links; the API host is not it on Plane Cloud.
    web_url: str = DEFAULT_WEB_URL
    integration_branch: str | None = None
    shared_paths: tuple[str, ...] = ()
    document: Mapping[str, object] = field(repr=False, default_factory=dict)

    def project(self, key: str | None) -> Project:
        """The project to act on, from the flag, the environment, or the file's default."""
        chosen = key or os.environ.get(PROJECT_ENV_VAR) or self.default_project
        if chosen is None:
            known = ", ".join(sorted(self.projects)) or "none"
            raise ConfigError(
                f"No project given. Pass --project, set {PROJECT_ENV_VAR}, or set "
                f'"defaults": {{"project": …}} in {self.path}. Known: {known}.'
            )
        if chosen not in self.projects:
            known = ", ".join(sorted(self.projects)) or "none"
            raise ConfigError(f"No project {chosen!r} in {self.path}. Known: {known}.")
        return self.projects[chosen]

    def member_id(self, name_or_id: str) -> str:
        """A member UUID from a UUID or from the name the config file gives it."""
        if name_or_id in self.members:
            return name_or_id
        matches = [uuid for uuid, name in self.members.items() if name == name_or_id]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ConfigError(
                f"{name_or_id!r} names {len(matches)} members in {self.path}. Pass the UUID."
            )
        known = ", ".join(sorted(self.members.values())) or "none"
        raise ConfigError(f"No member {name_or_id!r} in {self.path}. Known: {known}.")

    def member_name(self, uuid: str | None) -> str:
        """A member's name for display, falling back to the UUID the project gave."""
        if uuid is None:
            return "—"
        return self.members.get(uuid, uuid)

    def takes_no_estimate(self, assignee_id: str) -> bool:
        return assignee_id in self.unestimated_assignees


def bootstrap_document(
    *, slug: str, project_key: str, estimate_points: Mapping[str, str], rules: Rules,
) -> dict[str, object]:
    """Only inputs that cannot simply be listed from Plane."""
    return {
        "defaults": {
            "workspace": slug, "project": project_key,
            "web_url": DEFAULT_WEB_URL,
        },
        "state_file": "SPRINTS.sqlite",
        "estimate_points": dict(estimate_points),
        "rules": {
            "require_cycle": rules.require_cycle,
            "require_module": rules.require_module,
            "require_estimate": rules.require_estimate,
            "cycle_estimate_max": None,
            "wip_limit": None,
            "unestimated_assignees": [],
            "require_independent_verdicts": True,
            "require_transition_table": True,
            "require_delivery_plan": True,
            "require_declared_scope": True,
        },
    }


def merge_scale(
    existing: Mapping[str, str], captured: Mapping[str, str]
) -> tuple[dict[str, str], list[str], list[str]]:
    """Fold a captured scale into the one already recorded. **Never replaces it.**

    Only a value that some card carries can be captured, so a capture is a
    partial view by construction: the card that proved a value can be closed,
    archived or deleted, and the UUID stays valid forever afterwards. Replacing
    the map with what today's project happens to show would delete correct
    entries that nothing can recover — the cards that proved them are gone.

    Returns the merged map, the values kept without confirmation, and the
    values whose UUID the project now disagrees with. A conflict is handed back
    rather than resolved: it means the project's estimate set was rebuilt, and
    which map is right is not this function's call.
    """
    merged = dict(existing)
    kept, changed = [], []
    for value, uuid in captured.items():
        if value in existing and existing[value] != uuid:
            changed.append(value)
        merged[value] = uuid
    kept = sorted((set(existing) - set(captured)), key=lambda v: int(v) if v.isdigit() else 0)
    return (
        dict(sorted(merged.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0)),
        kept,
        sorted(changed, key=lambda v: int(v) if v.isdigit() else 0),
    )


DEFAULT_CONFIG = Path("plane/plane-proj.json")


def find_config_path(explicit: str | Path | None) -> Path:
    """Use the explicit path, environment override, or plane/plane-proj.json."""
    path = Path(
        explicit or os.environ.get(CONFIG_ENV_VAR) or DEFAULT_CONFIG
    ).expanduser()
    if not path.is_file():
        raise ConfigError(f"No config file at {path}. Run plane-proj init --project KEY.")
    return path


def load_config(explicit: str | Path | None = None) -> Config:
    """Read invocation defaults; fetch server metadata when connecting."""
    path = find_config_path(explicit)
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ConfigError(f"{path}: expected a JSON object.")
    unknown = document.keys() - {
        "defaults", "state_file", "estimate_points", "rules", "shared_paths",
    }
    if unknown:
        raise ConfigError(
            f"{path}: unsupported config keys: {', '.join(sorted(unknown))}. "
            "Keep defaults, state_file, estimate_points, rules and shared_paths only; "
            "Plane supplies project metadata."
        )
    defaults = document.get("defaults", {})
    points = document.get("estimate_points", {})
    if not isinstance(defaults, dict) or not isinstance(points, dict):
        raise ConfigError(f"{path}: defaults and estimate_points must be objects.")
    raw_shared = document.get("shared_paths")
    shared_paths: tuple[str, ...] = ()
    if raw_shared is not None:
        if not isinstance(raw_shared, list):
            raise ConfigError(f"{path}: shared_paths must be a list of repository paths.")
        for item in raw_shared:
            if not isinstance(item, str) or not item.strip():
                raise ConfigError(f"{path}: shared_paths must be a list of nonempty path strings.")
            cleaned = item.strip()
            if cleaned.startswith("/") or cleaned.startswith("\\") or Path(cleaned).is_absolute():
                raise ConfigError(
                    f"{path}: shared_paths must not contain absolute paths: {item!r}."
                )
        shared_paths = tuple(item.strip().removeprefix("./") for item in raw_shared)
    state_file = document.get("state_file")
    if state_file is not None and (not isinstance(state_file, str) or not state_file.strip()):
        raise ConfigError(f"{path}: state_file must be a nonempty path string.")
    if defaults.keys() - {"workspace", "project", "web_url", "integration_branch"}:
        raise ConfigError(
            f"{path}: defaults accepts only workspace, project, web_url and integration_branch."
        )
    for key, value in defaults.items():
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"{path}: defaults.{key} must be a nonempty string.")
    web_url = defaults.get("web_url", DEFAULT_WEB_URL).strip().rstrip("/")
    parsed = urlparse(web_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigError(
            f"{path}: defaults.web_url must be an http or https address, "
            f"such as {DEFAULT_WEB_URL}."
        )
    for value, uuid in points.items():
        if not value.isdigit() or str(int(value)) != value or not isinstance(uuid, str) or not uuid:
            raise ConfigError(f"{path}: estimate_points must map nonnegative integers to UUIDs.")
    if len(set(points.values())) != len(points):
        raise ConfigError(f"{path}: an estimate UUID cannot represent multiple values.")
    project_key = defaults.get("project")
    rules = document.get("rules", {})
    Rules.from_document(rules)
    if rules and project_key is None:
        raise ConfigError(f"{path}: rules requires defaults.project to scope its requirements.")
    if points and project_key is None:
        raise ConfigError(f"{path}: estimate_points requires defaults.project to scope its UUIDs.")
    projects = {}
    if project_key is not None:
        projects[project_key] = project_from_facts(
            project_key, {"id": "", "estimate_points": points, "rules": rules}, path,
        )
    return Config(
        path=path,
        workspace_slug=defaults.get("workspace"),
        members={},
        unestimated_assignees=frozenset(),
        default_project=project_key,
        projects=projects,
        # Relative state paths belong to the config, not the invocation cwd.
        state_file=(
            path.parent / Path(state_file).expanduser()
            if state_file is not None else None
        ),
        web_url=web_url,
        integration_branch=defaults.get("integration_branch"),
        shared_paths=shared_paths,
        document=document,
    )


def project_from_facts(key: str, body: Mapping[str, object], path: Path) -> Project:
    """One project entry, with the cross-checks the file cannot express itself."""
    states = {str(name): str(uuid) for name, uuid in body.get("states", {}).items()}  # type: ignore[union-attr]
    outside = frozenset(str(name) for name in body.get("states_outside_cycles", ()))  # type: ignore[arg-type]
    # Only cross-checked once the project has been captured. An empty `states`
    # means "not captured yet", and refusing to load then would make a fresh
    # config file impossible to run `project capture` against — the one command
    # that would fix it.
    unknown_outside = outside - states.keys() if states else frozenset()
    if unknown_outside:
        raise ConfigError(
            f"{path}: {key}.states_outside_cycles names states the project does not have: "
            f"{', '.join(sorted(unknown_outside))}. Known: {', '.join(sorted(states))}."
        )

    if "id" not in body:
        raise ConfigError(f'{path}: project {key} has no "id".')
    estimates_enabled = body.get("estimates_enabled", True)
    if not isinstance(estimates_enabled, bool):
        raise ConfigError(
            f"{path}: project {key}.estimates_enabled must be a boolean."
        )

    return Project(
        key=key,
        id=str(body["id"]),
        name=str(body.get("name", key)),
        estimates_enabled=estimates_enabled,
        estimate_points={
            int(value): str(uuid)
            for value, uuid in body.get("estimate_points", {}).items()  # type: ignore[union-attr]
        },
        cycles={str(n): str(u) for n, u in body.get("cycles", {}).items()},  # type: ignore[union-attr]
        modules={str(n): str(u) for n, u in body.get("modules", {}).items()},  # type: ignore[union-attr]
        states=states,
        labels={str(n): str(u) for n, u in body.get("labels", {}).items()},  # type: ignore[union-attr]
        states_outside_cycles=outside,
        rules=Rules.from_document(body.get("rules") or {}),  # type: ignore[arg-type]
    )
