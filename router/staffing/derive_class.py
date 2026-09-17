"""Conservative staffing-class derivation from Markdown work packets.

The packet shape is the small Markdown contract used by
``docs/staffing/phase3-acceptance-plan.md``.  This module deliberately derives
only class metadata.  It never selects, ranks, or names a route, model, or
provider (D206).

Domain tags (D213 P3-7 answer 3) come only from an explicit ``Domain:``
packet field or from the keyword table matched against owned paths — never
from prose elsewhere in the packet, which previously over-tagged packets on
incidental substrings (for example "authoritative" contributing the
"authority" tag to a text-only change).
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from lee_llm_router.staffing.catalog import canonical_class_key

__all__ = [
    "DEFAULT_CLASSES_PATH",
    "DerivedClass",
    "PacketClassError",
    "derive_class",
    "parse_class_key",
    "parse_packet",
]

_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CLASSES_PATH = _REPO_ROOT / "config" / "staffing" / "classes.yaml"

_CLASS_FIELDS = ("role", "oracle_type", "domain_tags", "size_band", "language")
_FIELD_LABELS = {
    "kind": "Kind",
    "declared size": "Declared size",
    "owned paths": "Owned paths",
    "oracle": "Oracle",
    "review": "Review",
    "domain": "Domain",
}
_FIELD_RE = re.compile(
    r"^\s*(?:-\s+)?(Kind|Declared\s+size|Owned\s+paths|Oracle|Review|Domain)"
    r"\s*:\s*(.*)$",
    re.IGNORECASE,
)
_ANY_BULLET_FIELD_RE = re.compile(r"^\s*-\s+[^:]+:\s*")

_EXTENSION_LANGUAGES = {
    ".py": "python",
    ".pyw": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "typescript",
    ".jsx": "typescript",
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
    ".fish": "shell",
    ".c": "c",
    ".h": "c",
    ".cc": "c",
    ".cpp": "c",
    ".cxx": "c",
    ".go": "go",
    ".sql": "sql",
    ".yaml": "yaml-config",
    ".yml": "yaml-config",
    ".json": "yaml-config",
    ".toml": "yaml-config",
    ".ini": "yaml-config",
    ".md": "markdown",
    ".markdown": "markdown",
}

_VALIDATOR_COMMANDS = frozenset(
    {
        "black",
        "check-jsonschema",
        "eslint",
        "flake8",
        "jsonschema",
        "markdownlint",
        "mypy",
        "nose",
        "nosetests",
        "nox",
        "py.test",
        "pyright",
        "pytest",
        "ruff",
        "shellcheck",
        "tox",
        "tsc",
        "unittest",
        "validate",
        "validator",
        "yamllint",
    }
)
_TEST_RUNNERS = frozenset(
    {"bun", "cargo", "deno", "go", "just", "make", "npm", "pnpm", "yarn"}
)
_TEST_SUBCOMMANDS = frozenset({"check", "lint", "test", "tests", "validate"})
_COMMAND_NAME_PARTS = frozenset(
    {"check", "lint", "test", "tests", "validate", "validator"}
)
_GENERIC_REVIEW = re.compile(
    r"^(?:an?\s+)?(?:independent\s+)?review\s+(?:is\s+)?"
    r"(?:required|needed|requested)(?:\s+after\s+.+)?$",
    re.IGNORECASE,
)


class PacketClassError(ValueError):
    """Raised when a packet or class keyword table is not safe to derive."""


@dataclass(frozen=True)
class ParsedPacket:
    """The packet facts used by class derivation."""

    kind: str
    owned_paths: tuple[str, ...]
    declared_size: dict[str, Any]
    oracle_command: str | None
    reviewer: str | None
    domain_tags_field: tuple[str, ...] | None


@dataclass(frozen=True)
class DerivedClass:
    """A class and the packet evidence from which it was derived."""

    role: str
    oracle_type: str
    domain_tags: tuple[str, ...]
    size_band: str
    language: str
    kind: str
    owned_paths: tuple[str, ...]
    declared_size: dict[str, Any]
    oracle_command: str | None
    reviewer: str | None
    domain_matches: tuple[dict[str, str], ...]
    domain_source: str
    overrides: dict[str, Any]
    override_records: tuple[dict[str, Any], ...]
    packet: str | None = None

    @property
    def class_key(self) -> str:
        """Return the canonical evidence/comparability key."""
        return canonical_class_key(
            self.role,
            self.oracle_type,
            self.domain_tags,
            self.size_band,
            self.language,
        )

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-safe derivation record with no route mapping."""
        effective = {
            "class_key": self.class_key,
            "role": self.role,
            "oracle_type": self.oracle_type,
            "domain_tags": list(self.domain_tags),
            "size_band": self.size_band,
            "language": self.language,
        }
        return {
            "source": "packet",
            "packet": self.packet,
            "kind": self.kind,
            "owned_paths": list(self.owned_paths),
            "declared_size": dict(self.declared_size),
            "oracle": {
                "type": self.oracle_type,
                "command": self.oracle_command,
                "named": self.oracle_command is not None,
            },
            "review": {"reviewer": self.reviewer},
            "domain_matches": [dict(match) for match in self.domain_matches],
            "domain_source": self.domain_source,
            "class": effective,
            # A simple mapping is convenient for consumers; the records retain
            # both the derived value and the explicit replacement.
            "overrides": {
                field: _json_value(value) for field, value in self.overrides.items()
            },
            "override_records": [
                {key: _json_value(value) for key, value in record.items()}
                for record in self.override_records
            ],
        }


