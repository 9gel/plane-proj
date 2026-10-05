# Design

## 1. Why a CLI and not an MCP server

An MCP server is loaded per session and costs tokens in every one of them,
including the sessions that never touch a project. It is also a schema wrapped
around an API, and the wrapper is where capability is lost: the Plane connector
could not set an estimate or a relation at all, and echoed a work item's whole
description back on every field update.

A CLI costs nothing until it runs, is reachable from a shell script and a
person's terminal as well as an agent, and fails with an exit status.

## 2. Layers

| Module | Holds | Talks to Plane |
|---|---|---|
| `guards` | The exception types, one per rule | no |
| `config` | Invocation defaults, saved estimates, runtime project model | no |
| `credentials` | Where the key comes from, and `Secret` | no |
| `text` | stdin, Markdown to HTML, HTML to one line | no |
| `board` | Name resolution, guarded writes, readback | **yes** |
| `output` | JSON or aligned columns | no |
| `cli` | The command tree, and the only error boundary | no |
| `sprints` | Local plans, lifecycle, history, and schema migration | no |

Everything except `board` is testable with no network and no credentials, and
that is the point of the split: the rules are the valuable part, and rules
behind a network call get tested once and then trusted.

Sprint timestamp readbacks and start retries compare parsed instants, so UTC `Z`
and equivalent numeric offsets match. Missing, malformed, or different readbacks
still fail. The local timestamp validator accepts either zone notation.

`SPRINTS.sqlite` holds ordered future plans, current sprints (supporting
parallel execution across distinct Plane cycles), the exact Plane cycle UUID
selected at start, and immutable closure facts. Planning and reporting are
independent of Plane configuration and credentials. Start and close
deliberately coordinate both stores: they preflight local facts, perform and
read back Plane writes, then commit the local transition. Schema v6 drops
the single-current index in place on the first write.

Starts are convergent rather than atomic because Plane exposes only per-card
state updates. A retry lists current state and writes only cards still needing
`Todo` or `Backlog`; the explicit cycle UUID and persisted timestamp prevent it
from selecting a different cycle. Closure preflights every cycle member as
`Done` or `Cancelled`, writes and reads back the end timestamp, then closes the
SQLite record without archiving the cycle or changing its membership. Human
listings retain the former standalone tool's Rich rounded tables and transpose
records on narrow terminals. Lifecycle section headings keep current, planned,
and completed records distinguishable despite their different fields. Planned
records join to live Plane cycles by the `Sprint N` name prefix so card and
estimate-point totals stay current without being duplicated in SQLite. Sprint
start persists its initial card and point totals. Current-sprint output combines
those facts with live total, Done, and Cancelled cycle metrics; current velocity
is Done points divided by wall-clock hours since the stored start. Reordering
requires every planned sprint ID exactly once. The register assigns contiguous
positions in one SQLite transaction and verifies them by readback, so an
omission, duplicate, or non-planned ID cannot produce a partial order. Past
sections include population statistics in a fixed metric-by-row table with Avg,
Med, Min, Max, and SD columns. Velocity is displayed per hour. The median is the
observed upper middle value rather than an interpolated value, keeping count
medians within the estimate domain; JSON retains numeric values while human
durations use abbreviated units with zero-padded hours, minutes, and seconds.

Machine-readable collections are arrays of named objects. `output.emit`
refuses positional rows so a new command cannot silently publish JSON whose
meaning depends on column order; identifier fields name the resource they
identify.

## 3. Minimal local configuration

`plane/plane-proj.json` is the default config unless `--conf` or
`PLANE_PROJ_CONFIG` names another file. It contains only `defaults.workspace`,
`defaults.project`, `defaults.web_url`, `state_file`, `estimate_points`, and
optional `rules`.
Relative state paths resolve from the config file's directory. Local sprint
reads default to `plane/SPRINTS.sqlite`, but starting a sprint requires
`state_file` or `--database` so a board mutation cannot bind to an implicit
register. A config belongs to one project's estimate scale.

The CLI reads `.env_plane`, or `.env` if absent, from the invocation directory,
so credentials stay out of the committed `plane/` folder. Process variables
override the chosen file; `--env-file` explicitly overrides them. The top-level
init command persists the resolved three Plane variables to a mode-`0600`
`.env_plane`, writes `plane/plane-proj.json`, and creates a project-bound
`plane/SPRINTS.sqlite`. There is no separate sprint initializer. A conflicting
register is rejected before the first Plane request. The parser is python-dotenv
with interpolation disabled; it never executes shell code. Only the three Plane
connection variables are consumed. Known Plane Cloud browser hosts are rejected
during credential loading, before the SDK makes a request. Plane Cloud uses
`https://api.plane.so`; sending API paths to `https://app.plane.so` returns HTML
rather than the documented JSON.

Connection startup lists projects and members, then reads the selected
project's states, cycles, modules, and labels. Those mappings are held only
in memory. Writes check guards after these reads and before their first
mutation. Duplicate names are refused, and unknown names name the live
alternatives. Cycle/module lifecycle commands do not rewrite local config.

Plane's cycle/module feature switches determine whether those assignments
are required. State groups identify backlog and settled states for auditing.
The optional `rules` object overrides workflow requirements and supplies
WIP limits, estimate ceilings, and unestimated-assignee names. These are local
decisions, not copies of server metadata. Parsing validates types before
connection; member and active WIP-state references resolve against live data.
Rules belong to the default project and are not reused on another project.

`project rules-check` audits without writes and exits 1 for findings. WIP is
counted per assignee, including each owner on multi-assignee cards. The audit
checks cycle/module membership, estimate ceilings, and owner exemptions, and
distinguishes uncaptured estimate UUIDs from blank estimates. Scale writes
preserve rules. Explicit `--estimate blank` remains available, but the audit
needs a configured exemption to distinguish a deliberate blank from missing
sizing.

Estimate mappings remain local because the estimates endpoint was measured
absent on self-hosted Plane CE (2026-09-09); see §7. Ordinary startup uses the
saved scale without scanning cards. Selecting another project resolves its
scale separately, and a scale write refuses to change the default project's
mapping. Capture writes persist only estimates, even when their output
includes other server metadata.

## 3b. Recovering the scale

With no estimates endpoint, the value behind a UUID has to come from a work
item. The route that looks obvious is wrong, and finding that out cost real
time:

