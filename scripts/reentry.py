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


def validate(root: Path, *, internal: bool = False) -> None:
    mode = os.environ.get("CHAINMAN_ACTIVE_MODE")
    if mode and os.environ.get("CHAINMAN_MODE", mode) != mode:
        raise ValueError("Start a different mode outside the active development shell")
    active = os.environ.get("CHAINMAN_ROOT", "")
    if (
        not active
        or Path(active).resolve() != root
        or (not internal and not os.environ.get("CHAINMAN_ACTIVE_PROFILE"))
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
    if internal:
        validate_internal_runtime(pin)


def validate_internal_runtime(pin: str) -> None:
    """Retain the admitted interpreter/source closure without inventing a profile."""
    import storage

    if (
        os.environ.get("CHAINMAN_INTERNAL_ENTRY") != "1"
        or os.environ.get("CHAINMAN_ACTIVE_MODE") not in {"host-nix", "container-nix"}
        or (os.environ.get("CHAINMAN_ACTIVE_MODE") == "container-nix")
        != (os.environ.get("TOOLCHAIN_CONTAINER") == "1")
        or os.environ.get("CHAINMAN_RUNTIME_STORAGE_ACTIVE") != pin
        or os.environ.get("CHAINMAN_RUNTIME") != str(chainman.RUNTIME)
        or not str(chainman.RUNTIME).startswith("/nix/store/")
        or chainman.RUNTIME.is_symlink()
    ):
        raise ValueError("Internal entry requires its verified runtime handoff")
    base = (
        Path("/nix/var/nix/chainman-runtime-roots")
        if os.environ.get("TOOLCHAIN_CONTAINER") == "1"
        else Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
        / "chainman/runtime-roots"
    )
    if storage.runtime_check([str(base), pin, str(chainman.RUNTIME)]) != 0:
        raise ValueError("Internal entry requires its inherited runtime lease")
    bootstrap = base / storage.POOL / pin / "bootstrap"
    interpreter = Path(sys.executable).resolve(strict=True)
    if (
        Path(os.environ.get("CHAINMAN_RUNTIME_PYTHON", "")).resolve(strict=True)
        != interpreter
        or tc.bootstrap_interpreter(bootstrap) != interpreter
    ):
        raise ValueError("Internal entry requires its held bootstrap interpreter")


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
    args = arguments[1:]
    if args[:1] == ["--entry"]:
        args = args[1:]
    else:
        args = ["run", *args]
    internal = args[:1] in (["_workflow-service"], ["_workflow-probe"])
    if internal:
        if len(args) != 3:
            raise ValueError("Internal service entry requires its name and fingerprint")
        validate(root, internal=True)
    else:
        validate(root)
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
        if internal:
            declarations = services.declarations(root, cfg)
            if args[1] not in declarations:
                raise ValueError("Internal entry requires a declared service")
            selected_transport = execution_transport.effective(
                cfg, declarations[args[1]]
            )
        elif args[:1] == ["run"] and len(args) >= 2:
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
    if internal:
        # The handoff retains bootstrap tools, not the service's environment.
        # Its actual declared profile still enters through execute_internal.
        os.environ.pop("CHAINMAN_ACTIVE_PROFILE", None)
        os.environ.pop("CHAINMAN_ACTIVE_FINGERPRINT", None)
        return services.execute_internal(root, args[0], args[1:])
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
    tc.foreground_signals()
    try:
        raise SystemExit(main(sys.argv[1:]))
    except (ValueError, OSError) as error:
        print(f"Chainman reentry: {error}", file=sys.stderr)
        raise SystemExit(2) from None
    except subprocess.CalledProcessError as error:
        raise SystemExit(error.returncode) from None
