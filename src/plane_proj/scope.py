"""Git commit trailers and declared card scope checks.

Reconciles git commit trailers (`Card: REFERENCE`) with declared card
scope (`Touches`), enforcing that a card's change set does not leave
its declared paths.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from plane_proj import delivery_plan as delivery_plan_module
from plane_proj.guards import DeliveryPlanRule, ScopeRule


def repo_root(board: Any = None, cwd: Path | None = None) -> Path | None:
    """Resolve repository root directory from explicit cwd or board config."""
    if cwd is not None:
        return cwd
    if board is not None:
        config = getattr(board, "config", None)
        config_path = getattr(config, "path", None)
        if config_path is not None:
            p = Path(config_path).resolve()
            if p.parent.name == "plane":
                return p.parent.parent
            return p.parent
    return None


def _require_git_repo(cwd: Path | None = None) -> None:
    """Refuse under ScopeRule if cwd is not inside a git repository."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0 or proc.stdout.strip() != "true":
            raise ScopeRule("Scope rule: project directory must be a git repository.")
    except (FileNotFoundError, OSError):
        raise ScopeRule("Scope rule: project directory must be a git repository.") from None


def require_tracked_files(
    touches: Sequence[delivery_plan_module.TouchedPath],
    cwd: Path | None = None,
) -> None:
    """Refuse under DeliveryPlanRule unless every declared path is one file.

    Each path must be a file git tracks, or a file the card creates, marked
    `(new)`. Directories and patterns are refused: two cards declaring the
    same directory overlap only if they change the same file in it, so only
    files make overlap measurable. Runs git locally, before any request.
    """
    for touched in touches:
        if delivery_plan_module.is_broad(touched.path):
            raise DeliveryPlanRule(
                "Delivery plan rule: File scope lists files, not "
                f"directories or patterns; got {touched.path!r}. List each "
                "file the card changes, and mark files it creates (new)."
            )
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=cwd, capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        raise DeliveryPlanRule(
            "Delivery plan rule: run from inside the project's git repository, "
            "so declared files can be checked against the files git tracks."
        )
    root = Path(proc.stdout.strip())
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--",
         *(f":(literal){t.path.removeprefix('./')}" for t in touches)],
        cwd=root, capture_output=True, text=True, check=True,
    ).stdout
    tracked = set(filter(None, listed.split("\0")))
    for touched in touches:
        path = touched.path.removeprefix("./")
        if (root / path).is_dir():
            raise DeliveryPlanRule(
                "Delivery plan rule: File scope lists files, not directories; "
                f"{path!r} is a directory. List each file the card changes."
            )
        if not touched.is_new and path not in tracked:
            raise DeliveryPlanRule(
                f"Delivery plan rule: {path!r} is not a file git tracks. "
                "Declare tracked files by their path from the repository "
                "root, and mark files the card creates (new)."
            )


def path_covered(declaration: str | delivery_plan_module.TouchedPath, file_path: str) -> bool:
    """Return True if declaration covers file_path.

    A declaration ending in '/' is a directory and covers everything below it.
    A (new) mark is informational and ignored during matching.
    """
    decl_path = (
        declaration.path if isinstance(declaration, delivery_plan_module.TouchedPath)
        else declaration
    ).strip().removeprefix("./")
    if decl_path.lower().endswith("(new)"):
        decl_path = decl_path[:-5].rstrip()
    decl_path = decl_path.removeprefix("./")
    clean_file = file_path.strip().removeprefix("./")
    if decl_path.endswith("/"):
        return clean_file == decl_path[:-1] or clean_file.startswith(decl_path)
    return clean_file == decl_path