**`point` is null on any estimate set through the UI.** Pairing `point` with
`estimate_point` therefore recovers nothing from exactly the cards someone
creates to publish a scale. Measured on the Testing project: six cards, one per
value, every `point` null.

`?expand=estimate_point` replaces the bare UUID with the estimate point object,
which carries `value`. Two traps inside it:

- **Use `value`, never `key`.** `key` is the ordinal. On a 1, 2, 3, 5, 10, 22
  scale the fourth point has `key` 4 and `value` 5 — reading `key` records five
  of the six values wrongly, and plausibly enough to go unnoticed.
- **A work item with no estimate still returns a dict**, a stub with no `id` and
  `value: ""`. Guard on both or the stub enters the scale.

`project scale` reports which card proved each value. The map is inferred rather
than read, so the evidence is part of the output.

## 3c. Why a capture merges the scale

Every other identifier has a list endpoint, so what the project shows is the
whole truth and overwriting is safe. The scale does not: it is recovered from
cards, so a capture sees only the values currently **in use**.

Those two facts together make replacement destructive. The card that published
a value can be closed, archived or deleted, and its UUID stays valid forever
afterwards — measured: DEMO's 5, 10 and 22 cards were removed between two runs
on the same day, and a replacing capture would have deleted three correct,
irrecoverable entries from a file that had them right.

So `merge_scale` folds captured values in, keeps the rest, and reports both what
it preserved without confirming and any UUID the project now disagrees with. A
disagreement is handed back rather than resolved: it means the estimate set was
rebuilt, and which map is right is not the tool's call.

## 4. The two-field estimate

The project displays `estimate_point`, a UUID. `point` is a legacy integer,
capped at 12, and invisible wherever an estimate set is configured.

- Writing `point` alone displays the work item as **unestimated**. Every card on
  this workspace did exactly that, for a sprint.
- Writing `estimate_point` alone makes the value **unrecoverable**: with no
  estimates endpoint, `capture` reads the scale back off cards, and a work item
  with no `point` cannot say which value its UUID means.

`Board.estimate_fields` therefore writes both from one argument, and no
caller anywhere handles a scale UUID. Above 12 only `estimate_point` is sent,
because the API rejects a larger `point`.

## 5. Placement is three calls, and all three are one command

Creating a work item puts it in **neither** a cycle nor a module. Both are
separate requests needing the id of a work item that does not exist until the
create returns. Nothing about a successful create makes the omission visible,
which is why cards created mid-sprint went missing from the burndown until a
manual audit found them.

`create_card` runs every check before the first request, then does all three
calls and reads the work item back. A work item created and then refused for its
module is worse than no card: it exists, unplaced, and nobody is looking for it.
The test `test_a_refused_card_sends_nothing` asserts that for every guard.

## 6. Every open card belongs to a sprint

`require_cycle` once admitted `--backlog` as a deliberate destination outside
every cycle, because a card above the cycle ceiling could not join one. Those
backlog cards belonged to no sprint. Nothing planned them, nothing had to close
them, and they accumulated without being finished or cancelled.

With cycles on, every card that is not Done or Cancelled now belongs to a
current sprint's bound cycle or to a cycle named for a planned sprint. A cycle
of a closed sprint, or any cycle not named `Sprint N` for a planned sprint,
holds no open card. The rule is enforced where a card could leave that set:

- `card new` requires a cycle, and `--backlog` no longer exists.
- `card rm-cycle` and `cycle rm` refuse an open card; `set-cycle` moves it to
  another sprint.
- `sprints start` reads membership before its first write and raises
  `OrphanedCard` while any open card is in no sprint. The start's move of
  active out-of-cycle cards to Backlog therefore applies only with
  `require_cycle` off.
- `sprints start` also raises `EmptyCycle` before its first write when the
  cycle holds no card other than Cancelled ones. Opening totals are read
  from the cycle at start, so an empty start records zero scope and counts
  every later card as added. A real register recorded a sprint opened this
  way at 0 cards and 0 points that closed at 3 cards and 8 points
  (observed 2026-10-04).

