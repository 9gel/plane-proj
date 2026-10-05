# plane-proj agent instructions

## Scope

This repository provides one CLI for operating a Plane board with enforced
workflow rules. Agents SHOULD read [`README.md`](README.md) for usage and
[`DESIGN.md`](DESIGN.md) for architecture and rationale.

## Environment and dependencies

- Project commands MUST run in the direnv-managed Nix flake environment.
- Nix MUST provide Python; `uv` MUST manage Python dependencies.
- Agents MUST NOT install packages into the development shell with `pip` or
  allow `uv` to download Python.
- Agents MUST use established libraries for HTTP, Markdown, argument parsing,
  retries, and other standard capabilities instead of implementing substitutes.
- If a missing dependency would shape the design, agents MUST ask whether to
  add it before designing around its absence.

## Python implementation

- Agents MUST load the `dignified-python` skill before editing Python.
- Guards MUST raise an exception, MUST NOT warn, substitute a default, or
  correct an argument, and each exception in `guards.py` MUST name its rule.
- A guard MUST fire before the first request. Tests MUST verify that a refused
  write leaves the board unchanged.
- Every successful write MUST be verified by readback before success is
  reported.
- Only `board.py` MAY access the network.
- Credentials MUST use `Secret`. The codebase MUST contain exactly one
  `reveal()` call unless a documented design decision approves another.

## Testing

- Every guard MUST have a test that triggers it and verifies that no board
  write occurred.
- Tests SHOULD seek the largest realistic defect that could otherwise remain
  green. `FakeClient` MUST reflect submitted values rather than return fixed
  values that make readback tests misleading.
- Live-server findings MUST record what was measured and when in `DESIGN.md`
  section 7. Unmeasured server limitations MUST NOT be stated as facts.
- Live tests MUST use a disposable `TEST` project in a non-production
  workspace, and agents MUST remove cards they create there.
- Commits that touch code MUST pass `pytest` and `ruff check .`.
- Markdown and documentation files MUST pass `rumdl check .`; rumdl lint
  checks are a MUST.
- Documentation-only commits SHOULD receive a wording, reference, and
  instruction-consistency review instead of the code gates, but rumdl lint
  checks MUST still pass.
- Changes to `pyproject.toml` or `uv.lock` MUST pass
  `nix build .#plane-proj`.

## Versioning and Git

- Builders MUST NOT change the version. The integrator MUST increment the
  version in `pyproject.toml` and refresh `uv.lock` once per card, in the
  commit that merges the card's work into the shared branch.
- A change made outside card delivery, including documentation, MUST
  increment the version and refresh `uv.lock` in its own commit.
- Non-breaking changes MUST increment the patch version without a patch
  ceiling. Breaking changes MUST increment the minor version and reset the
  patch to zero. The major version MUST change only when the user directs it.
- Version output MUST come from package metadata and MUST NOT be duplicated in
  source code.
- Commits MUST have a body describing the change, rationale, and verification.
- Agents MUST stage named paths, MUST NOT use `git add .`, and MUST NOT rewrite
  history.
- Agents MUST NOT push without explicit user consent.

---

**This file is:** a concise normative specification for agents working in this
repository.
