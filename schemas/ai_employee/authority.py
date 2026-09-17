"""The strict, versioned authority contract.

An ``AuthorityContract`` states what one installed thing may read, write,
execute, or invoke.  It is deliberately blind to *how* any of that is
enforced: nothing here names an execution environment, a runtime, a process
launcher, or a product.  Enforcement — turning a contract into an enforced
policy for one particular execution environment — is a separate, later
layer that consumes this module; this module only states meaning and
decides against it.

The module uses only immutable dataclasses, enums, and the standard
library.  Parsing is strict at every object boundary so a misspelled field
cannot silently weaken a downstream control.

Every authority-relevant string — every ``grant_ref``, ``pattern``, ``tokens``
element, ``root``, ``exceptions`` element, ``tool``, ``contract_ref``, ``argv``
element, and ``path`` — must be an exact ``str``: ``type(value) is str``, not
merely ``isinstance(value, str)``. This is a deliberate restriction, not an
oversight. A reference monitor decides by comparing strings (``==``,
membership in a ``set``, use as a ``dict`` key), and a ``str`` subclass can
override ``__eq__``, ``__hash__``, or ``__len__`` to behave differently on
each call — including comparing unequal to itself, or comparing equal to a
deny pattern only the first time and to an allow pattern every time after.
Such an object can defeat deny-wins-over-allow, defeat duplicate-grant
rejection at construction, or make one decision differ from the next for
identical-looking input. A module whose correctness depends on being able to
predict how ``==`` behaves must not accept an object whose author controls
that behavior. Ordinary ``str`` values are unaffected: this check never
rejects conventional input.

Every caller-supplied collection is likewise read exactly once. The
contract's ``filesystem_grants``, ``command_grants``, and ``tool_grants``, a
command grant's ``tokens``, and a filesystem grant's ``exceptions`` are all
collapsed into a plain ``tuple`` before any check looks at them, and every
later step — element validation, duplicate rejection, sorting, storage —
reads only that tuple. A ``Sequence`` whose ``__iter__`` returns something
different on every read could otherwise be validated as one set of grants
and stored as another.
"""

from __future__ import annotations

import collections.abc
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

AUTHORITY_SCHEMA_VERSION = "ai-employee-authority/1.0"

_REFERENCE_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9+.-]*(?::[A-Za-z0-9][A-Za-z0-9._/-]*)?$"
)
_TOKEN_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*(?:[-_.][A-Za-z0-9]+)*$")

_EFFECTS = frozenset({"allow", "deny"})
_FILESYSTEM_MODES = frozenset({"read", "readwrite"})


class AuthorityError(ValueError):
    """Raised when an authority contract or one of its grants is not well formed."""


def _canonical_bytes(body: Mapping[str, Any]) -> bytes:
    """Serialize a JSON object without whitespace or implementation variance."""

    return json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _digest_body(body: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(body)).hexdigest()


def _require_text(value: Any, field: str) -> str:
    if type(value) is not str:
        raise AuthorityError(
            f"{field} must be an exact str, not {type(value).__name__} "
            "(str subclasses are rejected — see module docstring)"
        )
    if not value.strip():
        raise AuthorityError(f"{field} must be a non-empty string")
    return value


def _require_str(value: Any, field: str) -> str:
    """Require an exact ``str``, blank or empty included — for argv positions
    where an empty or whitespace-only token is a legitimate value to grant."""

    if type(value) is not str:
        raise AuthorityError(
            f"{field} must be an exact str, not {type(value).__name__} "
            "(str subclasses are rejected — see module docstring)"
        )
    return value


def _validate_reference(value: Any, field: str) -> str:
    value = _require_text(value, field)
    if _REFERENCE_RE.fullmatch(value) is None:
        raise AuthorityError(f"{field} is not a valid reference: {value!r}")
    return value


def _validate_token(value: Any, field: str) -> str:
    value = _require_text(value, field)
    if _TOKEN_RE.fullmatch(value) is None:
        raise AuthorityError(
            f"{field} must match [A-Za-z][A-Za-z0-9]*(with - _ or . segments), "
            f"got {value!r}"
        )
    return value