def _json_value(value: Any) -> Any:
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value


def _clean_inline(value: str) -> str:
    """Remove Markdown code spans and collapse whitespace."""
    value = re.sub(r"`([^`]*)`", r"\1", value)
    return " ".join(value.split()).strip()


def _read_packet_fields(text: str) -> dict[str, str]:
    """Read the labelled fields from one packet-shaped Markdown file.

    Five fields (Kind, Declared size, Owned paths, Oracle, Review) are the
    packet shape; Domain is a sixth, optional field (D213 P3-7 answer 3).
    """
    lines = text.splitlines()
    fields: dict[str, str] = {}
    index = 0
    while index < len(lines):
        match = _FIELD_RE.match(lines[index])
        if match is None:
            index += 1
            continue
        label = re.sub(r"\s+", " ", match.group(1)).lower()
        if label in fields:
            raise PacketClassError(
                f"packet has duplicate field: {_FIELD_LABELS[label]}"
            )
        parts = [match.group(2).strip()]
        next_index = index + 1
        while next_index < len(lines):
            line = lines[next_index]
            if _FIELD_RE.match(line) or _ANY_BULLET_FIELD_RE.match(line):
                break
            if line.lstrip().startswith("#"):
                break
            if line.strip():
                parts.append(line.strip())
            next_index += 1
        fields[label] = " ".join(parts).strip()
        index = next_index
    if "kind" not in fields:
        raise PacketClassError("packet is missing required field: Kind")
    if "owned paths" not in fields:
        raise PacketClassError("packet is missing required field: Owned paths")
    return fields


def _parse_owned_paths(raw: str) -> tuple[str, ...]:
    """Parse only the explicit paths in the Owned paths field."""
    if not raw.strip():
        raise PacketClassError("Owned paths must name at least one path")
    code_spans = re.findall(r"`([^`]+)`", raw)
    source = " ".join(code_spans) if code_spans else raw
    if code_spans:
        candidates = code_spans
    else:
        candidates = re.split(r"\s*(?:,|;|\band\b)\s*", source, flags=re.IGNORECASE)
        if len(candidates) == 1 and len(source.split()) > 1:
            candidates = source.split()
    paths: list[str] = []
    for candidate in candidates:
        path = candidate.strip().strip(".,;:")
        path = path.lstrip("- ").strip()
        if not path or path.lower() in {"none", "n/a", "tbd"}:
            continue
        if any(character.isspace() for character in path):
            raise PacketClassError(f"Owned paths contains an invalid path: {path!r}")
        paths.append(path)
    if not paths:
        raise PacketClassError("Owned paths must name at least one path")
    if len(set(paths)) != len(paths):
        raise PacketClassError("Owned paths contains a duplicate path")
    return tuple(paths)


