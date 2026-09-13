"""Declared tasks and setup leases shared by consumer development workflows."""

from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Iterable, Iterator, Mapping
import fcntl
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from types import FrameType
from typing import NotRequired, TypedDict

import chainman
import toolchain as tc
import project_environment
import timing
from adapter_data import Table, array, string_map, strings, table, text


class SetupDetail(TypedDict):
    current: bool
    reason: str
    recovery: NotRequired[list[str]]


def declarations(cfg: Mapping[str, object], section: str) -> dict[str, Table]:
    return {
        key: table(value, f"{section}.{key}")
        for key, value in table(cfg.get(section, {}), section).items()
    }


def default_profile(cfg: Mapping[str, object]) -> str:
    return text(
        table(cfg.get("project", {}), "Project").get("default_profile", "default"),
        "Default profile",
    )


def task_seconds(spec: Mapping[str, object], field: str) -> int:
    default, lower, upper = {
        "timeout_seconds": (0, 0, 86400),
        "shutdown_seconds": (10, 1, 300),
    }[field]
    value = spec.get(field, default)
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError(f"Invalid task {field}")
    return value


def name(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]*", value
    ):
        raise ValueError(
            "Workflow names require letters, digits, underscores or hyphens"
        )
    return value


def names(values: object) -> list[str]:
    if not isinstance(values, list):
        raise ValueError("Workflow references must be arrays of names")
    return [name(value) for value in values]


def commands(value: object) -> list[list[str]]:
    if not isinstance(value, list) or not value:
        raise ValueError("A workflow requires nonempty argument-array commands")
    for argv in value:
        if (
            not isinstance(argv, list)
            or not argv
            or any(not isinstance(arg, str) or "\0" in arg for arg in argv)
        ):
            raise ValueError("Workflow commands must be nonempty argument arrays")
    return [strings(argv, "Workflow command") for argv in value]


