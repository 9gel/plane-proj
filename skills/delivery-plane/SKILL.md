---
name: delivery-plane
description: Run bounded delivery through sprint planning, sized work items, role-separated multi-agent execution, independent verification, and tracker state. Use Plane through plane-proj when configured, while keeping delivery policy adaptable to another agile tracker.
---

# Delivery Coordination

Apply repository instructions first. They define the project, tracker
configuration, storage locations, commands, gates, and local sizing calibration.
When present, read the project's `CARD-SIZING.md` before estimating or splitting
cards.

## 1. Load Only What the Work Requires

| Work | Required reference |
|---|---|
| Plan, start, coordinate, or close a sprint | [sprint-lifecycle.md](references/sprint-lifecycle.md) |
| Create, refine, split, or classify a card | [work-items.md](references/work-items.md) and [sizing.md](references/sizing.md) |
| Act as Coordinator (including parallel coordination) | [coordinator.md](references/coordinator.md) |
| Act as Tech Lead (including parallel boundary analysis) | [tech-lead.md](references/tech-lead.md) and [verification.md](references/verification.md) |
| Act as QA | [qa.md](references/qa.md) and [verification.md](references/verification.md) |
| Act as Builder | [builder.md](references/builder.md) and [verification.md](references/verification.md) |
| Operate a Plane board | [plane-proj.md](references/plane-proj.md) |

Load multiple references only when the assignment spans those concerns. Do not
load role instructions for another role merely to perform the current role.

## 2. Shared Invariants

- The human owns product goals, value, and backlog ordering. The Coordinator
  owns tracker state, delivery sequencing within those priorities, and agent
  dispatch, and MUST follow the
  [allowed card state transitions](references/coordinator.md#3-allowed-card-state-transitions).
- Parallel sprints on the same project MUST bind to distinct Plane cycles and
  follow the pre-start conflict handshake and execution silence protocol in
  [coordinator.md §4](references/coordinator.md#4-parallel-sprint-coordination-protocol);
  explicit human instruction or approval MAY replace the handshake's mutual
  confirmation.
- The delivery system and its accountabilities persist across a sprint;
  individual Builders MAY join for one bounded assignment and leave after
  handoff. The Coordinator preserves continuity in the tracker and sprint
  register as that worker pool changes.
- The Tech Lead owns technical design, acceptance tests, review, integration
  commits, and the design-fit verdict.
- QA owns acceptance-criteria measurability checks, hidden probes, and the
  blind behaviour verdict on the frozen merged revision.
- A card enters Done only with passing QA and Tech Lead verdicts on the same
  revision from different authors; see
  [verification](references/verification.md#1-independence).
- A Builder owns one bounded assignment and its implementation evidence.
- Cards intended for the current sprint MUST belong to its current tracker
  cycle.
- Every card that is not Done or Cancelled MUST belong to the current or a
  planned sprint's cycle; no card waits in a backlog outside every sprint. When
  no planned sprint fits, plan a one-card sprint and adjust its scope later.
  Every card ends Done or Cancelled.
- Sizes 1, 2, and 3 are normally executable. A size-5 card MAY be admitted
  only under the documented rare exception in
  [sizing.md](references/sizing.md#3-size-5-exception). A card sized 8 or
  higher MAY wait in Backlog in a planned sprint but MUST be split before
  admission to Todo or dispatch; the CLI refuses the admission.
- Delivery claims MUST be backed by read state or verification evidence, not
  agent reports alone.
- Project-specific environment, storage, and deployment rules MUST NOT be
  inferred from this skill.