def _parse_kind(raw: str) -> str:
    kind = _clean_inline(raw).lower()
    if kind not in {"impl", "plan", "review", "judge", "prose"}:
        raise PacketClassError(
            "Kind must be one of impl, plan, review, judge, or prose"
        )
    return kind


def _parse_size(raw: str | None, owned_count: int) -> dict[str, Any]:
    """Parse the declared file/line estimate, conservatively."""
    if raw is None:
        return {
            "declared": False,
            "file_count": None,
            "owned_path_count": owned_count,
            "estimated_lines": None,
            "raw": None,
            "size_band": "l",
        }
    cleaned = _clean_inline(raw)
    if not cleaned:
        raise PacketClassError("Declared size is present but empty")
    if re.search(r"\b(?:undeclared|unknown|not\s+specified|n/?a)\b", cleaned, re.I):
        return {
            "declared": False,
            "file_count": None,
            "owned_path_count": owned_count,
            "estimated_lines": None,
            "raw": cleaned,
            "size_band": "l",
        }

    file_match = re.search(r"\b(\d[\d,]*)\s+files?\b", cleaned, re.I)
    line_match = re.search(
        r"(?:at\s+most|up\s+to|approximately|about|estimated|<=|~)?\s*"
        r"(\d[\d,]*)\s*(?:changed\s+)?lines?\b",
        cleaned,
        re.I,
    )
    if file_match is None and line_match is None:
        raise PacketClassError(
            "Declared size must state a file count and/or estimated lines"
        )
    file_count = int(file_match.group(1).replace(",", "")) if file_match else None
    estimated_lines = int(line_match.group(1).replace(",", "")) if line_match else None
    if file_count is not None and file_count < 1:
        raise PacketClassError("Declared size file count must be positive")
    if estimated_lines is not None and estimated_lines < 0:
        raise PacketClassError("Declared size line estimate must not be negative")

    # A complete declaration can be compared to the taxonomy thresholds.  A
    # partial declaration remains conservative rather than inventing a band.
    if file_count is None or estimated_lines is None:
        band = "l"
    elif file_count <= 1 and estimated_lines <= 50:
        band = "xs"
    elif file_count <= 3 and estimated_lines <= 200:
        band = "s"
    elif file_count <= 8 and estimated_lines <= 600:
        band = "m"
    else:
        band = "l"
    return {
        "declared": file_count is not None and estimated_lines is not None,
        "file_count": file_count,
        "owned_path_count": owned_count,
        "estimated_lines": estimated_lines,
        "raw": cleaned,
        "size_band": band,
    }


def _command_name_parts(value: str) -> set[str]:
    """Return exact lowercase words from a command or script basename."""
    name = Path(value).name.lower()
    while Path(name).suffix.lower() in {".bash", ".py", ".sh"}:
        name = Path(name).stem
    return {part for part in re.split(r"[^a-z0-9]+", name) if part}


def _is_named_oracle(command: str) -> bool:
    """Return whether a concrete command names a test or validator.

    Literal command names and conventional test-runner subcommands count.
    Incidental prose substrings do not: for example, ``echo check this`` is
    not promoted to a deterministic oracle merely because it contains the
    word ``check``.
    """
    try:
        words = shlex.split(command)
    except ValueError as exc:
        raise PacketClassError(f"Oracle command is malformed: {exc}") from exc
    if not words:
        return False

    executable_index = 0
    if Path(words[0]).name.lower() == "env":
        executable_index = 1
        while executable_index < len(words) and re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*=.*", words[executable_index]
        ):
            executable_index += 1
    if executable_index >= len(words):
        return False

    executable = Path(words[executable_index]).name.lower()
    arguments = [word.lower() for word in words[executable_index + 1 :]]
    if executable in _VALIDATOR_COMMANDS or executable.startswith("pytest-"):
        return True
    if _command_name_parts(executable) & _COMMAND_NAME_PARTS:
        return True
    if executable in {"python", "python3"}:
        if len(arguments) >= 2 and arguments[0] == "-m":
            module = arguments[1].split(".", 1)[0]
            return module in _VALIDATOR_COMMANDS
        return bool(
            arguments and _command_name_parts(arguments[0]) & _COMMAND_NAME_PARTS
        )
    if executable in _TEST_RUNNERS:
        return bool(arguments and arguments[0] in _TEST_SUBCOMMANDS)
    return False


