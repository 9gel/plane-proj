# Plane Adapter

## 1. Authority and Configuration

When Plane is configured, use `plane-proj` exclusively for board operations. Run
it from the project directory so `plane-proj.json`, credentials, project rules,
and the sprint database resolve correctly. Never expose credentials or bypass
the CLI with direct API writes.

Use `plane-proj COMMAND --help` for command syntax.
Do not preserve copied command catalogs in project instructions.
Use `plane-proj intake new --title TITLE` with a Markdown description on stdin
to file an agent-discovered bug or suggestion for triage without assigning a
cycle. The CLI verifies the new pending Intake record before reporting success.

## 2. State Model

- Native cycle membership is authoritative; do not create sprint labels.
- Use the configured Backlog, Todo, In Progress, Verifying, Done, and Cancelled
  states.
- Follow the
  [Coordinator transition table](coordinator.md#3-allowed-card-state-transitions).
  Move cards when reality changes; an idle card MUST NOT remain In Progress.
- Use native dependency relations and configured estimate values.
- Serialize board writes through the Coordinator and honor `Retry-After` or
  reported rate limits without polling.
- Read every write back. Stop further writes when stored state differs from the
  request.

## 3. Sprint Register

`SPRINTS.sqlite` is the sprint plan and history store. The top-level
`plane-proj init` command creates and binds it; use `plane-proj sprints` to
plan, start, inspect, and close sprints. `SPRINTS.md` is obsolete and
MUST NOT be created or maintained. Agents MUST read and write the register
only through `plane-proj sprints` (with `--json` for machine-readable output),
never through Python `sqlite3`, the `sqlite3` CLI, or any other SQLite client.
Its location comes solely from the config's `state_file`, resolved relative
to the config file; agents MUST NOT create or copy a register elsewhere.
The register is bound to its Plane server, workspace, and project. Legacy
registers require `sprint bind` with the correct config. A mismatch MUST be
corrected by selecting the matching config/register, never by overwriting its
binding.

Every card not Done or Cancelled belongs to the current or a planned sprint's
cycle. `card new` requires `--cycle`; `card rm-cycle` and `cycle rm` refuse an
open card, so use `card set-cycle` to move one to another sprint.
`sprints check` lists open cards in no sprint, Backlog cards in a running
sprint, active cards in an unstarted sprint, open timers on settled cards, and
planned sprints without a cycle; it writes nothing and exits 1 on findings.

A card above the cycle ceiling may be filed with `--state Backlog` in a planned
sprint; every move into Todo or a later active state refuses it until it is
split. Starting a sprint refuses while any open card is in no sprint or its
cycle holds an unsplit card, then binds the
planned record to an explicit Plane cycle and moves its cards to Todo. Closing
MUST refuse while a cycle member is outside Done or Cancelled, MUST write and
verify the cycle end timestamp, and MUST leave the cycle unarchived with its
membership unchanged before closing the SQLite record. Never use `cycle archive`
as part of sprint closure. The adapter exposes no cycle archive or delete
command; `cycle restore` is recovery-only.

`SPRINTS.sqlite` supports multiple current sprints running in parallel on
the same project, provided each sprint binds to a distinct Plane cycle.
When multiple sprints are active, `sprints list` displays them under
`Current sprints`, `sprints show` displays all current sprint detail tables,
and `sprints telemetry` requires an explicit `SPRINT_ID` argument.

Run `plane-proj sprints check` and `plane-proj project rules-check` before
closure. Project configuration,
rather than this skill, defines required modules, estimates, WIP limits,
exemptions, and the sprint database path.

## 4. Execution Telemetry

Plane activity is authoritative for state-transition timestamps. Community
Edition timer boundaries are structured card comments. The Coordinator MUST be
the sole collector that persists their derived statistics into `SPRINTS.sqlite`.

### Activity timers

Use native state history for In Progress, Verifying, and rework. Active total is
In Progress plus Verifying residence, including ongoing intervals at capture;
Todo and Done are not Active. Do not start timers named after states or manually
calculate their durations or rework counts.
Do not create `verification`, `verifying`, or `review` timers. Moving the card
into Verifying and then to Done or In Progress is sufficient. Sprint reports
ignore legacy timers in these categories, including open ones; do not create
cleanup timer comments solely for reporting.

Capture finer activities with `card timer start CARD CATEGORY` and
`card timer stop CARD`: `coding`, `dependency-wait`, `service-wait`,
`blocking-run` (a test/build that blocks further work), and `manual-qa`. Use
`dependency-wait` when another card, agent, or external deliverable prevents
progress; use `service-wait` when an unavailable service or infrastructure does.
Use `user-ask` for a blocking user answer or approval, not dependency-wait.
Record only the affected card's actual blocked interval. Do not create new
generic `waiting` timers; historical ones remain unclassified. Implementers and
reviewers report actual activity boundaries promptly; the Coordinator serializes
these writes. On an activity change, prefer
`card activity CARD CATEGORY --operation-id ID`: it stops the open timer and
starts the new category exactly once, and a retry cannot duplicate boundaries.
Stop on handoff, completion, cancellation, or deferral; the next worker reports
the next activity. `card transition` to Done or Cancelled refuses while a timer
runs unless `--stop-activity` is passed. `card activity CARD stop` is refused;
close a timer with `card timer stop CARD`. Switch to the specific blocking
category instead of leaving a coding timer running. Do not split coding for each
short command or background test.

One timer can run per card. Repeated reports of the same ongoing activity need
no write. When ownership or a retry outcome is uncertain, inspect `card stats`
before changing timers; do not invent missing boundaries. Plane comment creation
times measure duration, so delayed reports cannot reconstruct past activity.
Timer categories and state residence are overlapping views, not additive totals.

#### Waiting for the user

For a planned wait on a user answer or approval on an irreversible one-way door
decision, stop the affected card's current activity timer and start `user-ask`
before asking and yielding. Reversible decisions (two-way doors) MUST NOT enter
`user-ask` or pause work; make a documented assumption and proceed. Stop
`user-ask` when the blocking answer arrives or the interruption ends; start the
next activity only when that work resumes. If another blocker remains, switch to
`dependency-wait` or `service-wait` according to its cause. Do not time
nonblocking questions or pause unrelated cards that can proceed. Apply the
normal blocked-state transition and collection rules as appropriate; the
activity category does not replace tracker state.

Only an unexpected user interruption that prevents recording the boundary calls
for transcript timing recovery. Inspect any open timer before resuming and, if
the harness supports it, search timestamped transcript events for the missing
interval. A missed interval with provenance MAY be recorded with
`card activity CARD CATEGORY --started-at TS --ended-at TS
--evidence-ref REF --operation-id ID`; it is stored and reported
separately from live-measured time, keeps occurrence and receipt times
apart, and refuses future, inverted, unevidenced, or overlapping times.
Do not simulate recovery by posting start and stop together after the wait, or
silently present transcript-derived time as collected timer data. An
interval without provenance remains missing; use live timers for
subsequent waits.

### Collection

- Prefer `plane-proj card transition CARD --from A --to B
  [--stop-activity] --operation-id ID` for single-card state changes: it
  runs inspect, optional timer stop, move, readback, and collection as
  one journaled operation, and a retry with the same id resumes without
  replaying moves or timer events. On a Tech Lead rework verdict, the
  Coordinator MUST transition Verifying → In Progress this way before
  redispatch. The native Verifying → In Progress transition is one
  rework; repeated feedback in the same round is not another.
- After EVERY successful card state transition not made through
  `card transition`, run `plane-proj sprints collect CARD` before
  further work or reporting. This applies to admission, dispatch,
  blocking, review, rework, Done, Cancelled, deferral, reopening, and
  restoration, including transitions made by lifecycle/batch commands.
  Collect each affected card; honor request limits between batches. The
  settled-state capture is the final snapshot.
- Collect before moving a deferred card to a planned sprint's cycle. If
  collection fails or the card cannot be associated with the register, resolve
  that failure; do not silently skip collection or claim the state operation
  fully recorded.
- Reuse collections already made after transitions. Timer-only activity changes
  do not require immediate collection; collect those cards at the next state
  transition or before a report needing the updated activity totals. Refresh
  ongoing intervals only when their freshness matters to that report. Do not
  poll or collect unchanged state merely on a schedule. Multiple affected cards
  can share one `sprints collect CARD...` invocation.
- Run `plane-proj sprints telemetry` to inspect the latest stored snapshot
  per card; use `--all` only when historical observations are required. Pass
  `SPRINT_ID` if multiple sprints are current.
- In parallel sprints, `plane-proj sprints collect [CARD...]` automatically
  routes passed card references to their owning current sprint. When collecting
  without card arguments while multiple sprints are current, pass
  `--sprint SPRINT_ID` to target the specific sprint's cycle cards.
- Immediately before sprint closure, collect every cycle card and confirm every
  settled card has a final snapshot. Missing or failed collection blocks
  closure.

Collection is observational: it MUST NOT move cards or replace technical
evidence. The database retains successive snapshots so early implementation,
waiting, QA, rework, and final totals remain available for later velocity
analysis.

### Evidence archives

Archive evidence files with `card evidence add CARD FILE --kind KIND
[--revision SHA]`, which uploads the exact bytes and returns a receipt
(attachment id, SHA-256, size, verification result) only after readback.
Failed readback MUST NOT be reported as a successful archive, and a
temporary source file MAY be removed only after a verified receipt.
Do not substitute rendered comments for byte-preserving archives.
Re-verify with `card evidence verify CARD ID --digest SHA` when receipt
integrity matters. Remove obsolete dependency edges with
`rel remove CARD TYPE OTHER`; cancellation alone never deletes relations.
