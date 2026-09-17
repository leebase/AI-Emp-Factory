"""Pure failure-class → next-action mapping (D211 ruling 6).

One deterministic, side-effect-free decision per failure: given an
observed ``failure_class`` (the closed five-value vocabulary of
attempt-record v2 — ``platform_timeout``, ``platform_env``,
``spec_rejected``, ``capability_rejected``, ``unaccounted_spend``, with
JSON ``null`` meaning "no failure recorded") the function returns the
next staff action:

* ``platform_*`` → ``retry_same_route_after_platform_repair``
  (prefix match: every platform-surface class shares one repair — fix
  the platform, then re-try the identical route);
* ``spec_rejected`` → ``return_to_planner``;
* ``capability_rejected`` → ``repair_same_route`` on the first
  capability attempt, ``escalate`` on the second and every subsequent
  one (an explicit 1-based attempt number, or the word context
  ``"first"``/``"second"``, is required evidence — the mapping never
  guesses a count);
* ``unaccounted_spend`` → ``reconcile_then_retry``;
* anything else — an unknown or misspelled class, wrong casing,
  surrounding whitespace, a non-string, ``None``, or a
  ``capability_rejected`` whose attempt context is missing or invalid —
  fails closed to ``supervisor_judgment``.

Boundaries, fixed so callers never re-derive them:

* Matching is exact and case-sensitive. The platform branch matches the
  literal lowercase prefix ``"platform_"``; ``"platform"`` alone,
  ``"platform-foo"``, ``"PLATFORM_TIMEOUT"``, and ``" platform_timeout"``
  are all unknown classes, because no named source proves them and
  invented leniency here would silently authorize a retry the evidence
  does not support.
* Only the exact class ``capability_rejected`` consumes the capability
  context. A ``capability_*`` prefix other than the exact class is
  unknown, not a capability attempt.
* A valid capability context is an ``int`` (never ``bool``) ``>= 1``, or
  exactly the strings ``"first"``/``"second"`` (lowercase). ``None``,
  ``0``, negatives, floats (``1.0``), numeric strings, ``"third"``,
  capitalized words, and booleans are invalid: with an invalid or
  missing count the first/subsequent distinction cannot be made, so the
  action fails closed to ``supervisor_judgment`` rather than picking a
  repair or an escalation. There is no ``"third"`` word: counts past the
  second are expressed as integers (``3``, ``4``, … → ``escalate``).
* The capability context is consulted only for ``capability_rejected``;
  every other class maps identically whether or not a context is
  supplied.

The function is pure: it reads no state, performs no I/O, dispatches
nothing, mutates nothing, and never raises for decision inputs — every
input yields exactly one of the six action strings.
"""

from __future__ import annotations

__all__ = [
    "ESCALATE",
    "FIRST_CAPABILITY_ATTEMPT",
    "PLATFORM_FAILURE_PREFIX",
    "RECONCILE_THEN_RETRY",
    "REPAIR_SAME_ROUTE",
    "RETURN_TO_PLANNER",
    "RETRY_SAME_ROUTE_AFTER_PLATFORM_REPAIR",
    "SECOND_CAPABILITY_ATTEMPT",
    "SUPERVISOR_JUDGMENT",
    "next_action",
]

#: Platform-surface failures share one action: repair the platform, then
#: re-try the identical route (D211 ruling 6, ``platform_*`` prefix).
PLATFORM_FAILURE_PREFIX = "platform_"

#: Exact lowercase word context naming the first capability attempt.
FIRST_CAPABILITY_ATTEMPT = "first"

#: Exact lowercase word context naming the second capability attempt.
SECOND_CAPABILITY_ATTEMPT = "second"

#: Any platform-surface failure: repair the platform, retry the same route.
RETRY_SAME_ROUTE_AFTER_PLATFORM_REPAIR = "retry_same_route_after_platform_repair"

#: The specification itself was rejected: back to the planner.
RETURN_TO_PLANNER = "return_to_planner"

#: First capability rejection: one repair attempt on the same route.
REPAIR_SAME_ROUTE = "repair_same_route"