def _normalize_absolute_components(path: str) -> tuple[str, ...]:
    """Lexically normalize an absolute, ``/``-separated path into components.

    Resolves ``.`` and ``..`` components and collapses duplicate or trailing
    separators without touching the filesystem — this is string manipulation
    only, never resolution against any real filesystem. A ``..`` that would
    climb above the root has nowhere to go and is absorbed, exactly as it
    would be for a real filesystem root. Normalization is lexical only: it
    resolves how the *spelling* of a path reduces, never how the filesystem
    would resolve it. This module has no way to know whether any component
    is a symlink. The contract's enforcement layer is required to guarantee
    the trusted-path invariant: a granted root must contain no symlink that
    escapes it, so that this lexical normalization and the real filesystem
    always agree.
    """

    components: list[str] = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if components:
                components.pop()
            continue
        components.append(part)
    return tuple(components)


def _normalize_absolute_path(path: str) -> str:
    return "/" + "/".join(_normalize_absolute_components(path))


def _path_components(path: str) -> tuple[str, ...]:
    """Split an already-normalized absolute path into components."""

    return tuple(part for part in path.split("/") if part)


def _validate_absolute_root(value: Any, field: str) -> str:
    """Require an absolute path and return its lexically normalized form.

    A relative root would resolve against whatever the current working
    location happens to be, so a durable authority record must not admit
    one. Absoluteness is judged lexically (a leading ``/``), never via the
    host filesystem.
    """

    value = _require_text(value, field)
    if not value.startswith("/"):
        raise AuthorityError(f"{field} must be a non-empty absolute path")
    return _normalize_absolute_path(value)


def _normalize_relative_pattern(value: Any, field: str) -> str:
    """Lexically normalize a relative exception pattern, rooted at its grant.

    Uses the same lexical rules as :func:`_normalize_absolute_components`:
    ``.`` components are dropped and duplicate/trailing separators collapse.
    Unlike an absolute path, a ``..`` here has a boundary that matters — the
    grant's own root — so a ``..`` that would climb above the pattern's
    starting point is rejected rather than silently absorbed: an exception
    that could climb outside its own root is a construction error, not a
    rule to enforce. A pattern that normalizes to nothing (``"."``, ``""``,
    or a run of components that all cancel via ``..``) is rejected for the
    same reason: a deny rule that can never fire is worse than no rule.
    """

    value = _require_text(value, field)
    if value.startswith("/"):
        raise AuthorityError(f"{field} must be a relative path pattern")
    components: list[str] = []
    for part in value.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not components:
                raise AuthorityError(f"{field} escapes its own root: {value!r}")
            components.pop()
            continue
        components.append(part)
    if not components:
        raise AuthorityError(f"{field} normalizes to an empty pattern: {value!r}")
    return "/".join(components)


def _validate_effect(value: Any, field: str) -> str:
    value = _require_text(value, field)
    if value not in _EFFECTS:
        raise AuthorityError(
            f"{field} must be one of {sorted(_EFFECTS)}, got {value!r}"
        )
    return value


def _materialize(value: Any, field: str) -> tuple[Any, ...]:
    """Read a caller-supplied collection exactly once into a plain tuple.

    A grant collection, a ``tokens`` tuple, or an ``exceptions`` tuple comes
    from the caller and may be any object that presents itself as a
    :class:`collections.abc.Sequence` — including a ``tuple`` or ``list``
    subclass whose ``__iter__`` yields something different on every read.
    Validation must never read such a container more than once: an object
    that yields one set of items to the checks and another set to the code
    that stores them could validate an ``allow`` grant and keep a ``deny``
    one (or the reverse). Each collection is therefore collapsed into a
    plain ``tuple`` here, once, and every later step reads only that tuple.

    A bare ``str``/``bytes`` is rejected explicitly — it is a ``Sequence``
    of characters, never a collection of grants, tokens, or patterns — as is
    a ``Mapping``, checked first so that a class registered as both is still
    rejected. Any error the caller's container raises while being read is
    reported as an :class:`AuthorityError` naming the field, so no bare
    ``TypeError`` from caller-supplied code can escape.
    """

    if isinstance(value, (str, bytes)) or isinstance(value, collections.abc.Mapping):
        raise AuthorityError(
            f"{field} must be a sequence of items, not a bare string/bytes "
            "or a mapping"
        )
    if not isinstance(value, collections.abc.Sequence):
        raise AuthorityError(f"{field} must be a sequence of items")
    try:
        return tuple(value)
    except Exception as exc:  # caller-supplied __iter__/__getitem__ may raise
        raise AuthorityError(
            f"{field} could not be read as a sequence: {type(exc).__name__}"
        ) from exc


