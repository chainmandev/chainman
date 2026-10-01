"""Owned disposable storage; legacy paths and package-manager homes survive GC."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
import fcntl
import errno
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
import uuid
from typing import TypedDict

import toolchain as tc

AGE = 30 * 24 * 3600
DOWNLOAD_LIMIT = 16 * 1024**3
POOL = ".chainman-storage-v1"
# Homes also contain credentials/configuration and installed tools. Only these
# package-manager payloads are disposable; never remove the enclosing home.
PAYLOADS = (
    "cargo/registry",
    "cargo/git",
    "go-mod",
    "gradle/caches",
    "gradle/wrapper/dists",
    "pub/hosted",
    "pub/hosted-hashes",
    "uv",
    "pip",
    "pnpm",
)
PATHS = "CHAINMAN_STORAGE_PATHS"
FDS = "CHAINMAN_STORAGE_FDS"
EPOCH = "CHAINMAN_DOWNLOAD_EPOCH"


class Receipt(TypedDict):
    schema: int
    kind: str
    touched: float
    epoch: str


class Entry(TypedDict):
    path: str
    bytes: int
    active: bool
    eligible: bool
    reason: str
    error: str


def private(path: Path, *, create: bool = False) -> None:
    if not path.is_absolute() or Path(os.path.normpath(path)) != path:
        raise ValueError("Storage path must be absolute and normalized")
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError("Storage path contains a symlink")
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise ValueError("Storage directory must be private and owned")


def regular(path: Path, *, create: bool = False) -> int:
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
    if create:
        flags |= os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or info.st_mode & 0o077
    ):
        os.close(fd)
        raise ValueError("Invalid storage ownership file")
    return fd


@contextmanager
def gate(pool: Path, *, create: bool = False) -> Iterator[None]:
    private(pool, create=create)
    try:
        fd = regular(pool / ".gate", create=create)
    except FileExistsError:
        fd = regular(pool / ".gate")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def receipt(entry: Path) -> Receipt:
    private(entry)
    fd = regular(entry / ".receipt.json")
    with os.fdopen(fd) as stream:
        raw = json.load(stream)
    if (
        not isinstance(raw, dict)
        or set(raw) != {"schema", "kind", "touched", "epoch"}
        or raw["schema"] != 1
        or raw["kind"] not in {"downloads", "runtime"}
        or not isinstance(raw["touched"], (int, float))
        or isinstance(raw["touched"], bool)
        or not 0 < raw["touched"] < float("inf")
        or not isinstance(raw["epoch"], str)
        or not re.fullmatch("[0-9a-f]{32}", raw["epoch"])
    ):
        raise ValueError("Unrecognized storage receipt")
    if (
        (raw["kind"] == "downloads" and entry.name != "downloads")
        or (raw["kind"] == "runtime" and not re.fullmatch("[0-9a-f]{40}", entry.name))
        or entry.parent.name != POOL
    ):
        raise ValueError("Storage identity does not match its path")
    return Receipt(
        schema=1, kind=raw["kind"], touched=float(raw["touched"]), epoch=raw["epoch"]
    )


def payloads(entry: Path, kind: str) -> list[Path]:
    if kind == "runtime":
        return [
            path
            for path in entry.iterdir()
            if path.name in {"source", "bootstrap"}
            or re.fullmatch(r"bootstrap-[0-9]+-link", path.name)
        ]
    result = []
    for name in PAYLOADS:
        path = tc.contained(entry, "data/" + name)
        if path.exists():
            result.append(path)
    return result


def prepare_removal(path: Path) -> None:
    # Go's module cache deliberately makes package directories read-only.
    # Only adjust owned directories reached without following links; changing
    # cached file modes could also change an installed hardlinked package.
    for _, _, _, fd in os.fwalk(path, follow_symlinks=False):
        info = os.fstat(fd)
        if info.st_uid != os.getuid():
            raise ValueError("Cache contains a foreign-owned directory")
        if not info.st_mode & stat.S_IWUSR:
            os.fchmod(fd, stat.S_IMODE(info.st_mode) | stat.S_IWUSR)


def collect(
    pool: Path,
    *,
    apply: bool = False,
    all_idle: bool = False,
    now: float | None = None,
    limit: int = DOWNLOAD_LIMIT,
) -> tuple[list[Entry], list[str]]:
    """Hold admission while examining leases and removing recognized payloads."""
    rows: list[Entry] = []
    removed: list[str] = []
    if not pool.exists():
        return rows, removed
    now = time.time() if now is None else now
    with gate(pool):
        for entry in sorted(pool.iterdir()):
            if entry.name == ".gate":
                continue
            row = Entry(
                path=str(entry),
                bytes=0,
                active=False,
                eligible=False,
                reason="unrecognized storage",
                error="",
            )
            rows.append(row)
            fd = None
            try:
                saved = receipt(entry)
                fd = regular(entry / ".lease")
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    row.update({"active": True, "reason": "active lifetime lease"})
                    continue
                paths = payloads(entry, saved["kind"])
                # Runtime symlinks count only the roots, not their shared Nix
                # closure. Ordinary Nix GC decides which store objects survive.
                sizes = {
                    p: tc.size(p, allow_external_links=True)
                    if p.is_dir() and not p.is_symlink()
                    else p.lstat().st_size
                    for p in paths
                    if p.exists() or p.is_symlink()
                }
                row["bytes"] = sum(sizes.values())
                row["eligible"] = (
                    all_idle
                    or now - saved["touched"] >= AGE
                    or (saved["kind"] == "downloads" and row["bytes"] > limit)
                )
                row["reason"] = (
                    "retention budget or age"
                    if row["eligible"]
                    else "within retention policy"
                )
                if (
                    saved["kind"] == "downloads"
                    and not all_idle
                    and now - saved["touched"] < AGE
                ):
                    # Evict cache families oldest-first, stopping at the budget.
                    # Package-manager homes themselves never participate.
                    remaining = row["bytes"]
                    paths = []
                    for path in sorted(
                        sizes, key=lambda p: (p.lstat().st_mtime_ns, str(p))
                    ):
                        if remaining <= limit:
                            break
                        paths.append(path)
                        remaining -= sizes[path]
                if apply and row["eligible"]:
                    # Invalidate setup evidence before the first unlink. A
                    # partial cleanup must never leave apparently valid stamps.
                    saved.update({"epoch": uuid.uuid4().hex, "touched": now})
                    tc.atomic_json(entry / ".receipt.json", saved)
                    for path in paths:
                        if path.is_symlink():
                            target = str(path.readlink())
                            if saved["kind"] != "runtime" or not (
                                target.startswith("/nix/store/")
                                or re.fullmatch(r"bootstrap-[0-9]+-link", target)
                            ):
                                raise ValueError("Unexpected storage payload symlink")
                            path.unlink()
                        elif path.is_dir() and saved["kind"] == "downloads":
                            prepare_removal(path)
                            shutil.rmtree(path)
                        elif path.exists():
                            raise ValueError("Unexpected storage payload file")
                        removed.append(str(path))
                    if saved["kind"] == "runtime" and {
                        p.name for p in entry.iterdir()
                    } <= {".lease", ".receipt.json"}:
                        # Every opener shares the pool admission gate. With no
                        # live lease or unknown files, the generation can retire
                        # completely and a future cold admission creates it anew.
                        (entry / ".receipt.json").unlink()
                        (entry / ".lease").unlink()
                        entry.rmdir()
                        removed.append(str(entry))
                    # Preserve lease/gate inodes and homes. Cold registration
                    # can rebuild a runtime, and download credentials survive.
            except (OSError, ValueError) as error:
                row.update(
                    {
                        "error": str(error),
                        "eligible": False,
                        "reason": "inspection or removal failed",
                    }
                )
            finally:
                if fd is not None:
                    os.close(fd)
    return rows, removed


def inherited(
    env: Mapping[str, str] | None = None,
) -> tuple[list[Path], tuple[int, ...]]:
    env = os.environ if env is None else env
    paths = json.loads(env.get(PATHS, "[]"))
    fds = json.loads(env.get(FDS, "[]"))
    if (
        not isinstance(paths, list)
        or not isinstance(fds, list)
        or len(paths) != len(fds)
    ):
        raise ValueError("Invalid inherited storage leases")
    for path, fd in zip(paths, fds, strict=True):
        if not isinstance(path, str) or type(fd) is not int or fd < 3:
            raise ValueError("Invalid storage lease identity")
        entry = Path(path)
        receipt(entry)
        if not os.path.samestat(os.fstat(fd), (entry / ".lease").lstat()):
            raise ValueError("Inherited storage descriptor changed")
    return [Path(p) for p in paths], tuple(fds)


def seed_homes(entry: Path) -> None:
    """Preserve legacy configuration on first use without moving its cache."""
    private(entry / "data", create=True)
    base = entry.parent.parent
    for name in (
        "cargo/config",
        "cargo/config.toml",
        "cargo/credentials",
        "cargo/credentials.toml",
        "cargo/.crates.toml",
        "cargo/.crates2.json",
        "gradle/gradle.properties",
        "gradle/init.gradle",
        "gradle/init.gradle.kts",
        "pub/credentials.json",
    ):
        source = base / name
        if source.is_symlink():
            raise ValueError(
                "Indirect legacy package configuration requires manual migration"
            )
        if not source.exists():
            continue
        source = tc.contained(base, name)
        info = source.stat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_size > 4 * 1024**2
        ):
            raise ValueError("Legacy package configuration requires manual migration")
        destination = tc.contained(entry, "data/" + name)
        if not destination.exists():
            tc.atomic_bytes(destination, source.read_bytes(), 0o600)
    # Installed tools and custom init scripts are user state. Retain their
    # original locations and avoid duplicating large installations.
    for name in ("cargo/bin", "pub/bin", "pub/global_packages", "gradle/init.d"):
        source = base / name
        if source.is_dir() and not source.is_symlink():
            source = tc.contained(base, name)
            destination = entry / "data" / name
            tc.contained(entry, "data/" + str(Path(name).parent))
            if destination.exists() or destination.is_symlink():
                continue
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            destination.symlink_to(source, target_is_directory=True)


@contextmanager
def use(pool: Path, name: str, kind: str) -> Iterator[Path]:
    entry = pool / name
    old_paths, old_fds = inherited()
    if entry in old_paths:
        yield entry
        return
    with gate(pool, create=True):
        if not entry.exists():
            private(entry, create=True)
            fd = regular(entry / ".lease", create=True)
            os.close(fd)
            tc.atomic_json(
                entry / ".receipt.json",
                Receipt(
                    schema=1, kind=kind, touched=time.time(), epoch=uuid.uuid4().hex
                ),
            )
        saved = receipt(entry)
        if saved["kind"] != kind:
            raise ValueError("Storage kind changed")
        if kind == "downloads":
            if not (entry / ".homes-ready").exists():
                seed_homes(entry)
                tc.atomic_bytes(entry / ".homes-ready", b"1\n")
            elif tc.regular_input(entry, ".homes-ready") != b"1\n":
                raise ValueError("Invalid package home migration receipt")
        fd = regular(entry / ".lease")
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            saved["touched"] = time.time()
            tc.atomic_json(entry / ".receipt.json", saved)
        except BaseException:
            os.close(fd)
            raise
    previous = {key: os.environ.get(key) for key in (PATHS, FDS, EPOCH)}
    os.environ[PATHS] = json.dumps([str(p) for p in [*old_paths, entry]])
    os.environ[FDS] = json.dumps([*old_fds, fd])
    if kind == "downloads":
        os.environ[EPOCH] = saved["epoch"]
    try:
        yield entry
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        os.close(fd)  # Never unlock a description still held by a child.


def download_base(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    default = str(
        Path(env.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
        / "nix-just-downloads"
    )
    return Path(env.get("TOOLCHAIN_DOWNLOAD_CACHE", default))


def managed_downloads(env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    default = (
        Path(env.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
        / "nix-just-downloads"
    )
    return download_base(env) == default or (
        env.get("TOOLCHAIN_CONTAINER") == "1"
        and download_base(env) == Path("/chainman-downloads")
    )


def download_path(env: Mapping[str, str]) -> Path:
    base = download_base(env)
    entry = base / POOL / "downloads"
    if not managed_downloads(env):
        return base
    return entry / "data"


def download_epoch(env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    if tc.host_mode() or not managed_downloads(env):
        return ""
    try:
        return receipt(download_base(env) / POOL / "downloads")["epoch"]
    except FileNotFoundError:
        return "uninitialized"


@contextmanager
def downloads() -> Iterator[None]:
    if tc.host_mode() or not managed_downloads():
        yield
        return
    pool = download_base() / POOL
    collect(pool, apply=True)
    try:
        with use(pool, "downloads", "downloads") as entry:
            private(entry / "data", create=True)
            yield
    finally:
        try:
            rows, _ = collect(pool, apply=True)
            for row in rows:
                if row["error"]:
                    print(
                        "Chainman storage maintenance: " + row["error"], file=sys.stderr
                    )
        except (OSError, ValueError) as error:
            print(f"Chainman storage maintenance: {error}", file=sys.stderr)


def runtime_child(argv: list[str], env: dict[str, str]) -> int:
    """Forward cancellation to the existing shell/native lifetime owner."""
    process: subprocess.Popen[bytes] | None = None
    pending: list[int] = []
    forwarded = False
    terminal = None
    foreground = None

    def forward(number: int) -> None:
        nonlocal forwarded
        if not forwarded and process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, number)
                forwarded = True
            except ProcessLookupError:
                pass

    def interrupted(number: int, _frame: object) -> None:
        if pending:
            return
        pending.append(number)
        forward(number)

    signals = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT)
    previous = {number: signal.signal(number, interrupted) for number in signals}
    previous_ttou = signal.getsignal(signal.SIGTTOU)
    try:
        try:
            terminal = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
            foreground = os.tcgetpgrp(terminal)
            if foreground != os.getpgrp():
                os.close(terminal)
                terminal = None
        except OSError as error:
            if error.errno not in (errno.ENXIO, errno.ENODEV, errno.ENOTTY):
                raise
        # This wrapper replaces a shell handoff, which preserves arbitrary
        # caller-owned descriptors as well as Chainman's lifetime leases.
        options: tc.ProcessOptions = {"env": env, "close_fds": False}
        options = tc.managed_options(options)
        descriptors = options.pop("pass_fds", ())
        flags = {fd: os.get_inheritable(fd) for fd in descriptors}
        try:
            for fd in descriptors:
                os.set_inheritable(fd, True)
            # A separate group prevents one terminal interrupt reaching the
            # child both directly and through this wrapper's forwarding trap.
            process = subprocess.Popen(argv, process_group=0, **options)
        finally:
            for fd, flag in flags.items():
                os.set_inheritable(fd, flag)
        if terminal is not None:
            signal.signal(signal.SIGTTOU, signal.SIG_IGN)
            try:
                os.tcsetpgrp(terminal, process.pid)
                os.killpg(process.pid, signal.SIGCONT)
            except OSError:
                if process.poll() is None:
                    raise
        for number in pending:
            forward(number)
        result = process.wait()
        return 128 + pending[0] if pending else (128 - result if result < 0 else result)
    finally:
        try:
            if terminal is not None:
                try:
                    if foreground is not None:
                        os.tcsetpgrp(terminal, foreground)
                finally:
                    os.close(terminal)
        finally:
            signal.signal(signal.SIGTTOU, previous_ttou)
            for number, handler in previous.items():
                signal.signal(number, handler)


def runtime_run(args: list[str]) -> int:
    if len(args) < 5:
        raise ValueError("Invalid runtime storage handoff")
    base, revision, source, bootstrap, launcher, *arguments = args
    if not re.fullmatch("[0-9a-f]{40}", revision):
        raise ValueError("Invalid runtime revision")
    pool = Path(base) / POOL
    collect(pool, apply=True)
    with use(pool, revision, "runtime") as entry:
        # Temporary bootstrap roots still exist while both permanent roots are
        # registered. The parent retains them throughout this child execution.
        with gate(pool):
            for name, target in (
                ("source", source),
                ("bootstrap", str(Path(bootstrap).resolve(strict=True))),
            ):
                if not target.startswith("/nix/store/") or not Path(target).exists():
                    raise ValueError("Invalid runtime store path")
                destination = entry / name
                nix_store = str(Path(tc.nix_command()).with_name("nix-store"))
                if destination.exists() or destination.is_symlink():
                    if not destination.is_symlink() or destination.readlink() != Path(
                        target
                    ):
                        raise ValueError(
                            "Runtime GC root must be a symlink to its verified store"
                        )
                    roots = subprocess.run(
                        [nix_store, "--query", "--roots", target],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    if (
                        roots.returncode == 0
                        and f"{destination} -> {target}" in roots.stdout.splitlines()
                    ):
                        continue
                subprocess.run(
                    [
                        nix_store,
                        "--realise",
                        target,
                        "--add-root",
                        str(destination),
                        "--indirect",
                    ],
                    check=True,
                    stdout=subprocess.DEVNULL,
                )
        env = dict(os.environ, CHAINMAN_RUNTIME_STORAGE_ACTIVE=revision)
        result = runtime_child([launcher, *arguments], env)
    collect(pool, apply=True)
    return result


def runtime_check(args: list[str]) -> int:
    if len(args) != 3:
        raise ValueError("Invalid runtime storage validation")
    base, revision, source = args
    entry = Path(base) / POOL / revision
    paths, _ = inherited()
    if entry not in paths:
        # A nested caller may select another cache root. It needs a fresh
        # admission there, not trust in the revision-only environment marker.
        return 3
    if receipt(entry)["kind"] != "runtime":
        raise ValueError("Runtime handoff requires its inherited lifetime lease")
    if (entry / "source").readlink() != Path(source):
        raise ValueError("Runtime lease does not match the verified source")
    return 0


def run(action: str, arguments: list[str]) -> int:
    import hook_worker
    import platform
    import tempfile

    if (
        action not in {"status", "prune"}
        or arguments not in ([], ["--all"])
        or (action == "status" and arguments)
    ):
        raise ValueError("usage: storage-status; storage-prune [--all]")
    cache = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
    runtime = (
        Path("/nix/var/nix/chainman-runtime-roots")
        if os.environ.get("TOOLCHAIN_CONTAINER") == "1"
        else cache / "chainman/runtime-roots"
    )
    report: dict[str, object] = {
        "download_limit_bytes": DOWNLOAD_LIMIT,
        "idle_age_seconds": AGE,
    }
    failed = False
    for kind, base in (("downloads", download_base()), ("runtimes", runtime)):
        if kind == "downloads" and not managed_downloads():
            report[kind] = {
                "reason": "user-managed cache override",
                "entries": [],
                "removed": [],
            }
            continue
        try:
            rows, removed = collect(
                base / POOL, apply=action == "prune", all_idle=bool(arguments)
            )
        except (OSError, ValueError) as error:
            rows = [
                Entry(
                    path=str(base / POOL),
                    bytes=0,
                    active=False,
                    eligible=False,
                    reason="inspection failed",
                    error=str(error),
                )
            ]
            removed = []
        report[kind] = {
            "entries": rows,
            "removed": removed,
            "legacy_path": str(base),
            "legacy_policy": "preserved",
        }
        failed |= any(row["error"] for row in rows)
    # Service ownership lives on the host. Container entry must explicitly
    # report this boundary, never mistake its empty HOME for a host inventory.
    if os.environ.get("TOOLCHAIN_CONTAINER") == "1":
        report["services"] = {
            "reason": "host-owned; run storage maintenance in host-nix mode"
        }
    else:
        target = (
            platform.system().lower()
            + "-"
            + {"aarch64": "arm64", "arm64": "arm64", "x86_64": "amd64"}[
                platform.machine()
            ]
        )
        try:
            with tempfile.TemporaryDirectory(
                prefix="chainman-storage-export-"
            ) as output:
                hook_worker.export_binary(
                    Path(output), target, "task", "chainman-control"
                )
                result = tc.managed_run(
                    [
                        str(Path(output) / "chainman-control"),
                        "storage-services",
                        action,
                        str(cache / "chainman/services"),
                        *arguments,
                    ],
                    capture_output=True,
                    check=False,
                )
                report["services"] = json.loads(result.stdout)
                failed |= result.returncode != 0
        except (OSError, ValueError, subprocess.CalledProcessError) as error:
            report["services"] = {
                "error": str(error),
                "reason": "inspection or native export failed",
            }
            failed = True
    print(json.dumps(report, indent=2))
    return int(failed)


if __name__ == "__main__":
    if sys.argv[1] == "runtime-run":
        raise SystemExit(runtime_run(sys.argv[2:]))
    if sys.argv[1] == "runtime-check":
        raise SystemExit(runtime_check(sys.argv[2:]))
    raise SystemExit(run(sys.argv[1], sys.argv[2:]))
