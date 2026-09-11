"""Declared tasks and setup leases shared by consumer development workflows."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import fnmatch
import hashlib
import json
from pathlib import Path
import re

import chainman
import toolchain as tc
import project_environment


def name(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]*", value
    ):
        raise ValueError(
            "Workflow names require letters, digits, underscores or hyphens"
        )
    return value


def names(values):
    if not isinstance(values, list):
        raise ValueError("Workflow references must be arrays of names")
    return [name(value) for value in values]


def commands(value):
    if not isinstance(value, list) or not value:
        raise ValueError("A workflow requires nonempty argument-array commands")
    for argv in value:
        if (
            not isinstance(argv, list)
            or not argv
            or any(not isinstance(arg, str) or "\0" in arg for arg in argv)
        ):
            raise ValueError("Workflow commands must be nonempty argument arrays")
    return value


def configuration(root):
    cfg = tc.config(root)
    if cfg["schema"] != 2:
        raise ValueError("Named workflows require configuration schema=2")
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
            allowed = {"commands", "profile", "directory", "depends_on"} | (
                {"inputs", "exclude_inputs", "artifacts"}
                if section == "setup"
                else {
                    "setup",
                    "services",
                    "cleanup_children",
                    "timeout_seconds",
                    "timeout_env",
                    "shutdown_seconds",
                    "wait_for_services",
                    "environment",
                    "transport",
                    "exclusive",
                    "network_service",
                }
            )
            if set(spec) - allowed:
                raise ValueError(
                    f"Unknown fields in {section}.{key}: {', '.join(sorted(set(spec) - allowed))}"
                )
            if section == "tasks":
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
            chainman.profile(
                root,
                spec.get(
                    "profile", cfg.get("project", {}).get("default_profile", "default")
                ),
            )
            if section == "setup":
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
                        tc.contained(root, artifact["path"])
                    else:
                        raise ValueError("Invalid setup readiness artifact")
            else:
                names(spec.get("setup", []))
                names(spec.get("services", []))
                if type(spec.get("exclusive", False)) is not bool:
                    raise ValueError("exclusive must be a boolean")
                if type(spec.get("cleanup_children", False)) is not bool:
                    raise ValueError("cleanup_children must be a boolean")
                if type(spec.get("wait_for_services", False)) is not bool:
                    raise ValueError("wait_for_services must be a boolean")
                for field, default, lower, upper in (
                    ("timeout_seconds", 0, 0, 86400),
                    ("shutdown_seconds", 10, 1, 300),
                ):
                    value = spec.get(field, default)
                    if type(value) is not int or not lower <= value <= upper:
                        raise ValueError(f"Invalid task {field}")
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
    return cfg


def order(entries, requested):
    result, active = [], set()

    def visit(key):
        name(key)
        if key not in entries:
            raise ValueError(f"Unknown workflow dependency: {key}")
        if key in active:
            raise ValueError(f"Workflow dependency cycle at {key}")
        if key in result:
            return
        active.add(key)
        for dependency in names(entries[key].get("depends_on", [])):
            visit(dependency)
        active.remove(key)
        result.append(key)

    for key in requested:
        visit(key)
    return result


def group_spec(cfg, key):
    spec = dict(cfg["setup"][key])
    spec.setdefault("directory", ".")
    spec.setdefault("profile", cfg.get("project", {}).get("default_profile", "default"))
    return spec


def group_specs(root, cfg, requested):
    specs = {
        key: group_spec(cfg, key) for key in order(cfg.get("setup", {}), requested)
    }
    for spec in specs.values():
        spec["dependency_fingerprints"] = {
            dependency: fingerprint(root, specs[dependency])
            for dependency in spec.get("depends_on", [])
        }
    return specs


def fingerprint(root, spec):
    digest = hashlib.sha256(
        json.dumps([1, tc.context_id(), spec], sort_keys=True).encode()
    )
    ref, _ = chainman.profile(root, spec["profile"])
    digest.update(chainman.profile_fingerprint(root, spec["profile"], ref).encode())
    paths = set()
    for pattern in spec["inputs"]:
        paths.update(root.glob(pattern))
    for path in sorted(paths):
        relative = path.relative_to(root).as_posix()
        if any(
            fnmatch.fnmatchcase(relative, pattern)
            for pattern in spec.get("exclude_inputs", [])
        ):
            continue
        tc.contained(root, relative)
        if path.is_file():
            digest.update(relative.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def stamp_path(root, key):
    return tc.contained(root, f".cache/toolchain/setup-groups/{name(key)}.json")


def current(root, key, spec, env):
    try:
        recorded = json.loads(stamp_path(root, key).read_text())
    except (FileNotFoundError, ValueError):
        return False
    return (
        isinstance(recorded, dict)
        and recorded.get("fingerprint") == fingerprint(root, spec)
        and all(artifact_ready(root, item, env) for item in spec["artifacts"])
        and recorded.get("artifact_digests", {}) == artifact_digests(root, spec)
    )


def artifact_ready(root, item, env):
    if isinstance(item, dict) and item.get("digest") is True:
        return tc.contained(root, item["path"]).is_file()
    return tc.artifact_ready(root, item, env)


def artifact_digests(root, spec):
    result = {}
    for item in spec["artifacts"]:
        if isinstance(item, dict) and item.get("digest") is True:
            path = tc.contained(root, item["path"])
            digest = hashlib.sha256()
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
            result[item["path"]] = digest.hexdigest()
    return result


@contextmanager
def setup_use(root, cfg, requested, env):
    specs = group_specs(root, cfg, requested)
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
        if any(not current(root, key, spec, env) for key, spec in specs.items()):
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
                expected = fingerprint(root, spec)
                for argv in spec["commands"]:
                    chainman.execute(
                        root,
                        spec["profile"],
                        argv,
                        env=env,
                        cwd=tc.contained(root, spec["directory"]),
                        pass_fds=(lease.fileno(),),
                    )
                if not all(
                    artifact_ready(root, item, env) for item in spec["artifacts"]
                ):
                    raise ValueError(
                        f"Setup group {key} did not create its declared artifacts"
                    )
                if fingerprint(root, spec) != expected:
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


def setup_status(root, requested):
    """Inspect the same readiness contract without installing or blessing outputs."""
    cfg = configuration(root)
    with tc.operation(root, exclusive=False, new_execution=True):
        env = tc.environment(root)
        specs = group_specs(root, cfg, requested or list(cfg.get("setup", {})))
        with tc.operation_file(root, "setup-use.lock") as lease:
            try:
                fcntl.flock(lease, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError(
                    "Another setup operation is installing project artifacts"
                ) from None
            result = {key: current(root, key, spec, env) for key, spec in specs.items()}
            print(
                json.dumps(
                    {"schema": 1, "current": all(result.values()), "groups": result}
                )
            )
            return 0 if all(result.values()) else 1


def run(root: Path, action: str, extra: list[str], *, service_context=False):
    cfg = configuration(root)
    task_names = order(cfg.get("tasks", {}), [action]) if action != "setup" else []
    exclusive = any(cfg["tasks"][key].get("exclusive", False) for key in task_names)
    if exclusive and (
        service_context or any(cfg["tasks"][key].get("services") for key in task_names)
    ):
        raise ValueError(
            "Exclusive maintenance tasks cannot acquire or borrow services"
        )
    with tc.operation(
        root,
        exclusive=exclusive,
        new_execution=True,
        automatic_prune=cfg.get("cache", {}).get("automatic_prune", True),
    ):
        env = tc.environment(root)
        if action == "setup":
            with setup_use(root, cfg, extra or list(cfg.get("setup", {})), env):
                return 0
        if not service_context and any(
            cfg["tasks"][key].get("services") for key in task_names
        ):
            raise ValueError("Service tasks require the checked-in host bootstrap")
        groups = list(
            dict.fromkeys(
                group
                for task in task_names
                for group in cfg["tasks"][task].get("setup", [])
            )
        )
        with setup_use(root, cfg, groups, env) as descriptors:
            for key in task_names:
                spec = dict(cfg["tasks"][key])
                if "timeout_env" in spec:
                    configured = project_environment.apply(
                        root, cfg.get("environment", {}), env
                    )
                    configured.update(
                        project_environment.expand(
                            spec.get("environment", {}), root, configured
                        )
                    )
                    value = configured.get(spec["timeout_env"])
                    if value is not None:
                        if not value.isdecimal() or not 1 <= int(value) <= 86400:
                            raise ValueError(
                                f"{spec['timeout_env']} must be an integer between 1 and 86400"
                            )
                        spec["timeout_seconds"] = int(value)
                profile = spec.get(
                    "profile", cfg.get("project", {}).get("default_profile", "default")
                )
                with tc.compiler_cache(profile, env, root) as selected:
                    arguments = [list(argv) for argv in spec.get("commands", [])]
                    if key == action and extra and not arguments:
                        raise ValueError("A task without commands takes no arguments")
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
                                cwd=tc.contained(root, spec.get("directory", ".")),
                                pass_fds=descriptors,
                            )
                    else:
                        for argv in arguments:
                            chainman.execute(
                                root,
                                profile,
                                argv,
                                env=selected,
                                overrides=spec.get("environment", {}),
                                cwd=tc.contained(root, spec.get("directory", ".")),
                                pass_fds=descriptors,
                            )
                    if spec.get("wait_for_services", False):
                        wait_for_services()
    return 0


def wait_for_services():
    """Retain project/setup leases while the host observes backend availability."""
    import signal

    def stopped(number, _frame):
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
