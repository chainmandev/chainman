"""Project commands, verified setup state, and bounded cache maintenance."""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
import contextlib
import errno
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
from urllib.parse import quote
from typing import IO, Literal, TextIO, TypedDict, Unpack, overload
import adapter_data as ad

RUNTIME = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ.get("CHAINMAN_ROOT", str(RUNTIME))).resolve()
_operation_fd: int | None = None
_operation_gate_fd: int | None = None
_operation_id = ""
_operation_compat_fd: int | None = None
_ancestor_fds: tuple[int, ...] = ()
_nix_root_fds: tuple[int, ...] = ()
_COMPILER_STARTUP_SECONDS = 120

type OperationState = tuple[int | None, int | None, str, int | None, tuple[int, ...]]


class ProcessIOOptions(TypedDict, total=False):
    stdin: int | IO[str] | IO[bytes] | None
    stdout: int | IO[str] | IO[bytes] | None
    stderr: int | IO[str] | IO[bytes] | None
    pass_fds: Sequence[int]
    start_new_session: bool
    close_fds: bool


class ProcessOptions(ProcessIOOptions, total=False):
    """The process controls used by managed runtime children."""

    cwd: str | Path | None
    env: Mapping[str, str] | None


class RunOptions(ProcessOptions, total=False):
    check: bool
    capture_output: bool
    timeout: float | None


def nix_path_reference(directory: Path, attribute: str) -> str:
    """Encode filesystem characters before using a path as a Nix flake URI."""
    return f"path:{quote(str(directory), safe='/')}#{attribute}"


def nix_command(env: Mapping[str, str] | None = None) -> str:
    """Retain the host/image Nix selected by the launcher across project shells."""
    selected = os.environ if env is None else env
    directory = selected.get("CHAINMAN_RUNTIME_NIX_BIN")
    return (
        str(Path(directory) / "nix")
        if directory
        else selected.get("CHAINMAN_NIX_BIN", "nix")
    )


def runtime_nix_environment(env: dict[str, str]) -> None:
    if env.get("TOOLCHAIN_CONTAINER") == "1":
        env["NIX_REMOTE"] = "daemon"
        # NIX_CONFIG's store setting takes precedence over NIX_REMOTE. Restore
        # the client policy after project shells, including their shell hooks.
        env["NIX_CONFIG"] = "build-users-group =\nstore = daemon"
        for name in ("NIX_STATE_DIR", "NIX_STORE_DIR", "NIX_DAEMON_SOCKET_PATH"):
            env.pop(name, None)
    directory = env.get("CHAINMAN_RUNTIME_NIX_BIN")
    if directory:
        if not Path(directory).is_absolute() or not (Path(directory) / "nix").is_file():
            raise ValueError("Invalid internal runtime Nix executable directory")
        env["PATH"] = os.pathsep.join(
            [
                directory,
                *[
                    part
                    for part in env.get("PATH", "").split(os.pathsep)
                    if part != directory
                ],
            ]
        )


@contextlib.contextmanager
def nix_root_gate(pool: Path) -> Iterator[None]:
    """Serialize publication and collection, not the commands using the roots."""
    pool.mkdir(mode=0o700, exist_ok=True)
    info = pool.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise ValueError("Nix root pool must be a private, owned directory")
    descriptor = os.open(pool / ".gate", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
        ):
            raise ValueError("Nix root gate must be an owned regular file")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def collect_nix_root(directory: Path) -> None:
    """Called under the pool gate; never collect a live or unrecognized entry."""
    try:
        info = directory.lstat()
    except FileNotFoundError:
        # Another admission may collect it after the last owner closes its FD.
        return
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        return
    try:
        descriptor = os.open(
            directory / ".lease", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        )
    except FileNotFoundError:
        return
    except OSError as error:
        if error.errno == errno.ELOOP:
            return
        raise
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
        ):
            return
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        shutil.rmtree(directory)
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def nix_temporary_directory(prefix: str) -> Iterator[str]:
    """Root packages through execution, then recover even from a killed owner.

    Kernel leases work across container PID namespaces. Managed children inherit
    them; a surviving child keeps its roots even after the original owner exits.
    Older unleased directories are outside this private pool and are untouched.
    """
    global _nix_root_fds
    if not prefix.startswith("chainman-") or any(
        c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in prefix
    ):
        raise ValueError("Invalid managed Nix root prefix")
    base = Path(
        "/nix/tmp"
        if os.environ.get("TOOLCHAIN_CONTAINER") == "1"
        else tempfile.gettempdir()
    )
    pool = base / f"chainman-gc-roots-{os.getuid()}"
    with nix_root_gate(pool):
        for entry in pool.iterdir():
            if entry.name.startswith("chainman-"):
                collect_nix_root(entry)
        directory = Path(tempfile.mkdtemp(prefix=prefix, dir=pool))
        descriptor = os.open(
            directory / ".lease",
            os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
        )
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    previous = _nix_root_fds
    _nix_root_fds = (*previous, descriptor)
    try:
        yield str(directory)
    finally:
        _nix_root_fds = previous
        # Do not LOCK_UN: forked children share this open-file description.
        os.close(descriptor)
        with nix_root_gate(pool):
            collect_nix_root(directory)


def entry_command(root: Path, profile: str) -> list[str]:
    if (root / "chainman.toml").is_file():
        return [
            sys.executable,
            str(RUNTIME / "scripts/chainman.py"),
            "--root",
            str(root),
            "exec",
            "--reuse-operation",
            "--profile",
            profile,
            "--",
        ]
    return [str(root / "scripts/enter.sh"), profile]


def contained(root: Path, relative: str) -> Path:
    """Require a relative, non-symlink path, including every existing parent."""
    if relative == ".":
        return root
    part = Path(relative)
    if (
        part.is_absolute()
        or not part.parts
        or any(p in ("..", ".git") for p in part.parts)
    ):
        raise ValueError(f"unsafe project path: {relative}")
    current = root
    for item in part.parts:
        current = current / item
        if current.is_symlink():
            raise ValueError(f"symlink in project path: {relative}")
    return current


