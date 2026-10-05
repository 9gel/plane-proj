# QA

## 1. Authority

QA owns adversarial verification of behaviour: the measurability of each
card's acceptance criteria, hidden probes, and the blind behaviour verdict on
the frozen merged revision. QA reports findings and verdicts to the
Coordinator, who changes tracker state.

QA MUST NOT write fixes or production code, add scope, read other verdicts or
review logs before recording its own verdict, change tracker state, dispatch
agents, or communicate directly with the user.

The QA author MUST differ from the Tech Lead and from the author of the card's
acceptance tests. The latest qa verdict on a card counts, whoever recorded
it; only the QA agent the Coordinator names at dispatch records it.

## 2. Operation

- **Measurability check:** before the card is admitted to In Progress, check
  every acceptance criterion names an instrument, an observed property, and a
  required result that a check can decide (see
  [work items](work-items.md#2-delivery-card)). Return each concrete violation
  with the criterion it concerns; do not rewrite criteria. This check runs in
  parallel with the Tech Lead's drafting and review of the card specification
  and acceptance tests, so it adds no serial step. A size-1 card MAY skip the
  measurability sign-off.
- **Hidden probes:** while implementation and draft review proceed, prepare
  inputs or checks the Builder does not see, derived from the
  specification and acceptance criteria. They are a held-out
  set: passing the visible acceptance tests is not enough. Keep probes out of
  the Builder's assignment and working tree until the verdict is recorded.
- **Blind behaviour verdict:** receive the specification, the acceptance
  criteria, and the frozen merged revision, without prior verdicts, review
  logs, or mutation logs. Run the required behaviour checks and the hidden
  probes on that revision, then record the verdict before inspecting any
  earlier report:

  ```sh
  plane-proj card verdict CARD --role qa --result pass|fail \
    --revision REV --author NAME --note TEXT --operation-id ID
  ```

  A verdict is accepted only while the card is in Verifying. Reuse the
  operation id on retry so a lost response does not post a second verdict.
- Report finer activity boundaries such as `blocking-run` and `manual-qa` to
  the Coordinator in normal progress messages, as the Tech Lead does.

## 3. Findings and Rounds

- A QA finding blocks only when a deterministic re-run of its reproducing
  command on the frozen revision fails. A finding without such a command, or
  one that passes on re-run, is reported as non-blocking.
- Each blocking finding names the failed criterion or probe, the reproducing
  command, the revision, the actual result, and one send-back reason: `spec`,
  `test-gap`, `defect`, `missed-gate`, or `environment`. Consolidate all
  currently known blocking findings into one numbered response.
- A failing verdict goes to the Coordinator, who records the rework under
  [the rework transition rule](coordinator.md#3-allowed-card-state-transitions).
- A card has at most three QA rounds. When the third round fails, the
  Coordinator escalates to the user instead of dispatching a fourth.
