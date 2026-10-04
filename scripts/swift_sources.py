"""Explicit SwiftPM candidate artifacts, separate from public release evidence."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile

import adapter_data as ad
from dependency_identity import Identity
import lock_adapters
import manifests
import registry
import toolchain as tc


@dataclass(frozen=True)
class Binding:
    package: str
    directory: str
    version_file: str
    version_pointer: tuple[str, ...]
    paths: tuple[str, ...]
    version: str
    files: dict[str, tuple[bytes, int]]

    def record(self) -> ad.Table:
        return {
            "package": self.package,
            "directory": self.directory,
            "version_file": self.version_file,
            "version_pointer": list(self.version_pointer),
            "paths": list(self.paths),
            "version": self.version,
            "files": [
                {"path": p, "sha256": hashlib.sha256(b).hexdigest(), "mode": m}
                for p, (b, m) in sorted(self.files.items())
            ],
        }


@dataclass
class Scope:
    key: tuple[Path, str]
    bindings: dict[str, Binding]
    groups: dict[str, set[str]]
    urls: dict[str, set[str]]
    declarations: list[ad.Table]
    repositories: dict[str, Path] = field(default_factory=dict)
    revisions: dict[str, str] = field(default_factory=dict)

    def records(self) -> list[ad.Table]:
        return [
            {
                **b.record(),
                "groups": sorted(
                    g for g, names in self.groups.items() if name in names
                ),
                "declarations": [d for d in self.declarations if d["package"] == name],
            }
            for name, b in sorted(self.bindings.items())
        ]


_active: ContextVar[Scope | None] = ContextVar("candidate_swift_scope", default=None)


def authority(records: object) -> list[ad.Table]:
    return [
        {k: v for k, v in ad.table(r, "Swift candidate record").items() if k != "files"}
        for r in ad.array(records, "Swift candidate records")
    ]


def file_images(
    root: Path, directory: Path, paths: tuple[str, ...]
) -> dict[str, tuple[bytes, int]]:
    result = {}
    for relative in paths:
        path = tc.contained(root, str((directory / relative).relative_to(root)))
        candidates = [path, *sorted(path.rglob("*"))] if path.is_dir() else [path]
        for candidate in candidates:
            name = str(candidate.relative_to(directory))
            if any(
                p in {".git", ".build", ".swiftpm", ".cache"} for p in Path(name).parts
            ):
                raise ValueError("Swift candidate paths must contain source files only")
            if candidate.is_symlink():
                raise ValueError("Swift candidate source cannot contain symlinks")
            if candidate.is_dir():
                continue
            source = str(candidate.relative_to(root))
            result[name] = (
                tc.regular_input(root, source),
                stat.S_IMODE(candidate.stat().st_mode),
            )
    if "Package.swift" not in result:
        raise ValueError("Swift candidate paths must include Package.swift")
    return result


def read(root: Path, spec: Mapping[str, object]) -> dict[str, Binding]:
    raw = ad.table(spec.get("swift_sources", {}), "Swift candidate sources")
    if not raw:
        return {}
    if (
        spec.get("adapter") != "swift"
        or spec.get("pins")
        or spec.get("resolve", [["swift", "package", "update"]])
        != [["swift", "package", "update"]]
    ):
        raise ValueError("swift_sources requires ordinary literal SwiftPM update")
    result = {}
    identities = set()
    for package, value in raw.items():
        if lock_adapters.swift_repository("https://github.com/" + package) != package:
            raise ValueError("Swift candidate requires a canonical GitHub repository")
        identity = package.rsplit("/", 1)[1].lower()
        if identity in identities:
            raise ValueError(
                "Swift candidate repositories have colliding package identities"
            )
        identities.add(identity)
        item = ad.table(value, "Swift candidate source")
        if set(item) != {"directory", "version_file", "version_pointer", "paths"}:
            raise ValueError(
                "Swift candidate source requires directory, version authority and paths"
            )
        relative = ad.text(item["directory"], "Swift source directory")
        directory = tc.contained(root, relative)
        version_file = ad.text(item["version_file"], "Swift version file")
        version_path = tc.contained(root, version_file)
        if not version_path.is_relative_to(directory):
            raise ValueError(
                "Swift candidate version authority must be within its source directory"
            )
        pointer = tuple(ad.strings(item["version_pointer"], "Swift version pointer"))
        if not pointer:
            raise ValueError("Swift candidate version pointer must not be empty")
        document = manifests.document(
            version_path, body=tc.regular_input(root, version_file).decode()
        )[0]
        version = ad.text(
            manifests.lookup(document, list(pointer)), "Swift candidate version"
        )
        if registry.version("swift", version) is None or version.startswith("v"):
            raise ValueError(
                "Swift candidate version authority requires a canonical stable version"
            )
        paths = tuple(ad.strings(item["paths"], "Swift source paths"))
        if (
            not paths
            or len(paths) != len(set(paths))
            or any(
                Path(p).is_absolute() or ".." in Path(p).parts or p == "."
                for p in paths
            )
        ):
            raise ValueError(
                "Swift candidate source paths must be distinct contained paths"
            )
        version_relative = str(version_path.relative_to(directory))
        images = file_images(root, directory, tuple(sorted({*paths, version_relative})))
        manifest = str((directory / "Package.swift").relative_to(root))
        if any(
            d["kind"] != "remote"
            for d in lock_adapters.swift_declarations(root, manifest)
        ):
            raise ValueError(
                "Swift candidate artifacts require self-contained GitHub dependency declarations"
            )
        result[package] = Binding(
            package, relative, version_file, pointer, paths, version, images
        )
    return result


def admit(
    root: Path, spec: Mapping[str, object], policy: Mapping[str, object]
) -> Scope | None:
    bindings = read(root, spec)
    if not bindings:
        return None
    import ecosystem_updates as native

    groups: dict[str, set[str]] = {}
    urls: dict[str, set[str]] = {name: set() for name in bindings}
    declarations: list[ad.Table] = []
    used = set()
    for group, member in native.specifications(root, spec).items():
        names: set[str] = set()
        pending = list(lock_adapters.swift_local_manifests(root, member))
        visited = set()
        while pending:
            manifest = pending.pop()
            if manifest in visited:
                continue
            visited.add(manifest)
            body = tc.regular_input(root, manifest).decode()
            for declaration in lock_adapters.swift_declarations(root, manifest):
                if (
                    declaration["kind"] != "remote"
                    or declaration["package"] not in bindings
                ):
                    continue
                name = declaration["package"]
                binding = bindings[name]
                check_version(binding, declaration["bound"], policy)
                # The existing literal parser has already validated this URL.
                matches = re.findall(
                    r'"(https://github\.com/[^"\n]+)"',
                    body[declaration["start"] : declaration["end"]],
                )
                if (
                    len(matches) != 1
                    or lock_adapters.swift_repository(matches[0]) != name
                ):
                    raise ValueError(
                        "Swift candidate lacks unique declared source authority"
                    )
                urls[name].add(matches[0])
                declarations.append(
                    {
                        "group": group,
                        "manifest": manifest,
                        "package": name,
                        "url": matches[0],
                        "bound": declaration["bound"],
                    }
                )
                if name not in names:
                    names.add(name)
                    pending.append(
                        str(
                            (
                                tc.contained(root, binding.directory) / "Package.swift"
                            ).relative_to(root)
                        )
                    )
        if names:
            groups[group] = names
            used.update(names)
    if used != set(bindings):
        raise ValueError(
            "Swift candidate binding is not reachable from selected manifests"
        )
    return Scope(
        (root, json.dumps(spec, sort_keys=True)), bindings, groups, urls, declarations
    )


def check_version(binding: Binding, bound: str, policy: Mapping[str, object]) -> None:
    safe = registry.minimum_safe("swift", policy, binding.package)
    if (
        not registry.compatible("swift", binding.version, bound)
        or not registry.compatible(
            "swift",
            binding.version,
            registry.constraint("swift", policy, binding.package),
        )
        or (
            safe is not None
            and registry.stable_version("swift", binding.version) < safe
        )
    ):
        raise ValueError("Swift candidate source violates its declaration or policy")


def git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_OPTIONAL_LOCKS="0",
        GIT_TERMINAL_PROMPT="0",
        GIT_AUTHOR_NAME="Chainman Candidate",
        GIT_AUTHOR_EMAIL="candidate@example.invalid",
        GIT_COMMITTER_NAME="Chainman Candidate",
        GIT_COMMITTER_EMAIL="candidate@example.invalid",
        GIT_AUTHOR_DATE="2000-01-01T00:00:00+00:00",
        GIT_COMMITTER_DATE="2000-01-01T00:00:00+00:00",
    )
    return env


def git(repository: Path, *arguments: str) -> str:
    return tc.managed_run(
        [
            "git",
            "-c",
            "core.hooksPath=" + os.devnull,
            "-c",
            "core.autocrlf=false",
            *arguments,
        ],
        cwd=repository,
        env=git_env(),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def checkout_images(directory: Path) -> dict[str, tuple[str, bool]]:
    images = {}
    for path in sorted(directory.rglob("*")):
        name = str(path.relative_to(directory))
        if ".git" in Path(name).parts:
            continue
        if path.is_symlink():
            raise ValueError("Swift candidate checkout contains a symlink")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError("Swift candidate checkout contains a nonregular file")
        images[name] = (
            hashlib.sha256(path.read_bytes()).hexdigest(),
            bool(path.stat().st_mode & stat.S_IXUSR),
        )
    return images


def expected_images(binding: Binding) -> dict[str, tuple[str, bool]]:
    return {
        name: (hashlib.sha256(body).hexdigest(), bool(mode & stat.S_IXUSR))
        for name, (body, mode) in binding.files.items()
    }


def validate_checkout(path: Path, binding: Binding, revision: str) -> None:
    administration = path / ".git"
    if (
        administration.is_symlink()
        or not administration.is_dir()
        or git(path, "rev-parse", "--show-toplevel") != str(path)
    ):
        raise ValueError("Swift candidate requires its own native Git checkout")
    if (
        checkout_images(path) != expected_images(binding)
        or git(path, "rev-parse", "HEAD") != revision
    ):
        raise ValueError("Swift candidate native checkout differs from admitted source")
    # A clean worktree alone cannot detect clean/smudge filters changing the
    # committed source. Compare the immutable Git blobs and file modes as well.
    entries = git(path, "ls-tree", "-rz", "HEAD").split("\0")
    observed = {}
    for entry in entries:
        if not entry:
            continue
        header, name = entry.split("\t", 1)
        mode, kind, digest = header.split()
        if kind != "blob" or mode not in {"100644", "100755"}:
            raise ValueError("Swift candidate Git tree contains a nonregular source")
        body = tc.managed_run(
            ["git", "cat-file", "blob", digest],
            cwd=path,
            env=git_env(),
            check=True,
            capture_output=True,
        ).stdout
        observed[name] = (hashlib.sha256(body).hexdigest(), mode == "100755")
    if observed != expected_images(binding):
        raise ValueError("Swift candidate Git tree differs from admitted source")


@contextmanager
def bind(
    root: Path,
    spec: Mapping[str, object],
    policy: Mapping[str, object] | None = None,
    *,
    project: bool = True,
) -> Iterator[Scope | None]:
    current = _active.get()
    key = (root, json.dumps(spec, sort_keys=True))
    if current is not None:
        if current.key != key:
            raise ValueError("Nested Swift candidate scopes have different authority")
        yield current
        return
    scope = admit(root, spec, policy or {})
    if scope is None:
        yield None
        return
    images: dict[str, tuple[tuple[bytes, int] | None, bytes]] = {}
    written: list[str] = []
    token = _active.set(scope)
    try:
        with tempfile.TemporaryDirectory(
            prefix="chainman-swift-candidates-"
        ) as temporary:
            if project:
                for name, binding in scope.bindings.items():
                    repository = Path(temporary) / name
                    repository.mkdir(parents=True)
                    for source_name, (body, mode) in binding.files.items():
                        destination = repository / source_name
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        destination.write_bytes(body)
                        destination.chmod(mode)
                    git(repository, "init", "-q")
                    git(repository, "add", "-f", "--all")
                    git(repository, "commit", "-qm", "Explicit Swift candidate source")
                    git(repository, "tag", binding.version)
                    scope.repositories[name] = repository
                    scope.revisions[name] = git(repository, "rev-parse", "HEAD")
                    validate_checkout(repository, binding, scope.revisions[name])
                import ecosystem_updates as native

                specs = native.specifications(root, spec)
                for group, names in scope.groups.items():
                    relative = str(
                        (
                            Path(ad.text(specs[group]["directory"], "Swift directory"))
                            / ".swiftpm/configuration/mirrors.json"
                        )
                    )
                    path = tc.contained(root, relative)
                    before = (
                        (
                            tc.regular_input(root, relative),
                            stat.S_IMODE(path.stat().st_mode),
                        )
                        if path.exists()
                        else None
                    )
                    value = ad.table(
                        json.loads(before[0])
                        if before
                        else {"version": 1, "object": []},
                        "Swift mirrors",
                    )
                    if set(value) != {"version", "object"} or value["version"] != 1:
                        raise ValueError("Swift candidate requires mirrors schema1")
                    mirrors = [
                        ad.table(m, "Swift mirror")
                        for m in ad.array(value["object"], "Swift mirrors")
                    ]
                    originals = {url for name in names for url in scope.urls[name]}
                    if any(m.get("original") in originals for m in mirrors):
                        raise ValueError(
                            "Swift candidate cannot replace an existing source mirror"
                        )
                    mirrors.extend(
                        {"original": url, "mirror": scope.repositories[name].as_uri()}
                        for name in sorted(names)
                        for url in sorted(scope.urls[name])
                    )
                    expected = (
                        json.dumps(
                            {"version": 1, "object": mirrors}, sort_keys=True, indent=2
                        )
                        + "\n"
                    ).encode()
                    images[relative] = (before, expected)
                for relative, (before, expected) in images.items():
                    path = tc.contained(root, relative)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    written.append(relative)
                    tc.atomic_bytes(path, expected, before[1] if before else 0o600)
            yield scope
    finally:
        _active.reset(token)
        drift = []
        for relative in written:
            before, expected = images[relative]
            try:
                path = tc.contained(root, relative)
                mode = before[1] if before else 0o600
                if (
                    tc.regular_input(root, relative) != expected
                    or stat.S_IMODE(path.stat().st_mode) != mode
                ):
                    drift.append(relative)
                    continue
                if before:
                    tc.atomic_bytes(path, *before)
                else:
                    path.unlink()
            except (OSError, ValueError):
                drift.append(relative)
        if drift:
            raise ValueError(
                "Swift resolver changed candidate mirrors; changes preserved"
            )


def mirrored_package(root: Path, url: str) -> str | None:
    scope = _active.get()
    if scope is None or scope.key[0] != root:
        return None
    return next(
        (
            name
            for name, repository in scope.repositories.items()
            if url == repository.as_uri()
        ),
        None,
    )


def graph_source(root: Path, url: str, path: Path, version: str) -> str | None:
    scope = _active.get()
    if scope is None or scope.key[0] != root:
        return None
    for name, repository in scope.repositories.items():
        if url != repository.as_uri():
            continue
        binding = scope.bindings[name]
        if not path.is_relative_to(root):
            raise ValueError("Swift candidate checkout escapes the project")
        tc.contained(root, str(path.relative_to(root)))
        if version != binding.version:
            raise ValueError(
                "Swift candidate native checkout differs from admitted source"
            )
        validate_checkout(path, binding, scope.revisions[name])
        return name
    return None


def audit_identity(
    root: Path, identity: Identity, policy: Mapping[str, object]
) -> bool:
    scope = _active.get()
    if (
        scope is None
        or scope.key[0] != root
        or identity.provider != "swift"
        or identity.package not in scope.bindings
    ):
        return False
    binding = scope.bindings[identity.package]
    safe = registry.minimum_safe("swift", policy, binding.package)
    if (
        identity.version != binding.version
        or lock_adapters.swift_repository(identity.url) != binding.package
        or identity.digest != "git:" + scope.revisions.get(binding.package, "")
        or not registry.compatible(
            "swift",
            binding.version,
            registry.constraint("swift", policy, binding.package),
        )
        or (
            safe is not None
            and registry.stable_version("swift", binding.version) < safe
        )
    ):
        raise ValueError("Swift candidate lock identity or policy changed")
    return True


def materialized(
    root: Path, spec: Mapping[str, object], scope: Scope, *, expected: object = None
) -> list[ad.Table]:
    if read(root, spec) != scope.bindings:
        raise ValueError("Swift candidate source changed during resolution")
    import ecosystem_updates as native

    specs = native.specifications(root, spec)
    result = []
    frozen = {
        (record["group"], record["package"]): record
        for raw in (
            ad.array(expected, "Swift candidate selections")
            if expected is not None
            else []
        )
        for record in [ad.table(raw, "Swift candidate selection")]
    }
    if expected is not None and len(frozen) != len(
        ad.array(expected, "Swift candidate selections")
    ):
        raise ValueError("Duplicate Swift candidate selection")
    for group, names in scope.groups.items():
        identities = lock_adapters.identities(root, specs[group])
        directory = tc.contained(
            root, ad.text(specs[group]["directory"], "Swift directory")
        )
        state_path = str((directory / ".build/workspace-state.json").relative_to(root))
        workspace = ad.table(
            json.loads(tc.regular_input(root, state_path)), "Swift workspace state"
        )
        if workspace.get("version") != 6:
            raise ValueError("Unsupported Swift candidate workspace state")
        dependencies = [
            ad.table(d, "Swift workspace dependency")
            for d in ad.array(
                ad.table(workspace.get("object"), "Swift workspace").get(
                    "dependencies"
                ),
                "Swift dependencies",
            )
        ]
        for name in sorted(names):
            matching = [i for i in identities if i.package == name]
            if len(matching) != 1 or not audit_identity(root, matching[0], {}):
                raise ValueError("Swift candidate has no unique native lock identity")
            identity = name.rsplit("/", 1)[1].lower()
            matches = [
                d
                for d in dependencies
                if ad.table(d.get("packageRef"), "Swift package reference").get(
                    "identity"
                )
                == identity
            ]
            selected = frozen.get((group, name))
            location = (
                selected.get("materialization")
                if selected is not None
                else scope.repositories[name].as_uri()
            )
            if len(matches) != 1:
                raise ValueError("Swift candidate lacks unique native imported source")
            dependency = matches[0]
            reference = ad.table(
                dependency.get("packageRef"), "Swift package reference"
            )
            state = ad.table(dependency.get("state"), "Swift checkout state")
            if (
                reference.get("kind") != "remoteSourceControl"
                or reference.get("location") != location
                or state
                != {
                    "name": "sourceControlCheckout",
                    "checkoutState": {
                        "revision": scope.revisions[name],
                        "version": scope.bindings[name].version,
                    },
                }
            ):
                raise ValueError("Swift candidate native imported identity changed")
            subpath = ad.text(dependency.get("subpath"), "Swift checkout subpath")
            if len(Path(subpath).parts) != 1 or subpath in {".", ".."}:
                raise ValueError(
                    "Swift candidate checkout subpath must be one contained component"
                )
            checkout = tc.contained(
                root, str((directory / ".build/checkouts" / subpath).relative_to(root))
            )
            validate_checkout(checkout, scope.bindings[name], scope.revisions[name])
            result.append(
                {
                    "group": group,
                    "package": name,
                    "version": scope.bindings[name].version,
                    "revision": scope.revisions[name],
                    "source": scope.bindings[name].record(),
                    "materialization": location,
                }
            )
    if expected is not None and set(frozen) != {
        (r["group"], r["package"]) for r in result
    }:
        raise ValueError("Swift candidate selection inventory changed")
    return result