def configuration(root: Path) -> Table:
    cfg = tc.config(root)
    if cfg["schema"] not in (2, 3):
        raise ValueError("Named workflows require configuration schema=2 or schema=3")
    checked_profiles: set[str] = set()

    def check_profile(profile: str) -> None:
        if profile not in checked_profiles:
            chainman.profile(root, profile, cfg=cfg)
            checked_profiles.add(profile)

    for section in ("tasks", "setup"):
        entries = cfg.get(section, {})
        if not isinstance(entries, dict):
            raise ValueError(f"{section} must contain named declarations")
        for key, spec in entries.items():
            name(key)
            if section == "tasks" and key == "setup":
                raise ValueError(
                    "Task name 'setup' is reserved for setup groups; choose another task name"
                )
            if not isinstance(spec, dict):
                raise ValueError(f"{section}.{key} must be a declaration")
            from configuration import FIELDS

            allowed = FIELDS[section]
            if set(spec) - allowed:
                raise ValueError(
                    f"Unknown fields in {section}.{key}: {', '.join(sorted(set(spec) - allowed))}"
                )
            if section == "tasks":
                context_values = spec.get("context_environment", {})
                if not isinstance(context_values, dict):
                    raise ValueError("Task context_environment must be a table")
                for variable, value in context_values.items():
                    project_environment.variable(variable)
                    if not isinstance(value, str) or "\0" in value:
                        raise ValueError(
                            "Task context values must be strings without NUL"
                        )
                project_environment.transport(spec.get("transport", {}))
                if "timeout_env" in spec:
                    project_environment.variable(spec["timeout_env"])
            if section == "tasks" and (
                spec.get("wait_for_services") is True or spec.get("depends_on")
            ):
                if spec.get("commands", []) != []:
                    commands(spec["commands"])
            else:
                commands(spec.get("commands"))
            names(spec.get("depends_on", []))
            tc.contained(root, spec.get("directory", "."))
            check_profile(
                spec.get(
                    "profile", cfg.get("project", {}).get("default_profile", "default")
                ),
            )
            if section == "setup":
                environment_inputs = spec.get("environment_inputs", [])
                if not isinstance(environment_inputs, list):
                    raise ValueError("Setup environment inputs must be variable names")
                for key in environment_inputs:
                    project_environment.variable(key)
                if len(set(environment_inputs)) != len(environment_inputs):
                    raise ValueError("Setup environment inputs must be unique")
                if not isinstance(spec.get("inputs"), list) or not spec["inputs"]:
                    raise ValueError("Setup groups require explicit fingerprint inputs")
                if not isinstance(spec.get("artifacts"), list) or not spec["artifacts"]:
                    raise ValueError("Setup groups require readiness artifacts")
                if not isinstance(spec.get("exclude_inputs", []), list):
                    raise ValueError("Setup input exclusions must be path patterns")
                for pattern in [*spec["inputs"], *spec.get("exclude_inputs", [])]:
                    if not isinstance(pattern, str):
                        raise ValueError("Setup input patterns must be strings")
                    tc.contained(root, pattern)
                for artifact in spec["artifacts"]:
                    if isinstance(artifact, str):
                        tc.contained(root, artifact)
                    elif isinstance(artifact, dict) and (
                        (
                            set(artifact) == {"path", "interpreter"}
                            and artifact["interpreter"] == "python"
                        )
                        or (
                            set(artifact) == {"path", "digest"}
                            and artifact["digest"] is True
                        )
                    ):
                        if artifact.get("interpreter") == "python":
                            path = Path(artifact["path"])
                            if not path.name or path.name in {"..", ".git"}:
                                raise ValueError("Invalid interpreter artifact path")
                            # Virtual environments link to the selected Nix
                            # Python. Parents remain confined; readiness below
                            # requires the exact pinned interpreter target.
                            tc.contained(root, str(path.parent))
                        else:
                            tc.contained(root, artifact["path"])
                    else:
                        raise ValueError("Invalid setup readiness artifact")
            else:
                names(spec.get("setup", []))
                names(spec.get("services", []))
                if "serial_group" in spec:
                    name(spec["serial_group"])
                if type(spec.get("exclusive", False)) is not bool:
                    raise ValueError("exclusive must be a boolean")
                if type(spec.get("exclusive_services", False)) is not bool:
                    raise ValueError("exclusive_services must be a boolean")
                if type(spec.get("cleanup_children", False)) is not bool:
                    raise ValueError("cleanup_children must be a boolean")
                if type(spec.get("wait_for_services", False)) is not bool:
                    raise ValueError("wait_for_services must be a boolean")
                for field in ("timeout_seconds", "shutdown_seconds"):
                    task_seconds(spec, field)
        order(entries, list(entries))
    for spec in cfg.get("tasks", {}).values():
        order(cfg.get("setup", {}), spec.get("setup", []))
    for key, spec in cfg.get("tasks", {}).items():
        graph = [cfg["tasks"][name] for name in order(cfg["tasks"], [key])]
        if any(task.get("exclusive", False) for task in graph) and any(
            task.get("services") for task in graph
        ):
            raise ValueError(
                "Exclusive maintenance tasks cannot acquire or borrow services"
            )
        if spec.get("wait_for_services") and not any(
            cfg["tasks"][name].get("services") for name in order(cfg["tasks"], [key])
        ):
            raise ValueError("wait_for_services requires a service-bearing task")
        if spec.get("exclusive_services") and not any(
            task.get("services") for task in graph
        ):
            raise ValueError("exclusive_services requires a service-bearing task")
    return table(cfg, "Workflow configuration")


def order(entries: Mapping[str, object], requested: Iterable[str]) -> list[str]:
    result: list[str] = []
    active: set[str] = set()

    def visit(key: str) -> None:
        name(key)
        if key not in entries:
            raise ValueError(f"Unknown workflow dependency: {key}")
        if key in active:
            raise ValueError(f"Workflow dependency cycle at {key}")
        if key in result:
            return
        active.add(key)
        spec = table(entries[key], f"Workflow {key}")
        for dependency in names(spec.get("depends_on", [])):
            visit(dependency)
        active.remove(key)
        result.append(key)

    for key in requested:
        visit(key)
    return result


def group_spec(cfg: Mapping[str, object], key: str) -> Table:
    spec = declarations(cfg, "setup")[key]
    spec.setdefault("directory", ".")
    spec.setdefault("profile", default_profile(cfg))
    return spec