def _parse_oracle(raw: str | None) -> str | None:
    if raw is None:
        return None
    command = _clean_inline(raw)
    if not command or re.fullmatch(
        r"(?:none|no(?:ne)?|n/?a|not\s+applicable|not\s+provided)",
        command,
        re.IGNORECASE,
    ):
        return None
    return command if _is_named_oracle(command) else None


def _parse_reviewer(raw: str | None) -> str | None:
    if raw is None:
        return None
    review = _clean_inline(raw)
    if not review or _GENERIC_REVIEW.fullmatch(review):
        return None
    patterns = (
        r"^(?:independent\s+)?review(?:er)?\s*(?:is|:|=)\s*(.+)$",
        r"^(?:independent\s+)?review\s+by\s+(.+)$",
        r"^(?:independent\s+)?review\s+(?:is\s+)?required\s+by\s+(.+)$",
        r"^review(?:er)?\s+(.+)$",
    )
    candidate: str | None = None
    for pattern in patterns:
        match = re.match(pattern, review, re.IGNORECASE)
        if match:
            candidate = match.group(1).strip()
            break
    if candidate is None:
        # A direct value such as "Sol Low" is a named reviewer.  Generic
        # workflow prose is not, and therefore cannot silently become judge.
        if re.search(
            r"\b(?:independent|after|oracle|passes|required|needed)\b", review, re.I
        ):
            return None
        candidate = review
    candidate = candidate.strip(" .,:;()[]")
    if not candidate or candidate.lower() in {
        "a different reviewer",
        "another reviewer",
    }:
        return None
    return candidate


def _parse_domain_field(raw: str | None) -> tuple[str, ...] | None:
    """Parse an explicit ``Domain:`` packet field, or ``None`` when absent.

    ``None`` means the packet states no explicit domain and the keyword
    table falls back to owned-path evidence (D213 P3-7 answer 3). An empty
    tuple is a packet's explicit, recorded declaration of no domain tags
    (``Domain: none``), which is not the same as an absent field.
    """
    if raw is None:
        return None
    cleaned = _clean_inline(raw)
    if not cleaned or cleaned.strip().lower() in {"none", "n/a", "tbd"}:
        return ()
    tags = tuple(
        sorted(
            {tag.strip().lower() for tag in re.split(r"[+,]", cleaned) if tag.strip()}
        )
    )
    if not tags:
        raise PacketClassError("Domain field is present but names no tag")
    return tags


def _load_classes(
    classes_path: str | Path,
) -> tuple[dict[str, tuple[str, ...]], tuple[tuple[str, str], ...]]:
    path = Path(classes_path).expanduser()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        raise PacketClassError(f"classes.yaml cannot be read: {exc}") from exc
    except yaml.YAMLError as exc:
        raise PacketClassError(f"classes.yaml is not valid YAML: {exc}") from exc
    if not isinstance(data, Mapping):
        raise PacketClassError("classes.yaml must contain a mapping")
    value_sets = data.get("value_sets")
    if not isinstance(value_sets, Mapping):
        raise PacketClassError("classes.yaml is missing value_sets")
    values: dict[str, tuple[str, ...]] = {}
    for field in _CLASS_FIELDS:
        raw_values = value_sets.get(field)
        if not isinstance(raw_values, list) or not all(
            isinstance(value, str) and value for value in raw_values
        ):
            raise PacketClassError(f"classes.yaml value_sets.{field} is invalid")
        values[field] = tuple(raw_values)

    notes = data.get("canonical_encoding", {}).get("notes")
    if not isinstance(notes, str):
        raise PacketClassError("classes.yaml has no keyword table notes")
    lines = notes.splitlines()
    marker = next(
        (
            index
            for index, line in enumerate(lines)
            if line.strip().lower() == "domain-keyword-table:"
        ),
        None,
    )
    if marker is None:
        raise PacketClassError("classes.yaml is missing domain-keyword-table")
    table: list[tuple[str, str]] = []
    for line in lines[marker + 1 :]:
        if not line.strip():
            continue
        match = re.fullmatch(r"\s*([^\s#]+)\s*->\s*([^\s#]+)\s*", line)
        if match is None:
            # The keyword table is the final indented block in the committed
            # notes.  Stop at a later prose paragraph so the notes can grow
            # without turning prose into a keyword.
            if table:
                break
            continue
        keyword, tag = match.groups()
        if tag not in values["domain_tags"]:
            raise PacketClassError(
                f"classes.yaml keyword {keyword!r} maps to unknown domain tag {tag!r}"
            )
        if any(existing == keyword.lower() for existing, _ in table):
            raise PacketClassError(f"classes.yaml repeats keyword {keyword!r}")
        table.append((keyword.lower(), tag))
    if not table:
        raise PacketClassError("classes.yaml domain-keyword-table is empty")
    return values, tuple(table)


