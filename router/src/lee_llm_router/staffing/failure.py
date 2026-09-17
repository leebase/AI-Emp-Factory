"""Deterministic failure classification and judgment validation (D213 ruling 3).

Authority: D213 ruling 3 in docs/staffing/phase3-contracts.md:
"3. Evidence classification. Deterministic classes are platform_timeout,
platform_env, unaccounted_spend, oracle_failed, and unknown.
spec_rejected and capability_rejected enter only through explicit --judgment
after review, never inferred from prose."

Platform environment patterns are copied with an inline source citation to
/home/lee/projects/agent-orch/src/agent_orch/failure_classification.py.
Judgment values must require explicit judgment and a review verdict; they are
never inferred from text.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

__all__ = [
    "ALL_FAILURE_CLASSES",
    "AUTH_PATTERNS",
    "CAPABILITY_REJECTED",
    "DETERMINISTIC_FAILURE_CLASSES",
    "FAILURE_CLASSES",
    "FailureClassificationError",
    "JUDGMENT_FAILURE_CLASSES",
    "LOGGED_OUT_PATTERNS",
    "MISSING_BINARY_PATTERNS",
    "ORACLE_FAILED",
    "PERMISSION_PATTERNS",
    "PLATFORM_ENV",
    "PLATFORM_ENV_EXIT_CODES",
    "PLATFORM_ENV_PATTERNS",
    "PLATFORM_TIMEOUT",
    "QUOTA_PATTERNS",
    "SERVICE_PATTERNS",
    "SPEC_REJECTED",
    "UNACCOUNTED_SPEND",
    "UNKNOWN",
    "classify_failure",
    "is_deterministic_failure_class",
    "is_judgment_failure_class",
]

# ---------------------------------------------------------------------------
# Closed failure class vocabularies (D213 ruling 3)
# ---------------------------------------------------------------------------

#: Timeout of the worker dispatch or oracle process.
PLATFORM_TIMEOUT = "platform_timeout"

#: Platform or environment infrastructure error.
PLATFORM_ENV = "platform_env"

#: Spend that the attempt ledger cannot account for.
UNACCOUNTED_SPEND = "unaccounted_spend"

#: Oracle executed and reported a test/validator failure.
ORACLE_FAILED = "oracle_failed"

#: Failure observed but unclassifiable into known deterministic categories.
UNKNOWN = "unknown"

#: Specification itself was rejected by review (judgment only).
SPEC_REJECTED = "spec_rejected"

#: Worker model capability proved insufficient on review (judgment only).
CAPABILITY_REJECTED = "capability_rejected"

#: Deterministic failure classes (D213 ruling 3).
DETERMINISTIC_FAILURE_CLASSES: frozenset[str] = frozenset(
    {
        PLATFORM_TIMEOUT,
        PLATFORM_ENV,
        UNACCOUNTED_SPEND,
        ORACLE_FAILED,
        UNKNOWN,
    }
)

#: Judgment failure classes (D213 ruling 3).
JUDGMENT_FAILURE_CLASSES: frozenset[str] = frozenset(
    {
        SPEC_REJECTED,
        CAPABILITY_REJECTED,
    }
)

#: All admissible failure classes.
ALL_FAILURE_CLASSES: frozenset[str] = (
    DETERMINISTIC_FAILURE_CLASSES | JUDGMENT_FAILURE_CLASSES
)
FAILURE_CLASSES = ALL_FAILURE_CLASSES

# ---------------------------------------------------------------------------
# Platform environment patterns
#
# Source citation: The platform environment regex patterns and exit codes
# (126, 127) below are copied from
# /home/lee/projects/agent-orch/src/agent_orch/failure_classification.py
# per D213 ruling 3 and docs/staffing/phase3-contracts.md.
# ---------------------------------------------------------------------------

# Source citation:
# /home/lee/projects/agent-orch/src/agent_orch/failure_classification.py
AUTH_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"auth(entication)?\s+(failed|expired|invalid)", re.IGNORECASE),
    re.compile(r"token\s+(expired|invalid|revoked)", re.IGNORECASE),
    re.compile(r"401\s+unauthorized", re.IGNORECASE),
    re.compile(r"invalid_api_key", re.IGNORECASE),
    re.compile(r"incorrect\s+api\s+key", re.IGNORECASE),
    re.compile(r"oauth\s+(error|expired|token)", re.IGNORECASE),
)

# Source citation:
# /home/lee/projects/agent-orch/src/agent_orch/failure_classification.py
LOGGED_OUT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"not\s+logged\s+in", re.IGNORECASE),
    re.compile(r"login\s+required", re.IGNORECASE),
    re.compile(r"run\s+.*login", re.IGNORECASE),
    re.compile(r"session\s+expired", re.IGNORECASE),
    re.compile(r"you('ve|\s+have)\s+hit\s+your\s+session\s+limit", re.IGNORECASE),
)

# Source citation:
# /home/lee/projects/agent-orch/src/agent_orch/failure_classification.py
MISSING_BINARY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"command\s+not\s+found", re.IGNORECASE),
    re.compile(r"no\s+such\s+file\s+or\s+directory:\s*['\"].*['\"]", re.IGNORECASE),
    re.compile(r"executable\s+file\s+not\s+found", re.IGNORECASE),
    re.compile(r"binary\s+not\s+found", re.IGNORECASE),
    re.compile(r"FileNotFoundError:\s*\[Errno\s+2\]", re.IGNORECASE),
)

# Source citation:
# /home/lee/projects/agent-orch/src/agent_orch/failure_classification.py
QUOTA_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"quota\s+(exceeded|exhausted|limit)", re.IGNORECASE),
    re.compile(r"rate\s+limit\s+exceeded", re.IGNORECASE),
    re.compile(r"429\s+too\s+many\s+requests", re.IGNORECASE),
    re.compile(r"insufficient_quota", re.IGNORECASE),
    re.compile(r"resource\s+exhausted", re.IGNORECASE),
    re.compile(r"billing\s+(limit|disabled)", re.IGNORECASE),
)

# Source citation:
# /home/lee/projects/agent-orch/src/agent_orch/failure_classification.py
PERMISSION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"permission\s+denied", re.IGNORECASE),
    re.compile(r"permissionerror:\s*\[Errno\s+13\]", re.IGNORECASE),
    re.compile(r"operation\s+not\s+permitted", re.IGNORECASE),
    re.compile(r"eacces", re.IGNORECASE),
)

# Source citation:
# /home/lee/projects/agent-orch/src/agent_orch/failure_classification.py
SERVICE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"503\s+service\s+unavailable", re.IGNORECASE),
    re.compile(r"502\s+bad\s+gateway", re.IGNORECASE),
    re.compile(r"504\s+gateway\s+timeout", re.IGNORECASE),
    re.compile(r"connection\s+refused", re.IGNORECASE),
    re.compile(r"endpoint\s+connection\s+error", re.IGNORECASE),
)

#: All platform environment patterns joined together.
# Source citation:
# /home/lee/projects/agent-orch/src/agent_orch/failure_classification.py
PLATFORM_ENV_PATTERNS: tuple[re.Pattern[str], ...] = (
    *AUTH_PATTERNS,
    *LOGGED_OUT_PATTERNS,
    *MISSING_BINARY_PATTERNS,
    *QUOTA_PATTERNS,
    *PERMISSION_PATTERNS,
    *SERVICE_PATTERNS,
)

#: Platform environment exit codes: 126 (non-executable), 127 (not found).
# Source citation:
# /home/lee/projects/agent-orch/src/agent_orch/failure_classification.py
PLATFORM_ENV_EXIT_CODES: frozenset[int] = frozenset({126, 127})

#: Standard POSIX exit code for command timeout (124).
TIMEOUT_EXIT_CODE = 124


class FailureClassificationError(ValueError):
    """Raised when failure classification inputs, judgments, or reviews are invalid."""


def is_deterministic_failure_class(failure_class: str | None) -> bool:
    """Return True if the failure class is one of the 5 deterministic classes."""
    return failure_class in DETERMINISTIC_FAILURE_CLASSES


def is_judgment_failure_class(failure_class: str | None) -> bool:
    """Return True if the failure class is one of the 2 judgment classes."""
    return failure_class in JUDGMENT_FAILURE_CLASSES


def _validate_judgment(
    judgment: str | None,
    review_verdict: str | Mapping[str, Any] | None,
) -> str | None:
    """Validate judgment values against review verdict requirements.

    D213 ruling 3: 'spec_rejected and capability_rejected enter only through
    explicit --judgment after review, never inferred from prose.'
    """
    if judgment is None:
        return None

    if not isinstance(judgment, str):
        raise FailureClassificationError(
            f"Judgment must be a string, got {type(judgment).__name__}"
        )

    if judgment != judgment.strip():
        raise FailureClassificationError(
            f"Judgment value {judgment!r} must not contain surrounding whitespace"
        )

    if judgment not in JUDGMENT_FAILURE_CLASSES:
        raise FailureClassificationError(
            f"Invalid judgment {judgment!r}; explicit judgment admits only "
            f"{sorted(JUDGMENT_FAILURE_CLASSES)} (D213 ruling 3)"
        )

    # Review verdict is required for judgment values
    if review_verdict is None:
        raise FailureClassificationError(
            f"Judgment {judgment!r} requires an explicit review verdict; "
            "never infer judgment from text without a review verdict."
        )

    norm_verdict = ""
    if isinstance(review_verdict, str):
        norm_verdict = review_verdict.strip().lower()
    elif isinstance(review_verdict, Mapping):
        v = (
            review_verdict.get("final_verdict")
            or review_verdict.get("verdict")
            or review_verdict.get("review_verdict")
            or ""
        )
        norm_verdict = str(v).strip().lower()

    if not norm_verdict:
        raise FailureClassificationError(
            f"Judgment {judgment!r} requires a non-empty review verdict"
        )

    if norm_verdict == "pass":
        raise FailureClassificationError(
            f"Review verdict {review_verdict!r} indicates pass, "
            f"which cannot justify failure judgment {judgment!r}"
        )

    return judgment


def classify_failure(
    record: Mapping[str, Any] | None = None,
    *,
    exit_code: int | None = None,
    timed_out: bool = False,
    stdout: str = "",
    stderr: str = "",
    error: str | None = None,
    oracle_exit_code: int | None = None,
    oracle_timed_out: bool = False,
    oracle_error: str | None = None,
    oracle_cmd: str | None = None,
    accounting_status: str | None = None,
    usage_basis: str | None = None,
    metered_route: bool | None = None,
    unaccounted_spend: bool = False,
    verdict: str | Mapping[str, Any] | None = None,
    judgment: str | None = None,
    review_verdict: str | Mapping[str, Any] | None = None,
) -> str | None:
    """Classify attempt failure according to D213 ruling 3.

    Deterministic classes:
        - ``platform_timeout``
        - ``platform_env``
        - ``unaccounted_spend``
        - ``oracle_failed``
        - ``unknown``

    Judgment classes (require explicit judgment and review verdict):
        - ``spec_rejected``
        - ``capability_rejected``

    Args:
        record: Optional attempt record mapping (e.g. from runner or ledger).
        exit_code: Worker dispatch exit code.
        timed_out: True if worker dispatch timed out.
        stdout: Worker stdout text.
        stderr: Worker stderr text.
        error: Worker error message.
        oracle_exit_code: Oracle exit code.
        oracle_timed_out: True if oracle timed out.
        oracle_error: Oracle launch or execution error.
        oracle_cmd: Oracle command string.
        accounting_status: Usage accounting status (e.g. "unaccounted").
        usage_basis: Usage basis (e.g. "unavailable"); ``unavailable`` usage
            on a metered route (any accounting status other than
            ``not_applicable``) classifies as ``unaccounted_spend``.
        unaccounted_spend: True if unaccounted spend observed.
        verdict: Attempt verdict (e.g. "pass", "fail").
        judgment: Explicit judgment value ("spec_rejected" or "capability_rejected").
        review_verdict: Review verdict justifying judgment (e.g. "fail", "rejected").

    Returns:
        One of the 7 failure class strings, or None if no failure occurred.

    Raises:
        FailureClassificationError: If judgment is specified without a valid
            review verdict, if an unknown judgment is supplied, or if review
            verdict contradicts the judgment.
    """
    notes_lines: list[str] = []

    # 1. Unpack record fields if provided and not explicitly overridden
    if record is not None:
        if judgment is None:
            judgment = record.get("judgment")
        if review_verdict is None:
            review_verdict = record.get("review_verdict")
            if review_verdict is None and isinstance(record.get("review"), Mapping):
                review_verdict = record["review"].get("final_verdict")

        if verdict is None:
            verdict = record.get("verdict")

        if oracle_cmd is None:
            oracle_cmd = record.get("oracle_cmd")

        if accounting_status is None:
            accounting_status = record.get("accounting_status")
            if accounting_status is None and isinstance(
                record.get("agent_orch_attempt"), Mapping
            ):
                accounting_status = record["agent_orch_attempt"].get(
                    "accounting_status"
                )
            if accounting_status is None and isinstance(record.get("usage"), Mapping):
                accounting_status = record["usage"].get("accounting_status")

        if usage_basis is None and isinstance(record.get("usage"), Mapping):
            raw_basis = record["usage"].get("basis")
            if isinstance(raw_basis, str):
                usage_basis = raw_basis

        if metered_route is None and isinstance(record.get("route"), Mapping):
            channel = record["route"].get("channel")
            if isinstance(channel, str):
                metered_route = channel in {"openrouter", "opencode-zen"}

        if not unaccounted_spend:
            unaccounted_spend = bool(record.get("unaccounted_spend"))

        # Provenance notes extraction
        prov = record.get("provenance")
        if isinstance(prov, Mapping):
            raw_notes = prov.get("notes")
            if isinstance(raw_notes, (list, tuple)):
                notes_lines.extend(str(n) for n in raw_notes)

        # Parse exit_code and timed_out from record / notes
        if exit_code is None:
            exit_code = record.get("exit_code")
            if exit_code is None and isinstance(record.get("dispatch"), Mapping):
                exit_code = record["dispatch"].get("exit_code")
            if exit_code is None and isinstance(
                record.get("agent_orch_attempt"), Mapping
            ):
                exit_code = record["agent_orch_attempt"].get("worker_exit_code")

        if not timed_out:
            if record.get("timed_out"):
                timed_out = True
            elif isinstance(record.get("dispatch"), Mapping) and record["dispatch"].get(
                "timed_out"
            ):
                timed_out = True

        # Parse oracle details from record / notes
        if oracle_exit_code is None:
            oracle_exit_code = record.get("oracle_exit_code")
            if oracle_exit_code is None and isinstance(record.get("oracle"), Mapping):
                oracle_exit_code = record["oracle"].get("exit_code")

        if not oracle_timed_out:
            if record.get("oracle_timed_out"):
                oracle_timed_out = True
            elif isinstance(record.get("oracle"), Mapping) and record["oracle"].get(
                "timed_out"
            ):
                oracle_timed_out = True

        if oracle_error is None:
            oracle_error = record.get("oracle_error")
            if oracle_error is None and isinstance(record.get("oracle"), Mapping):
                oracle_error = record["oracle"].get("error")

        # Parse notes for structured facts
        for note in notes_lines:
            if "router-run dispatch facts:" in note:
                m_to = re.search(r"timed_out=(True|False)", note)
                if m_to and m_to.group(1) == "True":
                    timed_out = True
                m_ec = re.search(r"exit_code=(-?\d+)", note)
                if m_ec and exit_code is None:
                    exit_code = int(m_ec.group(1))
            elif "oracle evidence:" in note:
                m_to = re.search(r"timed_out=(True|False)", note)
                if m_to and m_to.group(1) == "True":
                    oracle_timed_out = True
                m_ec = re.search(r"exit_code=(-?\d+|None)", note)
                if m_ec and oracle_exit_code is None and m_ec.group(1) != "None":
                    oracle_exit_code = int(m_ec.group(1))
                m_err = re.search(r"error=(.+)", note)
                if m_err and oracle_error is None:
                    parsed_err = m_err.group(1).strip()
                    if parsed_err.lower() not in ("none", ""):
                        oracle_error = parsed_err
            elif (
                "launch failure:" in note
                or "setup error:" in note
                or "workdir is not a directory" in note
            ):
                if oracle_error is None:
                    oracle_error = note

        # Extract text snippets
        rec_stdout = record.get("stdout")
        if isinstance(rec_stdout, str) and not stdout:
            stdout = rec_stdout
        rec_stderr = record.get("stderr")
        if isinstance(rec_stderr, str) and not stderr:
            stderr = rec_stderr
        rec_error = record.get("error")
        if isinstance(rec_error, str) and error is None:
            error = rec_error

    # 2. Check explicit judgment
    # D213 ruling 3: spec_rejected and capability_rejected enter ONLY through
    # explicit --judgment after review, never inferred from prose.
    if judgment is not None:
        validated_judgment = _validate_judgment(judgment, review_verdict)
        if validated_judgment is not None:
            return validated_judgment

    # 3. Deterministic: platform_timeout
    if (
        timed_out
        or oracle_timed_out
        or exit_code == TIMEOUT_EXIT_CODE
        or oracle_exit_code == TIMEOUT_EXIT_CODE
    ):
        return PLATFORM_TIMEOUT

    # 4. Deterministic: platform_env
    # Agent-Orch's own non-repairable classification (an imported record's
    # authoritative evidence) is platform environment evidence. Checked
    # after the explicit judgment so a supplied ``--judgment`` with a review
    # verdict is never shadowed by record content.
    if record is not None and isinstance(record.get("agent_orch_attempt"), Mapping):
        ao_fc = record["agent_orch_attempt"].get("failure_classification")
        if ao_fc:
            return PLATFORM_ENV

    # Check platform environment exit codes (126, 127)
    if (
        exit_code in PLATFORM_ENV_EXIT_CODES
        or oracle_exit_code in PLATFORM_ENV_EXIT_CODES
    ):
        return PLATFORM_ENV

    # Check oracle error (launch/setup failure, missing binary, etc.)
    if oracle_error is not None and str(oracle_error).strip() not in (
        "",
        "none",
        "None",
    ):
        return PLATFORM_ENV

    # Check platform environment text signatures
    # Source citation: agent-orch/src/agent_orch/failure_classification.py
    notes_text = "\n".join(notes_lines)
    combined_text = f"{stdout}\n{stderr}\n{error or ''}\n{notes_text}"
    for pattern in PLATFORM_ENV_PATTERNS:
        if pattern.search(combined_text):
            return PLATFORM_ENV

    # 5. Deterministic: unaccounted_spend
    # D213 ruling 3: unavailable usage on a metered route — any accounting
    # status other than the explicit non-metered ``not_applicable`` — is
    # spend the ledger cannot account for.
    usage_unavailable_on_metered_route = (
        isinstance(usage_basis, str)
        and usage_basis.strip().lower() == "unavailable"
        and metered_route is True
        and not (
            isinstance(accounting_status, str)
            and accounting_status.strip().lower() == "not_applicable"
        )
    )
    if (
        unaccounted_spend
        or (
            isinstance(accounting_status, str)
            and accounting_status.strip().lower() == "unaccounted"
        )
        or usage_unavailable_on_metered_route
    ):
        return UNACCOUNTED_SPEND

    # 6. Deterministic: oracle_failed
    # The oracle executed and reported a failed verdict (the canonical
    # ``fail`` verdict exists only when the oracle ran and failed), or the
    # oracle process itself returned a nonzero exit code — in either case
    # without platform evidence (timeout/environment/spend already ruled
    # out above).
    if oracle_exit_code not in (None, 0):
        return ORACLE_FAILED
    if isinstance(verdict, str) and verdict.strip().lower() == "fail":
        return ORACLE_FAILED
    if isinstance(verdict, Mapping):
        str_verdict = str(verdict.get("verdict") or "").strip().lower()
        if str_verdict == "fail":
            return ORACLE_FAILED

    # 7. Check if there was any failure at all
    has_failure = False
    if exit_code not in (None, 0):
        has_failure = True
    elif oracle_exit_code not in (None, 0):
        has_failure = True
    elif error is not None and str(error).strip() != "":
        has_failure = True
    elif isinstance(verdict, str) and verdict.strip().lower() in ("fail", "rejected"):
        has_failure = True
    elif isinstance(verdict, Mapping) and str(
        verdict.get("verdict") or ""
    ).strip().lower() in ("fail", "rejected"):
        has_failure = True
    elif (
        record is not None
        and record.get("verified_success") is False
        and record.get("verdict") != "pass"
    ):
        has_failure = True
    elif (
        record is None
        and exit_code is None
        and verdict is None
        and bool(stderr and stderr.strip())
    ):
        has_failure = True

    if has_failure:
        return UNKNOWN

    return None
