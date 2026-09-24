"""Explicit development presentation and observational workflow completion."""

from collections.abc import Mapping
import contextlib
import json
import os
from pathlib import Path
import re
import tempfile
import unicodedata
from urllib.parse import urlsplit

from adapter_data import Table, string_map, table
import project_environment


def display_text(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 2048
        or any(unicodedata.category(c).startswith("C") for c in value)
    ):
        raise ValueError(
            "Presentation values must be nonempty printable text under 2049 characters"
        )
    return value


def declaration(value: object) -> Table:
    spec = table(value, "Task presentation")
    if set(spec) - {"title", "urls", "details"}:
        raise ValueError("Task presentation supports title, urls and details")
    if "title" in spec:
        display_text(spec["title"])
    for section in ("urls", "details"):
        values = string_map(spec.get(section, {}), "Presentation " + section)
        if len(values) > 16:
            raise ValueError("Presentation supports at most 16 URLs and details each")
        for key, item in values.items():
            display_text(key)
            display_text(item)
    return spec


def resolve(value: object, root: Path, env: Mapping[str, str], task: str) -> Table:
    spec = declaration(value)
    result: Table = {"task": task}

    def expand(item: str) -> str:
        key = "presentation_value"
        while key in env or "{env:" + key + "}" in item:
            key += "_"
        return display_text(project_environment.expand({key: item}, root, env)[key])

    for section in ("urls", "details"):
        # Expand each value independently: a display label is not an environment
        # variable, and must not shadow an explicitly requested environment value.
        values = {
            key: expand(item)
            for key, item in string_map(spec.get(section, {}), section).items()
        }
        if section == "urls":
            for url in values.values():
                parsed = urlsplit(url)
                if (
                    parsed.scheme not in {"http", "https"}
                    or not parsed.hostname
                    or parsed.username
                    or parsed.password
                ):
                    raise ValueError(
                        "Presentation URLs require absolute HTTP(S) URLs without credentials"
                    )
        result[section] = values
    result["title"] = expand(str(spec.get("title", task)))
    return result


def publish(task: str, phase: str) -> None:
    """Only the verified workflow reports preparation, never application stdout.

    A dedicated regular-file directory crosses Docker Desktop/Podman VM mounts.
    This channel carries no control commands, credentials, paths or service leases.
    Failure is diagnostic only; presentation must not change task execution.
    """
    channel = os.environ.get("CHAINMAN_DEV_CHANNEL")
    operation = os.environ.get("CHAINMAN_DEV_OPERATION", "")
    if not channel or task != os.environ.get("CHAINMAN_DEV_TASK"):
        return
    if phase not in {"preparing", "ready"} or not re.fullmatch(
        r"[a-f0-9]{32}", operation
    ):
        raise ValueError("Invalid internal development status")
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=channel, delete=False) as output:
            temporary = output.name
            json.dump({"operation": operation, "phase": phase}, output)
        os.replace(temporary, Path(channel) / "progress.json")
    except OSError as error:
        import sys

        print(f"chainman: cannot report development status: {error}", file=sys.stderr)
    finally:
        if temporary:
            with contextlib.suppress(OSError):
                Path(temporary).unlink(missing_ok=True)
