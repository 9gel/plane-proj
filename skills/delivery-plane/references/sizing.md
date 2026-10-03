# Sizing and Splitting

## 1. Boundary

These rules apply to delivery in any language, medium, or technical stack. Use
the project's configured estimate scale and calibration. Project-specific
calibration SHOULD be named `CARD-SIZING.md` so it is discoverable without
introducing another work-item term.

Sizes 1, 2, and 3 are normally executable. Size 5 normally represents unsplit
scope and SHOULD be split. It MAY enter the current sprint only under the rare
exception in §3. A size-8-or-higher card represents unsplit or unresolved
scope and MUST NOT enter the current sprint or be dispatched.

Estimate the card as written, independent of worker identity, elapsed time,
token use, or prospective parallelism. Human-owned decision cards remain
unestimated.

## 2. General Calibration

Evaluate three properties: deliverable breadth, unresolved uncertainty, and the
clarity of the acceptance boundary.

- **1:** one localized, already-understood change; no new design decision;
  existing verification decides completion.
- **2:** one bounded deliverable; inputs and approach are known; completion has
  a clear verification path.
- **3:** one bounded deliverable with material breadth or uncertainty; the card
  still states a coherent approach and independently decidable completion.
- **5:** normally multiple deliverables or unusually broad work. It is
  executable only when it satisfies the rare exception in §3.
- **8 or higher:** multiple deliverables, an unresolved external decision, an
  unknown approach, or acceptance criteria that cannot yet decide completion.

File count, line count, programming language, worker speed, and implementation
phase MUST NOT determine size. They MAY be evidence of breadth only when the
project's calibration demonstrates that relationship.

Local calibration MAY refine the distinctions among sizes but MUST NOT make size
5 routine or raise the exceptional executable ceiling above 5.

## 3. Size-5 Exception

A size-5 card MAY be admitted only when all of these conditions hold:

- It is one understood, independently valuable deliverable with decisive
  acceptance criteria and no unresolved external decision.
- Every plausible split would leave an invalid, unusable, or undeliverable
  intermediate system rather than an independently valuable result.
- The size is caused by indivisible breadth, not urgency, worker availability,
  file count, implementation phase, or reluctance to refine the work.
- The Tech Lead documents why the candidate splits fail and approves the
  exception before admission.
- The Coordinator records that rationale on the card and confirms that project
  configuration permits size 5. A configured ceiling MUST NOT be bypassed.

If any condition is absent, split the card. The exception is expected to be
rare and MUST NOT become the project's normal executable size.

## 4. Splitting

Split by independently valuable deliverable, never by execution phase. Code and
its tests remain together. Every resulting card needs its own decisive
acceptance criteria; express required order through tracker dependencies.

Do not resize an admitted card merely because it became difficult. If another
deliverable appears, narrow the original to its admitted objective and create a
separately sized related card.
