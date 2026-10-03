# Verification and Integration

## 1. Independence

The author MUST NOT issue the independent acceptance verdict. Documentation
requires independent wording, reference, and instruction-consistency review.
Executable changes require independent code review and behavior verification.

## 2. Delivery Sequence

1. The Implementer passes focused changed tests and the complete ordinary suite.
2. The Tech Lead reviews the candidate, test design, acceptance criteria, and
   evidence.
3. The Tech Lead integrates accepted changes.
4. Freeze the merged tree and environment.
5. The Tech Lead, or a named independent verifier, runs the complete required
   merged-tree gates and targeted acceptance challenges.
6. The Tech Lead records a verdict for each card; the Coordinator closes
   accepted cards.

When review requires rework, the Tech Lead sends the defect and verdict to the
Coordinator. Before correction resumes, the Coordinator records the review
send-back under [the rework transition rule](coordinator.md); the Tech Lead does
not change tracker state.

On failure, preserve the result, return the defect for correction, pass focused
checks, then repeat the complete gate on the corrected merged tree. Evidence
MUST identify the revision, clean-tree state, environment, commands, and actual
results.

Where an authority lists gates as a set, evidence MUST report every gate in the
set; a missing gate is invisible when the reported ones are green. Name each
test lane by its canonical command and collected count; do not call a default
lane "full".

Blind re-verification receives the specification and test inputs without prior
verdicts or mutation logs. It records its verdict before inspecting earlier
reports.

## 3. Acceptance Instruments

Before implementation, the Tech Lead MUST record an acceptance matrix with
columns: behavior, representative input, observable assertion, instrument,
negative challenge, and execution stage. Where mutation testing is required,
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
