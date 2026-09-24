"""Runtime-owned nested entry; environment labels alone confer no service lease."""

from contextlib import contextmanager
from collections.abc import Iterator
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile

import admission
import chainman
import git_runtime
import services
import toolchain as tc
import workflows
from adapter_data import Table


@contextmanager
def service_context(
    root: Path,
    cfg: Table,
    task: str,
    env: dict[str, str],
    enabled: bool,
) -> Iterator[tuple[dict[str, str], tuple[int, ...]]]:
    if not enabled:
        yield env, ()
        return
    graph = admission.graph(root, cfg, [task])
    tasks = workflows.declarations(cfg, "tasks")
    receipt = {
        "root": str(root.resolve()),
        "configuration": services.config_fingerprint(root, cfg, env=env),
        "services": graph["services"],
        "exclusive": any(
            tasks[name].get("exclusive_services") for name in graph["tasks"]
        ),
        "network": tasks[task].get("network_service"),
    }
    with tempfile.TemporaryFile() as lease:
        lease.write(json.dumps(receipt).encode())
        lease.flush()
        selected = dict(env, CHAINMAN_SERVICE_CONTEXT_FD=str(lease.fileno()))
        yield selected, (lease.fileno(),)


def validate(root: Path) -> None:
    mode = os.environ.get("CHAINMAN_ACTIVE_MODE")
    if mode and os.environ.get("CHAINMAN_MODE", mode) != mode:
        raise ValueError("Start a different mode outside the active development shell")
    active = os.environ.get("CHAINMAN_ROOT", "")
    if (
        not active
        or Path(active).resolve() != root
        or not os.environ.get("CHAINMAN_ACTIVE_PROFILE")
    ):
        raise ValueError(
            "Nested entry belongs to a different project; leave the active shell first"
        )
    pin = git_runtime.pin(
        tc.regular_input(tc.configuration_root(root), "chainman.lock")
    )
    if pin != os.environ.get("CHAINMAN_ACTIVE_PIN"):
        raise ValueError(
            "Chainman pin changed; leave this shell and enter the project again"
        )


def borrow(root: Path, task: str) -> bool:
    cfg = workflows.configuration(root)
    graph = admission.graph(root, cfg, [task])
    if not graph["services"]:
        return False
    descriptor, _, identity, compat, ancestors = tc.inherited_operation()
    value = os.environ.get("CHAINMAN_SERVICE_CONTEXT_FD", "")
    if (
        not value.isdecimal()
        or int(value) not in ancestors
        or descriptor is None
        or not identity
        or compat is None
        or not tc.descriptor_matches(
            descriptor, root / f".cache/toolchain/operations/{identity}"
        )
        or not tc.descriptor_matches(compat, root / ".cache/toolchain/operation.lock")
    ):
        raise ValueError(
            "No live service ownership for nested entry; start this task from the host"
        )
    receipt = json.loads(os.pread(int(value), 65536, 0))
    env = tc.environment(root, create=False)
    if (
        receipt.get("root") != str(root)
        or receipt.get("configuration")
        != services.config_fingerprint(root, cfg, env=env)
        or not set(graph["services"]).issubset(receipt.get("services", []))
    ):
        raise ValueError(
            "Nested service context is missing or stale; restart the workflow from the host"
        )
    tasks = workflows.declarations(cfg, "tasks")
    for name in graph["tasks"]:
        spec = tasks[name]
        if (
            spec.get("wait_for_services")
            or spec.get("exclusive")
            or spec.get("serial_group")
            or spec.get("context_environment")
            or (spec.get("exclusive_services") and not receipt.get("exclusive"))
            or (
                spec.get("network_service")
                and spec["network_service"] != receipt.get("network")
            )
        ):
            raise ValueError("Nested service task requires fresh host admission")
    return True


def main(arguments: list[str]) -> int:
    root = Path(arguments[0]).resolve()
    validate(root)
    args = arguments[1:]
    if args[:1] == ["--entry"]:
        args = args[1:]
    else:
        args = ["run", *args]
    if args == ["hooks", "config"]:
        # This reads declarations only; it needs neither profile transport nor
        # setup admission, and cannot install or execute repository hooks.
        return chainman.main(["--root", str(root), "_hooks-config"])
    if args[:1] == ["script"]:
        args = args[1:]
        profile: list[str] = []
        if args[:1] == ["--profile"]:
            profile, args = args[:2], args[2:]
        if not args or not Path(args[0]).is_file() or Path(args[0]).is_symlink():
            raise ValueError("script requires a regular Bash script")
        file, *tail = args
        args = [
            "exec",
            *profile,
            "--",
            "bash",
            "--noprofile",
            "--norc",
            "-eu",
            "-o",
            "pipefail",
            "-c",
            os.fsdecode(Path(file).read_bytes()),
            file,
            *tail,
        ]
    if os.environ.get("CHAINMAN_ACTIVE_MODE") == "container-nix" and args[:1] != [
        "preflight"
    ]:
        import execution_transport

        cfg = workflows.configuration(root)
        if args[:1] == ["run"] and len(args) >= 2:
            selected_transport = execution_transport.effective(
                cfg, workflows.declarations(cfg, "tasks")[args[1]]
            )
        else:
            if args[1:2] == ["--profile"] and len(args) < 3:
                raise ValueError("--profile requires a name")
            selected_profile = (
                args[2]
                if args[1:2] == ["--profile"]
                else workflows.default_profile(cfg)
            )
            selected_transport = execution_transport.effective(
                cfg, {}, profile=selected_profile
            )
        active_transport = json.loads(
            os.environ.get("CHAINMAN_ACTIVE_TRANSPORT", '{"mounts": [], "ports": []}')
        )
        if not execution_transport.equivalent(selected_transport, active_transport):
            raise ValueError(
                "Nested entry requires different container transport; start it from the host"
            )
    if args[:1] == ["run"] and len(args) >= 2:
        borrowed = borrow(root, args[1])
        tail = args[2:]
        if tail[:1] == ["--"]:
            tail = tail[1:]
        return workflows.run(root, args[1], tail, service_context=borrowed)
    if args[:1] not in (["exec"], ["shell"], ["preflight"]):
        raise ValueError(
            "Nested entry supports exec, shell, script, preflight and declared tasks"
        )
    return chainman.main(["--root", str(root), *args])


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except (ValueError, OSError) as error:
        print(f"Chainman reentry: {error}", file=sys.stderr)
        raise SystemExit(2) from None
    except subprocess.CalledProcessError as error:
        raise SystemExit(error.returncode) from None