def atomic_json(path: Path, value: object) -> None:
    atomic_bytes(path, (json.dumps(value, sort_keys=True) + "\n").encode())


def regular_input(root: Path, relative: str) -> bytes:
    path = contained(root, relative)
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("Input must be a regular file")
    with path.open("rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Input must be a regular file")
        return stream.read()


def local_source(root: Path, base: Path, relative: str) -> Path:
    """Resolve sibling sources lexically without concealing a symlink traversal."""
    if not isinstance(relative, str) or Path(relative).is_absolute():
        raise ValueError("Local dependencies require a project-relative source")
    current = contained(root, str(base.relative_to(root)))
    for part in Path(relative).parts:
        if part == "..":
            if current == root:
                raise ValueError("Local dependency escapes the adopted project")
            current = current.parent
        else:
            current = contained(root, str((current / part).relative_to(root)))
    return current


def atomic_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
    if path.is_symlink() or (path.exists() and not stat.S_ISREG(path.lstat().st_mode)):
        raise ValueError("Output must be a regular file")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
            temporary = Path(f.name)
            os.fchmod(f.fileno(), mode)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def configuration_root(root: Path) -> Path:
    authority = os.environ.get("CHAINMAN_ENTRY_AUTHORITY")
    if authority:
        selected = Path(authority)
        if not selected.is_absolute():
            raise ValueError("Entry authority requires an absolute path")
        if regular_input(selected, "authority-root").decode().strip() == str(root):
            return selected
    return root


@dataclass(frozen=True)
class CachePolicy:
    build_limit_gib: float
    compiler_limit_gib: float
    stale_hours: float
    automatic_prune: bool
    preserve_environment: tuple[str, ...]

    @classmethod
    def decode(cls, cfg: Mapping[str, object]) -> CachePolicy:
        import configuration

        cache = configuration.table(cfg.get("cache", {}), "cache")
        numbers: dict[str, float] = {}
        for key, default in (
            ("build_limit_gib", 12),
            ("compiler_limit_gib", 8),
            ("stale_hours", 48),
        ):
            value = cache.get(key, default)
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or value < 0
                or (key == "compiler_limit_gib" and value == 0)
            ):
                raise ValueError(
                    f"cache.{key} must be finite and nonnegative; compiler_limit_gib must be positive"
                )
            numbers[key] = value
        automatic = cache.get("automatic_prune", True)
        if type(automatic) is not bool:
            raise ValueError("cache.automatic_prune must be boolean")
        return cls(
            build_limit_gib=numbers["build_limit_gib"],
            compiler_limit_gib=numbers["compiler_limit_gib"],
            stale_hours=numbers["stale_hours"],
            automatic_prune=automatic,
            preserve_environment=tuple(
                ad.strings(
                    cache.get("preserve_environment", []), "Cache preserved environment"
                )
            ),
        )


def config(root: Path = ROOT) -> ad.Table:
    selected = configuration_root(root)
    name = (
        "chainman.toml" if (selected / "chainman.toml").exists() else "toolchain.toml"
    )
    data = ad.table(
        tomllib.loads(regular_input(selected, name).decode()), "Project configuration"
    )
    if name == "chainman.toml":
        data.setdefault("modules", ["project"])
    schemas = (1, 2, 3) if name == "chainman.toml" else (1,)
    if (
        type(data.get("schema")) is not int
        or data["schema"] not in schemas
        or not data.get("modules")
    ):
        raise ValueError(
            f"{name} requires a supported schema and a nonempty modules list"
        )
    import configuration

    data, _ = configuration.compile(data)
    CachePolicy.decode(data)
    return data


def module(name: str, root: Path = ROOT) -> ad.Table:
    if not name.replace("-", "").isalnum():
        raise ValueError("invalid module name")
    if name == "project" and (root / "chainman.toml").exists():
        cfg = config(root)
        setup = ad.table(cfg.get("setup", {}), "Setup")
        data: ad.Table = {
            "name": name,
            "directory": ".",
            "profile": ad.table(cfg.get("project", {}), "Project").get(
                "default_profile", "default"
            ),
            "commands": cfg.get("commands", {}),
            "inputs": setup.get("inputs", []),
            "artifacts": setup.get("artifacts", []),
            "cache_setup": bool(setup.get("inputs")),
        }
    else:
        data = ad.table(
            tomllib.loads(contained(root, f"modules/{name}.toml").read_text()), "Module"
        )
    if data.get("name") != name:
        raise ValueError("module identity mismatch")
    contained(root, ad.text(data["directory"], "Module directory"))
    for commands in ad.table(data.get("commands", {}), "Module actions").values():
        if not isinstance(commands, list) or any(
            not isinstance(c, list) or not c or any(not isinstance(a, str) for a in c)
            for c in commands
        ):
            raise ValueError("module commands must be nonempty argument arrays")
    return data


def context_id() -> str:
    mode = os.environ.get("CHAINMAN_MODE", os.environ.get("TOOLCHAIN_MODE", "host-nix"))
    network = (
        "-network-host"
        if mode == "container-nix"
        and os.environ.get("CHAINMAN_CONTAINER_NETWORK_MODE") == "host"
        else ""
    )
    return f"{mode}-{platform.system().lower()}-{platform.machine()}{network}"


def cache_root(root: Path = ROOT) -> Path:
    return contained(root, ".cache/toolchain")


@contextlib.contextmanager
def operation_file(
    root: Path, name: str, *, create: bool = True, unique: bool = False
) -> Iterator[TextIO]:
    path = contained(root, f".cache/toolchain/{name}")
    if path.exists() and not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("Operation lock must be a regular file")
    flags = os.O_RDWR | os.O_NOFOLLOW
    if create:
        flags |= os.O_CREAT
    if unique:
        flags |= os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "a") as lock:
        if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
            raise ValueError("Operation lock must be a regular file")
        yield lock


@contextlib.contextmanager
def operation_gate(root: Path) -> Iterator[None]:
    # This mutex covers admission and stale-lease inspection only, never a child
    # command. A writer retains writer.lock after admission.
    with operation_file(root, "operation-admission.lock") as gate:
        fcntl.flock(gate, fcntl.LOCK_EX)
        yield


