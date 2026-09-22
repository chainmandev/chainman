"""Declarative lefthook preset; native runtime code owns installation/execution."""

from collections.abc import Mapping
import json
from pathlib import Path

import toolchain as tc
from adapter_data import Table, table, text

EVENTS = ("pre-commit", "pre-push")


def declaration(cfg: Mapping[str, object]) -> Table:
    spec = table(cfg.get("hooks", {}), "Hooks")
    if set(spec) - {"enabled", "config", "trojan_source"}:
        raise ValueError("Unknown hooks setting")
    if type(spec.get("enabled", False)) is not bool:
        raise ValueError("hooks.enabled must be boolean")
    if "config" in spec:
        text(spec["config"], "Hook configuration")
    return spec


def effective(root: Path, target: Path) -> Path:
    spec = declaration(tc.config(root))
    command = '"$CHAINMAN_HOOK_ENTRY" '
    config: dict[str, object] = {
        "no_auto_install": True,
        "pre-commit": {
            "parallel": False,
            "commands": {"format-staged": {"run": command + "format-staged"}},
        },
        "pre-push": {
            "parallel": False,
            "commands": {"trojan-source": {"run": command + "trojan-source"}},
        },
    }
    if "config" in spec:
        path = text(spec["config"], "Hook config")
        tc.regular_input(root, path)
        config["extends"] = [str(root / path)]
    output = target / "lefthook.json"
    tc.atomic_bytes(output, (json.dumps(config, indent=2) + "\n").encode())
    return output
