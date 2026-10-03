"""Explicit candidate Pub sources; never evidence of registry publication."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, MutableMapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from urllib.parse import unquote, urlsplit

import adapter_data as ad
import manifests
import registry
import toolchain as tc


@dataclass(frozen=True)
class Binding:
    name: str
    manifest: str
    version: str
    digest: str


@dataclass
class Scope:
    key: tuple[Path, str]
    bindings: dict[str, Binding]
    groups: dict[str, set[str]]
    declarations: list[ad.Table]

    def records(self) -> list[ad.Table]:
        return [
            {
                "name": b.name,
                "manifest": b.manifest,
                "version": b.version,
                "manifest_sha256": b.digest,
                "groups": sorted(
                    g for g, names in self.groups.items() if name in names
                ),
                "declarations": [d for d in self.declarations if d["name"] == name],
            }
            for name, b in sorted(self.bindings.items())
        ]


_active: ContextVar[Scope | None] = ContextVar("candidate_pub_scope", default=None)


def authority(records: object) -> list[ad.Table]:
    # Other selected adapters may update source dependency ranges before this
    # adapter resolves. The resolution receipt, not the original intake digest,
    # freezes those bytes for the final post-hook audit.
    return [
        {
            key: value
            for key, value in ad.table(raw, "Pub candidate record").items()
            if key != "manifest_sha256"
        }
        for raw in ad.array(records, "Pub candidate records")
    ]


def read(root: Path, spec: Mapping[str, object]) -> dict[str, Binding]:
    raw = ad.string_map(spec.get("pub_sources", {}), "Pub candidate sources")
    if not raw:
        return {}
    if spec.get("adapter") != "flutter" or spec.get(
        "resolve", [["flutter", "pub", "get"]]
    ) not in ([["flutter", "pub", "get"]], [["dart", "pub", "get"]]):
        raise ValueError("pub_sources requires ordinary native Pub get")
    result = {}
    for name, relative in raw.items():
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,127}", name):
            raise ValueError("Pub candidate source requires an ordinary package name")
        path = tc.contained(root, relative)
        if path.name != "pubspec.yaml":
            raise ValueError("Pub candidate source must name a pubspec.yaml manifest")
        body = tc.regular_input(root, relative)
        value = manifests.document(path, body=body.decode())[0]
        if not isinstance(value, Mapping):
            raise ValueError("Pub candidate source manifest must be a mapping")
        if value.get("name") != name:
            raise ValueError(
                "Pub candidate source package name differs from its binding"
            )
        version = ad.text(value.get("version"), "Pub candidate source version")
        if (
            not re.fullmatch(r"\d+\.\d+\.\d+", version)
            or registry.version("pub", version) is None
        ):
            raise ValueError("Pub candidate source requires a stable release version")
        result[name] = Binding(
            name, relative, version, hashlib.sha256(body).hexdigest()
        )
    return result


def admission(
    root: Path, spec: Mapping[str, object], policy: Mapping[str, object]
) -> Scope:
    # Import only after ecosystem_updates has finished defining its API.
    import ecosystem_updates as native

    bindings = read(root, spec)
    scope = Scope(
        (root.resolve(), json.dumps(dict(spec), sort_keys=True)), bindings, {}, []
    )
    for group, member in native.specifications(root, spec).items():
        workspace = ad.table(member["pub"], "Pub workspace")
        for relative in ad.strings(workspace["guarded_inputs"], "Pub guarded inputs"):
            path = tc.contained(root, relative)
            if not path.exists():
                continue
            value = manifests.document(path)[0]
            if isinstance(value, Mapping):
                overrides = value.get("dependency_overrides", {})
                if isinstance(overrides, Mapping):
                    for name in bindings.keys() & overrides.keys():
                        existing = overrides[name]
                        if (
                            not isinstance(existing, Mapping)
                            or set(existing) != {"path"}
                            or tc.local_source(
                                root,
                                path.parent,
                                ad.text(
                                    existing["path"], "Pub existing candidate path"
                                ),
                            )
                            != tc.contained(root, bindings[name].manifest).parent
                        ):
                            raise ValueError(
                                "Pub candidate source cannot replace a declared override"
                            )
        for raw in ad.array(workspace["pins"], "Pub pins"):
            pin = ad.table(raw, "Pub dependency")
            name = ad.text(pin["name"], "Pub package")
            if name not in bindings:
                continue
            if pin.get("pub_override"):
                raise ValueError(
                    "Pub candidate source cannot replace a declared override"
                )
            requirement = native.old_requirement(root, pin)
            binding = bindings[name]
            safe = registry.minimum_safe("pub", policy, name)
            if (
                not native.accepts("pub", binding.version, requirement)
                or not registry.compatible(
                    "pub", binding.version, registry.constraint("pub", policy, name)
                )
                or (
                    safe is not None
                    and registry.stable_version("pub", binding.version) < safe
                )
            ):
                raise ValueError(
                    "Pub candidate source version violates its requirement or policy"
                )
            scope.groups.setdefault(group, set()).add(name)
            scope.declarations.append(
                {"name": name, "file": pin["file"], "range": requirement}
            )
    used = set().union(*scope.groups.values()) if scope.groups else set()
    if used != set(bindings):
        raise ValueError(
            "Pub candidate source binding is unused by hosted declarations"
        )
    return scope


@contextmanager
def bind(
    root: Path, spec: Mapping[str, object], policy: Mapping[str, object] | None = None
) -> Iterator[Scope | None]:
    if not ad.string_map(spec.get("pub_sources", {}), "Pub candidate sources"):
        yield None
        return
    key = (root.resolve(), json.dumps(dict(spec), sort_keys=True))
    active = _active.get()
    if active is not None:
        if active.key != key:
            raise ValueError("Nested Pub source scope changed its authority")
        yield active
        return
    scope = admission(root, spec, policy or {})
    import ecosystem_updates as native

    specs = native.specifications(root, spec)
    images: dict[str, tuple[bytes | None, int, bytes]] = {}
    for group, names in scope.groups.items():
        directory = tc.contained(
            root, ad.text(specs[group]["directory"], "Pub directory")
        )
        relative = str((directory / "pubspec_overrides.yaml").relative_to(root))
        path = tc.contained(root, relative)
        before = tc.regular_input(root, relative) if path.exists() else None
        mode = stat.S_IMODE(path.stat().st_mode) if before is not None else 0o644
        value, render = manifests.document(path, body=(before or b"{}\n").decode())
        if not isinstance(value, MutableMapping):
            raise ValueError("Pub override file must be a mutable mapping")
        overrides = value.setdefault("dependency_overrides", {})
        if not isinstance(overrides, MutableMapping):
            raise ValueError("Pub dependency overrides must be a mapping")
        for name in sorted(names):
            if name in overrides:
                raise ValueError(
                    "Pub candidate source conflicts with an existing override"
                )
            target = tc.contained(root, scope.bindings[name].manifest).parent
            overrides[name] = {
                "path": Path(os.path.relpath(target, directory)).as_posix()
            }
        images[relative] = (before, mode, render().encode())
    written: list[str] = []
    token = _active.set(scope)
    try:
        for relative, (before, mode, expected) in images.items():
            path = tc.contained(root, relative)
            current = tc.regular_input(root, relative) if path.exists() else None
            if current != before or (
                before is not None and stat.S_IMODE(path.stat().st_mode) != mode
            ):
                raise ValueError("Pub override changed before candidate source binding")
            tc.atomic_bytes(path, expected, mode)
            written.append(relative)
        yield scope
    finally:
        _active.reset(token)
        drift = []
        for relative in written:
            before, mode, expected = images[relative]
            try:
                path = tc.contained(root, relative)
                if (
                    tc.regular_input(root, relative) != expected
                    or stat.S_IMODE(path.stat().st_mode) != mode
                ):
                    drift.append(relative)
                    continue
                if before is None:
                    path.unlink()
                else:
                    tc.atomic_bytes(path, before, mode)
            except (OSError, ValueError):
                drift.append(relative)
        if drift:
            raise ValueError(
                "Pub resolver changed a candidate override; changes preserved for inspection"
            )


def materialized(
    root: Path, spec: Mapping[str, object], scope: Scope
) -> list[ad.Table]:
    import ecosystem_updates as native

    if read(root, spec) != scope.bindings:
        raise ValueError("Pub candidate source manifest changed during resolution")
    specs = native.specifications(root, spec)
    for group, names in scope.groups.items():
        directory = tc.contained(
            root, ad.text(specs[group]["directory"], "Pub directory")
        )
        locked = native.pub_lock_packages(root, directory)
        config_path = str(
            (directory / ".dart_tool/package_config.json").relative_to(root)
        )
        config = ad.table(
            json.loads(tc.regular_input(root, config_path)), "Pub package configuration"
        )
        if config.get("configVersion") != 2:
            raise ValueError(
                "Pub candidate import requires package configuration version 2"
            )
        packages = ad.array(config.get("packages"), "Pub native packages")
        for name in sorted(names):
            binding = scope.bindings[name]
            item = ad.table(locked.get(name), "Pub candidate lock entry")
            description = ad.table(item.get("description"), "Pub candidate description")
            if (
                item.get("source") != "path"
                or item.get("version") != binding.version
                or description.get("relative") is not True
            ):
                raise ValueError(
                    "Pub candidate source differs from its native lock identity"
                )
            target = tc.contained(root, binding.manifest).parent
            if (
                tc.local_source(
                    root,
                    directory,
                    ad.text(description.get("path"), "Pub candidate path"),
                )
                != target
            ):
                raise ValueError(
                    "Pub candidate source lock refers to another project path"
                )
            matches = [
                ad.table(p, "Pub native package")
                for p in packages
                if ad.table(p, "Pub native package").get("name") == name
            ]
            if len(matches) != 1:
                raise ValueError(
                    "Pub candidate source has a missing or ambiguous native import"
                )
            if matches[0].get("packageUri") != "lib/":
                raise ValueError(
                    "Pub candidate import must use its native lib directory"
                )
            uri = urlsplit(ad.text(matches[0].get("rootUri"), "Pub native root URI"))
            if (
                uri.scheme not in ("", "file")
                or uri.netloc
                or uri.query
                or uri.fragment
            ):
                raise ValueError(
                    "Pub candidate native import requires a local file URI"
                )
            actual = Path(unquote(uri.path))
            if not actual.is_absolute():
                actual = (directory / ".dart_tool" / actual).resolve()
            if actual != target:
                raise ValueError(
                    "Pub candidate native import refers to another project path"
                )
    return scope.records()