def acquire_operation(descriptor: int) -> None:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise ValueError(
            "Another managed operation is active; no cleanup or concurrent update was started."
        ) from None


def active_operations(root: Path) -> set[tuple[int, int]]:
    """Inspect leases only while holding the admission mutex and writer gate."""
    active = set()
    for path in contained(root, ".cache/toolchain/operations").iterdir():
        with operation_file(root, f"operations/{path.name}", create=False) as lease:
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                metadata = os.fstat(lease.fileno())
                active.add((metadata.st_dev, metadata.st_ino))
            else:
                path.unlink()
    return active


def descriptor_identities(descriptors: Iterable[int]) -> set[tuple[int, int]]:
    result = set()
    for descriptor in descriptors:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("Operation descriptors must identify regular files")
        result.add((metadata.st_dev, metadata.st_ino))
    return result


@contextlib.contextmanager
def operation_state(
    descriptor: int | None,
    gate: int | None,
    identity: str,
    compat: int | None,
    ancestors: tuple[int, ...],
) -> Iterator[None]:
    global \
        _operation_fd, \
        _operation_gate_fd, \
        _operation_id, \
        _operation_compat_fd, \
        _ancestor_fds
    previous = (
        _operation_fd,
        _operation_gate_fd,
        _operation_id,
        _operation_compat_fd,
        _ancestor_fds,
    )
    (
        _operation_fd,
        _operation_gate_fd,
        _operation_id,
        _operation_compat_fd,
        _ancestor_fds,
    ) = descriptor, gate, identity, compat, ancestors
    try:
        yield
    finally:
        (
            _operation_fd,
            _operation_gate_fd,
            _operation_id,
            _operation_compat_fd,
            _ancestor_fds,
        ) = previous


def inherited_operation() -> OperationState:
    if _operation_fd is not None:
        return (
            _operation_fd,
            _operation_gate_fd,
            _operation_id,
            _operation_compat_fd,
            _ancestor_fds,
        )
    value = os.environ.get("TOOLCHAIN_LOCK_FD")
    if value is None:
        return None, None, "", None, ()
    descriptor = int(value)
    gate_value = os.environ.get("TOOLCHAIN_GATE_FD")
    compat_value = os.environ.get("TOOLCHAIN_COMPAT_FD")
    identity = os.environ.get("TOOLCHAIN_OPERATION_ID", "")
    if identity and (
        len(identity) != 12 or any(c not in "0123456789abcdef" for c in identity)
    ):
        raise ValueError("Invalid inherited operation identity")
    ancestors = json.loads(os.environ.get("TOOLCHAIN_ANCESTOR_FDS", "[]"))
    if not isinstance(ancestors, list) or any(
        type(fd) is not int or fd < 0 for fd in ancestors
    ):
        raise ValueError("Invalid inherited ancestor descriptors")
    # Legacy entry held operation.lock exclusively. Keep it intact during an
    # incoming runtime handoff; never convert that inherited kernel lock.
    # An old exclusive compatibility lease is not a modern writer gate: nested
    # writers must still exclude later modern siblings through writer.lock.
    gate = int(gate_value) if gate_value is not None else None
    compat = (
        int(compat_value)
        if compat_value is not None
        else (None if identity else descriptor)
    )
    try:
        descriptor_identities(
            [
                descriptor,
                *ancestors,
                *([gate] if gate is not None else []),
                *([compat] if compat is not None else []),
            ]
        )
    except OSError as error:
        if error.errno != errno.EBADF:
            raise
        # Subprocess wrappers can retain the environment while closing FDs.
        # Re-enter ordinary admission without borrowing any parent ownership.
        # A live parent still excludes mutation through its own kernel lease.
        return None, None, "", None, ()
    return descriptor, gate, identity, compat, tuple(ancestors)


def descriptor_matches(descriptor: int, path: Path) -> bool:
    actual = os.fstat(descriptor)
    expected = path.lstat() if path.exists() else None
    return (
        expected is not None
        and stat.S_ISREG(actual.st_mode)
        and (actual.st_dev, actual.st_ino) == (expected.st_dev, expected.st_ino)
    )