def group_specs(
    root: Path,
    cfg: Mapping[str, object],
    requested: Iterable[str],
    env: Mapping[str, str] | None = None,
) -> dict[str, Table]:
    specs = {
        key: group_spec(cfg, key)
        for key in order(declarations(cfg, "setup"), requested)
    }
    for spec in specs.values():
        spec["dependency_fingerprints"] = {
            dependency: fingerprint(root, specs[dependency], env)
            for dependency in names(spec.get("depends_on", []))
        }
    return specs


def fingerprint(
    root: Path, spec: Mapping[str, object], env: Mapping[str, str] | None = None
) -> str:
    digest = hashlib.sha256(
        json.dumps([1, tc.context_id(), spec], sort_keys=True).encode()
    )
    profile_name = text(spec["profile"], "Setup profile")
    ref, profile = chainman.profile(root, profile_name)
    if spec.get("environment_inputs"):
        selected = chainman.profile_environment(
            root, profile, os.environ if env is None else env
        )
        digest.update(
            json.dumps(
                {
                    key: selected.get(key)
                    for key in strings(
                        spec["environment_inputs"], "Setup environment inputs"
                    )
                },
                sort_keys=True,
            ).encode()
        )
    digest.update(chainman.profile_fingerprint(root, profile_name, ref).encode())
    paths: set[Path] = set()
    for pattern in strings(spec["inputs"], "Setup inputs"):
        paths.update(root.glob(pattern))
    for path in sorted(paths):
        relative = path.relative_to(root).as_posix()
        if any(
            fnmatch.fnmatchcase(relative, pattern)
            for pattern in strings(spec.get("exclude_inputs", []), "Setup exclusions")
        ):
            continue
        tc.contained(root, relative)
        if path.is_file():
            digest.update(relative.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def stamp_path(root: Path, key: str) -> Path:
    return tc.contained(root, f".cache/toolchain/setup-groups/{name(key)}.json")


def current(
    root: Path, key: str, spec: Mapping[str, object], env: dict[str, str]
) -> bool:
    return setup_detail(root, key, spec, env)["current"]


def artifact_ready(root: Path, item: object, env: dict[str, str]) -> bool:
    if isinstance(item, dict) and item.get("digest") is True:
        return tc.contained(root, item["path"]).is_file()
    return tc.artifact_ready(root, item, env)


def artifact_digests(root: Path, spec: Mapping[str, object]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in array(spec["artifacts"], "Setup artifacts"):
        if isinstance(item, dict) and item.get("digest") is True:
            path = tc.contained(root, item["path"])
            digest = hashlib.sha256()
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
            result[item["path"]] = digest.hexdigest()
    return result


@contextmanager
def setup_use(
    root: Path, cfg: Mapping[str, object], requested: Iterable[str], env: dict[str, str]
) -> Iterator[tuple[int, ...]]:
    with timing.span("setup_validation", env):
        specs = group_specs(root, cfg, requested, env)
    if not specs:
        yield ()
        return
    # One installation lock protects overlapping physical outputs across groups.
    # Shared leases are inherited by task processes, including after parent exit.
    with tc.operation_file(root, "setup-use.lock") as lease:
        try:
            fcntl.flock(lease, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(
                "Another setup operation is installing project artifacts"
            ) from None
        with timing.span("setup_validation", env):
            stale = any(
                not current(root, key, spec, env) for key, spec in specs.items()
            )
        if stale:
            fcntl.flock(lease, fcntl.LOCK_UN)
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError(
                    "Setup is stale while another task uses installed artifacts; finish that task before reinstalling"
                ) from None
            for key, spec in specs.items():
                if current(root, key, spec, env):
                    continue
                expected = fingerprint(root, spec, env)
                for argv in commands(spec["commands"]):
                    chainman.execute(
                        root,
                        text(spec["profile"], "Setup profile"),
                        argv,
                        env=env,
                        cwd=tc.contained(
                            root, text(spec["directory"], "Setup directory")
                        ),
                        pass_fds=(lease.fileno(),),
                    )
                if not all(
                    artifact_ready(root, item, env)
                    for item in array(spec["artifacts"], "Setup artifacts")
                ):
                    raise ValueError(
                        f"Setup group {key} did not create its declared artifacts"
                    )
                if fingerprint(root, spec, env) != expected:
                    raise ValueError(
                        f"Setup inputs changed during installation of {key}; readiness was not recorded"
                    )
                tc.atomic_json(
                    stamp_path(root, key),
                    {
                        "fingerprint": expected,
                        "artifact_digests": artifact_digests(root, spec),
                    },
                )
            fcntl.flock(lease, fcntl.LOCK_SH)
        yield (lease.fileno(),)


@contextmanager
def inspection_lock(root: Path, name: str) -> Iterator[None]:
    """Borrow an existing lock without creating or touching project state."""
    path = tc.contained(root, f".cache/toolchain/{name}")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        yield
        return
    with os.fdopen(descriptor, "rb") as lease:
        if not stat.S_ISREG(os.fstat(lease.fileno()).st_mode):
            raise ValueError("Inspection lock must be a regular file")
        try:
            fcntl.flock(lease, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(
                "Setup state is changing; retry after the active operation finishes"
            ) from None
        yield


def setup_detail(
    root: Path, key: str, spec: Mapping[str, object], env: dict[str, str]
) -> SetupDetail:
    try:
        recorded = json.loads(stamp_path(root, key).read_text())
    except FileNotFoundError:
        reason = "not-installed"
    except ValueError:
        reason = "invalid-record"
    else:
        if not isinstance(recorded, dict):
            reason = "invalid-record"
        elif recorded.get("fingerprint") != fingerprint(root, spec, env):
            reason = "inputs-changed"
        elif not all(
            artifact_ready(root, item, env)
            for item in array(spec["artifacts"], "Setup artifacts")
        ):
            reason = "artifact-missing-or-incompatible"
        elif recorded.get("artifact_digests", {}) != artifact_digests(root, spec):
            reason = "artifact-content-changed"
        else:
            return {"current": True, "reason": "current"}
    return {"current": False, "reason": reason, "recovery": ["setup", key]}


def setup_status(root: Path, requested: list[str]) -> int:
    """Inspect readiness without creating caches, installing or blessing outputs."""
    cfg = configuration(root)
    with inspection_lock(root, "writer.lock"), inspection_lock(root, "setup-use.lock"):
        env = tc.environment(root, create=False)
        specs = group_specs(
            root, cfg, requested or list(declarations(cfg, "setup")), env
        )
        details = {
            key: setup_detail(root, key, spec, env) for key, spec in specs.items()
        }
        result = {key: value["current"] for key, value in details.items()}
        print(
            json.dumps(
                {
                    "schema": 1,
                    "current": all(result.values()),
                    "groups": result,
                    "details": details,
                }
            )
        )
        return 0 if all(result.values()) else 1


@contextmanager
def serial_use(root: Path, spec: Mapping[str, object]) -> Iterator[tuple[int, ...]]:
    """Serialize a task's command phase using a worktree-local kernel lease.

    The inherited descriptor protects live children even if their Python parent
    dies. It is released before a development task waits on its services, so a
    maintenance task can borrow those services and serialize only its mutation.
    """
    group = spec.get("serial_group")
    if group is None:
        yield ()
        return
    with tc.operation_file(root, f"task-{name(group)}.lock") as lease:
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(
                f"Task group {group} is busy; retry after its current command finishes"
            ) from None
        yield (lease.fileno(),)


def context_environment(
    root: Path, cfg: Mapping[str, object], task: str, inherited: Mapping[str, str]
) -> dict[str, str]:
    """Select graph-wide inherited values without changing the planner process.

    All tasks in one dependency closure share one graph. Conflicting declarations
    are rejected instead of depending on task execution order.
    """
    values: dict[str, str] = {}
    tasks = declarations(cfg, "tasks")
    for key in order(tasks, [task]):
        for variable, value in string_map(
            tasks[key].get("context_environment", {}), "Task context environment"
        ).items():
            if variable in values and values[variable] != value:
                raise ValueError(f"Conflicting task context value: {variable}")
            values[variable] = value
    spec = table(cfg.get("environment", {}), "Project environment")
    # Literal task selectors apply before reading provider-specific files.
    # Values containing references resolve against the configured environment.
    literal = {
        key: value
        for key, value in values.items()
        if not project_environment.REFERENCE.search(value)
    }
    configured = project_environment.apply(root, spec, dict(inherited, **literal))
    expanded = project_environment.expand(values, root, configured)
    # The context replaces caller inputs; explicit project and profile policy
    # still has the same precedence as it does for a normal caller environment.
    return project_environment.apply(root, spec, dict(inherited, **expanded))


def run(
    root: Path,
    action: str,
    extra: list[str],
    *,
    service_context: bool = False,
    context_task: str | None = None,
) -> int:
    cfg = configuration(root)
    tasks = declarations(cfg, "tasks")
    task_names = order(tasks, [action]) if action != "setup" else []
    exclusive = any(tasks[key].get("exclusive", False) for key in task_names)
    if exclusive and (
        service_context or any(tasks[key].get("services") for key in task_names)
    ):
        raise ValueError(
            "Exclusive maintenance tasks cannot acquire or borrow services"
        )
    with tc.operation(
        root,
        exclusive=exclusive,
        new_execution=True,
        automatic_prune=table(cfg.get("cache", {}), "Cache").get(
            "automatic_prune", True
        )
        is True,
    ):
        # Managed paths and compiler ownership belong to this execution. Apply
        # the requesting graph's project values only after creating its lease.
        env = tc.environment(root)
        if context_task:
            env = context_environment(root, cfg, context_task, env)
        if action != "setup":
            env = context_environment(root, cfg, action, env)
        if action == "setup":
            with setup_use(root, cfg, extra or list(declarations(cfg, "setup")), env):
                return 0
        if not service_context and any(
            tasks[key].get("services") for key in task_names
        ):
            raise ValueError("Service tasks require the checked-in host bootstrap")
        groups = list(
            dict.fromkeys(
                group
                for task in task_names
                for group in names(tasks[task].get("setup", []))
            )
        )
        with setup_use(root, cfg, groups, env) as descriptors:
            for key in task_names:
                spec = dict(tasks[key])
                if "timeout_env" in spec:
                    configured = project_environment.apply(
                        root,
                        table(cfg.get("environment", {}), "Project environment"),
                        env,
                    )
                    configured.update(
                        project_environment.expand(
                            spec.get("environment", {}), root, configured
                        )
                    )
                    value = configured.get(
                        text(spec["timeout_env"], "Task timeout variable")
                    )
                    if value is not None:
                        if not value.isdecimal() or not 1 <= int(value) <= 86400:
                            raise ValueError(
                                f"{spec['timeout_env']} must be an integer between 1 and 86400"
                            )
                        spec["timeout_seconds"] = int(value)
                profile = text(
                    spec.get("profile", default_profile(cfg)), "Task profile"
                )
                with tc.compiler_cache(profile, env, root) as selected:
                    with serial_use(root, spec) as serial_descriptors:
                        arguments = [
                            strings(argv, "Task command")
                            for argv in array(spec.get("commands", []), "Task commands")
                        ]
                        if key == action and extra and not arguments:
                            raise ValueError(
                                "A task without commands takes no arguments"
                            )
                        if key == action and arguments:
                            arguments[-1] += extra
                        if arguments and (
                            spec.get("cleanup_children", False)
                            or spec.get("timeout_seconds", 0)
                        ):
                            import native_tasks

                            with native_tasks.command(root, arguments, spec) as argv:
                                chainman.execute(
                                    root,
                                    profile,
                                    argv,
                                    env=selected,
                                    overrides=spec.get("environment", {}),
                                    cwd=tc.contained(
                                        root,
                                        text(
                                            spec.get("directory", "."), "Task directory"
                                        ),
                                    ),
                                    pass_fds=(*descriptors, *serial_descriptors),
                                )
                        else:
                            for argv in arguments:
                                chainman.execute(
                                    root,
                                    profile,
                                    argv,
                                    env=selected,
                                    overrides=spec.get("environment", {}),
                                    cwd=tc.contained(
                                        root,
                                        text(
                                            spec.get("directory", "."), "Task directory"
                                        ),
                                    ),
                                    pass_fds=(*descriptors, *serial_descriptors),
                                )
                    if spec.get("wait_for_services", False):
                        wait_for_services()
    return 0


def wait_for_services() -> None:
    """Retain project/setup leases while the host observes backend availability."""
    import signal

    def stopped(number: int, _frame: FrameType | None) -> None:
        raise SystemExit(128 + number)

    previous = {
        number: signal.signal(number, stopped)
        for number in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        while True:
            signal.pause()
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