def _ensure_unique(values: Sequence[str], field: str) -> None:
    if len(set(values)) != len(values):
        raise AuthorityError(f"{field} contains duplicate references")


def _strict_object(
    body: Any,
    *,
    field: str,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise AuthorityError(f"{field} must be an object")
    allowed = required | optional
    unknown = [key for key in body if key not in allowed]
    if unknown:
        unknown_names = ", ".join(sorted((str(key) for key in unknown)))
        raise AuthorityError(f"{field} has unknown field(s): {unknown_names}")
    missing = sorted(required - set(body))
    if missing:
        raise AuthorityError(
            f"{field} is missing required field(s): {', '.join(missing)}"
        )
    return body


def _strict_array(body: Any, field: str) -> list[Any]:
    if not isinstance(body, list):
        raise AuthorityError(f"{field} must be an array")
    return body


def _token_matches(pattern_token: str, actual_token: str) -> bool:
    """Match one whole token.

    ``*`` matches any single token; every other pattern token must equal the
    actual token exactly, character for character. No other metacharacter
    (``?``, ``[...]``, ``[!...]``, or any other borrowed glob syntax) has any
    special meaning — it is matched as a literal character. This grammar is
    closed: it is the entire matching behavior, not an approximation of some
    other library's semantics.
    """

    return pattern_token == "*" or pattern_token == actual_token


def _pattern_components(pattern: str) -> tuple[str, ...]:
    return tuple(part for part in pattern.split("/") if part)


def _path_pattern_matches(pattern: str, relative_components: tuple[str, ...]) -> bool:
    """Match a ``/``-separated exception pattern against a relative path.

    Each ``/``-separated component of ``pattern`` is matched against the
    component in the same position using :func:`_token_matches` (literal
    equality, or ``*`` as a whole-component wildcard). A pattern matches a
    relative path that has at least as many components as the pattern and
    agrees on every one of them — so a pattern naming a directory also
    excepts everything nested under it.
    """

    pattern_components = _pattern_components(pattern)
    if len(relative_components) < len(pattern_components):
        return False
    return all(
        _token_matches(token, actual)
        for token, actual in zip(pattern_components, relative_components)
    )


def _command_tokens(pattern: str) -> tuple[str, ...]:
    tokens = tuple(pattern.split())
    if not tokens:
        raise AuthorityError("command_grant.pattern must contain at least one token")
    return tokens


def _command_matches(tokens: tuple[str, ...], argv: tuple[str, ...]) -> bool:
    if len(argv) < len(tokens):
        return False
    return all(_token_matches(token, actual) for actual, token in zip(argv, tokens))


def _command_specificity(grant: CommandGrant) -> tuple[int, tuple[int, ...]]:
    """Total, name-independent order over command grants.

    More tokens beats fewer. At an equal token count, tokens are compared
    left to right and a literal token outranks ``*`` at the first position
    where they differ — encoded here as a per-position flag (``1`` for a
    literal token, ``0`` for ``*``) compared the same way Python compares
    tuples: element by element, first difference decides. Nothing here
    reads ``grant_ref``; two grants that would tie under this order are
    rejected as duplicates when the contract is constructed, so ``max()``
    over this key never has to break a tie by identifier.
    """

    tokens = grant._match_tokens
    return (len(tokens), tuple(0 if token == "*" else 1 for token in tokens))


def _reject_duplicate_command_grants(
    command_grants: Sequence[CommandGrant],
) -> None:
    seen: dict[tuple[str, tuple[str, ...]], None] = {}
    for grant in command_grants:
        key = (grant.effect, grant._match_tokens)
        if key in seen:
            raise AuthorityError(
                "command_grants contains two grants that are exactly tied "
                "in effect and specificity (identical tokens); a grant_ref "
                "must never be load-bearing for a decision, so tied grants "
                "are rejected as duplicates rather than resolved by name"
            )
        seen[key] = None


def _reject_duplicate_tool_grants(tool_grants: Sequence[ToolGrant]) -> None:
    seen: dict[tuple[str, str], None] = {}
    for grant in tool_grants:
        key = (grant.tool, grant.effect)
        if key in seen:
            raise AuthorityError(
                "tool_grants contains two grants for the same tool and "
                "effect; a grant_ref must never be load-bearing for a "
                "decision, so duplicates are rejected rather than resolved "
                "by name"
            )
        seen[key] = None


@dataclass(frozen=True)
class Decision:
    """The outcome of one authority question: an effect and what decided it."""

    effect: str
    grant_ref: str | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "effect", _validate_effect(self.effect, "decision.effect")
        )
        if self.grant_ref is not None:
            _validate_reference(self.grant_ref, "decision.grant_ref")

    @property
    def allowed(self) -> bool:
        return self.effect == "allow"