@contextlib.contextmanager
def operation(
    root: Path = ROOT,
    *,
    exclusive: bool = True,
    automatic_prune: bool = False,
    new_execution: bool = False,
) -> Iterator[bool]:
    cache_root(root).mkdir(parents=True, exist_ok=True)
    contained(root, ".cache/toolchain/operations").mkdir(exist_ok=True)
    descriptor, inherited_gate, identity, inherited_compat, ancestors = (
        inherited_operation()
    )
    compat_path = contained(root, ".cache/toolchain/operation.lock")
    writer_path = contained(root, ".cache/toolchain/writer.lock")
    lease_path = (
        contained(root, f".cache/toolchain/operations/{identity}")
        if identity
        else compat_path
    )
    same = descriptor is not None and descriptor_matches(descriptor, lease_path)
    if same:
        if inherited_compat is None or not descriptor_matches(
            inherited_compat, compat_path
        ):
            raise ValueError(
                "Inherited compatibility lease does not belong to this project"
            )
        if inherited_gate is not None and not descriptor_matches(
            inherited_gate, writer_path
        ):
            raise ValueError("Inherited writer gate does not belong to this project")
    owners = tuple(
        dict.fromkeys(
            (
                *ancestors,
                *[
                    fd
                    for fd in (descriptor, inherited_gate, inherited_compat)
                    if fd is not None
                ],
            )
        )
    )
    nested_project = any(descriptor_matches(fd, compat_path) for fd in owners)
    if same and not new_execution:
        if exclusive and inherited_gate is None:
            with operation_file(root, "writer.lock") as gate:
                with operation_gate(root):
                    acquire_operation(gate.fileno())
                    if active_operations(root) - descriptor_identities(owners):
                        raise ValueError(
                            "Another managed operation is active; close independent commands before updating"
                        )
                with operation_state(
                    descriptor, gate.fileno(), identity, inherited_compat, ancestors
                ):
                    yield False
        else:
            if exclusive:
                with operation_gate(root):
                    if active_operations(root) - descriptor_identities(owners):
                        raise ValueError(
                            "Another managed operation is active; close independent commands before updating"
                        )
            with operation_state(
                descriptor, inherited_gate, identity, inherited_compat, ancestors
            ):
                yield False
        return
    with contextlib.ExitStack() as stack:
        if same:
            compat_descriptor = inherited_compat
        else:
            compat = stack.enter_context(operation_file(root, "operation.lock"))
            try:
                fcntl.flock(compat, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError(
                    "Another managed operation is active; an older runtime owns exclusive access"
                ) from None
            compat_descriptor = compat.fileno()
        writer = (
            None
            if same and inherited_gate is not None
            else stack.enter_context(operation_file(root, "writer.lock"))
        )
        with operation_gate(root):
            if writer is not None:
                acquire_operation(writer.fileno())
            active = active_operations(root)
            if exclusive and active - descriptor_identities(owners):
                raise ValueError(
                    "Another managed operation is active; no cleanup or concurrent update was started."
                )
            identity = uuid.uuid4().hex[:12]
            lease = stack.enter_context(
                operation_file(root, f"operations/{identity}", unique=True)
            )
            acquire_operation(lease.fileno())
            if (
                automatic_prune
                and not host_mode()
                and not active
                and not nested_project
            ):
                prune(root)
            gate_descriptor = (
                inherited_gate
                if writer is None
                else (writer.fileno() if exclusive else None)
            )
            if writer is not None and not exclusive:
                writer.close()
        with operation_state(
            lease.fileno(), gate_descriptor, identity, compat_descriptor, owners
        ):
            yield not nested_project


def managed_options[Options: ProcessOptions](kwargs: Options) -> Options:
    """Retain every outstanding project lease through nested managed children."""
    options: ProcessOptions = kwargs
    options["pass_fds"] = tuple(
        dict.fromkeys((*options.get("pass_fds", ()), *_nix_root_fds))
    )
    descriptor, gate, identity, compat, ancestors = inherited_operation()
    if descriptor is not None:
        inherited = options.get("env")
        env = dict(os.environ if inherited is None else inherited)
        env["TOOLCHAIN_LOCK_FD"] = str(descriptor)
        env["TOOLCHAIN_OPERATION_ID"] = identity
        ancestors = tuple(dict.fromkeys((*ancestors, *options.get("pass_fds", ()))))
        env["TOOLCHAIN_ANCESTOR_FDS"] = json.dumps(ancestors)
        descriptors = [descriptor, *ancestors]
        for name, value in (
            ("TOOLCHAIN_GATE_FD", gate),
            ("TOOLCHAIN_COMPAT_FD", compat),
        ):
            if value is None:
                env.pop(name, None)
            else:
                env[name] = str(value)
                descriptors.append(value)
        descriptor_identities(descriptors)
        options["env"] = env
        options["pass_fds"] = tuple(dict.fromkeys(descriptors))
    return kwargs


@overload
def managed_run(
    argv: Sequence[str | Path],
    *,
    text: Literal[True],
    input: str | None = None,
    **kwargs: Unpack[RunOptions],
) -> subprocess.CompletedProcess[str]: ...


@overload
def managed_run(
    argv: Sequence[str | Path],
    *,
    text: Literal[False] = False,
    input: bytes | None = None,
    **kwargs: Unpack[RunOptions],
) -> subprocess.CompletedProcess[bytes]: ...


@overload
def managed_run(
    argv: Sequence[str | Path],
    *,
    text: bool,
    input: str | bytes | None = None,
    **kwargs: Unpack[RunOptions],
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]: ...


def managed_run(
    argv: Sequence[str | Path],
    *,
    text: bool = False,
    input: str | bytes | None = None,
    **kwargs: Unpack[RunOptions],
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    return subprocess.run(argv, text=text, input=input, **managed_options(kwargs))


PNPM_STORE_VARIABLES = (
    "PNPM_CONFIG_STORE_DIR",
    "PNPM_STORE_DIR",
    "npm_config_store_dir",
)
PNPM_SETTING_VARIABLES = (
    PNPM_STORE_VARIABLES,
    (
        "PNPM_CONFIG_ENABLE_GLOBAL_VIRTUAL_STORE",
        "pnpm_config_enable_global_virtual_store",
        "npm_config_enable_global_virtual_store",
    ),
    (
        "PNPM_CONFIG_VERIFY_DEPS_BEFORE_RUN",
        "pnpm_config_verify_deps_before_run",
        "npm_config_verify_deps_before_run",
    ),
)


def pnpm_environment(env: dict[str, str], values: dict[str, str]) -> None:
    """Keep pnpm versions and nested scripts on the same explicit settings."""
    for aliases in PNPM_SETTING_VARIABLES:
        for name in aliases:
            if name in values:
                env.update(dict.fromkeys(aliases, values[name]))
                break


def host_mode() -> bool:
    """The deliberately unprovisioned execution mode."""
    return os.environ.get("CHAINMAN_MODE") == "host"


def environment(root: Path = ROOT, *, create: bool = True) -> dict[str, str]:
    env = dict(os.environ)
    if host_mode():
        env.update(
            CHAINMAN_ROOT=str(root.resolve()),
            CHAINMAN_PROJECT_ROOT=str(root.resolve()),
            CHAINMAN_RUNTIME=str(RUNTIME),
        )
        return env
    if _operation_id and env.get("TOOLCHAIN_OPERATION_ID") != _operation_id:
        # Public child commands own their server lifetime. Reusing the parent
        # owner with a new socket would spawn an unmanaged daemon; reusing its
        # socket would let parent exit stop an active child's compiler.
        env.pop("CHAINMAN_COMPILER_OWNER", None)
    work = contained(root, f".cache/toolchain/work/{context_id()}")
    if create:
        work.mkdir(parents=True, exist_ok=True)
    downloads = Path(
        env.get(
            "TOOLCHAIN_DOWNLOAD_CACHE",
            str(
                Path(env.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
                / "nix-just-downloads"
            ),
        )
    )
    if not downloads.is_absolute():
        raise ValueError("Download-cache overrides must select an absolute path")
    # Shared package caches and sccache are separate from project build outputs;
    # package managers own their cache locking.
    if create:
        downloads.mkdir(parents=True, exist_ok=True)
    socket_directory = Path("/tmp").resolve() / f"nix-just-sockets-{os.getuid()}"
    if socket_directory.is_symlink():
        raise ValueError("compiler socket directory must not be a symlink")
    if create:
        socket_directory.mkdir(mode=0o700, exist_ok=True)
    if socket_directory.exists() and (
        socket_directory.stat().st_uid != os.getuid()
        or socket_directory.stat().st_mode & 0o077
    ):
        raise ValueError(
            "compiler socket directory must be private to the current user"
        )
    socket_name = hashlib.sha256(
        (str(root.resolve()) + context_id()).encode()
    ).hexdigest()[:24]
    # Independent Rust commands own separate foreground servers. Descendants
    # share the operation identity and endpoint; the on-disk compiler cache is
    # still shared and its implementation owns concurrent access to those bytes.
    identity = _operation_id or os.environ.get("TOOLCHAIN_OPERATION_ID", "")
    if identity:
        if len(identity) != 12 or any(c not in "0123456789abcdef" for c in identity):
            raise ValueError("Invalid inherited operation identity")
        socket_name += identity
    cache_policy = CachePolicy.decode(config(root))
    env.update(
        CARGO_HOME=str(downloads / "cargo"),
        CARGO_TARGET_DIR=str(work / "cargo"),
        SCCACHE_DIR=str(downloads / "sccache"),
        SCCACHE_CACHE_SIZE=str(int(cache_policy.compiler_limit_gib * 1024**3)),
        SCCACHE_SERVER_UDS=str(socket_directory / socket_name),
        RUSTC_WRAPPER="",
        CARGO_INCREMENTAL="0",
        UV_CACHE_DIR=str(downloads / "uv"),
        RUFF_CACHE_DIR=str(work / "ruff"),
        PIP_CACHE_DIR=str(downloads / "pip"),
        GOMODCACHE=str(downloads / "go-mod"),
        GOCACHE=str(work / "go-build"),
        PUB_CACHE=str(downloads / "pub"),
        GRADLE_USER_HOME=str(downloads / "gradle"),
        PYTHONDONTWRITEBYTECODE="1",
        GOTOOLCHAIN="local",
        TOOLCHAIN_WORK=str(work),
        TOOLCHAIN_DOWNLOAD_CACHE=str(downloads),
    )
    pnpm_environment(
        env,
        {
            "PNPM_CONFIG_STORE_DIR": str(downloads / "pnpm"),
            # pnpm changes its default virtual-store layout in CI. Stable
            # layout avoids replacing a setup still leased by running tasks.
            "PNPM_CONFIG_ENABLE_GLOBAL_VIRTUAL_STORE": "false",
            # Frozen installation belongs to setup; run/exec may only check it.
            "PNPM_CONFIG_VERIFY_DEPS_BEFORE_RUN": "error",
        },
    )
    # Keep build JVMs within the command lifetime. Gradle may use a single-use
    # daemon for JVM settings, but it exits after the build; Kotlin stays in it.
    # Explicit project/profile options remain available through environment.
    env.setdefault(
        "GRADLE_OPTS",
        "-Dorg.gradle.daemon=false "
        "-Dorg.gradle.project.kotlin.compiler.execution.strategy=in-process",
    )
    preserved = {}
    for name in cache_policy.preserve_environment:
        if name in {
            "RUSTC_WRAPPER",
            "SCCACHE_SERVER_UDS",
            "TOOLCHAIN_LOCK_FD",
            "TOOLCHAIN_GATE_FD",
            "TOOLCHAIN_OPERATION_ID",
            "TOOLCHAIN_COMPAT_FD",
            "TOOLCHAIN_ANCESTOR_FDS",
        }:
            raise ValueError("Cannot override managed compiler-cache lifecycle")
        if name in os.environ:
            env[name] = os.environ[name]
            preserved[name] = os.environ[name]
    pnpm_environment(env, preserved)
    # A refreshed subprocess may execute immutable runtime code outside this
    # project (notably a disposable source preview). Bind its data root explicitly
    # after inherited cache settings; source location is never project authority.
    env.update(
        CHAINMAN_ROOT=str(root.resolve()),
        CHAINMAN_PROJECT_ROOT=str(root.resolve()),
        CHAINMAN_RUNTIME=str(RUNTIME),
    )
    if create:
        (work / "last-used").touch()
    return env


@contextlib.contextmanager
def compiler_cache(
    profile: str | None, env: dict[str, str], root: Path = ROOT
) -> Iterator[dict[str, str]]:
    if host_mode():
        yield env
        return
    profiles = ad.table(config(root).get("profiles", {}), "Profiles")
    owns_cache = ad.table(
        profiles.get(profile, {}) if profile is not None else {}, "Profile"
    ).get("compiler_cache", False)
    if profile is None or (profile != "rust" and not owns_cache):
        yield env
        return
    if env.get("CHAINMAN_COMPILER_OWNER") == str(root):
        yield env
        return
    endpoint = Path(env["SCCACHE_SERVER_UDS"])
    if endpoint.exists() or endpoint.is_symlink():
        raise ValueError(
            "Compiler cache endpoint already exists; inspect the previous Rust operation before stopping its server"
        )
    prefix = entry_command(root, profile)
    server_env = dict(
        env,
        SCCACHE_START_SERVER="1",
        SCCACHE_NO_DAEMON="1",
        # The owned server spans the entire operation, including long browser or
        # service phases with no Rust compilation. Cleanup owns its shutdown.
        SCCACHE_IDLE_TIMEOUT="0",
        CHAINMAN_COMPILER_OWNER=str(root),
    )
    import native_tasks

    # The same standard Nix profile roots the preflight closure throughout the
    # server's lifetime. A successful preflight alone leaves a GC race before
    # the second entry that starts sccache.
    with nix_temporary_directory("chainman-compiler-") as directory:
        executable_file = Path(directory) / "executable"
        resolve_command = [
            "sh",
            "-eu",
            "-c",
            'command -v sccache > "$1"',
            "chainman-compiler",
            str(executable_file),
        ]
        if (root / "chainman.toml").is_file():
            import chainman

            chainman.execute(
                root,
                profile,
                resolve_command,
                env=dict(env, CHAINMAN_COMPILER_OWNER=str(root)),
                gc_root=Path(directory) / "profile",
                stdout=sys.stderr,
            )
        else:
            managed_run(
                [*prefix, *resolve_command],
                cwd=root,
                env=dict(env, CHAINMAN_COMPILER_OWNER=str(root)),
                check=True,
                stdout=sys.stderr,
            )
        executable = executable_file.read_text().removesuffix("\n")
        if (
            not Path(executable).is_absolute()
            or "\n" in executable
            or "\r" in executable
            or not os.access(executable, os.X_OK)
        ):
            raise ValueError(
                "Compiler cache executable must resolve to an absolute executable path"
            )
        # The shared native command owner contains both the Nix launcher and
        # foreground compiler. Failed startup uses the same bounded cleanup as
        # ordinary finite tasks, including children of an intermediate launcher.
        with native_tasks.command(
            root, [[*prefix, "sccache"]], {"shutdown_seconds": 5}
        ) as command:
            with owned_compiler_cache(root, env, executable, server_env, command):
                yield dict(
                    env, RUSTC_WRAPPER="sccache", CHAINMAN_COMPILER_OWNER=str(root)
                )


@contextlib.contextmanager
def owned_compiler_cache(
    root: Path,
    env: dict[str, str],
    executable: str,
    server_env: dict[str, str],
    command: list[str],
) -> Iterator[dict[str, str]]:
    endpoint = Path(env["SCCACHE_SERVER_UDS"])
    lifecycle_name = "compiler-" + uuid.uuid4().hex + ".lock"
    # Keep a distinct lifetime lease in the actual compiler process and every
    # intermediate launcher. Reaping only the launcher does not prove its child
    # has exited. The parent closes its copy immediately after spawn.
    with operation_file(root, lifecycle_name, unique=True) as lifecycle:
        acquire_operation(lifecycle.fileno())
        options: ProcessOptions = {
            "cwd": root,
            "env": server_env,
            "stdout": sys.stderr,
            "pass_fds": (lifecycle.fileno(),),
        }
        server = subprocess.Popen(command, **managed_options(options))
    deadline = time.monotonic() + _COMPILER_STARTUP_SECONDS
    owned_socket = None
    try:
        while True:
            if server.poll() is not None:
                raise ValueError("Compiler cache server exited before becoming ready")
            try:
                with socket.socket(socket.AF_UNIX) as connection:
                    connection.connect(str(endpoint))
                owned_socket = endpoint.lstat()
                if not stat.S_ISSOCK(owned_socket.st_mode):
                    raise ValueError("Compiler cache endpoint is not a socket")
                break
            except (FileNotFoundError, ConnectionRefusedError) as error:
                if time.monotonic() >= deadline:
                    raise ValueError("Compiler cache startup timed out") from error
                time.sleep(0.05)
    except BaseException as failure:
        try:
            stop_owned_compiler(server)
            release_compiler_lifetime(root, lifecycle_name, endpoint, owned_socket)
        except BaseException as cleanup:
            failure.add_note(f"Compiler cache startup cleanup also failed: {cleanup}")
            print(
                f"Compiler cache startup cleanup also failed: {cleanup}",
                file=sys.stderr,
            )
        raise
    primary = None
    try:
        yield dict(env, RUSTC_WRAPPER="sccache", CHAINMAN_COMPILER_OWNER=str(root))
    except BaseException as exc:
        primary = exc
        raise
    finally:
        try:
            if server.poll() is None:
                try:
                    # The preflight profile keeps this executable alive. Cleanup
                    # must not queue another Nix evaluation behind a build or GC.
                    managed_run(
                        [executable, "--stop-server"],
                        cwd=root,
                        env=dict(env, CHAINMAN_COMPILER_OWNER=str(root)),
                        check=True,
                        timeout=15,
                        stdout=sys.stderr,
                    )
                except subprocess.CalledProcessError as stop_failure:
                    # The socket may close before the owned process exits.
                    # Reap that process to preserve its actual exit status;
                    # a still-live server must retain the stop failure.
                    try:
                        server.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        raise stop_failure from None
                except subprocess.TimeoutExpired:
                    raise ValueError(
                        "Compiler cache stop request timed out; inspect the owned server and its operation lock"
                    ) from None
            try:
                status = server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                raise ValueError(
                    "Compiler cache shutdown timed out; the server retains the operation lock until it exits"
                ) from None
            release_compiler_lifetime(root, lifecycle_name, endpoint, owned_socket)
            if status:
                raise ValueError(f"Compiler cache server exited with status {status}")
        except BaseException as cleanup:
            if server.poll() is None:
                try:
                    stop_owned_compiler(server)
                    release_compiler_lifetime(
                        root, lifecycle_name, endpoint, owned_socket
                    )
                except BaseException as forced_cleanup:
                    cleanup.add_note(
                        f"Owned compiler termination also failed: {forced_cleanup}"
                    )
            if primary is None:
                raise
            primary.add_note(f"Compiler cache cleanup also failed: {cleanup}")
            print(f"Compiler cache cleanup also failed: {cleanup}", file=sys.stderr)


def stop_owned_compiler(server: subprocess.Popen[bytes]) -> None:
    if server.poll() is None:
        server.terminate()
    try:
        server.wait(timeout=15)
    except subprocess.TimeoutExpired:
        raise ValueError(
            "Compiler owner did not finish cleanup; its operation lease remains authoritative"
        ) from None


def release_compiler_lifetime(
    root: Path, lifecycle_name: str, endpoint: Path, owned_socket: os.stat_result | None
) -> None:
    with operation_file(root, lifecycle_name, create=False) as lifecycle:
        try:
            fcntl.flock(lifecycle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(
                "Compiler cache process is still active after its launcher exited; its endpoint was preserved"
            ) from None
        contained(root, f".cache/toolchain/{lifecycle_name}").unlink()
    if owned_socket is not None and (endpoint.exists() or endpoint.is_symlink()):
        current = endpoint.lstat()
        if not stat.S_ISSOCK(current.st_mode) or (
            current.st_dev,
            current.st_ino,
        ) != (owned_socket.st_dev, owned_socket.st_ino):
            raise ValueError("Compiler cache endpoint changed; it was preserved")
        endpoint.unlink()


def fingerprint(spec: Mapping[str, object], root: Path = ROOT) -> str:
    digest = hashlib.sha256()
    digest.update(json.dumps([2, context_id(), spec], sort_keys=True).encode())
    paths = {root / "toolchain.toml", root / "chainman.toml", root / "chainman.lock"}
    digest.update(str(RUNTIME).encode())
    if (root / "chainman.toml").exists():
        import chainman

        profile = ad.text(spec["profile"], "Module profile")
        ref, _ = chainman.profile(root, profile)
        digest.update(chainman.profile_fingerprint(root, profile, ref).encode())
    for directory in ("scripts", "nix", "modules"):
        paths.update(
            p
            for p in (root / directory).rglob("*")
            if p.is_file() and "__pycache__" not in p.parts
        )
    for pattern in ad.strings(spec.get("inputs", []), "Module inputs"):
        paths.update(root.glob(pattern))
    for p in sorted(paths):
        if p.is_file():
            relative = p.relative_to(root).as_posix()
            contained(root, relative)
            digest.update(relative.encode() + b"\0" + p.read_bytes() + b"\0")
    return digest.hexdigest()


def run_commands(
    spec: Mapping[str, object], action: str, env: dict[str, str], root: Path = ROOT
) -> None:
    commands = ad.table(spec.get("commands", {}), "Module commands").get(action)
    if commands is None:
        raise ValueError(f"{spec['name']} does not define {action}")
    if spec.get("native") == "darwin" and (
        platform.system() != "Darwin" or env.get("TOOLCHAIN_CONTAINER") == "1"
    ):
        raise ValueError(
            f"{spec['name']} requires a macOS host with Xcode; this lane has not run"
        )
    cwd = contained(root, ad.text(spec["directory"], "Module directory"))
    profile = ad.text(spec["profile"], "Module profile")

    def launch(argv: list[str], selected_env: dict[str, str]) -> None:
        # The working directory travels as an argument, never shell syntax.
        launch = [
            *entry_command(root, profile),
            "sh",
            "-eu",
            "-c",
            'cd "$1"; shift; exec "$@"',
            "sh",
            str(cwd),
            *argv,
        ]
        managed_run(launch, cwd=root, env=selected_env, check=True)

    with compiler_cache(profile, env, root) as selected_env:
        for argv in ad.array(commands, "Module commands"):
            launch(ad.strings(argv, "Module command"), selected_env)


def artifact_ready(root: Path, artifact: object, env: dict[str, str]) -> bool:
    if isinstance(artifact, str):
        return contained(root, artifact).exists()
    if (
        not isinstance(artifact, dict)
        or set(artifact) != {"path", "interpreter"}
        or artifact["interpreter"] != "python"
    ):
        raise ValueError("Unknown generated artifact readiness rule")
    path = Path(artifact["path"])
    parent = contained(root, str(path.parent))
    candidate = parent / path.name
    expected = Path(env["UV_PYTHON"])
    if not expected.is_absolute() or not str(expected).startswith("/nix/store/"):
        raise ValueError(
            "Virtual environment interpreter must come from the pinned Nix shell"
        )
    # Permit only this declared interpreter link into the selected Nix Python.
    # Source and cleanup containment never use this special readiness rule.
    return candidate.is_file() and candidate.resolve() == expected.resolve()


def setup_reason(
    spec: Mapping[str, object], env: dict[str, str], root: Path = ROOT
) -> str | None:
    if not spec.get("cache_setup", True):
        return "uncached legacy setup"
    stamp = contained(root, f".cache/toolchain/setup/{spec['name']}.json")
    try:
        recorded: object = json.loads(stamp.read_text())
    except (FileNotFoundError, ValueError):
        recorded = {}
    if (
        isinstance(recorded, dict)
        and recorded.get("fingerprint") == fingerprint(spec, root)
        and all(
            artifact_ready(root, item, env)
            for item in ad.array(spec.get("artifacts", []), "Module artifacts")
        )
    ):
        return None
    return "legacy setup inputs or artifacts changed"


def setup_many(
    specs: Sequence[Mapping[str, object]],
    env: dict[str, str],
    root: Path = ROOT,
    *,
    explicit: bool = False,
) -> None:
    import setup_readiness

    setup_readiness.policy(env)
    repairs = {
        ad.text(spec["name"], "Module name"): reason
        for spec in specs
        if (reason := setup_reason(spec, env, root)) is not None
    }
    if repairs and not explicit:
        setup_readiness.authorize(
            repairs, env, recovery=["just", "chainman", "modules", "setup"]
        )
    # Admit the complete module operation before installing or running any task.
    for spec in specs:
        setup(spec, env, root, explicit=True)


def setup(
    spec: Mapping[str, object],
    env: dict[str, str],
    root: Path = ROOT,
    *,
    explicit: bool = False,
) -> None:
    import setup_readiness

    setup_readiness.policy(env)
    reason = setup_reason(spec, env, root)
    if reason is None:
        return
    if not explicit:
        setup_readiness.authorize(
            {ad.text(spec["name"], "Module name"): reason},
            env,
            recovery=["just", "chainman", "modules", "setup"],
        )
    if not spec.get("cache_setup", True):
        # Opaque legacy adapters do not receive a fingerprint-based stamp.
        run_commands(spec, "setup", env, root)
        return
    stamp = contained(root, f".cache/toolchain/setup/{spec['name']}.json")
    expected = fingerprint(spec, root)
    artifacts = ad.array(spec.get("artifacts", []), "Module artifacts")
    stamp.unlink(missing_ok=True)
    run_commands(spec, "setup", env, root)
    if not all(artifact_ready(root, p, env) for p in artifacts):
        raise ValueError(f"{spec['name']} setup did not create its declared artifacts")
    if fingerprint(spec, root) != expected:
        raise ValueError(
            f"{spec['name']} setup inputs changed during installation; readiness was not recorded"
        )
    atomic_json(stamp, {"fingerprint": expected})


def size(path: Path, *, allow_external_links: bool = False) -> int:
    if path.is_symlink():
        raise ValueError("cache inventory refuses symlinks")
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    boundary = path.resolve()
    for parent, dirs, files in os.walk(path, followlinks=False):
        for name in dirs + files:
            item = Path(parent) / name
            if item.is_symlink() and not allow_external_links:
                try:
                    target = item.resolve()
                except RuntimeError:
                    raise ValueError("cache inventory refuses symlink loops") from None
                if not target.is_relative_to(boundary):
                    raise ValueError("cache inventory refuses symlink escapes")
            if item.is_symlink() or not item.is_dir():
                total += item.lstat().st_size
    return total


def prune(
    root: Path = ROOT, *, all_outputs: bool = False, now: float | None = None
) -> list[str]:
    """Expire idle contexts and enforce the size budget under maintenance access.

    Age is not an exemption from the budget: a frequently used context must not
    grow forever merely because each command refreshes its last-used stamp.
    Callers acquire the existing project gate and exclude active operations.
    """
    settings = CachePolicy.decode(config(root))
    base = contained(root, ".cache/toolchain/work")
    if not base.exists():
        return []
    now = time.time() if now is None else now
    entries = []
    for item in base.iterdir():
        contained(root, str(item.relative_to(root)))
        # CXX and other compilers link generated outputs to source/download files.
        # Inventory the links themselves; rmtree also unlinks without following
        # them. The context root and its parents must still be real directories.
        count = size(item, allow_external_links=True)
        stamp = item / "last-used"
        age = now - (stamp.stat().st_mtime if stamp.exists() else item.stat().st_mtime)
        entries.append((age, item, count))
    removed = []
    total = sum(item[2] for item in entries)
    limit = settings.build_limit_gib * 1024**3
    for age, item, count in sorted(entries, reverse=True):
        if not all_outputs and age < settings.stale_hours * 3600 and total <= limit:
            continue
        shutil.rmtree(
            item
        )  # Failure propagates; never subtract pretend reclaimed bytes.
        total -= count
        removed.append(str(item.relative_to(root)))
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument(
        "action",
        choices=[
            "setup",
            "build",
            "test",
            "verify",
            "format",
            "module",
            "exec",
            "cache-status",
            "cache-prune",
            "clean",
            "doctor",
        ],
    )
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        cfg = config()
        if args.action == "cache-status":
            downloads = Path(
                os.environ.get(
                    "TOOLCHAIN_DOWNLOAD_CACHE",
                    str(
                        Path(
                            os.environ.get(
                                "XDG_CACHE_HOME", str(Path.home() / ".cache")
                            )
                        )
                        / "nix-just-downloads"
                    ),
                )
            )
            print(
                json.dumps(
                    {
                        "free_bytes": shutil.disk_usage(ROOT).free,
                        "build_bytes": size(
                            contained(ROOT, ".cache/toolchain/work"),
                            allow_external_links=True,
                        ),
                        "download_cache": str(downloads),
                        "download_bytes": size(downloads, allow_external_links=True),
                        "compiler_limit_gib": CachePolicy.decode(
                            cfg
                        ).compiler_limit_gib,
                        "note": "Apparent bytes without following symlinks; shared cache activity may change this snapshot.",
                    },
                    indent=2,
                )
            )
            return 0
        with operation(
            exclusive=args.action != "exec",
            new_execution=args.action == "exec",
            automatic_prune=args.action not in ("cache-prune", "clean")
            and CachePolicy.decode(cfg).automatic_prune,
        ) as outer_operation:
            if args.action in ("cache-prune", "clean"):
                if not outer_operation:
                    raise ValueError(
                        "Cleanup cannot run inside an active managed operation"
                    )
                if any(a != "--all" for a in args.arguments):
                    raise ValueError("cache-prune accepts only --all")
                print(
                    json.dumps(
                        {
                            "removed": prune(
                                all_outputs=args.action == "clean"
                                or "--all" in args.arguments
                            )
                        }
                    )
                )
                if args.action == "clean":
                    dist = contained(ROOT, "dist")
                    if dist.exists():
                        size(dist)
                        shutil.rmtree(dist)
                return 0
            env = environment()
            if args.action == "doctor":
                for executable in ("nix", "python3", "git", "just"):
                    subprocess.run([executable, "--version"], check=True)
                print(
                    json.dumps(
                        {
                            "context": context_id(),
                            "modules": cfg["modules"],
                            "container": env.get("TOOLCHAIN_CONTAINER") == "1",
                            "native_apple": platform.system() == "Darwin",
                            "compiler_socket": env["SCCACHE_SERVER_UDS"],
                        },
                        indent=2,
                    )
                )
                return 0
            if args.action == "exec":
                argv = (
                    args.arguments[1:]
                    if args.arguments[:1] == ["--"]
                    else args.arguments
                )
                if not argv:
                    raise ValueError("exec requires a command")
                try:
                    with compiler_cache(
                        env.get("TOOLCHAIN_ACTIVE_PROFILE"), env
                    ) as selected_env:
                        result = managed_run(argv, env=selected_env)
                        if result.returncode:
                            raise subprocess.CalledProcessError(result.returncode, argv)
                    return 0
                except subprocess.CalledProcessError as exc:
                    return exc.returncode
            selected, action = ad.strings(cfg["modules"], "Modules"), args.action
            if action == "module":
                if len(args.arguments) not in (1, 2):
                    raise ValueError("module requires a name and optional action")
                selected = [args.arguments[0]]
                action = args.arguments[1] if len(args.arguments) == 2 else "verify"
                if action not in ("setup", "build", "test", "verify", "format"):
                    raise ValueError("unsupported module action")
            specs = [module(name) for name in selected]
            if action != "format":
                setup_many(specs, env, explicit=action == "setup")
            if action != "setup":
                for spec in specs:
                    run_commands(spec, action, env)
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Toolchain failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