def parse_class_key(class_key: str) -> dict[str, Any]:
    """Parse a canonical five-segment class key into override fields.

    Args:
        class_key: Canonical ``role/oracle/tags/size/language`` value.

    Returns:
        The five structured class fields.

    Raises:
        PacketClassError: If the key shape or tag encoding is not canonical.
    """
    parts = class_key.split("/")
    if len(parts) != 5 or any(not part for part in parts):
        raise PacketClassError(
            "class override must be role/oracle_type/domain_tags/size_band/language"
        )
    role, oracle_type, tags, size_band, language = parts
    domain_tags = () if tags == "none" else tuple(tags.split("+"))
    if any(not tag for tag in domain_tags):
        raise PacketClassError("class override has an empty domain tag")
    if domain_tags != tuple(sorted(set(domain_tags))):
        raise PacketClassError(
            "class override domain tags must be sorted and deduplicated"
        )
    return {
        "role": role,
        "oracle_type": oracle_type,
        "domain_tags": domain_tags,
        "size_band": size_band,
        "language": language,
    }


def _coerce_override(field: str, value: Any) -> Any:
    if field not in _CLASS_FIELDS:
        raise PacketClassError(
            f"unknown class override {field!r}; expected {', '.join(_CLASS_FIELDS)}"
        )
    if field == "domain_tags":
        if isinstance(value, str):
            value = value.strip()
            if value.lower() == "none" or not value:
                return ()
            values = re.split(r"[+,]", value)
        elif isinstance(value, (list, tuple)):
            values = list(value)
        else:
            raise PacketClassError("domain_tags override must be a tag list")
        tags = tuple(str(tag).strip() for tag in values if str(tag).strip())
        return tuple(sorted(set(tags)))
    if not isinstance(value, str) or not value.strip():
        raise PacketClassError(f"{field} override must be a non-empty value")
    return value.strip().lower()


def _validate_class(
    values: Mapping[str, tuple[str, ...]], fields: Mapping[str, Any]
) -> None:
    for field in ("role", "oracle_type", "size_band", "language"):
        if fields[field] not in values[field]:
            raise PacketClassError(
                f"{field} {fields[field]!r} is outside classes.yaml value_sets"
            )
    unknown_tags = [
        tag for tag in fields["domain_tags"] if tag not in values["domain_tags"]
    ]
    if unknown_tags:
        raise PacketClassError(
            f"domain_tags outside classes.yaml value_sets: {', '.join(unknown_tags)}"
        )


def parse_packet(packet_path: str | Path) -> ParsedPacket:
    """Parse the labelled Markdown packet fields without inferring prose."""
    path = Path(packet_path).expanduser()
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise PacketClassError(f"packet cannot be read: {exc}") from exc
    fields = _read_packet_fields(text)
    kind = _parse_kind(fields["kind"])
    owned_paths = _parse_owned_paths(fields["owned paths"])
    declared_size = _parse_size(fields.get("declared size"), len(owned_paths))
    oracle_command = _parse_oracle(fields.get("oracle"))
    reviewer = _parse_reviewer(fields.get("review"))
    domain_tags_field = _parse_domain_field(fields.get("domain"))
    return ParsedPacket(
        kind=kind,
        owned_paths=owned_paths,
        declared_size=declared_size,
        oracle_command=oracle_command,
        reviewer=reviewer,
        domain_tags_field=domain_tags_field,
    )


