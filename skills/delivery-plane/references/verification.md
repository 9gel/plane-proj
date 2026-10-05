# Verification and Integration

## 1. Independence

The author MUST NOT issue the independent acceptance verdict. Documentation
requires independent wording, reference, and instruction-consistency review.
Executable changes require independent code review and behavior verification.

A card's acceptance verdict has two parts, both recorded with
`plane-proj card verdict` while the card is in Verifying:

- a [QA](qa.md) verdict (`--role qa`) on behaviour, issued blind on the frozen
  merged revision; and
- a [Tech Lead](tech-lead.md) verdict (`--role tech-lead`) on design fit:
  architecture, maintainability, and integration correctness.

A card enters Done only when the latest QA and Tech Lead verdicts recorded
since it last entered Verifying both pass, name the same revision, and have
different authors. The QA author MUST differ from the Tech Lead and from the
author of the acceptance tests. Builders never record verdicts.

## 2. Delivery Sequence

Expensive gates run once, after the judgment that is most likely to reject a
candidate. Review that rejects on design or specification grounds MUST NOT
follow a long suite or pipeline run that it makes worthless.

1. Before implementation, the Tech Lead writes executable acceptance tests for
   the acceptance matrix rows that a test can decide. The Builder MUST NOT
   weaken or delete them; a change to one needs the Tech Lead's approval.
   In parallel with this draft, QA checks that every acceptance criterion is
   measurable; the card enters In Progress only after the Tech Lead resolves
   QA's violations. A size-1 card MAY skip this sign-off.
2. The Builder loops locally until the acceptance tests and focused
   changed tests pass, then requests a draft review.
3. Draft review: the Tech Lead reviews the diff, design, test design, and
   focused evidence. A rejection here sends the work back before any complete
   suite or full pipeline run. Meanwhile QA prepares hidden probes the
   Builder does not see.
4. After draft review passes, the Builder passes the complete ordinary
   suite and hands off for independent verification.
5. The Tech Lead integrates accepted changes. Several accepted cards MAY be
   integrated before the next step, so one merged-tree run covers them.
6. Freeze the merged tree and environment.
7. QA runs the complete required merged-tree gates, the acceptance
   challenges, and its hidden probes on the frozen revision, and records its
   behaviour verdict blind.
8. The Tech Lead reviews the same revision for design fit and records its
   verdict. When both verdicts pass, the Coordinator closes the card.

When review or a verdict requires rework, the Tech Lead or QA sends the
defect, the verdict, and its reason (`spec`, `test-gap`, `defect`,
`missed-gate`, or `environment`) to the Coordinator. Before correction
resumes, the Coordinator records the send-back under
[the rework transition rule](coordinator.md#3-allowed-card-state-transitions);
neither the Tech Lead nor QA changes tracker state. Verdicts recorded before
the card returns to Verifying judge an earlier candidate and do not count.

On failure, preserve the result and return the defect for correction. The
corrected candidate passes its acceptance tests and focused checks, then draft
review, before any expensive gate runs again; the complete gate then repeats
on the corrected merged tree. Evidence MUST identify the revision, clean-tree
state, environment, commands, and actual results.

Where an authority lists gates as a set, evidence MUST report every gate in the
set; a missing gate is invisible when the reported ones are green. Name each
test lane by its canonical command and collected count; do not call a default
lane "full".

QA owns blind re-verification. It receives the specification and test inputs
without prior verdicts, review logs, or mutation logs, and records its verdict
before inspecting earlier reports.

## 3. Acceptance Instruments

Before implementation, the Tech Lead MUST record an acceptance matrix with
columns: behavior, representative input, observable assertion, instrument,
negative challenge, and execution stage. The execution stage MUST be the
cheapest stage that can fail the row: a unit or focused test, a fixture-scale
run of a pipeline on constructed inputs, or the full pipeline on the merged
tree. Card-level loops use the cheapest stages; full runs belong to the
merged-tree gate. Where mutation testing is required,
distinguish production-code mutation from corrupted-input testing. Neither
substitutes for the other. New safety findings MAY add criteria, but MUST
include the concrete newly discovered risk; do not silently redefine the
delivery contract during successive reviews.

Before offering a passing check as evidence, state what change it would fail
on; a check that no plausible defect fails is not evidence. A test that a guard
rejects something MUST be shown to fail when the rule it tests is deleted.
Where the definitions supply an exact inventory of outputs, checks, or
identities, assert that exact set; "non-empty" is not a substitute. A numeric
criterion MUST name its command, starting environment state, and sample count;
an effect within run-to-run spread is reported, not gated on.

Review handoffs SHOULD consolidate all currently known blocking findings into
one numbered response. Each finding MUST name the failed criterion, evidence,
and required correction. New findings remain reportable; consolidation MUST
NOT suppress a later-discovered defect.

## 4. Performance Instrument Qualification

Before the full capture, demonstrate that the action changes the intended
state and the completion marker observes the user-visible result. Record cold
and warm definitions, sample counts, units, aggregation, and exact budget
semantics. A per-task limit MUST NOT be implemented as a sum-of-tasks limit.
Startup work MUST remain inside startup measurement. Relative comparison
MUST reject mismatched input, fixture, or measurement identities; report
absolute results separately rather than disabling comparison guards.

## 5. Evidence Reuse

Reuse requires the same complete affected-project tree and relevant execution
environment, plus command, exit status, suite scope, and retained output.
Identical log hashes alone MUST NOT establish code or environment equality.

A pipeline stage's result MAY be reused across a rework when the project
records, for that stage, content hashes of its code, configuration, and inputs,
and every hash is unchanged. Rerun every stage whose hashes changed and every
stage downstream of one. Without such records, rerun the complete gate.
