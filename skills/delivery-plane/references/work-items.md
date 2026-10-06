# Work Items

## 1. Placement

- Sprint and future delivery MUST be represented in the configured tracker.
- Card decisions, findings, evidence, and verdicts MUST remain with the card.
- User-directed exploratory research normally happens outside a sprint and MUST
  produce a durable write-up in the repository-configured research location.
  It is not a delivery card unless that write-up is itself an admitted
  deliverable.
- Authorized one-off delivery MAY remain in the session handoff when project
  rules do not require a card.
- Agents who discover a separate bug or have a suggestion MAY file it as an
  Intake item for triage. Describe the observed behavior or proposal and its
  evidence. Do not create a sprint card before the work is accepted and planned.

The researching agent or a fresh-context planning agent MAY turn that write-up
into delivery cards. Planning MUST use the durable artifact as its input rather
than depend on the originating agent's transcript or retained context.

## 2. Delivery Card

A card MUST state, in this order:

```markdown
## What to build

## Current status

## Acceptance criteria

## Delivery plan
```

The human owns product value, priority, and negotiation. This card format is a
delivery contract, not a user-story persona: agents MUST state the work and its
acceptance boundary without inventing stakeholder value or an `As a ...` voice.

### Delivery plan

Every card MUST declare the repository paths it touches and that its
dependencies were assessed:

```markdown
## Delivery plan

Touches:
- `src/path/to/file.py`
- `src/path/__init__.py`
- `src/new_file.py` (new)
Dependencies: assessed
```

- Planners MUST declare `Touches` listing every file the card changes, each by
  its path from the repository root with `/` separators. Each entry MUST be a
  single file that git tracks, or a file the card creates, marked `(new)`.
  Directories and patterns MUST NOT be declared: two cards declaring the same
  directory overlap only if they change the same file in it, so a directory
  hides real overlap and invents false overlap. A card that changes no code
  declares `Touches: none`.
- Each path is a Markdown code span, so characters such as the underscores
  in `__init__.py` are kept literally; `card new` and `card plan` write it so.
- When the files are not yet known, find them before declaring; a card whose
  files cannot be named is not ready to plan.
- Planners MUST assess whether the card waits on any prerequisite cards, and
  record `Dependencies: assessed`. Card dependencies themselves remain native
  Plane `blocked_by` relations; `Dependencies: assessed` proves that the check
  was performed rather than omitted.
- Write both declarations at card creation with `plane-proj card new --touches
  PATH --deps-assessed` (repeat `--touches` for each path, or pass `--touches
  none`). To update only the delivery plan of an existing card without modifying
  the rest of the description, use `plane-proj card plan CARD --touches PATH
  --deps-assessed`.
- With the `require_delivery_plan` project rule enabled, `card new` refuses
  cards lacking either declaration before the first network request.
- `card new` and `card plan` refuse, under `DeliveryPlanRule`, a directory, a
  pattern, or a path git does not track unless marked `(new)`. Run them from
  inside the project's git repository. Readiness reports an existing card
  that declares a directory or pattern as `Unverified` until its files are
  listed.

Verify every factual assertion in a card description (that a file cites
something, that a check exercises something) when the card is written; do not
assert it from memory.

The title names a concrete artifact or behavior. `What to build` identifies the
target and explicit exclusions. `Current status` contains present facts, not a
chronology. Each acceptance criterion names a capable instrument, the observed
property, and the required result.

Before dispatch, each data-delivery card MUST identify the exact output set:
artifact count, format/schema, producer, consumer, identity/provenance source,
and whether outputs remain separate. It MUST distinguish implementation,
constructed-fixture verification, full-input generation, publication, and
deployment, explicitly naming which are in scope. Required upstream inputs
MUST have a producer or an explicit external-asset owner and acquisition path.
Asset-derived facts MUST NOT become operator configuration to hide a missing
dependency. A bare capability adjective (for example "spatially indexed"
alone) is not an acceptance criterion.

Work items are disjoint and MUST NOT overlap: there are no epics, parent
cards, or umbrella cards, and no card may contain or duplicate another
card's scope. Cross-work-item design documentation belongs outside cards,
in a location the project's own rules choose; plane-proj does not mandate
one and never will.

Dependencies belong in native tracker relations between cards, not titles; a
relation MAY cross sprints. Update superseded
descriptions in place. Do not retain obsolete alternatives or narrative recaps.

## 3. Human Decisions

Escalate only decisions that existing project authority cannot resolve and that
represent irreversible one-way doors. Reversible choices (two-way doors) MUST be
resolved autonomously by adopting documented assumptions. When escalation is
unavoidable, present the decision, evidence, options and trade-offs, impact of
delay, and a recommendation. Human-owned decision cards MUST remain unestimated.