The cycle ceiling guards admission, not membership. A card above it may wait
in Backlog, which now means in a planned sprint, until it is split. It never
enters Todo or a later active state: `card new` with any other state (or none,
since Plane's default state is unknown), a state move, `card transition`, a
cycle join for an active card, a resize of an active card, and `sprints start`
all raise `EstimateTooLargeForCycle` before their first write. `sprints start`
therefore cannot begin while its cycle holds an unsplit card. `project
rules-check` reports an oversized card only outside Backlog, Done, and
Cancelled.

`sprints check` reports orphaned cards together with Backlog cards in a running
sprint, active cards in an unstarted sprint, open timers on settled cards in a
running sprint, and planned sprints with no cycle. It reads the register for
current and planned sprints, reads only those cycles' members, writes nothing,
and exits 1 on findings.

Dependencies may cross sprints. A card in one sprint can be `blocked_by` a
card in another, so a later sprint waits on the one card it needs rather than
on a whole earlier sprint. Refusing such a relation made dependencies coarser
than the work and serialized sprints that could overlap.

`sprints ready` and `sprints critical-path` read every open card in the
current sprints (by bound cycle) and the planned sprints (by `Sprint N` cycle
name) once each, and one relations request per open card. A blocker outside
those cycles is retrieved by id. The archived-items listing is not used
because some servers answer it with 404 (§7c); a blocker the server no longer
returns (404) is archived, and Plane archives only Completed or Cancelled work
items, so it counts as settled. `ready` lists Todo and Backlog cards whose
blockers are all settled. The
critical path is the heaviest `blocked_by` chain among open cards, weighed in
points (cards when estimates are off); open work divided by it is the most any
number of agents can shorten delivery. A relation cycle is refused by the
dependency rule because no order satisfies it.

### Sprint register ownership

Schema v5 adds a singleton `register_binding` row. Ownership is the workspace
slug and project key, resolved from local config/environment without requiring
a server URL or API key. The legacy host column is retained for compatibility.
Existing values remain untouched; new rows store the constant `unused` to meet
the legacy nonempty-column constraint. Ownership checks ignore that column.
Network connections use the current credential environment and still require
its server URL and API key. Endpoint changes need no migration
or rebind. Every ordinary sprint command validates this
binding before any network request or writable database connection. `init`
stores it on creation; explicit `bind` checks stored cycle IDs under the
selected project before migrating legacy data. An unbound legacy register cannot
be silently claimed by the first config used to read it. An existing binding
cannot be overwritten, including by `bind` retries. A register without cycle
evidence relies on the explicit bind declaration.

### Sprint planning owns its cycle

`sprints plan --id N` creates and maintains both halves of a sprint plan: the
register row and the Plane cycle named `Sprint N`. A sprint cycle made by hand
had no plan behind it, and a plan with no cycle had nowhere for its cards to
go, so `sprints check` reported the gap after the fact. One command closes it.

The order of work is fixed. Local validation runs first: title, goal, and
every acceptance criterion must be non-blank, the alias valid, and the sprint
still planned. Blank text is refused before any request because a server that
trims it could never read it back, and planning would fail on every retry
after creating the cycle.

The command then lists cycles to find `Sprint N`, and refuses before any
write when:

- more than one cycle claims the name (`ConfigError`);
- the register binds the cycle to another sprint (`SprintCycleBound`, which
  says to rename that cycle back to `Sprint M`);
- the register has no sprint N and the cycle holds a different, non-blank
  description (`SprintCycleTaken`). Another register, for instance on another
  git branch, may have planned it, and adopting it would silently replace
  that plan. A blank cycle, or one already holding exactly this plan, is
  adopted; the latter is the retry case. A server that rewrote the
  description, or a corrected re-run after a failed register write, also
  lands here, so `sprints plan --adopt-cycle` takes the cycle over and
  overwrites its description. `--adopt-cycle` never overrides
  `SprintCycleBound`.

With no match it creates the cycle and reads it back by name. The cycle
description is the plan in Markdown: the title as a heading, the goal, any
execution guidance, and the acceptance criteria as a list. The cycle name
stays `Sprint N`. The description is written only when it differs and is
verified by readback. The register row is written last. A failure between the
Plane write and the register write therefore converges on retry: the cycle is
found by name and reused, and an unchanged description is not rewritten.

The register's write lock (`BEGIN IMMEDIATE`) is taken only for that final
write, never across the Plane exchange. A plan makes several requests, each
with a long timeout, while other register writers wait only SQLite's
five-second busy timeout; holding the lock that long could make a command such
as `card transition` fail after its board write but before journalling it.
Under the lock the command repeats the plan validation and the bound-cycle
check, and refuses with `SprintCycleTaken` if a sprint N row appeared during
the exchange, unless `--adopt-cycle` was given. That late refusal follows the
Plane write: the cycle then holds this plan while the register holds the
other planner's, and either planner re-runs `sprints plan` to settle it.
Concurrent planners on different registers rely on the name lookup and
`SprintCycleTaken`; two that both find no cycle can each create one, which
every sprint command then refuses with `ConfigError` until one is removed in
Plane.

The lookup reads active cycles only and ignores archived ones. That is
acceptable because the tool never archives a cycle: closure leaves it
unarchived, and there is no cycle archive command. An archived `Sprint N`
exists only through a manual change in Plane, is invisible to every sprint
command, and `cycle restore` brings it back as a duplicate that those commands
then refuse with `ConfigError`.

Binding is unchanged. A planned row keeps no `cycle_id`; `sprints start`
binds the cycle, as before. Planning now needs Plane credentials and a
reachable server, where it was previously a register-only command.

`cycle new` is deprecated for sprints and hidden from help. Names are matched
after trimming surrounding whitespace. Three hand-made changes raise
`SprintCycleByHand` before any request and name `sprints plan --id N`:

- `cycle new` with a `Sprint N` name;
- `cycle rename` that moves a cycle into or out of a sprint's name: to a
  `Sprint N` name unless the old name already names sprint N, or from a
  `Sprint N` name to one that no longer names sprint N, which would break
  that sprint's cycle lookup. `Sprint 9` may still become `Sprint 9 — Title`;
- `cycle set` with `--description` on a `Sprint N` cycle, whose description
  the plan owns. Its dates may still be set.

Any other `cycle new` still creates the cycle, after a one-line deprecation
notice on stderr. The guard on renames means a cycle bound to another sprint
is renamed back in the Plane UI. Whether a live server stores a cycle
description verbatim has not been measured; a server that rewrites it fails
the readback rather than reporting success.

### Merging the register in git (0.21.13)

Each project commits `plane/SPRINTS.sqlite`, so sprints run on different
branches change the same binary file. `plane-proj register` gives git a merge
driver and a text view. Neither needs credentials, a server, or a binding,
because git runs them during merges and diffs.

`register merge BASE OURS THEIRS` (git's `%O %A %B`) opens all three
read-only. A missing or empty BASE, as git passes without a common ancestor,
is an empty register. It refuses under the register merge rule, naming the
input, when a file is not a readable SQLite register (for example an LFS
pointer), when its schema version differs (run `plane-proj sprints migrate`
on each branch first), or when its tables or columns differ from the current
schema. Rows merge per table:

- `sprints` by `sprint_id`, `card_execution_snapshots` by its primary key, and
  `operation_journal` by `operation_id`: equal sides win; a side equal to BASE
  takes the other side, so a row added, changed, or removed on one branch only
  is taken; anything else, including a removal against a change, conflicts.
- `register_binding`: a branch may add the binding, but every bound input must
  hold the same one; otherwise the merge is refused.

The merged rows must then keep the application's invariants: unique aliases,
no numeric alias naming another sprint, one current sprint per cycle, and no
planned sprints sharing a position. A position collision already present on
one branch is that branch's state and does not block; a new one asks for
`plane-proj sprints reorder` after resolving. The driver builds the result in
`<OURS>.plane-proj-merge.tmp` beside OURS with the current schema, so CHECK
constraints apply on insert. It then runs `PRAGMA foreign_key_check`, which
reports a snapshot whose sprint was removed on the other branch by snapshot
key, and `PRAGMA integrity_check`, reads the rows back, fsyncs, and replaces
OURS with `os.replace`. On any conflict OURS stays byte-for-byte unchanged,
the temporary file is removed, every conflict (table, key, differing fields)
goes to stderr, and the exit status 1 makes git report a conflict.

`register dump FILE` prints the schema version, then each table sorted by key
as one compact JSON line per row with sorted keys; git uses it as the diff
textconv. `register git-setup` adds the `.gitattributes` line for the
configured register (`state_file`, or `plane/SPRINTS.sqlite`) and the local
`merge.plane-proj-register.*` and `diff.plane-proj-register.textconv` keys,
then reads both back through `git check-attr` and `git config`. It is
idempotent. It refuses before writing when git is unavailable, outside a git
work tree, when the register file does not exist, or when its path holds
whitespace or a `.gitattributes` pattern character. A readback failure, such
as `*.sqlite binary` in `.git/info/attributes`, is refused with that hint.
`init` runs it once the project is created; if setup is refused, `init` still
succeeds and prints how to run `plane-proj register git-setup` later.

SQLite remains the authoritative store. Writes need transactions for
journal claims and multi-row reorders, the schema enforces CHECK, unique, and
foreign-key constraints on every write, and `user_version` drives
migrations. A text register would have to re-implement all three; the driver
and textconv give git what it needs without moving the data out of SQLite.

### Plane/ layout and schema v9 (0.20.0)

Version 0.20.0 restores the pre-Compose code (0.7.3 plus the
endpoint-independent binding fix) after the Plane Compose integration
(0.8–0.19, tagged `pre-rollback-compose-0.19.0`) was abandoned: it added no
delivery value and made every read parse every Task YAML file. Two
compatibility points remain so registers written by those versions keep
working. Config and register default to `plane/plane-proj.json` and
`plane/SPRINTS.sqlite`; `.env_plane` stays in the invocation directory.
Schema v9 only widens the operation journal's kind check to include
`sprint-start` and `sprint-close`; the v8-to-v9 migration copies the journal
unchanged. This version writes neither kind.

### Optional sprint aliases and schema v8

Numeric primary IDs, snapshot foreign keys, and `Sprint N` cycle names remain
unchanged. Schema v8 adds a nullable alias column with format checks and a
unique index. Aliases use 2–7 uppercase ASCII letters or digits and at most
one interior dash. Application validation also prevents numeric aliases from
colliding with another sprint's ID, including when a later sprint is created.
Alias writes validate under a write transaction and verify by readback before
commit. Replanning preserves an omitted alias; clearing requires
`alias --clear`.

CLI references resolve locally before board access. Outputs retain numeric
IDs and expose aliases separately. Sprint start also accepts an ID or alias,
using the bound cycle or the existing numeric cycle-name lookup. Explicit
cycle UUIDs remain supported.

The v7-to-v8 migration adds the column and index transactionally; existing
rows receive NULL aliases. Read commands refuse old schemas without changing
them. Explicit migration and ordinary writes follow the same upgrade path,
and retries are safe. Older binaries reject v8: stop old clients before
upgrading. For rollback, retain a pre-migration SQLite backup and restore it
with the old binary; doing so discards subsequent register writes. No live
register is migrated by the repository tests, which use temporary databases
and fake board clients to verify data preservation and reference resolution.

### Parallel sprint execution and schema v6

Schema v6 removes the `one_current_sprint` partial unique index on
`sprints(status) WHERE status = 'current'`, enabling multiple sprints to be
active concurrently on the same project when bound to distinct Plane cycles.

When multiple sprints are current:

- `sprints list` displays all current sprints under `Current sprints`.
- `sprints show` displays full detail tables for all current sprints (or for
  a single sprint if `SPRINT_ID` is specified).
- `sprints telemetry` requires an explicit `SPRINT_ID` argument to avoid
  ambiguity when more than one sprint is in progress.
- `sprints collect` routes card references to their owning current sprint
  automatically across active cycles, or accepts `--sprint SPRINT_ID` when
  targeting a specific current sprint without explicit card references.
- `sprints start` ensures each active sprint uses a unique Plane cycle ID,
  preventing accidental cycle collisions across concurrent sprint teams.

### Sprint timing and rework reporting

`show`, `list`, and `stats` aggregate local card snapshots, adding no network
requests. The latest capture instant per card wins even across timezone
spellings. Sprint reports merge closed and ongoing state residence up to capture
time, omit Done, and define Active as In Progress plus Verifying. Explicit
activity timers are reported separately; open timers stop at capture time. These
snapshots contain card history, not a reconstruction clipped to a sprint
boundary.

Completed-sprint timing statistics require complete final timing coverage and no
open timer. Rework coverage is checked independently: snapshots written before
`rework_count` existed do not imply zero. Summaries expose included/excluded
sprint counts; historical delivery metrics retain their original population.
Rework is the count of native Verifying → In Progress events, as recorded by the
Coordinator under the delivery skill's allowed-transition table.

Every send-back names its reason: `card transition --from Verifying --to
'In Progress'` requires `--reason` (`spec`, `test-gap`, `defect`,
`missed-gate`, or `environment`) and posts it as a visible
`plane-proj-rework/v1` comment, carrying the operation id, before the move.
A retry finds that comment and does not post a second. `card move` and
`card move-many` refuse a send-back, so none escapes without a reason.
Counting reasons shows which process change would remove the most rework.

Rework cost is the card's In Progress and Verifying time from its first
send-back until settled (or until capture while still active), and the timer
time after that instant, split by category; a timer that straddles the
send-back counts only its later part. Snapshots taken before these fields
existed leave the cost unknown, not zero: the rework time statistic uses
only completed sprints whose every ending card has a final snapshot with it.

Timing averages use only completed sprints with final timing observations for
all ending cards and no open timer. Rework averages independently require a
recorded count for every ending card. Missing old fields are unknown, not zero;
excluded sprint counts appear in a coverage table, including when no sprints
qualify. Existing delivery statistics still include legacy sprints. For included
sprints, an absent timing category contributes zero to that category's average.
Active and timer totals are sums across cards, so parallel work can exceed
sprint wall-clock duration. Summary Avg/Med/Min/Max/SD describe per-sprint
totals over qualifying completed sprints, not per-card averages. Med is the
upper middle value for an even population; SD is population standard deviation.
JSON retains the original closed `state_minutes`, `current_state_minutes` and
`execution_minutes` fields for compatibility. `residence_minutes` combines
closed and ongoing state intervals excluding Done; `active_minutes` selects In
Progress and Verifying. Summary statistics expose these new groups as well.

### Acceptance verdicts and the transition table

`card verdict CARD --role {qa,tech-lead} --result {pass,fail} --revision REV
--author NAME [--note TEXT] [--operation-id ID]` records one verdict as a
visible comment: the prefix `plane-proj-verdict/v1` and compact JSON with
`author`, `note`, `operation_id`, `result`, `revision`, and `role`. The
revision is a git commit hash of 7 to 40 lowercase hex characters. A verdict is
accepted only on a card in Verifying. The operation id is optional but should
be given: a retry with the same id and the same fields finds the earlier
comment and posts nothing, and the same id with different fields is refused.

The author is asserted by the caller through `--author`; it is not
authenticated, and the Plane actor that posts the comment is not compared with
it. The guard reads the comments as they stand: editing or deleting a comment
in Plane is outside it, so verdict comments are an audit trail only as far as
the workspace's own comment permissions keep them unchanged.

A comment whose text starts with the verdict prefix but does not parse refuses
under the Verdict rule, naming the comment id, rather than being skipped; so
does a verdict comment without a valid `created_at`. Until it is fixed, every
move into Done and every later `card verdict` on that card refuses. Recovery is
to delete or correct that comment in Plane and retry. `card comment` refuses a
body whose text starts with the verdict, execution (v1 or v2), or rework
prefix under the Structured comment rule, so a free comment cannot forge or
corrupt a record.

Two project rules use these records and the coordinator's transition table:

- `require_independent_verdicts` (IndependentVerdictRule,
  `MissingIndependentVerdict`): a move into Done through `card transition`,
  `card move`, or `card move-many` must come from Verifying, and requires the
  latest qa verdict and the latest tech-lead verdict, among those posted after
  the card last entered Verifying in Plane's state history, to both pass, name
  the same revision, and have different authors. An author must not issue its
  own acceptance verdict, and two passes on different revisions accept no
  single candidate. A verdict older than the last return to Verifying judged an
  earlier candidate, so it does not count.
- `require_transition_table` (TransitionTableRule, `TransitionNotAllowed`):
  the same three commands refuse any move outside the delivery skill's allowed
  card transitions, comparing state names case-insensitively. Sprint start and
  close, and cycle membership commands, are outside this rule; `sprints start`
  moves admitted cards to Todo and others to Backlog as a lifecycle step.

With either rule on, `card new --state` accepts only Backlog or Todo, so a card
cannot be created past the checked moves. Both rules refuse before the first
write. Both default to off, so an existing config keeps its behaviour;
`plane-proj init` writes both as on.

The "since it last entered Verifying" cutoff compares comment `created_at`
with the state activity's `created_at`, and assumes Plane writes the activity
for a move before any verdict posted after it. That ordering has not been
measured against a live server; a verdict stamped strictly after the entry
counts, and one stamped at the same instant does not.

### Guards enforce invariants, never sequencing

A guard refuses a state that must never exist: a Done card without verified
acceptance, a move outside the transition table, an open card in no sprint.
Each refusal happens before the first write, so the board never holds the
forbidden state even briefly. These are properties of every correct board,
whatever the delivery path that reached it.

The order of work is not such a property. Which card to dispatch next, when
to send a card back, how to classify discovered scope, and when to escalate
to the user are judgments, and they stay with the Coordinator agent following
the delivery skill. A scripted sprint engine that encoded those branches would
be rigid where delivery needs judgment, and it could fail on its own bugs in
ways a guard cannot, because a wrong branch produces a legal but wrong state.

Deterministic code is therefore reserved for four things: invariants checked
by guards, journaled transitions that a retry resumes without replaying, the
telemetry derived from them, and well-shaped fan-outs that return data, such
as `sprints ready`, with a manual fallback when they cannot run. The
[verdict rules](#acceptance-verdicts-and-the-transition-table) follow this
split: the CLI refuses an unverified Done, while choosing reviewers,
dispatching QA, and escalating after repeated failures stay with the agent.

### Declared card scope and dependency assessment

Two things a planner knows and the board cannot infer are declared on each
card: which files the card will change, and that its dependencies were
assessed. Without them, a sprint with no `blocked_by` relations is
indistinguishable from one whose relations were never entered, and two
sprints that change the same code look independent.

Both live in one section of the card's Plane description, so they travel
with the card and need no paid Plane feature:

```markdown
## Delivery plan
Touches:
- src/api/members.ts
- src/shop/pickup.ts (new)
Dependencies: assessed
```

- `Touches` lists repository paths relative to the root, with `/`
  separators. A path ending in `/` is a directory and covers everything
  below it. A file the card will create is listed by its intended path; the
  `(new)` mark is informational. A card that changes no code declares
  `Touches: none`.
- `Dependencies: assessed` records that someone checked what the card waits
  on. The dependencies themselves stay Plane `blocked_by` relations, the only
  copy; the marker never lists them. Assessed with no relations means the
  card is independent; no marker means unknown, whatever relations exist.
- A card without the section, or without one of its two lines, is
  undeclared in that respect. Undeclared cards are reported, never refused,
  so an existing board can be backfilled at any pace.

`card new --touches PATH` (repeatable, or `--touches none`) and
`--deps-assessed` write the section. `card plan CARD` replaces only that
section of an existing card and leaves the rest of the description as it
was. Both read the description back and parse the section from the stored
HTML before reporting success. With `require_delivery_plan` on, `card new`
refuses a card without both declarations before the first request; the rule
defaults to off and `plane-proj init` writes it as on.

#### Commits name their card

Every commit that implements a card carries a git trailer naming it, such as
`Card: PLANEPROJ-12`; a commit for two cards carries two trailers. A card's
change set is the union of files changed by the non-merge commits reachable
from a given revision whose trailers name it. This depends only on commit
content, not on branch names, so it holds for feature branches, trunk-based
work, rebases, and merges alike. A squash merge must keep the trailers in the
squashed message.

With `require_declared_scope` on, `card verdict --revision REV` refuses
(`ScopeRule`) when the card declares `Touches` and its change set from REV
contains a path the declaration does not cover, or when a `Touches: none`
card has any change. The fix is to correct the declaration with
`card plan`, which keeps the change of scope visible. An undeclared card is
not checked. The rule needs the project directory to be a git repository and
refuses without one rather than skip the check. It defaults to off and
`init` writes it as on. Git is run locally as a subprocess; it is not a
network access.

`sprints preflight` and `sprints close` add a scope report against
`defaults.integration_branch`, the shared branch that delivery merges into
(for example `dev`). For each card it lists paths outside its declaration,
and cards with no commit reachable from that branch yet, meaning unmerged
work. It also lists commits on the branch, made while the sprint ran, that
name no card. The report never refuses closure: merging may follow close,
and judging the findings stays with the Coordinator.

#### Sprint readiness

The Planned page and `sprints readiness` give each planned sprint one state,
using only cross-sprint facts; blockers inside a sprint are handled during
it. In precedence order:

| State | Meaning |
| --- | --- |
| Not ready | A card waits on an unfinished card in another sprint |
| Overlap | Shares a declared path with a running sprint, or with an earlier sprint in the queue that can also start |
| Unverified | Some card lacks a `Touches` declaration or the assessed marker |
| Can start | None of the above |

Two cards overlap when one declared path equals or lies under the other.
Overlap counts only between sprints that could run at the same time, so the
first of two overlapping planned sprints can start and the later one shows
the overlap. The sprints in Can start therefore never share code with each
other or with a running sprint, and can all start at once.

Times use the median velocity of completed sprints, or an assumed 3.5
points per hour, marked as such, when there is none. The serial time is all
planned points divided by velocity. The parallel time is the longest chain
of sprint durations along cross-sprint dependencies, with a running sprint
counting its remaining points; it is a lower bound that ignores overlap and
assumes enough agents.

## 7. Two server limits and one SDK defect

All three measured, none inferred.

| | Behaviour | Consequence |
|---|---|---|
| Estimates endpoint | 404 | Scale lives in the config file; `capture` tries the endpoint first and falls back, so an upgrade needs no change here |
| `relations/remove/` | 404 | Relations are created once the work item set is settled; a wrong edge is removed in the UI |
| `relations.list` (SDK) | `ValidationError` | Buckets typed `list[str]`; the server returns objects. `Board.relations` stops one layer short of the model |
| `expand=estimate_point` (SDK) | `ValidationError` | `estimate_point` typed `str \| None`; expansion returns an object. `Board._expanded_cards` goes raw |
| `work_items.update` (SDK) | **silent no-op** | Serialises `model_dump(exclude_none=True)`, so every field set to `None` is dropped before the request. Clearing a field is impossible through it |

That last one is the worst of the four, because it does not fail. Blanking an
estimate sent a PATCH with no fields, the server answered 200, and the tool
reported success over a work item that still read `point: 0`. The server accepts
`{"point": null}` perfectly well — this is the SDK's limit, not Plane's — so
`update_card` sends raw dicts, and `_verify_blank` exists because
`_verify_card` treats a `None` estimate as "nothing was asked for", which is
precisely the case that was silently doing nothing.

The relations GET returns `{"project_id", "issue_id"}` — **no `id`, no
`sequence_id`, no `name`**. A readback keyed on `id` finds nothing and reports
a write that in fact landed; this cost a debugging cycle here and is why
`Board.relations` normalises to plain ids and `cli._reference` resolves them
against a work item index.

### Rework observation (2026-09-16)

Read-only activity checks against a live production project found two Verifying
→ In Progress transitions for one card and four for another. Their stored
snapshots lacked `rework_count`, including captures through 11:58:46Z and
11:43:33Z respectively. The current computation returned 2 and 4; the stored
data needs recollection using the updated collector, not manual counts. The
Coordinator skill now requires collection after every state transition so sprint
reports do not remain stale.

### Sprint reporting observations (2026-09-16)

Read-only tests against a live production project using its own config and
sprint register: cycle listing completed in 1.0 seconds; the full sprint listing
completed in 4.0 seconds without a lock error. No intermittent hang was
reproduced. The cycle cards carried estimate UUIDs absent from that config's
scale, causing current and planned points to display zero. Sprint totals now
refuse unknown estimate IDs with a scale-refresh instruction instead of counting
them as zero. The CLI's live `show` combines persisted starting totals with
current cycle totals and Done counts; velocity uses Done points divided by
elapsed hours. After the user refreshed the estimate UUIDs, live current-sprint
detail showed 15 starting/current cards, 43 starting/current points, zero Done
cards/points, `00:27:47` elapsed and `0.00/h` velocity. Instrumenting the
Requests session counted exactly seven HTTP sends; the read completed in 1.7
seconds.

### Plane Cloud lifecycle observation (2026-09-17)

A live run against the disposable `PPT` project measured the project-features
endpoint reporting Cycles, Modules and Intake disabled. Existing cycles and
modules remained listable while their feature switches were off; Intake returned
an empty list. Updating the feature resource changed Cycles from false to true,
and immediate readback confirmed true. A fresh `plane-proj init` then wrote
`require_cycle: true`, while leaving Modules optional. The CLI does not
currently expose a feature-toggle command. Cycle writes now react only to
Plane's specific disabled-Cycles 400 response: they enable the feature through
the SDK, require readback showing it enabled, and retry the original write once.
A live create verified the false-to-true recovery and the probe cycle was
deleted afterward.

With a temporary config requiring cycles, a card creation without `--cycle` or
`--backlog` raised `MissingCycle`; the complete card listing contained 16 items
both before and after the refusal. Two subsequent cards omitted both estimates
and modules and completed a full sprint through Todo, In Progress, Verifying and
Done. Comment-based implementation and verification timers, transition
snapshots, final telemetry, sprint accounting and readbacks all succeeded while
estimate, module and Intake requirements were disabled.

The project estimate set was then temporarily unlinked without deleting it. A
fresh initialization detected estimates as disabled even though existing cards
still carried old estimate UUIDs. It wrote no scale mapping, disabled the local
estimate requirement, and card plus planned-sprint output omitted estimate and
velocity fields. The original estimate-set UUID was restored and verified after
each probe.

Cancellation after work started is also a settled path. An In Progress card may
move to Cancelled; collection marks the snapshot final while retaining completed
activity timers and the native state-residence interval. Sprint closure already
accepts either Done or Cancelled cards, and reports cancelled cards separately.

Before 0.5.16, one close-time edge was measured. `sprints close` accepted an
`--ended` instant 29 seconds in the future, updated the cycle end date, and then
Plane Cloud rejected archival with HTTP 400:
`Only completed cycles can be archived`.
The local sprint correctly remained current, but the Plane write had already
occurred. Retrying after the end instant passed archived the cycle and completed
the local sprint. Version 0.5.16 removed close-time archival; closure now ends
the cycle without changing its archive state or membership.

The two test cards, archived cycle and temporary Verifying state were deleted;
retrieval of each returned 404. Work-item cleanup required the SDK because the
CLI has no card deletion command. Cycles was deliberately left enabled, and a
post-cleanup `init` confirmed that state.

### Per-resource SDK sessions (measured 2026-09-29)

The Plane SDK gives each of its ~80 resource objects its own
`requests.Session`, so every resource a command touched opened a new
connection: DNS lookup plus TLS handshake. Measured against the self-hosted
server over Tailscale MagicDNS, each uncached lookup took about 0.45 s
(A 0.2 s, AAAA failing after 0.6 s), and `sprints list` spent 2.8 s of about
7 s in `getaddrinfo`. `discover` now points every resource at one keep-alive
session; `sprints list` took about 3.4 s after. Fetching the 18 planned
cycles concurrently was also measured and gave no further gain, so reads
remain sequential.

## 7a. Intake is a separate resource

`intake new` sends a nested `issue` request through the SDK's Intake create
method. It asks only for a title and description, leaving cycle, owner, and
estimate decisions to triage. The command retrieves the new Intake record by
its work-item ID and verifies its pending status, identity, title, and stored
HTML before reporting success. Intake creation never calls the ordinary project
work-item create endpoint.

Plane's ordinary project work-item collection excludes work items waiting in
Intake because an Intake record is not interchangeable with a work item. The
separate `Board.intake_items` path reads `/intake-issues/`, retaining the Intake
record's own id, status, source and lifecycle. `Board.cards` reads only the
project work-item collection. Self-hosted Plane was measured on 2026-09-14
including `issue_detail` by default but returning it as null when the SDK sends
`expand=issue_detail`. Intake reads therefore deliberately omit that expansion.
If a list item still lacks detail, `intake show` re-retrieves it through Intake
without expansion; it never asks the ordinary work-item endpoint for a Pending
item, because that endpoint returns 404 until the item is accepted.
Accepting or rejecting uses the SDK's ordinary Intake update, addressed by the
record's `issue` id rather than its Intake id, then retrieves that same record
and requires the requested status before the CLI reports success. Acceptance
also retrieves the ordinary project work item and verifies its id and sequence.
The SDK's dedicated
`update_status` method is unusable through Plane's PAT API: it adds a `/status`
subroute which that API answers with 404. The self-hosted server measured on
2026-09-14 silently returned HTTP 200 without changing status for a role-15 PAT;
its implementation applies Intake updates only for project roles greater than
15. The CLI's mandatory readback turns that silent permission failure into an
actionable error.

Detail commands expose `description_html` exactly as Plane returns it.
Converting descriptions to plain text destroys editor structure and silently
removes custom `image-component` nodes; listings may abbreviate titles, but full
card and Intake descriptions are lossless.

An `image-component`'s `src` is a Plane file-asset UUID. Measured on 2026-09-14,
plane-sdk 0.2.24's work-item attachment collection is empty for these inline
description images, and the SDK has no generic-assets resource for resolving the
UUID to a download URL. The CLI preserves the UUID but does not bypass the SDK
with a hand-written request to Plane's separate generic-asset endpoint.

Regular file attachments are separate from these inline image nodes. The
identifiers below are synthetic; the behavior and measurements are unchanged.
Measured on 2026-09-14 using Pending TEST-20 (work-item ID
`4d2a9f61-7b35-48c0-a6e4-1f93d8b572ac`): the SDK attachment-list endpoint
returned `image.png`, `image/png`, 65,589 bytes, attachment ID
`6c8f31a5-9e72-4d04-b153-2a7d9f46c820`. `intake show` now includes this
metadata, including the ID accepted by `intake attachment`. Attachment lookup
uses the Intake record's issue ID without retrieving an ordinary project card.

The SDK's `get_download_url` obtained a redirect for that Pending item's
attachment; the CLI downloaded 65,589 bytes with content type image/png.
Downloads stream through a separate unauthenticated requests session with
ambient authentication/proxies disabled. A temporary file in the destination
directory is published using a no-overwrite hard link only after the stream
completes; failures remove temporary files. Storage URLs are not printed.

Rechecked on 2026-09-14 after TEST-20 became an ordinary Backlog card:
`card show TEST-20` listed the same attachment, and `card attachment`
downloaded 65,589 bytes with a valid PNG signature. Card attachment commands
use the work-item ID directly, without requiring an Intake record.
Both detail commands render the same named attachment fields; downloads use
`intake attachment` or `card attachment`, each with `--out`.

## 7b. The workspace slug

The slug is the only entry in the config file that `project capture` can never
recover: every Plane API path is `/api/v1/workspaces/{slug}/…`, so it is needed
before any call can be made, and nothing returns it — `get_user` has no
workspace and the projects listing gives a workspace UUID.

It resolves from `defaults.workspace` first, then `PLANE_WORKSPACE_SLUG`
from the credential sources described in §3. The saved estimate UUIDs belong
to that workspace's project, so the config selects where they are valid.

## 7c. Listing scope and archived work items

Measured through the CLI against example-workspace on 2026-09-14: project
listing returned 4 projects, including 2 with `archived_at` set. The default
listing filters those out; `projects --all` retains them and labels them
Archived. Filtering does not affect project discovery for other commands.

Default work-item display filters Done, Backlog, Cancelled, and `archived_at`
after reading all pages. Internal card reads used by rules-check and other
commands retain their existing scope. An explicit `--state` shows that state
even when it is hidden by default, but never archived items; only `--all` reads
those. Requiring `--all` for `--state Backlog` left no way to list Backlog
cards on a server whose archived route returns 404.

The SDK's archived-card route
`/api/v1/workspaces/{slug}/projects/{id}/archived-work-items/` returned 404
for DEMO. `card list --all` therefore fails explicitly on this installation.
On servers supporting that route, it combines both paginated collections,
deduplicating by work-item ID. Other HTTP errors are propagated unchanged.
This avoids presenting an incomplete archive listing as complete.

Upstream's
[work-item view](https://github.com/makeplane/plane/blob/master/apps/api/plane/api/views/issue.py)
uses `Issue.issue_objects`; its
[manager](https://github.com/makeplane/plane/blob/master/apps/api/plane/db/models/issue.py)
excludes archived items. The ordinary listing is therefore not assumed to be a
substitute for the missing archive endpoint.

## 7d. Bounded batch state moves

Inspected on 2026-09-14: Plane's public API documents a PATCH only for one
work-item ID, and the installed `plane-sdk` 0.2.24 `WorkItems` resource exposes
only its corresponding per-item `update`; neither exposes a generic atomic bulk
work-item update. Internal application endpoints are not a supported API-key
surface and are not used.

`card move-many` therefore batches at the CLI boundary. One paginated card
listing resolves and preflights the entire selection before any mutation. Each
selected card then receives the supported PATCH and its own mandatory readback.
The operation requires the expected source state, refuses duplicate or stale
references before writing, and caps selection at 20 cards.

With single-page metadata and card listings, startup uses six requests, card
selection uses one, and each move uses two: a full batch consumes 47 requests.
The ceiling leaves 13 requests below Plane's user-specified 60-request rolling
minute limit for pagination and bookkeeping. The operation is not atomic: an
HTTP failure can leave the already read-back prefix moved, so callers must
inspect current state before retrying.

## 8. Errors

One boundary, in `cli.main`. A `GuardViolation` prints its rule and exits 1 —
the message names the rule and what to do instead, because the caller is
usually an agent that cannot go and read the sizing document. An `HttpError`
prints its status, because a 404 from this API usually means "your server does
not have that endpoint" rather than "the thing you asked for is missing".

## 8a. Execution telemetry without paid worklogs

Plane activity is the authority for lifecycle history. State-change activities
carry the server timestamp, actor, and old and new values, so the CLI derives
state residence from the history instead of duplicating timestamps in custom
properties. Raw state names remain project data. Sprint Active reporting selects
the workflow states In Progress and Verifying; Done residence is omitted.

Explicit activity timing is a different measurement from state residence.
Community Edition has no worklogs, so timer boundaries use visible work-item
comments prefixed by `plane-proj-execution/v1` and a minimal JSON object
containing only `action` and `category`. Comments provide server timestamps,
actors, portability, and an append-only audit trail without another database.
One open timer per card makes durations unambiguous. Arbitrary lowercase-slug
categories keep the mechanism generic while reports group exact category values
rather than guessing intent.

Custom properties are not used as an event store: they are typed current values
attached to work-item types, overwrite prior values, and require
property-specific requests to retrieve. They are unsuitable for repeated
temporal observations and consume scarce API requests without improving the
audit trail.

`card_execution_snapshots` persists the complete derived statistics document
with the sprint, work-item identity, capture timestamp, and finality marker. The
key retains multiple observations of a card so early sprint reports do not erase
the final measurement. Raw Plane activities and comments remain authoritative;
SQLite is the durable analysis register and can be refreshed from those sources.

A collection reads a card's activities and comments first and the clock second,
keeping full precision for the statistics cutoff. Plane stamps the state move a
transition has just written with sub-second precision. A cutoff read before that
telemetry, or truncated to whole seconds, precedes the move and trips the
`Statistics cutoff precedes recorded execution telemetry` guard. Sprints 18, 28,
29, and 31 recorded that failure on almost every first transition attempt and
success on an immediate retry. The register still stores the capture label in
whole seconds.

## 8b. Read-only web view

`sprints web` serves one static page (`web.html`, packaged with the module)
and two JSON routes from `web.py`:

- `/api/sprints` returns the `sprints list --json --all` payload, built by the
  same `_sprint_listing` code path, plus the project key and name, the Plane
  board link, and each current cycle's cards (reference, title, state,
  points) from `Board.sprint_cycle_cards`. Every request reads afresh.
- `/api/sprints/local` returns the same payload from the register alone,
  marked partial, without project name, board link, cards, planned totals, or
  live current counts. It answers in milliseconds where the full payload takes
  seconds (measured 2026-10-05 against a live register: 0.08 s against 3.4 s,
  most of it one request per planned cycle). The page paints it first, with
  shimmering placeholders the same size as the Plane-only elements they stand
  for, shows "Updating from Plane…", and swaps the data into their places when
  the full payload arrives, so the layout does not move. Later
  refreshes keep the current view until the full payload arrives, so the page
  never falls back to placeholders.
- Each current card carries its blockers' references and states, read
  through `Board.dependency_facts` for the current cycles only, so a refresh
  costs one relations request per open current card.
- `/api/readiness` returns the `sprints readiness` report: each planned
  sprint's state and reasons, overlapping card pairs, the sprint dependency
  graph, and the serial and parallel times. It reads every open card's
  relations and description, so the Planned page requests it on opening and
  shows placeholders until it arrives.
- `/api/version` returns the register file's (and WAL's) modification time
  and size. The page polls it every 2 seconds and refetches `/api/sprints`
  only when it changes, so an idle page makes no Plane requests. A change made
  only in Plane appears with the next register write, such as a `collect`
  after a transition, or a reload.

The server is the standard library's single-threaded `HTTPServer`: no new
dependency, and board reads never run concurrently. It serves inbound
requests only; outbound Plane access still happens only in `board.py`. It
never writes the register or the board, and the payload carries no
credentials. The default bind address is loopback.

Plane links use `defaults.web_url` from `plane-proj.json`, which `init` writes
as the Plane Cloud web app, `https://app.plane.so`; a missing key means the
same. The API host (`PLANE_API_HOST_URL`) is not used because on Plane Cloud
it is not the web host. A self-hosted project sets its own address, and
`--plane-url` overrides it for one run. The board
link is `<web>/<workspace>/projects/<project id>/issues/`; a card link appends
the work-item UUID. A sprint links to its cycle at
`<web>/<workspace>/projects/<project id>/cycles/<cycle id>`, the route Plane's
web app defines (read from its source on 2026-10-05). Neither link form has
been measured against every Plane version.

## 9. Secrets

The API key is carried in `Secret`, whose `__repr__` and `__str__` redact, from
the environment to the single `reveal()` in `board.connect`. `Secret` is
neither a dataclass nor a `str` subclass; both would print the value through
some path nobody asked for.

The tests assert the redaction marker is **present** before asserting the value
is absent — otherwise a test passes against an object that rendered nothing at
all.

---

**This file is:** Design rationale and specifics for plane-proj.
**Form:** Specification. No history, no status. Never any agent hand-offs.