def derive_class(
    packet_path: str | Path,
    *,
    classes_path: str | Path = DEFAULT_CLASSES_PATH,
    overrides: Mapping[str, Any] | None = None,
) -> DerivedClass:
    """Derive one conservative staffing class from a Markdown packet.

    Args:
        packet_path: Markdown packet in the labelled shape used by the Phase 3
            acceptance plan.
        classes_path: The authoritative ``config/staffing/classes.yaml``.
        overrides: Explicit field replacements.  Values are recorded even when
            they equal the derived value; no route or model is consulted.

    Returns:
        A class plus source evidence and structured override records.

    Raises:
        PacketClassError: If packet shape, taxonomy, or an override is invalid.
    """
    packet = parse_packet(packet_path)
    values, keyword_table = _load_classes(classes_path)
    role = packet.kind
    oracle_type = (
        "deterministic"
        if packet.oracle_command
        else ("judge" if packet.reviewer else "none")
    )
    language_values = {
        _EXTENSION_LANGUAGES.get(Path(path).suffix.lower())
        for path in packet.owned_paths
    }
    if None in language_values:
        bad = next(
            path
            for path in packet.owned_paths
            if _EXTENSION_LANGUAGES.get(Path(path).suffix.lower()) is None
        )
        raise PacketClassError(
            f"Owned path {bad!r} has no supported language extension; "
            "language is derived only from owned-path extensions"
        )
    languages = {value for value in language_values if value is not None}
    language = next(iter(languages)) if len(languages) == 1 else "mixed"

    # D213 P3-7 answer 3: domain tags come only from an explicit ``Domain:``
    # packet field, or else from the keyword table matched against owned
    # paths — never from free text (a prose mention such as "authoritative"
    # must never contribute the "authority" tag by substring accident).
    domain_matches: list[dict[str, str]] = []
    if packet.domain_tags_field is not None:
        domain_tags = packet.domain_tags_field
        domain_source = "explicit_domain_field"
    else:
        lowered_paths = " ".join(path.lower() for path in packet.owned_paths)
        for keyword, tag in keyword_table:
            if keyword in lowered_paths:
                domain_matches.append({"keyword": keyword, "tag": tag})
        domain_tags = tuple(sorted({match["tag"] for match in domain_matches}))
        domain_source = "owned_paths"
    fields: dict[str, Any] = {
        "role": role,
        "oracle_type": oracle_type,
        "domain_tags": domain_tags,
        "size_band": packet.declared_size["size_band"],
        "language": language,
    }
    _validate_class(values, fields)

    explicit = dict(overrides or {})
    if "class_key" in explicit:
        class_fields = parse_class_key(str(explicit.pop("class_key")))
        explicit = {**class_fields, **explicit}
    records: list[dict[str, Any]] = []
    recorded: dict[str, Any] = {}
    for field, raw_value in explicit.items():
        new_value = _coerce_override(field, raw_value)
        old_value = fields[field]
        fields[field] = new_value
        _validate_class(values, fields)
        recorded[field] = new_value
        records.append(
            {
                "field": field,
                "derived": _json_value(old_value),
                "value": _json_value(new_value),
            }
        )

    return DerivedClass(
        role=fields["role"],
        oracle_type=fields["oracle_type"],
        domain_tags=tuple(fields["domain_tags"]),
        size_band=fields["size_band"],
        language=fields["language"],
        kind=packet.kind,
        owned_paths=packet.owned_paths,
        declared_size=packet.declared_size,
        oracle_command=packet.oracle_command,
        reviewer=packet.reviewer,
        domain_matches=tuple(domain_matches),
        domain_source=domain_source,
        overrides=recorded,
        override_records=tuple(records),
        packet=str(Path(packet_path).expanduser()),
    )
