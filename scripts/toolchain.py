"""Project commands, verified setup state, and bounded cache maintenance."""

from __future__ import annotations

import argparse
import contextlib
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

RUNTIME = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ.get("CHAINMAN_ROOT", str(RUNTIME))).resolve()
_operation_fd: int | None = None


def entry_command(root: Path, profile: str) -> list[str]:
    if (root / "chainman.toml").is_file():
        return [
            sys.executable,
            str(RUNTIME / "scripts/chainman.py"),
            "--root",
            str(root),
            "exec",
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


def config(root: Path = ROOT) -> dict:
    name = "chainman.toml" if (root / "chainman.toml").exists() else "toolchain.toml"
    data = tomllib.loads(contained(root, name).read_text())
    if name == "chainman.toml":
        data.setdefault("modules", ["project"])
    if data.get("schema") != 1 or not data.get("modules"):
        raise ValueError(f"{name} requires schema=1 and a nonempty modules list")
    cache = data.get("cache", {})
    for key, default in (
        ("build_limit_gib", 12),
        ("compiler_limit_gib", 8),
        ("stale_hours", 48),
    ):
        value = cache.get(key, default)
        if (
            type(value) not in (int, float)
            or not math.isfinite(value)
            or value < 0
            or (key == "compiler_limit_gib" and value == 0)
        ):
            raise ValueError(
                f"cache.{key} must be finite and nonnegative; compiler_limit_gib must be positive"
            )
    if type(cache.get("automatic_prune", True)) is not bool:
        raise ValueError("cache.automatic_prune must be boolean")
    return data


def module(name: str, root: Path = ROOT) -> dict:
    if not name.replace("-", "").isalnum():
        raise ValueError("invalid module name")
    if name == "project" and (root / "chainman.toml").exists():
        cfg = config(root)
        data = {
            "name": name,
            "directory": ".",
            "profile": cfg.get("project", {}).get("default_profile", "default"),
            "commands": cfg.get("commands", {}),
            "inputs": cfg.get("setup", {}).get("inputs", []),
            "artifacts": cfg.get("setup", {}).get("artifacts", []),
            "cache_setup": bool(cfg.get("setup", {}).get("inputs")),
        }
    else:
        data = tomllib.loads(contained(root, f"modules/{name}.toml").read_text())
    if data.get("name") != name:
        raise ValueError("module identity mismatch")
    contained(root, data["directory"])
    for commands in data.get("commands", {}).values():
        if not isinstance(commands, list) or any(
            not isinstance(c, list) or not c or any(not isinstance(a, str) for a in c)
            for c in commands
        ):
            raise ValueError("module commands must be nonempty argument arrays")
    return data


def context_id() -> str:
    mode = os.environ.get("CHAINMAN_MODE", os.environ.get("TOOLCHAIN_MODE", "host-nix"))
    return f"{mode}-{platform.system().lower()}-{platform.machine()}"


def cache_root(root: Path = ROOT) -> Path:
    return contained(root, ".cache/toolchain")


@contextlib.contextmanager
def operation(root: Path = ROOT):
    global _operation_fd
    directory = cache_root(root)
    directory.mkdir(parents=True, exist_ok=True)
    path = contained(root, ".cache/toolchain/operation.lock")
    inherited = (
        _operation_fd
        if _operation_fd is not None
        else os.environ.get("TOOLCHAIN_LOCK_FD")
    )
    if inherited:
        descriptor = int(inherited)
        actual = os.fstat(descriptor)
        expected = path.stat() if path.exists() else None
        if expected is not None and (actual.st_dev, actual.st_ino) == (
            expected.st_dev,
            expected.st_ino,
        ):
            previous = _operation_fd
            _operation_fd = descriptor
            try:
                yield False
            finally:
                _operation_fd = previous
            return
    if path.exists() and not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("Operation lock must be a regular file")
    with path.open("a") as lock:
        if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
            raise ValueError("Operation lock must be a regular file")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(
                "Another managed operation is active; no cleanup or concurrent update was started."
            ) from None
        previous = _operation_fd
        _operation_fd = lock.fileno()
        try:
            yield True
        finally:
            _operation_fd = previous


def managed_options(kwargs):
    """Keep the cooperating operation lock open in a managed child."""
    descriptor = _operation_fd
    if descriptor is None and os.environ.get("TOOLCHAIN_LOCK_FD"):
        descriptor = int(os.environ["TOOLCHAIN_LOCK_FD"])
        os.fstat(descriptor)  # A lost lock is an error, never permission to continue.
    if descriptor is not None:
        env = dict(kwargs.get("env", os.environ))
        env["TOOLCHAIN_LOCK_FD"] = str(descriptor)
        kwargs.update(env=env, pass_fds=(descriptor,))
    return kwargs


def managed_run(argv, **kwargs):
    return subprocess.run(argv, **managed_options(kwargs))


PNPM_STORE_VARIABLES = (
    "PNPM_CONFIG_STORE_DIR",
    "PNPM_STORE_DIR",
    "npm_config_store_dir",
)


def pnpm_store_environment(env: dict[str, str], values: dict[str, str]) -> None:
    """Keep pnpm's current setting and legacy adapter aliases on one store."""
    for name in PNPM_STORE_VARIABLES:
        if name in values:
            env.update(dict.fromkeys(PNPM_STORE_VARIABLES, values[name]))
            return


def environment(root: Path = ROOT) -> dict[str, str]:
    env = dict(os.environ)
    work = contained(root, f".cache/toolchain/work/{context_id()}")
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
    downloads.mkdir(parents=True, exist_ok=True)
    socket_directory = Path("/tmp").resolve() / f"nix-just-sockets-{os.getuid()}"
    if socket_directory.is_symlink():
        raise ValueError("compiler socket directory must not be a symlink")
    socket_directory.mkdir(mode=0o700, exist_ok=True)
    if (
        socket_directory.stat().st_uid != os.getuid()
        or socket_directory.stat().st_mode & 0o077
    ):
        raise ValueError(
            "compiler socket directory must be private to the current user"
        )
    socket_name = hashlib.sha256(
        (str(root.resolve()) + context_id()).encode()
    ).hexdigest()[:24]
    env.update(
        CARGO_HOME=str(downloads / "cargo"),
        CARGO_TARGET_DIR=str(work / "cargo"),
        SCCACHE_DIR=str(downloads / "sccache"),
        SCCACHE_CACHE_SIZE=str(
            int(config(root).get("cache", {}).get("compiler_limit_gib", 8) * 1024**3)
        ),
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
    pnpm_store_environment(env, {"PNPM_CONFIG_STORE_DIR": str(downloads / "pnpm")})
    preserved = {}
    for name in config(root).get("cache", {}).get("preserve_environment", []):
        if name in {"RUSTC_WRAPPER", "SCCACHE_SERVER_UDS", "TOOLCHAIN_LOCK_FD"}:
            raise ValueError("Cannot override managed compiler-cache lifecycle")
        if name in os.environ:
            env[name] = os.environ[name]
            preserved[name] = os.environ[name]
    pnpm_store_environment(env, preserved)
    (work / "last-used").touch()
    return env


@contextlib.contextmanager
def compiler_cache(profile: str, env: dict[str, str], root: Path = ROOT):
    owns_cache = (
        config(root).get("profiles", {}).get(profile, {}).get("compiler_cache", False)
    )
    if profile != "rust" and not owns_cache:
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
        CHAINMAN_COMPILER_OWNER=str(root),
    )
    # Realizing a cold Nix closure can take longer than a server readiness
    # deadline. Finish it synchronously before starting the owned server clock.
    managed_run(
        [*prefix, "sh", "-eu", "-c", "command -v sccache >/dev/null"],
        cwd=root,
        env=dict(env, CHAINMAN_COMPILER_OWNER=str(root)),
        check=True,
        stdout=sys.stderr,
    )
    server = subprocess.Popen(
        [*prefix, "sccache"],
        **managed_options({"cwd": root, "env": server_env, "stdout": sys.stderr}),
    )
    deadline = time.monotonic() + 120
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
            except (FileNotFoundError, ConnectionRefusedError):
                if time.monotonic() >= deadline:
                    raise ValueError(
                        "Compiler cache startup timed out; its inherited operation lock remains until the server exits"
                    )
                time.sleep(0.05)
    except BaseException:
        # A still-starting child retains the lock; never announce successful cleanup.
        if server.poll() is not None:
            server.wait()
        raise
    primary = None
    try:
        yield dict(env, RUSTC_WRAPPER="sccache", CHAINMAN_COMPILER_OWNER=str(root))
    except BaseException as exc:
        primary = exc
        raise
    finally:
        try:
            try:
                managed_run(
                    [*prefix, "sccache", "--stop-server"],
                    cwd=root,
                    env=dict(env, CHAINMAN_COMPILER_OWNER=str(root)),
                    check=True,
                    timeout=15,
                    stdout=sys.stderr,
                )
            except subprocess.TimeoutExpired:
                raise ValueError(
                    "Compiler cache stop request timed out; the server retains its operation lock"
                ) from None
            try:
                status = server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                raise ValueError(
                    "Compiler cache shutdown timed out; the server retains the operation lock until it exits"
                ) from None
            if endpoint.exists() or endpoint.is_symlink():
                current = endpoint.lstat()
                if not stat.S_ISSOCK(current.st_mode) or (
                    current.st_dev,
                    current.st_ino,
                ) != (owned_socket.st_dev, owned_socket.st_ino):
                    raise ValueError(
                        "Compiler cache endpoint changed; it was preserved"
                    )
                endpoint.unlink()
            if status:
                raise ValueError(f"Compiler cache server exited with status {status}")
        except BaseException as cleanup:
            if primary is None:
                raise
            primary.add_note(f"Compiler cache cleanup also failed: {cleanup}")
            print(f"Compiler cache cleanup also failed: {cleanup}", file=sys.stderr)


def fingerprint(spec: dict, root: Path = ROOT) -> str:
    digest = hashlib.sha256()
    digest.update(json.dumps([2, context_id(), spec], sort_keys=True).encode())
    paths = {root / "toolchain.toml", root / "chainman.toml", root / "chainman.lock"}
    digest.update(str(RUNTIME).encode())
    if (root / "chainman.toml").exists():
        import chainman

        ref, _ = chainman.profile(root, spec["profile"])
        digest.update(chainman.profile_fingerprint(root, spec["profile"], ref).encode())
    for directory in ("scripts", "nix", "modules"):
        paths.update(
            p
            for p in (root / directory).rglob("*")
            if p.is_file() and "__pycache__" not in p.parts
        )
    for pattern in spec.get("inputs", []):
        paths.update(root.glob(pattern))
    for p in sorted(paths):
        if p.is_file():
            relative = p.relative_to(root).as_posix()
            contained(root, relative)
            digest.update(relative.encode() + b"\0" + p.read_bytes() + b"\0")
    return digest.hexdigest()


def run_commands(
    spec: dict, action: str, env: dict[str, str], root: Path = ROOT
) -> None:
    commands = spec.get("commands", {}).get(action)
    if commands is None:
        raise ValueError(f"{spec['name']} does not define {action}")
    if spec.get("native") == "darwin" and (
        platform.system() != "Darwin" or env.get("TOOLCHAIN_CONTAINER") == "1"
    ):
        raise ValueError(
            f"{spec['name']} requires a macOS host with Xcode; this lane has not run"
        )
    cwd = contained(root, spec["directory"])

    def launch(argv, selected_env):
        # The working directory travels as an argument, never shell syntax.
        launch = [
            *entry_command(root, spec["profile"]),
            "sh",
            "-eu",
            "-c",
            'cd "$1"; shift; exec "$@"',
            "sh",
            str(cwd),
            *argv,
        ]
        managed_run(launch, cwd=root, env=selected_env, check=True)

    with compiler_cache(spec["profile"], env, root) as selected_env:
        for argv in commands:
            launch(argv, selected_env)


def artifact_ready(root: Path, artifact, env: dict[str, str]) -> bool:
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


def setup(spec: dict, env: dict[str, str], root: Path = ROOT) -> None:
    if not spec.get("cache_setup", True):
        # An opaque project adapter owns its readiness checks until it explicitly
        # declares fingerprint inputs; never cache an unknown manifest surface.
        run_commands(spec, "setup", env, root)
        return
    # A project-local installed environment can belong to only one active context.
    # A stamp per context would falsely reuse files last installed by another mode.
    stamp = contained(root, f".cache/toolchain/setup/{spec['name']}.json")
    expected = fingerprint(spec, root)
    artifacts = spec.get("artifacts", [])
    try:
        recorded = json.loads(stamp.read_text())
    except (FileNotFoundError, ValueError):
        recorded = {}
    if recorded.get("fingerprint") == expected and all(
        artifact_ready(root, p, env) for p in artifacts
    ):
        return
    run_commands(spec, "setup", env, root)
    if not all(artifact_ready(root, p, env) for p in artifacts):
        raise ValueError(f"{spec['name']} setup did not create its declared artifacts")
    atomic_json(stamp, {"fingerprint": fingerprint(spec, root)})


def size(path: Path, *, reporting: bool = False) -> int:
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
            if item.is_symlink() and not reporting:
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
    settings = config(root).get("cache", {})
    base = contained(root, ".cache/toolchain/work")
    if not base.exists():
        return []
    now = time.time() if now is None else now
    entries = []
    for item in base.iterdir():
        contained(root, str(item.relative_to(root)))
        count = size(item)  # Validate the entire candidate before any deletion.
        stamp = item / "last-used"
        age = now - (stamp.stat().st_mtime if stamp.exists() else item.stat().st_mtime)
        entries.append((age, item, count))
    removed = []
    total = sum(item[2] for item in entries)
    limit = settings.get("build_limit_gib", 12) * 1024**3
    for age, item, count in sorted(entries, reverse=True):
        if not all_outputs and (
            age < settings.get("stale_hours", 48) * 3600 or total <= limit
        ):
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
                            contained(ROOT, ".cache/toolchain/work"), reporting=True
                        ),
                        "download_cache": str(downloads),
                        "download_bytes": size(downloads, reporting=True),
                        "compiler_limit_gib": cfg.get("cache", {}).get(
                            "compiler_limit_gib", 8
                        ),
                        "note": "Apparent bytes without following symlinks; shared cache activity may change this snapshot.",
                    },
                    indent=2,
                )
            )
            return 0
        with operation():
            if args.action in ("cache-prune", "clean"):
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
            if cfg.get("cache", {}).get("automatic_prune", True):
                prune()
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
            selected, action = cfg["modules"], args.action
            if action == "module":
                if len(args.arguments) not in (1, 2):
                    raise ValueError("module requires a name and optional action")
                selected = [args.arguments[0]]
                action = args.arguments[1] if len(args.arguments) == 2 else "verify"
                if action not in ("setup", "build", "test", "verify", "format"):
                    raise ValueError("unsupported module action")
            for name in selected:
                spec = module(name)
                if action != "format":
                    setup(spec, env)
                if action != "setup":
                    run_commands(spec, action, env)
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Toolchain failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
