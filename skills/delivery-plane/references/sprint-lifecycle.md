# Sprint Lifecycle

## 1. Planning

A sprint MUST define one outcome, concrete deliverables, decidable acceptance
criteria, explicit non-goals, and dependency order before implementation starts.
Only delivery contributing directly to that outcome belongs in the sprint.
Record dependency order as card-to-card `blocked_by` relations, including
relations to cards in other sprints, so later work waits only on the cards it
needs. After planning, run `plane-proj sprints critical-path`: open work
divided by the critical path is the most parallel execution can achieve, and a
low ceiling is a reason to restructure the plan, for example around an
interface card.

Agent-delivery sprints SHOULD be planned to complete in one to eight hours of
elapsed time. Group a multi-sprint plan into roughly equal point loads where
outcome and dependency boundaries permit, but never split a coherent outcome or
combine unrelated value merely to equalize totals. The sprint boundary is an
accepted, valuable, self-contained outcome; the duration is a forecast, not
permission to close partial delivery.

Planning SHOULD begin from durable research or an already-understood problem.
The researching agent MAY plan the delivery, or a fresh-context agent MAY plan
from its write-up. Do not make a sprint depend on an earlier agent's transcript
or private retained context.

The current tracker cycle is authoritative for sprint membership. Cards slated
for the current sprint MUST be assigned to it. Every other card that is not Done
or Cancelled MUST belong to a planned sprint's cycle; planned-cycle membership
does not admit it for execution. When no planned sprint fits a new card, plan a
one-card sprint and grow or merge its scope as similar work arrives. Run
`sprints check` before starting a sprint and resolve every finding; the start
refuses while any open card is in no sprint.

The sprint that first applies a new specification to running code MUST be
planned as a test of that specification: name an owner, and give each applying
card a criterion to report every rule that could not be applied cleanly, or to
state that none was found. Defects in the specification are an expected output,
not slippage.

Capture the opening revision and baseline gate state before dispatch.

## 2. Discovered Work

| Finding | Treatment |
|---|---|
| Defect preventing an admitted card's criteria | Resolve within that card. |
| Separate deliverable required for the sprint goal | Create, size, relate, and admit a new card. |
| Future delivery | Assign to a planned sprint's cycle, planning a one-card sprint if none fits. |
| Duplicate or obsolete admitted work | Cancel with rationale; retain cycle membership. |
| Unrelated bug or suggestion | File in Intake for triage when the project uses Plane Intake; keep outside the sprint. |

## 3. Closure

A sprint MUST NOT close until every admitted card is Done or Cancelled, every
Done card has independent acceptance, tracker membership matches the admitted
set, the merged tree has passed the project-required complete verification,
temporary work areas are removed, every settled card has a final execution
snapshot in the sprint register, at least one accepted deliverable realizes the
Sprint Goal, and the repository is at a clean verified commit. Run
`sprints preflight N` before closing: it reports nonterminal cards, missing
final snapshots, open timers, and the derived accounting without writing.
`sprints close N --ended TS --delivered TEXT` derives hours, closing totals, and
velocity; do not hand-calculate them. The preflight verifies the presence and
identity of closure facts, not their technical validity — acceptance stays
human/agent-reviewed. Run `sprints check` beside the preflight: open timers on
settled cards and Backlog cards left in the sprint both block closure. Record a
retrospective grounded in the observed scope, timing, blockers, verification,
and rework before closure.

Where the project specifies a retrospective directory, longer
retrospectives MUST be written there as `RETRO-<sprint #>.md`, opening
with a short "What went well" then "What can be improved", followed by
concrete changes to named instruction files, skill references, and tools
with proposed text and verification criteria. Record a concise summary
and the durable document reference in the sprint closure record; do not
duplicate the full retrospective into tracker comments.

Reuse evidence only for the identical tree and environment. Closure does not by
itself require rerunning valid verification.

Report initial and final card/point totals so discovered scope remains visible.
Use the stored execution snapshots to report implementation, blocking-run,
manual-QA, verification, and rework observations when present; distinguish
measured active time from wall-clock state residence.

## 4. Parallel Sprints

Multiple sprints MAY execute concurrently on the same project when bound to
distinct Plane cycles.

- **Admission and Start:** The incoming sprint MUST NOT start or admit cards
  until its Coordinator completes the pre-start coordination handshake defined
  in [coordinator.md](coordinator.md#4-parallel-sprint-coordination-protocol).
- **Cycle Isolation:** Each concurrent sprint MUST bind to a separate Plane
  cycle. A cycle cannot be shared across multiple active sprints.
- **Execution:** Teams execute in mutual silence. Coordinators communicate only
  to resolve unexpected boundary breaches or breaking contract changes.
- **Closure:** When a parallel sprint closes, its Coordinator sends a single
  completion notice to remaining running sprints before completing closure.