def card_commits(
    card_ref: str,
    revision: str,
    *,
    cwd: Path | None = None,
) -> list[str]:
    """Return commit hashes reachable from revision whose Card: trailers name card_ref."""
    _require_git_repo(cwd)
    proc = subprocess.run(
        [
            "git",
            "log",
            "-z",
            "--no-merges",
            "--format=%H%x1f%(trailers:key=Card,valueonly=true,separator=%x1f)",
            revision,
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise ScopeRule(
            f"Scope rule: git log failed for revision {revision!r}: {proc.stderr.strip()}"
        )

    target_ref = card_ref.strip().casefold()
    target_short = target_ref.split("-")[-1] if "-" in target_ref else target_ref

    matching_commits: list[str] = []
    for record in proc.stdout.split("\0"):
        if not record.strip():
            continue
        parts = record.strip().split("\x1f")
        commit_hash = parts[0].strip()
        tokens: list[str] = []
        for raw in parts[1:]:
            for piece in raw.replace(",", " ").split():
                if piece.strip():
                    tokens.append(piece.strip().casefold())
        for t in tokens:
            t_short = t.split("-")[-1] if "-" in t else t
            if "-" in target_ref:
                matches = (t == target_ref) or (t == target_short and t.isdigit())
            else:
                matches = (t == target_ref) or (t_short == target_ref and target_ref.isdigit())
            if matches:
                matching_commits.append(commit_hash)
                break

    return matching_commits


def card_change_set(
    card_ref: str,
    revision: str,
    *,
    cwd: Path | None = None,
) -> frozenset[str]:
    """Union of files changed by non-merge commits reachable from revision

    whose Card: trailers name card_ref.
    """
    matching_commits = card_commits(card_ref, revision, cwd=cwd)

    changed_files: set[str] = set()
    for commit in matching_commits:
        diff_proc = subprocess.run(
            ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", "--root", commit],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        if diff_proc.returncode != 0:
            err = diff_proc.stderr.strip()
            raise ScopeRule(
                f"Scope rule: git diff-tree failed for commit {commit!r}: {err}"
            )
        for file_path in diff_proc.stdout.splitlines():
            cleaned = file_path.strip().removeprefix("./")
            if cleaned:
                changed_files.add(cleaned)

    return frozenset(changed_files)


def check_verdict_scope(
    card: Any,
    reference: str,
    revision: str,
    *,
    cwd: Path | None = None,
    shared_paths: Sequence[str] = (),
) -> None:
    """Refuse under ScopeRule if a card's change set leaves its declared Touches."""
    desc = getattr(card, "description_html", None) or ""
    plan = delivery_plan_module.parse_delivery_plan(desc)
    # Undeclared cards are not checked
    if plan.touches is None:
        return

    changes = card_change_set(reference, revision, cwd=cwd)

    uncovered: list[str] = []
    for file_path in sorted(changes):
        if any(path_covered(p, file_path) for p in (plan.touches or ())):
            continue
        if any(path_covered(p, file_path) for p in shared_paths):
            continue
        uncovered.append(file_path)

    if plan.is_touches_none:
        if uncovered:
            raise ScopeRule(
                f"Scope rule: {reference} declares File scope: none, "
                f"but change set from {revision} contains "
                f"{len(uncovered)} changed file(s): "
                f"{', '.join(sorted(uncovered))}."
            )
        return

    if uncovered:
        raise ScopeRule(
            f"Scope rule: change set for {reference} from {revision} contains path(s) outside "
            f"declared scope: {', '.join(sorted(uncovered))}."
        )


def scope_report(
    board: Any,
    cards: Sequence[Any],
    *,
    sprint_started_at: str | None = None,
    sprint_ended_at: str | None = None,
    cwd: Path | None = None,
) -> dict[str, Any]:
    """Generate a scope report against defaults.integration_branch."""
    target_cwd = repo_root(board, cwd)
    config = getattr(board, "config", None)
    branch = getattr(config, "integration_branch", None) if config is not None else None
    if not branch:
        return {
            "configured": False,
            "message": "defaults.integration_branch is not configured",
        }

    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=target_cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0 or proc.stdout.strip() != "true":
            return {
                "configured": False,
                "branch": branch,
                "message": "project directory is not a git repository",
            }
    except (FileNotFoundError, OSError):
        return {
            "configured": False,
            "branch": branch,
            "message": "project directory is not a git repository",
        }

    verify_proc = subprocess.run(
        ["git", "rev-parse", "--verify", branch],
        cwd=target_cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if verify_proc.returncode != 0:
        return {
            "configured": False,
            "branch": branch,
            "message": f"integration branch {branch!r} does not exist",
        }

    unmerged_cards: list[str] = []
    out_of_scope: list[dict[str, Any]] = []

    for card in cards:
        seq = getattr(card, "sequence_id", None)
        ref = f"{board.project.key}-{seq}" if seq is not None else str(getattr(card, "id", "?"))
        commits = card_commits(ref, branch, cwd=target_cwd)
        if not commits:
            unmerged_cards.append(ref)
            continue

        desc = getattr(card, "description_html", None) or ""
        plan = delivery_plan_module.parse_delivery_plan(desc)
        if plan.touches is None:
            continue

        changes = card_change_set(ref, branch, cwd=target_cwd)
        if plan.is_touches_none:
            if changes:
                out_of_scope.append({"card": ref, "paths": sorted(changes)})
        else:
            uncovered = [
                p for p in sorted(changes)
                if not any(path_covered(decl, p) for decl in plan.touches)
            ]
            if uncovered:
                out_of_scope.append({"card": ref, "paths": uncovered})

    unnamed_commits: list[str] = []
    if sprint_started_at:
        log_proc = subprocess.run(
            [
                "git",
                "log",
                "-z",
                "--no-merges",
                "--format=%H%x1f%cI%x1f%(trailers:key=Card,valueonly=true,separator=%x1f)",
                branch,
            ],
            cwd=target_cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        if log_proc.returncode == 0:
            start_dt = datetime.fromisoformat(sprint_started_at)
            end_dt = datetime.fromisoformat(sprint_ended_at) if sprint_ended_at else None
            for record in log_proc.stdout.split("\0"):
                if not record.strip():
                    continue
                parts = record.strip().split("\x1f")
                commit_hash = parts[0].strip()
                commit_date_str = parts[1].strip() if len(parts) > 1 else ""
                if not commit_date_str:
                    continue
                try:
                    commit_dt = datetime.fromisoformat(commit_date_str)
                except ValueError:
                    continue

                if start_dt.tzinfo is None and commit_dt.tzinfo is not None:
                    start_dt = start_dt.replace(tzinfo=UTC)
                elif commit_dt.tzinfo is None and start_dt.tzinfo is not None:
                    commit_dt = commit_dt.replace(tzinfo=UTC)

                if commit_dt < start_dt:
                    continue

                if end_dt is not None:
                    if end_dt.tzinfo is None and commit_dt.tzinfo is not None:
                        end_dt = end_dt.replace(tzinfo=UTC)
                    elif commit_dt.tzinfo is None and end_dt.tzinfo is not None:
                        commit_dt = commit_dt.replace(tzinfo=UTC)
                    if commit_dt > end_dt:
                        continue

                tokens: list[str] = []
                for raw in parts[2:]:
                    for piece in raw.replace(",", " ").split():
                        if piece.strip():
                            tokens.append(piece.strip())
                if not tokens:
                    unnamed_commits.append(commit_hash)

    out_of_scope_paths = [p for item in out_of_scope for p in item.get("paths", [])]

    return {
        "configured": True,
        "branch": branch,
        "unmerged_cards": unmerged_cards,
        "out_of_scope": out_of_scope,
        "out_of_scope_paths": out_of_scope_paths,
        "unnamed_commits": unnamed_commits,
    }