#: Second or subsequent capability rejection: escalate to the supervisor.
ESCALATE = "escalate"

#: Spend the ledger cannot account for: reconcile the accounting, then retry.
RECONCILE_THEN_RETRY = "reconcile_then_retry"

#: Fail-closed action for every unclassifiable input, including null.
SUPERVISOR_JUDGMENT = "supervisor_judgment"

#: The closed five-value ``failure_class`` vocabulary of attempt-record v2.
_KNOWN_FAILURE_CLASSES = frozenset(
    {"spec_rejected", "capability_rejected", "unaccounted_spend"}
)

#: Word context → the 1-based attempt number it names.
_CAPABILITY_WORD_ATTEMPTS = {
    FIRST_CAPABILITY_ATTEMPT: 1,
    SECOND_CAPABILITY_ATTEMPT: 2,
}


def _capability_attempt_number(context: int | str | None) -> int | None:
    """Normalize the explicit capability context to a 1-based count.

    Args:
        context: The caller's explicit first/second capability context: a
            1-based ``int`` attempt number (``1`` = first, ``2`` = second,
            ``3+`` = subsequent) or the exact lowercase word ``"first"`` or
            ``"second"``.

    Returns:
        The normalized positive attempt number, or ``None`` when the
        context is missing or invalid — the caller then fails closed to
        :data:`SUPERVISOR_JUDGMENT`.
    """
    if isinstance(context, bool):
        return None
    if isinstance(context, int):
        return context if context >= 1 else None
    if isinstance(context, str):
        return _CAPABILITY_WORD_ATTEMPTS.get(context)
    return None


def next_action(
    failure_class: str | None,
    capability_attempt_number: int | str | None = None,
) -> str:
    """Map one observed failure class to its next staff action (D211 ruling 6).

    Args:
        failure_class: The attempt's ``failure_class`` value — one of the
            five closed-vocabulary strings, JSON ``null`` when no failure
            was recorded, or any other value the source produced.
        capability_attempt_number: Required only for the exact class
            ``capability_rejected``: the explicit 1-based capability
            attempt number (``1`` = first attempt, ``2+`` = second or
            subsequent), or the exact word ``"first"``/``"second"``. Any
            other value — including ``None``, ``0``, negatives, floats,
            booleans, or an unrecognized word — is invalid and fails
            closed. Ignored for every other failure class.

    Returns:
        Exactly one action string:
        :data:`RETRY_SAME_ROUTE_AFTER_PLATFORM_REPAIR`,
        :data:`RETURN_TO_PLANNER`, :data:`REPAIR_SAME_ROUTE`,
        :data:`ESCALATE`, :data:`RECONCILE_THEN_RETRY`, or
        :data:`SUPERVISOR_JUDGMENT`. The mapping never dispatches,
        mutates state, or raises for a decision input.

    Examples:
        >>> next_action("platform_env")
        'retry_same_route_after_platform_repair'
        >>> next_action("capability_rejected", 1)
        'repair_same_route'
        >>> next_action("capability_rejected", "second")
        'escalate'
        >>> next_action(None)
        'supervisor_judgment'
    """
    if not isinstance(failure_class, str) or not failure_class:
        return SUPERVISOR_JUDGMENT

    # Surrounding whitespace is not part of any named class: the value
    # must already be exact (equal to its stripped form). The original is
    # matched unmodified — no normalization, no trimming.
    if failure_class != failure_class.strip():
        return SUPERVISOR_JUDGMENT

    if failure_class.startswith(PLATFORM_FAILURE_PREFIX):
        return RETRY_SAME_ROUTE_AFTER_PLATFORM_REPAIR
    if failure_class not in _KNOWN_FAILURE_CLASSES:
        return SUPERVISOR_JUDGMENT

    if failure_class == "capability_rejected":
        attempt = _capability_attempt_number(capability_attempt_number)
        if attempt is None:
            return SUPERVISOR_JUDGMENT
        return REPAIR_SAME_ROUTE if attempt == 1 else ESCALATE
    if failure_class == "spec_rejected":
        return RETURN_TO_PLANNER
    return RECONCILE_THEN_RETRY
