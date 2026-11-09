"""Reconcile and publish exact, verified npm archives from a private snapshot."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import base64
import builtins
from http.client import HTTPMessage
import os
from pathlib import Path
import tempfile
import time
from typing import Protocol, TypeVar
from urllib.error import HTTPError
from urllib.parse import SplitResult, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from attestation_bridge import PackageMetadata, inspect_package, npm_executable
from attestation_common import (
    Command,
    CommandResult,
    command,
    object_value,
    parse,
    sha512,
    string,
)
from attestation_model import Policy
from attestation_provenance import PREDICATE_TYPE, VerifiedManifest, verify_manifest


@dataclass(frozen=True)
class Package:
    name: str
    version: str
    path: Path
    bundle_path: Path
    bytes: bytes
    bundle_bytes: builtins.bytes
    bundle: object
    dependencies: tuple[str, ...]


class Registry(Protocol):
    def lookup(self, pkg: Package, wait: bool = False) -> str | None: ...

    def publish(self, pkg: Package) -> None: ...

    def verify(self, pkg: Package) -> None: ...


def dependency_order(packages: Sequence[Package]) -> list[Package]:
    by_name = {pkg.name: pkg for pkg in packages}
    if len(by_name) != len(packages):
        raise ValueError("Duplicate npm publication package")
    visiting: set[str] = set()
    complete: set[str] = set()
    ordered: list[Package] = []

    def visit(pkg: Package) -> None:
        if pkg.name in complete:
            return
        if pkg.name in visiting:
            raise ValueError(f"Publication dependency cycle: {pkg.name}")
        visiting.add(pkg.name)
        for name in pkg.dependencies:
            dependency = by_name.get(name)
            if dependency is not None:
                visit(dependency)
        visiting.remove(pkg.name)
        complete.add(pkg.name)
        ordered.append(pkg)

    for pkg in packages:
        visit(pkg)
    return ordered


def publish(packages: Sequence[Package], registry: Registry) -> None:
    """Reconcile every immutable version before uploading the first archive."""
    ordered = dependency_order(packages)
    observed: dict[str, str | None] = {}
    for pkg in ordered:
        digest = registry.lookup(pkg)
        if digest is not None:
            if digest != sha512(pkg.bytes):
                raise ValueError(f"Registry digest mismatch: {pkg.name}@{pkg.version}")
            registry.verify(pkg)
        observed[pkg.name] = digest
    for pkg in ordered:
        if observed[pkg.name] is not None:
            continue
        digest = None
        try:
            registry.publish(pkg)
        except Exception as cause:
            try:
                digest = registry.lookup(pkg, True)
            except Exception:
                raise cause from None
            if digest is None:
                raise
        if digest is None:
            digest = registry.lookup(pkg, True)
        if digest != sha512(pkg.bytes):
            raise ValueError(f"Registry digest mismatch: {pkg.name}@{pkg.version}")
        registry.verify(pkg)


def _missing(result: CommandResult) -> bool:
    streams = [
        stream.strip() for stream in (result.stdout, result.stderr) if stream.strip()
    ]
    if not streams:
        return False
    for stream in streams:
        try:
            if (
                object_value(object_value(parse(stream.encode()))["error"])["code"]
                != "E404"
            ):
                return False
        except (ValueError, KeyError, TypeError):
            return False
    return True


def _origin(url: SplitResult) -> tuple[str, str | None, int | None]:
    port = url.port
    return (
        url.scheme,
        url.hostname,
        port if port is not None else {"http": 80, "https": 443}.get(url.scheme),
    )


class _NoRedirect(HTTPRedirectHandler):
    # Returning no replacement request makes urllib reject every redirect.
    def redirect_request(
        self,
        req: Request,
        fp: object,
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> None:
        return None


def _download(url: str) -> bytes:
    try:
        with build_opener(_NoRedirect()).open(url, timeout=30) as response:
            status: object = response.status
            if not isinstance(status, int) or not 200 <= status < 300:
                raise ValueError(f"Registry download failed ({status})")
            data: bytes = response.read()
            return data
    except HTTPError as error:
        error.close()
        raise ValueError(f"Registry download failed ({error.code})") from None


def _same_json(left: object, right: object) -> bool:
    # Python considers True equal to 1; JSON/native npm distinguish them.
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _same_json(left[key], right[key]) for key in left
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _same_json(a, b) for a, b in zip(left, right, strict=True)
        )
    return left == right


T = TypeVar("T")
_ABSENT = object()


class NpmRegistry:
    def __init__(
        self,
        *,
        command: Command = command,
        fetch: Callable[[str], bytes] = _download,
        attempts: int = 5,
        delay_ms: int = 1000,
        registry: str = "https://registry.npmjs.org/",
        npm_program: str | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        selected = urlsplit(registry)
        self.registry = urlunsplit(selected._replace(path=selected.path or "/"))
        if self.registry != "https://registry.npmjs.org/" and selected.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ValueError("Publication requires the public npm registry")
        if selected.scheme not in {"http", "https"} or not selected.netloc:
            raise ValueError("Publication requires the public npm registry")
        self._origin = _origin(selected)
        if (
            type(attempts) is not int
            or not 1 <= attempts <= 9007199254740991
            or type(delay_ms) is not int
            or not 0 <= delay_ms <= 9007199254740991
        ):
            raise ValueError("Invalid registry retry options")
        self._command = command
        self._fetch = fetch
        self._attempts = attempts
        self._delay_ms = delay_ms
        self._sleep = sleep
        self._npm_program = npm_program
        self._configured: set[str] = set()

    def _invoke(self, args: Sequence[str], interactive: bool = False) -> CommandResult:
        if self._npm_program is None:
            self._npm_program = npm_executable()
        return self._command(self._npm_program, args, interactive)

    def _flags(self, pkg: Package) -> list[str]:
        return [
            "--registry",
            self.registry,
            "--scope=",
            *(
                [f"--{pkg.name.split('/')[0]}:registry={self.registry}"]
                if pkg.name.startswith("@")
                else []
            ),
            "--ignore-scripts",
            "--access",
            "public",
            "--tag",
            "next",
            "--provenance-file",
            str(pkg.bundle_path),
            "--update-notifier=false",
        ]

    def _configuration(self, pkg: Package) -> None:
        if pkg.name in self._configured:
            return
        result = self._invoke(["config", "get", "registry", *self._flags(pkg)])
        if result.status != 0 or result.stdout.strip() != self.registry:
            raise ValueError("npm publication configuration check failed before upload")
        self._configured.add(pkg.name)

    def _view(self, pkg: Package, field: str) -> object:
        result = self._invoke(
            [
                "view",
                f"{pkg.name}@{pkg.version}",
                field,
                "--json",
                "--loglevel=silent",
                *self._flags(pkg),
            ]
        )
        if result.status != 0:
            if _missing(result):
                return _ABSENT
            raise ValueError(
                f"Registry lookup failed ({result.status}): {pkg.name}@{pkg.version}"
            )
        value = parse(result.stdout.encode())
        if isinstance(value, list):
            if len(value) != 1:
                raise ValueError("Expected exactly one npm registry version result")
            return value[0]
        return value

    def _retry(self, read: Callable[[], T]) -> T:
        for attempt in range(self._attempts):
            try:
                return read()
            except Exception:
                if attempt + 1 == self._attempts:
                    raise
            self._sleep(self._delay_ms * (attempt + 1) / 1000)
        raise AssertionError("Unreachable registry retry state")

    def _download(self, value: object) -> bytes:
        url = string(value)
        parsed = urlsplit(url)
        if (
            _origin(parsed) != self._origin
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("Registry download has an unexpected origin")
        return self._fetch(url)

    def lookup(self, pkg: Package, wait: bool = False) -> str | None:
        self._configuration(pkg)

        def read() -> str | None:
            value = self._view(pkg, "dist")
            if value is _ABSENT:
                if wait:
                    raise ValueError("Registry upload is not visible yet")
                return None
            dist = object_value(value)
            digest = sha512(self._download(dist.get("tarball")))
            integrity = "sha512-" + base64.b64encode(bytes.fromhex(digest)).decode(
                "ascii"
            )
            if integrity != dist.get("integrity"):
                raise ValueError("Registry metadata disagrees with downloaded bytes")
            return digest

        return self._retry(read)

    def publish(self, pkg: Package) -> None:
        self._configuration(pkg)
        if (
            pkg.path.read_bytes() != pkg.bytes
            or pkg.bundle_path.read_bytes() != pkg.bundle_bytes
        ):
            raise ValueError("Frozen publication files changed before upload")
        result = self._invoke(["publish", str(pkg.path), *self._flags(pkg)], True)
        if result.status != 0:
            raise ValueError(f"Package upload failed ({result.status}): {pkg.name}")

    def verify(self, pkg: Package) -> None:
        def read() -> None:
            dist = object_value(self._view(pkg, "dist"))
            attestations = object_value(dist.get("attestations"))
            if (
                object_value(attestations.get("provenance")).get("predicateType")
                != PREDICATE_TYPE
            ):
                raise ValueError("Registry provenance metadata missing")
            entries = object_value(parse(self._download(attestations.get("url")))).get(
                "attestations"
            )
            if not isinstance(entries, list) or not any(
                object_value(entry).get("predicateType") == PREDICATE_TYPE
                and _same_json(object_value(entry).get("bundle"), pkg.bundle)
                for entry in entries
            ):
                raise ValueError(
                    "Registry provenance differs from the exact verified bundle"
                )
            if object_value(self._view(pkg, "dist-tags")).get("next") != pkg.version:
                raise ValueError(
                    f"Registry next tag does not select {pkg.name}@{pkg.version}"
                )

        self._retry(read)


def npm_registry(
    *,
    command: Command = command,
    fetch: Callable[[str], bytes] = _download,
    attempts: int = 5,
    delay_ms: int = 1000,
    registry: str = "https://registry.npmjs.org/",
    npm_program: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> NpmRegistry:
    """The registry override exists only for isolated loopback integration tests."""
    return NpmRegistry(
        command=command,
        fetch=fetch,
        attempts=attempts,
        delay_ms=delay_ms,
        registry=registry,
        npm_program=npm_program,
        sleep=sleep,
    )


def _write_snapshot(path: Path, value: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    with os.fdopen(descriptor, "wb") as output:
        output.write(value)


def _truthy(value: object) -> bool:
    return value is not None and value is not False and value != "" and value != 0


def snapshot_packages(
    verified: VerifiedManifest,
    directory: Path | str,
    policy: Policy,
    inspector: Callable[[Path], PackageMetadata] = inspect_package,
) -> list[Package]:
    packages: list[Package] = []
    root = Path(directory)
    for index, selected in enumerate(verified.artifacts):
        path = root / f"{index}.tgz"
        bundle_path = Path(str(path) + ".sigstore.json")
        _write_snapshot(path, selected.bytes)
        _write_snapshot(bundle_path, selected.bundle_bytes)
        metadata = inspector(path)
        data = metadata.manifest
        name, version = string(data.get("name")), string(data.get("version"))
        if metadata.repository != f"github.com/{policy.repository}":
            raise ValueError(
                f"Local npm package {name}@{version} repository must match signing repository https://github.com/{policy.repository}"
            )
        publish_config = data.get("publishConfig")
        if (
            publish_config is not None
            and object_value(publish_config).get("provenance") is True
        ):
            raise ValueError(
                f"Local npm package {name}@{version} publishConfig.provenance conflicts with its saved provenance bundle"
            )
        if (
            _truthy(data.get("private"))
            or "packageExtensions" in data
            or ("tag" in data and data["tag"] != "next")
            or metadata.subject != selected.artifact.subject
        ):
            raise ValueError(
                "Local npm tarball metadata does not match its manifest PURL or next publication policy"
            )
        dependencies = data.get("dependencies")
        packages.append(
            Package(
                name=name,
                version=version,
                path=path,
                bundle_path=bundle_path,
                bytes=selected.bytes,
                bundle_bytes=selected.bundle_bytes,
                bundle=selected.bundle,
                dependencies=tuple(
                    object_value({} if dependencies is None else dependencies)
                ),
            )
        )
    dependency_order(packages)
    return packages


def npm_publish(path: Path | str, run_id: str, policy: Policy) -> None:
    if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
        raise ValueError("npm publication is manual only; CI never publishes")
    if policy.tag != "next":
        raise ValueError("npm publication requires explicit next tag policy")
    verified = verify_manifest(path, run_id, policy)
    try:
        with tempfile.TemporaryDirectory(prefix="chainman-npm-") as temporary:
            directory = Path(temporary)
            directory.chmod(0o700)
            packages = snapshot_packages(verified, directory, policy)
            publish(packages, npm_registry())
    finally:
        verified.dispose()