@dataclass(frozen=True)
class FilesystemGrant:
    """Authority over one filesystem subtree.

    ``mode`` is one of two values, and it is the sole control over how the
    caller's ``write`` argument to :meth:`AuthorityContract.decide_path`
    interacts with this grant:

    - ``"read"``: a read access (``write=False``) that matches this grant is
      allowed; a write access (``write=True``) that matches this grant is
      denied. ``write`` downgrades the grant's own effect for that one
      decision — it can turn an allow into a deny, never the reverse.
    - ``"readwrite"``: both a read access and a write access that match this
      grant are allowed. ``write`` has no effect on the outcome.

    Exceptions and the write/mode interaction compose within one grant: an
    exception match decides first (always ``deny``, regardless of ``mode``
    or ``write``); only when no exception matches does the ``mode``/``write``
    rule above apply.

    ``exceptions`` are relative path patterns under ``root`` that are denied
    even though the root itself is granted — a carve-out from an otherwise
    broad grant, not a separate authority. Each pattern is lexically
    normalized the same way an access path is (see
    :func:`_normalize_relative_pattern`), then matched as a ``/``-separated
    sequence of components against the same number of leading components of
    the path's location relative to ``root``, using the same
    literal/``*``-wildcard grammar as a command pattern (see
    :class:`CommandGrant`). A relative path with more components than the
    pattern still matches, so a pattern naming a directory excepts
    everything nested under it.

    Precedence when grants for different roots both cover a path: the most
    specific grant — the one with the longest normalized root — governs the
    path completely, on its own, exceptions included. A shallower grant's
    exceptions are not consulted once a deeper grant's root matches, even if
    the shallower grant's exception would otherwise have applied to that
    path. A deeper grant is a full override, not an addition.
    """

    grant_ref: str
    root: str
    mode: str
    exceptions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_reference(self.grant_ref, "filesystem_grant.grant_ref")
        object.__setattr__(
            self, "root", _validate_absolute_root(self.root, "filesystem_grant.root")
        )
        mode = _require_text(self.mode, "filesystem_grant.mode")
        if mode not in _FILESYSTEM_MODES:
            raise AuthorityError(
                f"filesystem_grant.mode must be one of {sorted(_FILESYSTEM_MODES)}, "
                f"got {mode!r}"
            )
        exceptions = _materialize(self.exceptions, "filesystem_grant.exceptions")
        exceptions = tuple(
            _normalize_relative_pattern(item, "filesystem_grant.exceptions item")
            for item in exceptions
        )
        object.__setattr__(self, "exceptions", tuple(sorted(set(exceptions))))

    @classmethod
    def from_dict(cls, body: Any) -> FilesystemGrant:
        body = _strict_object(
            body,
            field="filesystem_grant",
            required=frozenset({"grant_ref", "root", "mode"}),
            optional=frozenset({"exceptions"}),
        )
        exceptions = tuple(
            _require_text(item, "filesystem_grant.exceptions item")
            for item in _strict_array(body.get("exceptions", []), "exceptions")
        )
        return cls(
            grant_ref=body["grant_ref"],
            root=body["root"],
            mode=body["mode"],
            exceptions=exceptions,
        )

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "grant_ref": self.grant_ref,
            "root": self.root,
            "mode": self.mode,
        }
        if self.exceptions:
            body["exceptions"] = list(self.exceptions)
        return body


