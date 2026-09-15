"""Small admission boundary for caller-maintained, unprovisioned execution."""

from collections.abc import Mapping, Sequence
from pathlib import Path

from adapter_data import table
import toolchain as tc


def validate_action(root: Path, action: str) -> None:
    cfg = tc.config(root)
    allowed = {
        "exec",
        "shell",
        "version",
        "config",
        "explain",
        "doctor",
        "setup",
        "setup-status",
        "run",
        "_recipe-plan",
    }
    reserved = {
        "module",
        "modules",
        "clean",
        "cache-prune",
        "cache-status",
        "ci-prune",
        "sdk-doctor",
        "nix-update",
        "chainman-update",
    }
    unsupported = (
        action.startswith(("deps-", "_")) and action != "_recipe-plan"
    ) or action in reserved
    if unsupported or (
        action not in allowed and action not in table(cfg.get("tasks", {}), "Tasks")
    ):
        raise ValueError(
            f"{action} requires CHAINMAN_MODE=host-nix or container-nix; "
            "host execution supports ordinary tasks, setup and inspection only"
        )
    if cfg["schema"] not in (2, 3) and action in {"run", "setup"}:
        raise ValueError("Host tasks and setup require schema 3 workflows")


def validate_tasks(tasks: Mapping[str, object], names: Sequence[str]) -> None:
    # Inspect the complete graph before leases, setup or earlier tasks can have
    # effects. Native containment is a task contract, never silently disabled.
    for name in names:
        spec = table(tasks[name], "Task")
        required = [
            key
            for key in (
                "services",
                "exclusive_services",
                "wait_for_services",
                "cleanup_children",
                "timeout_seconds",
                "timeout_env",
            )
            if spec.get(key)
        ]
        if required:
            raise ValueError(
                f"Task {name} requires Nix execution ({', '.join(required)}); "
                "select CHAINMAN_MODE=host-nix or container-nix"
            )
