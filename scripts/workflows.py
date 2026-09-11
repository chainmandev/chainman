"""Declared tasks and setup leases shared by consumer development workflows."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
from pathlib import Path
import re

import chainman
import toolchain as tc


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
            if not isinstance(spec, dict):
                raise ValueError(f"{section}.{key} must be a declaration")
            allowed = {"commands", "profile", "directory", "depends_on"} | (
                {"inputs", "artifacts"} if section == "setup" else {"setup", "services"}
            )
            if set(spec) - allowed:
                raise ValueError(
                    f"Unknown fields in {section}.{key}: {', '.join(sorted(set(spec) - allowed))}"
                )
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
                for pattern in spec["inputs"]:
                    if not isinstance(pattern, str):
                        raise ValueError("Setup input patterns must be strings")
                    tc.contained(root, pattern)
                for artifact in spec["artifacts"]:
                    if isinstance(artifact, str):
                        tc.contained(root, artifact)
                    elif (
                        isinstance(artifact, dict)
                        and set(artifact) == {"path", "interpreter"}
                        and artifact["interpreter"] == "python"
                    ):
                        tc.contained(root, artifact["path"])
                    else:
                        raise ValueError("Invalid setup readiness artifact")
            else:
                names(spec.get("setup", []))
                if spec.get("services"):
                    raise ValueError(
                        "Service workflows are pending backend qualification"
                    )
        order(entries, list(entries))
    for spec in cfg.get("tasks", {}).values():
        order(cfg.get("setup", {}), spec.get("setup", []))
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
        and all(tc.artifact_ready(root, item, env) for item in spec["artifacts"])
    )


@contextmanager
def setup_use(root, cfg, requested, env):
    selected = order(cfg.get("setup", {}), requested)
    if not selected:
        yield ()
        return
    specs = {key: group_spec(cfg, key) for key in selected}
    for key, spec in specs.items():
        spec["dependency_fingerprints"] = {
            dependency: fingerprint(root, specs[dependency])
            for dependency in spec.get("depends_on", [])
        }
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
                    tc.artifact_ready(root, item, env) for item in spec["artifacts"]
                ):
                    raise ValueError(
                        f"Setup group {key} did not create its declared artifacts"
                    )
                if fingerprint(root, spec) != expected:
                    raise ValueError(
                        f"Setup inputs changed during installation of {key}; readiness was not recorded"
                    )
                tc.atomic_json(stamp_path(root, key), {"fingerprint": expected})
            fcntl.flock(lease, fcntl.LOCK_SH)
        yield (lease.fileno(),)


def run(root: Path, action: str, extra: list[str]):
    cfg = configuration(root)
    with tc.operation(root, exclusive=False, new_execution=True, automatic_prune=False):
        env = tc.environment(root)
        if action == "setup":
            with setup_use(root, cfg, extra or list(cfg.get("setup", {})), env):
                return 0
        task_names = order(cfg.get("tasks", {}), [action])
        groups = list(
            dict.fromkeys(
                group
                for task in task_names
                for group in cfg["tasks"][task].get("setup", [])
            )
        )
        with setup_use(root, cfg, groups, env) as descriptors:
            for key in task_names:
                spec = cfg["tasks"][key]
                profile = spec.get(
                    "profile", cfg.get("project", {}).get("default_profile", "default")
                )
                with tc.compiler_cache(profile, env, root) as selected:
                    for index, argv in enumerate(spec["commands"]):
                        suffix = (
                            extra
                            if key == action and index == len(spec["commands"]) - 1
                            else []
                        )
                        chainman.execute(
                            root,
                            profile,
                            [*argv, *suffix],
                            env=selected,
                            cwd=tc.contained(root, spec.get("directory", ".")),
                            pass_fds=descriptors,
                        )
    return 0