@dataclass(frozen=True)
class CommandGrant:
    """Authority over one class of command invocation.

    A grant states its tokens in exactly one of two forms:

    - ``pattern`` — a whitespace-separated string of tokens. Readable, but a
      token that itself contains whitespace, or that is empty, is
      inexpressible in this form by construction: splitting on whitespace
      can never produce an empty or whitespace-only token, so this form's
      restriction to non-blank tokens is a direct consequence of its
      spelling, not an extra rule layered on top.
    - ``tokens`` — an explicit immutable ``tuple[str, ...]`` of tokens, one
      per argv position. Every token can be granted exactly, including one
      that contains whitespace, is empty (``""``), or is whitespace-only
      (``"   "``). The only restriction is on token ``0`` — the
      executable — which must be a non-empty, non-whitespace-only string,
      exactly like ``pattern``'s first token can never be blank. Every
      other position is unrestricted so that least-privilege authority for
      a legitimate argv value (an empty trailing argument, for instance)
      never has to fall back to over-granting with ``*``.

    A contract declares which form it uses by which of the two fields is
    present: exactly one of ``pattern`` or ``tokens`` must be set, never both
    and never neither. For any set of tokens containing no whitespace and no
    empty/blank non-executable token, the two forms decide identically — the
    string form is purely a convenience spelling of the same token sequence.

    Whichever form is used, matching is by whole token only: a token of
    ``*`` matches any single argv token in that position, and every other
    token must equal the actual argv token exactly, character for character
    (including the empty string, which matches only an empty argv token). No
    other character has special meaning — ``?``, ``[abc]``, ``[!abc]``, and
    every other glob metacharacter are matched literally. This is the
    complete grammar; nothing is inherited from any pattern-matching
    library.

    A grant's tokens match an argv **prefix**: ``argv`` must have at least as
    many elements as the grant has tokens, and every one of the grant's
    tokens must match the argv element at the same position; any elements
    of ``argv`` beyond the grant's token count are unconstrained by that
    grant. Equal length is never required — a grant with fewer, more
    general tokens matches every longer argv that agrees on its leading
    tokens (``pattern="git"`` matches ``("git", "push")``).

    When more than one grant matches the same ``argv``, resolution proceeds
    in two steps:

    1. **Effect**: any matching ``deny`` grant overrides every matching
       ``allow`` grant, regardless of specificity — a one-token ``deny``
       beats a five-token, otherwise-more-specific ``allow``. Only when no
       ``deny`` grant matches does an ``allow`` grant decide.
    2. **Specificity**, among the surviving grants of the decided effect: a
       grant with more tokens beats one with fewer. Among grants with an
       equal token count, tokens are compared position by position, left to
       right; at the first position where one grant has a literal token and
       the other has ``*``, the literal token wins. This order never
       consults a grant's identifier — two grants that would remain exactly
       tied after both steps are rejected as duplicates when the contract
       is constructed, so no decision ever depends on ``grant_ref``
       spelling or on the order grants happen to be declared in.
    """

    grant_ref: str
    pattern: str | None = None
    effect: str | None = None
    tokens: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        _validate_reference(self.grant_ref, "command_grant.grant_ref")
        object.__setattr__(
            self, "effect", _validate_effect(self.effect, "command_grant.effect")
        )
        if (self.pattern is None) == (self.tokens is None):
            raise AuthorityError(
                "command_grant must set exactly one of 'pattern' or 'tokens'"
            )
        if self.pattern is not None:
            pattern = _require_text(self.pattern, "command_grant.pattern")
            match_tokens = _command_tokens(pattern)
            object.__setattr__(self, "pattern", pattern)
        else:
            tokens = _materialize(self.tokens, "command_grant.tokens")
            if not tokens:
                raise AuthorityError(
                    "command_grant.tokens must contain at least one token"
                )
            tokens = tuple(
                _require_str(item, "command_grant.tokens item") for item in tokens
            )
            if not tokens[0].strip():
                raise AuthorityError(
                    "command_grant.tokens[0] (the executable) must be a "
                    "non-empty, non-whitespace string"
                )
            match_tokens = tokens
            object.__setattr__(self, "tokens", tokens)
        object.__setattr__(self, "_match_tokens", match_tokens)

    @classmethod
    def from_dict(cls, body: Any) -> CommandGrant:
        body = _strict_object(
            body,
            field="command_grant",
            required=frozenset({"grant_ref", "effect"}),
            optional=frozenset({"pattern", "tokens"}),
        )
        has_pattern = "pattern" in body
        has_tokens = "tokens" in body
        if has_pattern == has_tokens:
            raise AuthorityError(
                "command_grant must have exactly one of 'pattern' or 'tokens'"
            )
        if has_pattern:
            return cls(
                grant_ref=body["grant_ref"],
                pattern=body["pattern"],
                effect=body["effect"],
            )
        tokens = tuple(
            _require_str(item, "command_grant.tokens item")
            for item in _strict_array(body["tokens"], "tokens")
        )
        return cls(grant_ref=body["grant_ref"], tokens=tokens, effect=body["effect"])

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"grant_ref": self.grant_ref, "effect": self.effect}
        if self.pattern is not None:
            body["pattern"] = self.pattern
        else:
            body["tokens"] = list(self.tokens)
        return body


