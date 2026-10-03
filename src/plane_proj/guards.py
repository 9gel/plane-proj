"""The board rules, as exception types.

A guard here raises. It does not warn, it does not fall back to a default, and
it does not correct the argument for you. Each one corresponds to a way a board
has actually been damaged by a tool that did one of those three things, and a
warning would have been scrolled past exactly as the original miss was.

Every guard carries the rule it enforces in its message, not just the value it
rejected: the caller is usually an agent that has never read the config file.
"""

from __future__ import annotations


class PlaneProjError(Exception):
    """Base of everything this tool raises deliberately."""


class ConfigError(PlaneProjError):
    """The tool was not told something it cannot proceed without.

    Credentials, the config file, or which project to act on. Never a board
    rule — those are `GuardViolation`.
    """


class GuardViolation(PlaneProjError):
    """A rule about the board was broken."""


class MissingCycle(GuardViolation):
    """A card was planned without naming the cycle it joins.

    Creating a work item puts it in no cycle: the create call has no cycle
    field, and membership is a second request against a card that does not
    exist until the first one returns. Nothing about a successful create makes
    the omission visible, so cards created mid-sprint stay invisible in the
    burndown until somebody audits by hand.
    """


class OrphanedCard(GuardViolation):
    """An open card would belong to no planned or current sprint.

    With cycles on, every card that is not Done or Cancelled belongs to a
    sprint, even a planned one-card sprint whose scope grows later. A backlog
    of cards in no sprint accumulates work nobody plans, finishes, or
    cancels, and it grows unnoticed because nothing ever has to close it.
    """


class EmptyCycle(GuardViolation):
    """A sprint was started on a cycle with no card to deliver.

    Opening totals are read from the cycle at start. An empty cycle opens at
    zero cards and points, so every card added afterwards reads as scope
    growth and velocity counts work the sprint never planned. Cancelled
    members are not admitted scope, so a cycle of only cancelled cards is
    empty too.
    """


class UnknownModule(GuardViolation):
    """A card named a module the project does not have.

    Every card carries a module, so an unresolvable name is a rule broken
    rather than a lookup that missed. Adding a module is a change to the config
    file, never to code.
    """


class MissingModule(GuardViolation):
    """A card was planned without a module, on a project that requires one."""


class MissingEstimate(GuardViolation):
    """A card for someone on the team was planned with no estimate.

    A card with no estimate counts in no total and no velocity, which is the
    same invisibility as a card outside the cycle.
    """


class EstimateOnUnestimatedAssignee(GuardViolation):
    """A card was given an estimate whose assignee takes none.

    Sizing these inflates the sprint with work the team is not doing. Passing
    an estimate is an error rather than a value quietly dropped, because a
    caller who passed one believed it would be recorded.
    """


class UnknownEstimateValue(GuardViolation):
    """An estimate was given that is not a point on the project's scale.

    Rounding to the nearest point would silently record a size nobody chose.
    """


class EstimateTooLargeForCycle(GuardViolation):
    """A card above the cycle ceiling was admitted for execution.

    Above the ceiling the number is no longer a size; it says the size is
    unknown. Such a card may wait in Backlog inside a planned sprint, but it
    never enters Todo or a later active state: the answer is to split it,
    never to choose a larger number.
    """


class CrossCycleDependency(GuardViolation):
    """A card in a cycle was blocked by a card outside it.

    The blocker cannot be worked in this cycle, so the blocked card cannot
    complete in it either, and the commitment is false the moment it is made.
    """


class ScaleContradiction(GuardViolation):
    """Captured board facts disagree with themselves.

    Raised by capture, never by a write. One point value carrying two different
    UUIDs — or one UUID under two values — means the recovered mapping cannot
    be trusted, and guessing which card is right would put the wrong UUID in
    the config file permanently.
    """


class ReadbackFailed(GuardViolation):
    """A write was accepted and the readback does not show it.

    Reported as a failure rather than a success with a caveat: a tool that says
    it set a field it did not set is worse than one that fails, because the
    board and the transcript now disagree and only the board is real.
    """
