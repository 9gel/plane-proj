"""Git commit trailers and declared card scope checks.

Reconciles git commit trailers (`Card: REFERENCE`) with declared card
scope (`Touches`), enforcing that a card's change set does not leave
its declared paths.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from plane_proj import delivery_plan as delivery_plan_module
from plane_proj.guards import ScopeRule


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


def card_change_set(
    card_ref: str,
    revision: str,
    *,
    cwd: Path | None = None,
) -> frozenset[str]:
    """Union of files changed by non-merge commits reachable from revision

    whose Card: trailers name card_ref.
    """
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
) -> None:
    """Refuse under ScopeRule if a card's change set leaves its declared Touches."""
    desc = getattr(card, "description_html", None) or ""
    plan = delivery_plan_module.parse_delivery_plan(desc)
    # Undeclared cards are not checked
    if plan.touches is None:
        return

    changes = card_change_set(reference, revision, cwd=cwd)

    if plan.is_touches_none:
        if changes:
            raise ScopeRule(
                f"Scope rule: {reference} declares Touches: none, but change set from {revision} "
                f"contains {len(changes)} changed file(s): {', '.join(sorted(changes))}."
            )
        return

    uncovered: list[str] = []
    for file_path in changes:
        if not any(path_covered(p, file_path) for p in (plan.touches or ())):
            uncovered.append(file_path)

    if uncovered:
        raise ScopeRule(
            f"Scope rule: change set for {reference} from {revision} contains path(s) outside "
            f"declared scope: {', '.join(sorted(uncovered))}."
        )