@dataclass(frozen=True)
class ToolGrant:
    """Authority to invoke one neutral capability.

    ``tool`` is a neutral capability name, such as ``subagent`` or
    ``network``.
    """

    grant_ref: str
    tool: str
    effect: str

    def __post_init__(self) -> None:
        _validate_reference(self.grant_ref, "tool_grant.grant_ref")
        object.__setattr__(self, "tool", _validate_token(self.tool, "tool_grant.tool"))
        object.__setattr__(
            self, "effect", _validate_effect(self.effect, "tool_grant.effect")
        )

    @classmethod
    def from_dict(cls, body: Any) -> ToolGrant:
        body = _strict_object(
            body,
            field="tool_grant",
            required=frozenset({"grant_ref", "tool", "effect"}),
        )
        return cls(
            grant_ref=body["grant_ref"], tool=body["tool"], effect=body["effect"]
        )

    def to_dict(self) -> dict[str, str]:
        return {"grant_ref": self.grant_ref, "tool": self.tool, "effect": self.effect}


@dataclass(frozen=True)
class AuthorityContract:
    """The complete, versioned statement of what one installed thing may do.

    Absence decides: anything not named by a grant falls to
    ``default_effect``, which must be ``deny``. There is no way to construct
    a contract that defaults open.
    """

    contract_ref: str
    default_effect: str
    filesystem_grants: tuple[FilesystemGrant, ...] = ()
    command_grants: tuple[CommandGrant, ...] = ()
    tool_grants: tuple[ToolGrant, ...] = ()
    schema_version: str = AUTHORITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _validate_reference(self.contract_ref, "authority_contract.contract_ref")
        schema_version = _require_text(
            self.schema_version, "authority_contract.schema_version"
        )
        if schema_version != AUTHORITY_SCHEMA_VERSION:
            raise AuthorityError(
                f"unsupported authority contract schema version: {schema_version!r}"
            )
        object.__setattr__(self, "schema_version", schema_version)
        default_effect = _require_text(
            self.default_effect, "authority_contract.default_effect"
        )
        if default_effect != "deny":
            raise AuthorityError(
                f"authority_contract.default_effect must be 'deny', "
                f"got {default_effect!r}"
            )
        object.__setattr__(self, "default_effect", default_effect)

        filesystem_grants = _materialize(
            self.filesystem_grants, "authority_contract.filesystem_grants"
        )
        command_grants = _materialize(
            self.command_grants, "authority_contract.command_grants"
        )
        tool_grants = _materialize(self.tool_grants, "authority_contract.tool_grants")
        if not all(isinstance(item, FilesystemGrant) for item in filesystem_grants):
            raise AuthorityError("filesystem_grants contains an invalid grant")
        if not all(isinstance(item, CommandGrant) for item in command_grants):
            raise AuthorityError("command_grants contains an invalid grant")
        if not all(isinstance(item, ToolGrant) for item in tool_grants):
            raise AuthorityError("tool_grants contains an invalid grant")

        normalized_roots = [item.root for item in filesystem_grants]
        if len(set(normalized_roots)) != len(normalized_roots):
            raise AuthorityError(
                "filesystem_grants contains duplicate normalized root(s); "
                "a grant_ref must never be load-bearing for a decision"
            )

        all_refs = (
            tuple(item.grant_ref for item in filesystem_grants)
            + tuple(item.grant_ref for item in command_grants)
            + tuple(item.grant_ref for item in tool_grants)
        )
        _ensure_unique(all_refs, "grant_ref")

        _reject_duplicate_command_grants(command_grants)
        _reject_duplicate_tool_grants(tool_grants)

        object.__setattr__(
            self,
            "filesystem_grants",
            tuple(sorted(filesystem_grants, key=lambda item: item.grant_ref)),
        )
        object.__setattr__(
            self,
            "command_grants",
            tuple(sorted(command_grants, key=lambda item: item.grant_ref)),
        )
        object.__setattr__(
            self,
            "tool_grants",
            tuple(sorted(tool_grants, key=lambda item: item.grant_ref)),
        )

    @classmethod
    def from_dict(cls, body: Any) -> AuthorityContract:
        body = _strict_object(
            body,
            field="authority_contract",
            required=frozenset({"contract_ref", "schema_version", "default_effect"}),
            optional=frozenset({"filesystem_grants", "command_grants", "tool_grants"}),
        )
        filesystem_grants = tuple(
            FilesystemGrant.from_dict(item)
            for item in _strict_array(
                body.get("filesystem_grants", []), "filesystem_grants"
            )
        )
        command_grants = tuple(
            CommandGrant.from_dict(item)
            for item in _strict_array(body.get("command_grants", []), "command_grants")
        )
        tool_grants = tuple(
            ToolGrant.from_dict(item)
            for item in _strict_array(body.get("tool_grants", []), "tool_grants")
        )
        return cls(
            contract_ref=body["contract_ref"],
            default_effect=body["default_effect"],
            filesystem_grants=filesystem_grants,
            command_grants=command_grants,
            tool_grants=tool_grants,
            schema_version=body["schema_version"],
        )

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "contract_ref": self.contract_ref,
            "schema_version": self.schema_version,
            "default_effect": self.default_effect,
        }
        if self.filesystem_grants:
            body["filesystem_grants"] = [
                item.to_dict() for item in self.filesystem_grants
            ]
        if self.command_grants:
            body["command_grants"] = [item.to_dict() for item in self.command_grants]
        if self.tool_grants:
            body["tool_grants"] = [item.to_dict() for item in self.tool_grants]
        return body

    def digest(self) -> str:
        """Content digest of the canonical contract, stable across reserialization."""

        return _digest_body(self.to_dict())

    def decide_path(self, path: str, write: bool) -> Decision:
        """Decide access to ``path``. Never touches the filesystem.

        ``path`` is normalized lexically (``.``/``..`` resolved, duplicate
        separators collapsed) before any grant is matched, so a traversal
        spelling is judged by its normalized form, never by raw string
        prefix. This normalization is lexical only — it never touches the
        filesystem and has no notion of a symlink. The contract's
        enforcement layer is required to guarantee the trusted-path
        invariant: a granted root must contain no symlink that escapes it,
        so that a path's lexical normalization always agrees with what the
        real filesystem would do with it.

        The most specific matching grant — the one with the longest
        normalized root — decides the path completely, its own exceptions
        included; a shallower grant's exceptions are not consulted once a
        deeper grant matches. ``write`` must be a real ``bool``.
        """

        if not isinstance(write, bool):
            raise AuthorityError("decide_path.write must be a bool")
        path = _validate_absolute_root(path, "decide_path.path")
        path_components = _path_components(path)
        candidates = [
            (grant, _path_components(grant.root)) for grant in self.filesystem_grants
        ]
        candidates = [
            (grant, root_components)
            for grant, root_components in candidates
            if path_components[: len(root_components)] == root_components
        ]
        if not candidates:
            return Decision(effect=self.default_effect, grant_ref=None)
        winner, winner_root_components = max(candidates, key=lambda pair: len(pair[1]))
        relative_components = path_components[len(winner_root_components) :]
        if any(
            _path_pattern_matches(pattern, relative_components)
            for pattern in winner.exceptions
        ):
            return Decision(effect="deny", grant_ref=winner.grant_ref)
        if write and winner.mode == "read":
            return Decision(effect="deny", grant_ref=winner.grant_ref)
        return Decision(effect="allow", grant_ref=winner.grant_ref)

    def decide_command(self, argv: Sequence[str]) -> Decision:
        """Decide whether ``argv`` may run. Never executes anything.

        ``argv`` must be a genuine runtime :class:`collections.abc.Sequence`
        — not a bare ``str``/``bytes`` (a sequence of characters, never a
        valid argv), not a ``Mapping`` (checked before the ``Sequence`` check,
        so a class registered as both is still rejected), and not an
        arbitrary one-shot iterator — whose every element is an exact
        ``str`` (``type(item) is str``; no subclass — see module docstring),
        and whose first element (the executable) is not empty or
        whitespace-only. Anything else raises :class:`AuthorityError`; this
        never lets a Python ``TypeError`` escape to a caller, since an
        uncaught ``TypeError`` is exactly the kind of surprise that would
        otherwise be mistaken for an ``allow``.

        ``argv`` is read exactly once, by materializing it into a tuple
        immediately after the type check. Every subsequent check —
        element-type validation, the non-blank-executable check, and the
        actual grant matching — reads only that materialized tuple, never
        ``argv`` itself. This closes a validate-then-use gap: a caller could
        otherwise supply a stateful, non-idempotent iterable that presents
        all-``str`` elements while being *validated* and different,
        non-``str`` elements while being *matched*, since a naive
        implementation would iterate the caller's object twice.
        """

        if isinstance(argv, collections.abc.Mapping):
            raise AuthorityError(
                "decide_command.argv must not be a Mapping, even one that "
                "also registers as a Sequence"
            )
        if isinstance(argv, (str, bytes)) or not isinstance(
            argv, collections.abc.Sequence
        ):
            raise AuthorityError(
                "decide_command.argv must be a Sequence[str], not a bare "
                "string/bytes or a non-sequence"
            )
        try:
            argv_tuple = tuple(argv)
        except Exception as exc:  # caller-supplied __iter__/__getitem__ may raise
            raise AuthorityError(
                "decide_command.argv could not be read as a sequence: "
                f"{type(exc).__name__}"
            ) from exc
        if not argv_tuple:
            raise AuthorityError("decide_command.argv must be non-empty")
        if not all(type(item) is str for item in argv_tuple):
            raise AuthorityError(
                "decide_command.argv must contain only exact str elements, "
                "not a str subclass (see module docstring)"
            )
        if not argv_tuple[0].strip():
            raise AuthorityError(
                "decide_command.argv[0] must be a non-empty executable name"
            )
        matches = [
            grant
            for grant in self.command_grants
            if _command_matches(grant._match_tokens, argv_tuple)
        ]
        if not matches:
            return Decision(effect=self.default_effect, grant_ref=None)

        denials = [grant for grant in matches if grant.effect == "deny"]
        if denials:
            winner = max(denials, key=_command_specificity)
            return Decision(effect="deny", grant_ref=winner.grant_ref)
        winner = max(matches, key=_command_specificity)
        return Decision(effect="allow", grant_ref=winner.grant_ref)

    def decide_tool(self, name: str) -> Decision:
        """Decide whether capability ``name`` may be invoked.

        At most one grant can match a given ``(tool, effect)`` pair — a
        contract with two ``allow`` grants, or two ``deny`` grants, for the
        same tool is rejected at construction as a duplicate. This does
        *not* mean at most one grant can match ``name`` at all: an ``allow``
        grant and a ``deny`` grant for the same tool may coexist, since they
        differ in effect. When both match, the outcome is decided exactly as
        for a command: any matching ``deny`` grant overrides a matching
        ``allow`` grant, regardless of which was declared first. Because at
        most one grant of each effect can match, the deny grant that wins
        (when one matches) is itself unique, and the allow grant that wins
        when no deny matches is also unique — but the winning *effect* is
        decided by the deny-wins rule, not by any claim that only one grant
        can ever match.
        """

        name = _validate_token(name, "decide_tool.name")
        matches = [grant for grant in self.tool_grants if grant.tool == name]
        if not matches:
            return Decision(effect=self.default_effect, grant_ref=None)
        denials = [grant for grant in matches if grant.effect == "deny"]
        if denials:
            return Decision(effect="deny", grant_ref=denials[0].grant_ref)
        return Decision(effect="allow", grant_ref=matches[0].grant_ref)
