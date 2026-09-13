"""Typed decoding of the private schema-1 update checkpoint.

The wire format remains flat for retained candidates from earlier runtimes.
Inspection is explicit in memory; preparation and resume carry no approval of
candidate contents. Filesystem and Git authority checks remain in the coordinator.
"""

from dataclasses import asdict, dataclass
from datetime import datetime


def table(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"Invalid update state: {field} must be an object")
    return {key: item for key, item in value.items()}


def text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Invalid update state: {field} must be text")
    return value


def flag(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"Invalid update state: {field} must be boolean")
    return value


def strings(value: object, field: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"Invalid update state: {field} must be an array")
    return [text(item, field) for item in value]


def pair(value: object, field: str) -> tuple[str, str]:
    items = strings(value, field)
    if len(items) != 2:
        raise ValueError(f"Invalid update state: {field} requires two strings")
    return items[0], items[1]


def string_map(value: object, field: str) -> dict[str, str]:
    return {key: text(item, field) for key, item in table(value, field).items()}


def index(value: object, field: str) -> dict[str, tuple[str, str]]:
    return {key: pair(item, field) for key, item in table(value, field).items()}


def modes(value: object) -> dict[str, int]:
    result = {}
    for key, item in table(value, "candidate_modes").items():
        if (
            not isinstance(item, int)
            or isinstance(item, bool)
            or not 0 <= item <= 0o777
        ):
            raise ValueError(
                "Invalid update state: candidate_modes requires file modes"
            )
        result[key] = item
    return result


@dataclass(frozen=True)
class Options:
    format: bool
    staged: bool
    preview: bool
    no_commit: bool
    json: bool
    message: str
    only_chainman: bool
    skip_chainman: bool
    extra: list[str]

    @classmethod
    def decode(cls, value: object) -> "Options":
        data = table(value, "options")
        result = cls(
            format=flag(data.get("format"), "options.format"),
            staged=flag(data.get("staged"), "options.staged"),
            preview=flag(data.get("preview"), "options.preview"),
            no_commit=flag(data.get("no_commit"), "options.no_commit"),
            json=flag(data.get("json"), "options.json"),
            message=text(data.get("message"), "options.message"),
            only_chainman=flag(data.get("only_chainman"), "options.only_chainman"),
            skip_chainman=flag(data.get("skip_chainman"), "options.skip_chainman"),
            extra=strings(data.get("extra"), "options.extra"),
        )
        if (
            not result.message.strip()
            or "\0" in result.message
            or result.skip_chainman == result.only_chainman
            or (
                result.staged
                and (not result.format or result.preview or not result.no_commit)
            )
            or (result.format and (result.only_chainman or result.extra))
            or (result.only_chainman and result.extra)
        ):
            raise ValueError("Invalid update state: inconsistent options")
        return result


@dataclass(frozen=True)
class Inspection:
    updated: dict[str, str]
    paths: list[str]


@dataclass(frozen=True, kw_only=True)
class State:
    root: str
    candidate: str
    identity: tuple[str, str]
    before: dict[str, str]
    index: dict[str, tuple[str, str]]
    patterns: list[str]
    options: Options
    runtime_files: list[str]
    verify: list[str]
    at: datetime
    source: bool
    candidate_identity: tuple[str, str]
    candidate_before: dict[str, str]
    candidate_index: dict[str, tuple[str, str]]
    candidate_modes: dict[str, int]
    candidate_git: dict[str, str]
    selected: list[str] | None = None
    inspection: Inspection | None = None

    def require_inspection(self) -> Inspection:
        if self.inspection is None:
            raise ValueError("Update candidate has not been inspected")
        return self.inspection

    def encode(self) -> dict[str, object]:
        data = asdict(self)
        data["schema"] = 1
        data["at"] = self.at.isoformat()
        data.pop("inspection")
        if self.selected is None:
            data.pop("selected")
        if self.inspection is not None:
            data.update(asdict(self.inspection))
        return data

    @classmethod
    def decode(cls, value: object) -> "State":
        data = table(value, "transaction")
        if type(data.get("schema")) is not int or data["schema"] != 1:
            raise ValueError("Invalid update state: unsupported schema")
        at = datetime.fromisoformat(text(data.get("at"), "at"))
        if at.utcoffset() is None:
            raise ValueError("Invalid update state: at requires a timezone")
        options = Options.decode(data.get("options"))
        selected = strings(data.get("selected"), "selected") if options.staged else None
        inspection = None
        if "updated" in data or "paths" in data:
            inspection = Inspection(
                updated=string_map(data.get("updated"), "updated"),
                paths=strings(data.get("paths"), "paths"),
            )
        return cls(
            root=text(data.get("root"), "root"),
            candidate=text(data.get("candidate"), "candidate"),
            identity=pair(data.get("identity"), "identity"),
            before=string_map(data.get("before"), "before"),
            index=index(data.get("index"), "index"),
            patterns=strings(data.get("patterns"), "patterns"),
            options=options,
            runtime_files=strings(data.get("runtime_files"), "runtime_files"),
            verify=strings(data.get("verify"), "verify"),
            at=at,
            source=flag(data.get("source", False), "source"),
            candidate_identity=pair(
                data.get("candidate_identity"), "candidate_identity"
            ),
            candidate_before=string_map(
                data.get("candidate_before"), "candidate_before"
            ),
            candidate_index=index(data.get("candidate_index"), "candidate_index"),
            candidate_modes=modes(data.get("candidate_modes")),
            candidate_git=string_map(data.get("candidate_git"), "candidate_git"),
            selected=selected,
            inspection=inspection,
        )
