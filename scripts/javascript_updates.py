"""Plan and audit pnpm workspace updates without application-specific update code.

Paths in spec.manifests, workspace and copy_inputs are relative to spec.directory.
spec.held_dependencies contains exact manifest/package/reason entries for pins
owned by a preceding toolchain stage; those dependencies retain their exact pin.
Policies share registry age/constraint/exception rules. policy.javascript adds
mode, package_constraints, catalog_constraints, prefix_constraints, peer_exceptions,
solver_states and fallback_attempts. Each compatibility rule has range and reason;
peer exceptions identify an exact manifest/source/peer triple and a reason.

resolve writes only after resolution and audit in a disposable declared-input copy.
The caller owns its clean-repository transaction, verification and Git checkpoint.
SDK pins and application generators remain separate stages of that transaction.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import subprocess
import tempfile
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import chainman
import adapter_data as inputs
import registry
from dependency_identity import Identity, inventory as identity_inventory
import toolchain as tc
import updates
from ruamel.yaml import YAML
from semantic_version import NpmSpec, Version

SECTIONS = inputs.DEPENDENCY_SECTIONS
NAME = r"(?:@[a-z0-9_.~-]+/)?[a-z0-9_.~-]+"


def package_name(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(NAME, value):
        raise ValueError("Expected a registry package name")
    return value


def rule_range(rule):
    if not isinstance(rule, Mapping) or not str(rule.get("reason", "")).strip():
        raise ValueError("JavaScript compatibility rules require a reason")
    value = rule.get("range")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("JavaScript compatibility rules require a range")
    NpmSpec(value)
    return value


def bounded(options, name, default, maximum):
    value = options.get(name, default)
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}")
    return value


def parse_requirement(
    alias: str, value: object
) -> tuple[str, str, str, str | None] | None:
    if not isinstance(value, str):
        raise ValueError("JavaScript dependency requirements must be strings")
    actual, prefix = alias, ""
    if value.startswith("npm:"):
        match = re.fullmatch(rf"npm:({NAME})@(.+)", value)
        if not match:
            raise ValueError("Malformed npm alias requirement")
        actual, value = match.groups()
        prefix = f"npm:{actual}@"
    package_name(actual)
    if value.startswith(("workspace:", "catalog:", "file:", "link:")):
        return None
    if value.startswith(("git", "http", "github:", "patch:")):
        raise ValueError(
            "Remote JavaScript sources require a separate immutable-source adapter"
        )
    if value == "latest":
        value = "*"
    requirement = peer_range(value)
    NpmSpec(requirement)
    simple = re.fullmatch(r"([~^]?)([0-9]+\.[0-9]+\.[0-9]+)", value)
    # Complex declared ranges are contracts, not templates to rewrite loosely.
    return actual, prefix, requirement, simple.group(1) if simple else None


def document(path: Path, body: str) -> tuple[dict, Callable[[], str]]:
    if path.suffix == ".json":
        value = json.loads(body)
        whitespace = re.search(r"\n([ \t]+)\"", body)
        indent = whitespace[1] if whitespace else 2
        return (
            value,
            lambda: json.dumps(value, indent=indent, ensure_ascii=False) + "\n",
        )
    yaml = YAML()
    yaml.preserve_quotes = True
    value = yaml.load(body) or {}

    def render():
        buffer = io.StringIO()
        yaml.dump(value, buffer)
        return buffer.getvalue()

    return value, render


@dataclass
class Pin:
    file: str
    pointer: tuple[str, ...]
    alias: str
    name: str
    original: str
    prefix: str
    requirement: str
    operator: str | None
    catalog: str | None = None
    users: set[str] = field(default_factory=set)
    ranges: list[str] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)
    held: bool = False

    def replacement(self, selected: str) -> str:
        if self.operator is None:
            # Preserve unions, bounded ranges and wildcards. The lock and the
            # post-resolution audit still enforce their selected compatibility.
            return self.original
        return self.prefix + self.operator + selected


def manifest_paths(
    directory: Path,
    spec: Mapping[str, object],
    root_manifest: Mapping[str, object],
    settings: Mapping[str, object],
) -> list[str]:
    patterns = spec.get("manifests")
    if patterns is None:
        workspaces = settings.get("packages", root_manifest.get("workspaces", []))
        if isinstance(workspaces, Mapping):
            workspaces = workspaces.get("packages", [])
        patterns = [
            "package.json",
            *[
                p + "/package.json"
                for p in inputs.strings(workspaces, "JavaScript workspaces")
            ],
        ]
    paths = inputs.strings(patterns, "JavaScript manifest patterns")
    included, excluded = {"package.json"}, set[str]()
    for pattern in paths:
        negative = pattern.startswith("!")
        pattern = pattern.removeprefix("!")
        tc.contained(directory, pattern)
        found = {p.relative_to(directory).as_posix() for p in directory.glob(pattern)}
        (excluded if negative else included).update(found)
    return sorted(included - excluded)


class Workspace:
    def __init__(self, root: Path, spec: dict) -> None:
        self.root, self.spec = root, spec
        self.directory = tc.contained(root, spec.get("directory", "."))
        self.manager = spec.get("manager", "pnpm")
        if self.manager not in ("npm", "pnpm"):
            raise ValueError("JavaScript manager must be npm or pnpm")
        self.workspace = spec.get("workspace", "pnpm-workspace.yaml")
        self.lock = "package-lock.json" if self.manager == "npm" else "pnpm-lock.yaml"
        self.documents: dict[str, tuple[dict, Callable[[], str]]] = {}
        self.original: dict[str, bytes] = {}
        self.modes: dict[str, int] = {}
        self.pins: list[Pin] = []
        self.refs: dict[str, dict[str, int]] = {}
        self.locals: dict[str, str] = {}
        self.duplicates: list[tuple[int, int]] = []
        self.patches: dict[str, str] = {}
        self.source_contents: dict[str, dict] = {}
        root_manifest = self.load("package.json")
        workspace_path = tc.contained(self.directory, self.workspace)
        self.settings = (
            self.load(self.workspace)
            if self.manager == "pnpm" and workspace_path.exists()
            else {}
        )
        self.manifests = manifest_paths(
            self.directory, spec, root_manifest, self.settings
        )
        if spec.get("retained_sources"):
            import javascript_sources

            if any(
                item["manifest"] not in self.manifests
                for item in javascript_sources.declarations(spec)
            ):
                raise ValueError(
                    "Retained sources must belong to declared JavaScript manifests"
                )
        for path in self.manifests:
            if not path.endswith("package.json"):
                raise ValueError(
                    "JavaScript manifest patterns may select only package.json files"
                )
            content = self.load(path)
            if content.get("name"):
                if content["name"] in self.locals:
                    raise ValueError("Duplicate local workspace package name")
                self.locals[content["name"]] = path
        for name, loaded in list(self.documents.items()):
            value = loaded[0]
            patched = (
                value.get("patchedDependencies", {})
                if name == self.workspace
                else value.get("pnpm", {}).get("patchedDependencies", {})
            )
            for selector, relative in patched.items():
                if isinstance(relative, Mapping):
                    relative = relative.get("path")
                if not isinstance(relative, str):
                    raise ValueError(
                        "Patched dependencies require a contained patch path"
                    )
                self.keep(relative)
                self.patches[selector] = relative
        for pattern in spec.get("copy_inputs", []):
            tc.contained(self.directory, pattern)
            for copy_path in self.directory.glob(pattern):
                if copy_path.is_dir():
                    continue
                self.keep(copy_path.relative_to(self.directory).as_posix())
        lock = tc.contained(self.directory, self.lock)
        if lock.exists():
            self.keep(self.lock)
        self.discover()
        self.apply_held()
        if spec.get("retained_sources"):
            import javascript_sources

            javascript_sources.configured(self)

    def apply_held(self) -> None:
        for rule in self.spec.get("held_dependencies", []):
            if (
                set(rule) != {"manifest", "package", "reason"}
                or not str(rule["reason"]).strip()
            ):
                raise ValueError("Held dependencies require manifest/package/reason")
            matches = [
                p
                for p in self.pins
                if rule["manifest"] in p.users and p.alias == rule["package"]
            ]
            if not matches or any(p.operator != "" for p in matches):
                raise ValueError(
                    "Held dependencies must identify an existing exact version pin"
                )
            for pin in matches:
                pin.held = True

    def keep(self, relative: str) -> bytes:
        path = tc.contained(self.directory, relative)
        data = tc.regular_input(self.directory, relative)
        if len(data) > 16 * 1024 * 1024:
            raise ValueError("JavaScript resolver input exceeds 16 MiB")
        self.original[relative] = data
        self.modes[relative] = path.stat().st_mode & 0o777
        return data

    def load(self, relative: str) -> dict:
        if relative not in self.documents:
            self.documents[relative] = document(
                Path(relative), self.keep(relative).decode()
            )
        result = self.documents[relative][0]
        if not isinstance(result, Mapping):
            raise ValueError(
                "JavaScript manifests and workspace settings must be objects"
            )
        return result

    def add(
        self,
        file: str,
        pointer: Sequence[str],
        alias: str,
        value: str,
        *,
        catalog: str | None = None,
        user: str | None = None,
    ) -> int | None:
        if isinstance(value, str) and value.startswith(
            ("file:", "link:", "workspace:")
        ):
            self.local_manifest(alias, value, str(Path(user or file).parent))
            return None
        if self.spec.get("retained_sources") and user:
            import javascript_sources

            if javascript_sources.matched(self.spec, user, alias, value):
                return None
        if value == "-" or (isinstance(value, str) and value.startswith("$")):
            return None  # Removal and dependency-reference overrides are structural.
        parsed = parse_requirement(alias, value)
        if parsed is None:
            return None
        actual, prefix, requirement, operator = parsed
        pin = Pin(
            file,
            tuple(pointer),
            alias,
            actual,
            value,
            prefix,
            requirement,
            operator,
            catalog,
        )
        if user:
            pin.users.add(user)
        self.pins.append(pin)
        return len(self.pins) - 1

    def local_manifest(self, alias: str, value: str, base: str = ".") -> str:
        """Bind local declarations to the explicitly included workspace inputs."""
        protocol, _, relative = value.partition(":")
        if protocol == "workspace" and not relative.startswith((".", "/")):
            manifest = self.locals.get(alias)
            if manifest is None:
                raise ValueError(
                    "Workspace dependency names an undeclared local package"
                )
            version = self.documents[manifest][0].get("version")
            requirement = "*" if relative in ("*", "^", "~") else relative
            if (
                not isinstance(version, str)
                or registry.lock_version("npm", version) is None
                or Version(version) not in NpmSpec(requirement)
            ):
                raise ValueError(
                    "Workspace dependency violates the declared local version"
                )
        else:
            path = tc.local_source(
                self.directory, tc.contained(self.directory, base), relative
            )
            manifest = (path / "package.json").relative_to(self.directory).as_posix()
        if (
            manifest not in self.manifests
            or self.documents[manifest][0].get("name") != alias
        ):
            raise ValueError(
                "Local dependency must bind its name to a declared workspace manifest"
            )
        return manifest

    def discover(self) -> None:
        catalogs = {}
        for name, entries in [
            ("default", self.settings.get("catalog", {})),
            *self.settings.get("catalogs", {}).items(),
        ]:
            for alias, value in entries.items():
                pointer = (
                    ("catalog", alias)
                    if name == "default" and "catalog" in self.settings
                    else ("catalogs", name, alias)
                )
                catalogs[name, alias] = self.add(
                    self.workspace, pointer, alias, value, catalog=name
                )
        for file in self.manifests:
            value = self.documents[file][0]
            self.refs[file] = {}
            for section in SECTIONS:
                for alias, requirement in value.get(section, {}).items():
                    if isinstance(requirement, str) and requirement.startswith(
                        "catalog:"
                    ):
                        key = requirement.removeprefix("catalog:") or "default"
                        if (key, alias) not in catalogs:
                            raise ValueError(
                                "Manifest references a missing catalog entry"
                            )
                        index = catalogs[key, alias]
                        if index is not None:
                            self.pins[index].users.add(file)
                    else:
                        index = self.add(
                            file, (section, alias), alias, requirement, user=file
                        )
                    if index is not None:
                        if alias in self.refs[file] and self.refs[file][alias] != index:
                            self.duplicates.append((self.refs[file][alias], index))
                        else:
                            self.refs[file][alias] = index
            for selector, requirement in (
                value.get("pnpm", {}).get("overrides", {}).items()
            ):
                self.override(
                    file, ("pnpm", "overrides", selector), selector, requirement
                )
            if self.manager == "npm":
                self.npm_overrides(file, ("overrides",), value.get("overrides", {}))
        for selector, requirement in self.settings.get("overrides", {}).items():
            self.override(
                self.workspace, ("overrides", selector), selector, requirement
            )

    def override(
        self, file: str, pointer: Sequence[str], selector: str, requirement: str
    ) -> None:
        if self.spec.get("retained_sources"):
            import javascript_sources

            if javascript_sources.override(self.spec, selector, requirement):
                return
        # Keep parent/version selectors intact while updating their replacement.
        target = re.split(r">(?=@?[A-Za-z_~])", selector)[-1].strip()
        match = re.fullmatch(rf"({NAME})(?:@(.+))?", target)
        if not match:
            raise ValueError("Unsupported pnpm override selector")
        if match[2]:
            NpmSpec(match[2])
        self.add(file, pointer, match[1], requirement)

    def npm_overrides(
        self,
        file: str,
        pointer: Sequence[str],
        table: Mapping,
        parent: str | None = None,
    ) -> None:
        for selector, requirement in table.items():
            target = parent if selector == "." else selector
            match = re.fullmatch(rf"({NAME})(?:@(.+))?", target or "")
            if not match:
                raise ValueError("Unsupported npm override selector")
            if isinstance(requirement, Mapping):
                self.npm_overrides(file, (*pointer, selector), requirement, target)
            else:
                self.add(file, (*pointer, selector), match[1], requirement)

    def render(
        self, selected: Sequence[str], *, resolver_pins: bool = False
    ) -> dict[str, bytes]:
        # An empty selection serializes existing declarations without repinning.
        for pin, version in zip(self.pins, selected, strict=False):
            table = self.documents[pin.file][0]
            for component in pin.pointer[:-1]:
                table = table[component]
            table[pin.pointer[-1]] = (
                pin.prefix + version
                if resolver_pins and pin.operator is None
                else pin.replacement(version)
            )
        result = dict(self.original)
        for file, (_, render) in self.documents.items():
            result[file] = render().encode()
        return result


class Evidence:
    def __init__(self, policy: dict, now: datetime) -> None:
        if now.tzinfo is None:
            raise ValueError("JavaScript update time requires a timezone")
        registry.minimum_age(policy)
        self.policy, self.now = policy, now
        self.cache: dict[str, tuple[list[registry.Release], dict[str, object]]] = {}
        self.baseline: set[Identity] = set()

    def get(self, name: str) -> tuple[list[registry.Release], dict[str, object]]:
        package_name(name)
        if name not in self.cache:
            body = inputs.table(
                registry.data(f"https://registry.npmjs.org/{quote(name, safe='')}"),
                "Registry package metadata",
            )
            if body.get("name") != name:
                raise ValueError("Registry package identity disagrees with its request")
            releases = registry.releases(
                "npm", name, include_prerelease=True, include_deprecated=True
            )
            versions = inputs.table(body.get("versions", {}), "Registry versions")
            for release in releases:
                if release.version not in versions:
                    raise ValueError(
                        "Registry release metadata disagrees with its manifest"
                    )
                info = inputs.table(
                    versions[release.version], "Registry version manifest"
                )
                if info.get("name") != name or info.get("version") != release.version:
                    raise ValueError("Registry version manifest identity mismatch")
            # Baseline maturity checks visit every transitive package. Keep the
            # complete version inventory and dependency evidence, not each release's
            # unrelated README, scripts and development dependency payloads.
            # Dates and immutable artifact identities remain in releases above.
            dependency_versions = {
                value: {
                    key: info[key]
                    for key in (
                        "dependencies",
                        "optionalDependencies",
                        "bundleDependencies",
                        "bundledDependencies",
                        "peerDependencies",
                        "peerDependenciesMeta",
                    )
                    if key in info
                }
                if isinstance(info, Mapping)
                else info
                for value, info in versions.items()
            }
            self.cache[name] = releases, dependency_versions
        return self.cache[name]

    def peers(
        self, name: str, version: str, *, manifest: str | None = None
    ) -> tuple[dict[str, str], dict[str, inputs.PeerMetadata]]:
        info = self.get(name)[1].get(version)
        if not isinstance(info, Mapping):
            raise ValueError("Selected package lacks registry dependency metadata")
        peers = info.get("peerDependencies", {})
        if not isinstance(peers, Mapping):
            raise ValueError("Peer dependencies must be an object of named ranges")
        peers = {peer: peer_range(value) for peer, value in peers.items()}
        for peer, value in peers.items():
            package_name(peer)
            # An exception belongs to one importer edge, never the cached
            # package. Keep structural validation even for excepted ranges.
            if manifest is None or not peer_ignored(
                self.policy.get("javascript", {}), manifest, name, peer
            ):
                NpmSpec(value)
        return peers, inputs.peer_metadata(info.get("peerDependenciesMeta", {}))


def peer_range(value: object) -> str:
    # npm permits whitespace after comparators; semantic_version does not.
    # Preserve token boundaries and leave all other syntax to its strict parser.
    if not isinstance(value, str):
        raise ValueError("Peer dependency requirements must be strings")
    return re.sub(r"(?<=[<>=~^])\s+(?=[v0-9xX*])", "", value)


def peer_ignored(options: dict, manifest: str, source: str, peer: str) -> bool:
    ignored = False
    for rule in options.get("peer_exceptions", []):
        if (
            set(rule) != {"manifest", "source", "peer", "reason"}
            or not str(rule["reason"]).strip()
        ):
            raise ValueError(
                "Peer exceptions require exact manifest/source/peer and reason"
            )
        if any(
            not isinstance(rule[k], str) or any(c in rule[k] for c in "*?!")
            for k in ("manifest", "source", "peer")
        ):
            raise ValueError("Peer exceptions cannot use wildcard scope")
        if (rule["manifest"], rule["source"], rule["peer"]) == (manifest, source, peer):
            ignored = True
    return ignored


def compatibility(pin, options):
    ranges = []
    for prefix in options.get("prefix_constraints", []):
        if not isinstance(prefix.get("prefix"), str) or not prefix["prefix"]:
            raise ValueError("Compatibility prefixes must be explicit nonempty strings")
        bound = rule_range(prefix)
        excluded = prefix.get("exclude", [])
        if not isinstance(excluded, list) or any(
            not isinstance(name, str) or not re.fullmatch(NAME, name)
            for name in excluded
        ):
            raise ValueError("Prefix exclusions must identify exact package names")
        if pin.name.startswith(prefix["prefix"]) and pin.name not in excluded:
            ranges.append(bound)
    packages = options.get("package_constraints", {})
    for user in pin.users:
        rule = packages.get(user, {}).get(pin.alias)
        if rule:
            ranges.append(rule_range(rule))
    if pin.catalog:
        rule = (
            options.get("catalog_constraints", {}).get(pin.catalog, {}).get(pin.alias)
        )
        if rule:
            ranges.append(rule_range(rule))
    return ranges


def scoped_policy(policy, name, ranges):
    common = registry.constraint("npm", policy, name)
    inherited = common if isinstance(common, tuple) else ((common,) if common else ())
    terms = [*inherited, *ranges]
    if any(not isinstance(bound, str) or not bound.strip() for bound in terms):
        raise ValueError("JavaScript constraint scopes require nonempty range strings")
    bounds = list(dict.fromkeys(terms))
    # Keep each original range's prerelease semantics and avoid distributing AND
    # over OR. Work is bounded by the input terms, not their Cartesian product.
    combined = list(registry.validate_constraint("npm", bounds)) if bounds else "*"
    return {
        **policy,
        "constraints": {
            **policy.get("constraints", {}),
            "npm:" + name: {
                "range": combined,
                "reason": "Intersection of the active JavaScript compatibility contracts.",
            },
        },
    }


def compatible_scope(pin, before, *, spec, check_declaration=False):
    originals = [
        value
        for value in before.get("requirements", [])
        if value["file"] == pin.file
        and tuple(value["pointer"]) == pin.pointer
        and value["name"] == pin.name
    ]
    if (
        not originals
        and spec.get("manager", "pnpm") == "pnpm"
        and spec.get("reconcile_policy")
        and pin.file == spec.get("workspace", "pnpm-workspace.yaml")
        and len(pin.pointer) == 2
        and pin.pointer[0] == "overrides"
    ):
        # Reconciliation changes the owner, not the original compatibility
        # contract. Join only this exact selector and package to its old owner.
        originals = [
            value
            for value in before.get("requirements", [])
            if value["file"] == "package.json"
            and tuple(value["pointer"]) == ("pnpm", "overrides", pin.pointer[1])
            and value["name"] == pin.name
        ]
    if len(originals) != 1:
        raise ValueError("Missing or ambiguous original compatible dependency scope")
    original = originals[0]["requirement"]
    simple = re.fullmatch(r"([~^]?)([0-9]+\.[0-9]+\.[0-9]+)", original)
    bound = (simple[1] or "^") + simple[2] if simple else original
    NpmSpec(bound)
    if check_declaration and pin.requirement != original:
        current = re.fullmatch(r"([~^]?)([0-9]+\.[0-9]+\.[0-9]+)", pin.requirement)
        if (
            not simple
            or not current
            or current[1] != simple[1]
            or Version(current[2]) not in NpmSpec(bound)
        ):
            raise ValueError("Written dependency escaped its original compatible range")
    return bound


def selected_policy(workspace, pin, selected, evidence, options):
    ranges = list(pin.ranges)
    for manifest in pin.users:
        for index in workspace.refs[manifest].values():
            source = workspace.pins[index]
            for peer, bound in evidence.peers(
                source.name, selected[index], manifest=manifest
            )[0].items():
                if peer in (pin.alias, pin.name) and not peer_ignored(
                    options, manifest, source.name, peer
                ):
                    ranges.append(bound)
    return scoped_policy(evidence.policy, pin.name, ranges)


def reconcile_policy(workspace, policy, *, check=False):
    """Apply declared shared catalog/range policy without project-specific code."""
    if not workspace.spec.get("reconcile_policy"):
        return
    if workspace.manager != "pnpm":
        raise ValueError("Catalog policy reconciliation requires pnpm")
    options = policy.get("javascript", {})
    catalogs = options.get("catalog_constraints", {})
    packages = options.get("package_constraints", {})
    changed = False

    def assign(table, key, expected):
        nonlocal changed
        changed |= table.get(key) != expected
        table[key] = expected

    if workspace.workspace not in workspace.documents:
        workspace.documents[workspace.workspace] = document(
            Path(workspace.workspace), "{}\n"
        )
        workspace.settings = workspace.documents[workspace.workspace][0]
    for name, rules in catalogs.items():
        table = (
            workspace.settings.setdefault("catalog", {})
            if name == "default"
            else workspace.settings.setdefault("catalogs", {}).setdefault(name, {})
        )
        for alias, rule in rules.items():
            package_name(alias)
            assign(table, alias, rule_range(rule))
    for file in workspace.manifests:
        content = workspace.documents[file][0]
        for section in SECTIONS:
            for alias, value in content.get(section, {}).items():
                exception = packages.get(file, {}).get(alias)
                if exception:
                    assign(content[section], alias, rule_range(exception))
                elif alias in catalogs.get("default", {}) and not value.startswith(
                    ("workspace:", "file:", "link:", "catalog:")
                ):
                    assign(content[section], alias, "catalog:")
    overrides = options.get("override_constraints", {})
    manifest = workspace.documents["package.json"][0]
    legacy_settings = manifest.get("pnpm", {})
    legacy = legacy_settings.get("overrides", {})
    effective = workspace.settings.get("overrides", {})
    # pnpm 11 no longer reads package.json configuration. Preserve every legacy
    # override, but do not guess precedence when two declarations disagree.
    if any(
        key in effective and effective[key] != value for key, value in legacy.items()
    ):
        raise ValueError(
            "Conflicting pnpm override declarations require reconciliation"
        )
    if "overrides" in legacy_settings or overrides:
        table = workspace.settings.setdefault("overrides", {})
        for selector, value in legacy.items():
            assign(table, selector, value)
        if "overrides" in legacy_settings:
            del legacy_settings["overrides"]
            if not legacy_settings:
                del manifest["pnpm"]
            changed = True
        for selector, rule in overrides.items():
            assign(table, selector, rule_range(rule))
    if check and changed:
        raise ValueError(
            "JavaScript catalog references or declared policy ranges drifted"
        )
    workspace.pins, workspace.refs, workspace.duplicates = [], {}, []
    workspace.discover()
    workspace.apply_held()
    # Governed ranges are contractual strings, not caret/tilde update templates.
    # The resolver still receives exact chosen versions and the final lock is
    # checked against the restored policy strings.
    for pin in workspace.pins:
        if (
            (pin.catalog and pin.alias in catalogs.get(pin.catalog, {}))
            or any(pin.alias in packages.get(user, {}) for user in pin.users)
            or ("overrides" in pin.pointer and pin.pointer[-1] in overrides)
        ):
            pin.operator = None


def plan(workspace, policy, now, *, before=None):
    options = policy.get("javascript", {})
    mode = workspace.spec.get("mode", options.get("mode", "aggressive"))
    if mode not in ("aggressive", "compatible"):
        raise ValueError("JavaScript update mode must be aggressive or compatible")
    if mode == "compatible" and before is None:
        before = snapshot(workspace.root, workspace.spec)
    evidence = Evidence(policy, now)
    baseline = locked_identities(workspace)
    evidence.baseline = baseline
    for pin in workspace.pins:
        pin.ranges = compatibility(pin, options)
        if workspace.spec.get("retained_sources"):
            import javascript_sources

            for item in javascript_sources.declarations(workspace.spec):
                if (
                    "parent" in item
                    and item["manifest"] in pin.users
                    and pin.name == item["parent"].rpartition("@")[0]
                ):
                    pin.ranges.append(item["parent"].rpartition("@")[2])
        if pin.operator is None or "overrides" in pin.pointer:
            pin.ranges.append(pin.requirement)
        if pin.held:
            pin.ranges.append(pin.requirement)
        elif mode == "compatible":
            pin.ranges.append(compatible_scope(pin, before, spec=workspace.spec))
        floor_match = re.match(
            r"(?:[~^]|>=?)?([0-9]+)(?:\.([0-9]+))?(?:\.([0-9]+))?", pin.requirement
        )
        floor = (
            Version(".".join(x or "0" for x in floor_match.groups()))
            if floor_match
            else None
        )
        for selector in workspace.patches:
            match = re.fullmatch(rf"({NAME})@(.+)", selector)
            if match and match[1] == pin.name and floor and floor in NpmSpec(match[2]):
                pin.ranges.append(match[2])
        releases = evidence.get(pin.name)[0]
        active_policy = scoped_policy(policy, pin.name, pin.ranges)
        eligible = registry.maturity(
            "npm", releases, active_policy, pin.name, now
        ) + registry.active_exceptions("npm", releases, active_policy, pin.name, now)
        # A peer constraint can make a globally mature alternative unusable.
        # Keep exact declared exception candidates until the selected peer graph
        # supplies the final scope; the solver rechecks retirement and expiry.
        eligible += [
            r
            for r in releases
            if not r.deprecated
            and any(
                e.get("package") == "npm:" + pin.name and e.get("version") == r.version
                for e in policy.get("exceptions", [])
            )
        ]
        baseline_versions = {
            i[2] for i in baseline if i[0] == "npm" and i[1] == pin.name
        }
        # Deprecation removes releases from new selection, not the evidence for
        # an already locked artifact. Final audit still binds the exact tuple.
        eligible += [
            r
            for r in releases
            if r.deprecated
            and Version(r.version) in NpmSpec(pin.requirement)
            and registry.compatible(
                "npm", r.version, registry.constraint("npm", policy, pin.name)
            )
            and any(
                i[:3] == ("npm", pin.name, r.version)
                and any(
                    a.digest == i[4] and (not i[3] or a.url == i[3])
                    for a in r.artifacts
                )
                for i in baseline
            )
        ]
        eligible += [
            r
            for r in releases
            if r.version in baseline_versions
            and registry.version("npm", r.version) is None
            and Version(r.version) in NpmSpec(pin.requirement)
            and registry.compatible(
                "npm", r.version, registry.constraint("npm", policy, pin.name)
            )
        ]
        # An already locked immutable artifact can be retained when the mature
        # selection is older. It receives no exemption from identity or range
        # auditing, and no new young artifact can inherit this allowance.
        if floor and any(i[1:3] == (pin.name, str(floor)) for i in baseline):
            bound = registry.constraint("npm", policy, pin.name)
            if registry.compatible("npm", str(floor), bound):
                eligible += [
                    r
                    for r in releases
                    if r.version == str(floor)
                    and all(
                        Version(candidate.version) <= floor for candidate in eligible
                    )
                ]
        pin.candidates = sorted(
            {
                r.version
                for r in eligible
                if all(Version(r.version) in NpmSpec(bound) for bound in pin.ranges)
                and (
                    registry.minimum_safe("npm", policy, pin.name) is None
                    or Version(r.version)
                    >= registry.minimum_safe("npm", policy, pin.name)
                )
            },
            key=Version,
            reverse=True,
        )
        if not pin.candidates:
            raise ValueError(
                f"No eligible release satisfies JavaScript compatibility for {pin.name}"
            )
    return evidence, solve(workspace, evidence, options)


def solve(workspace, evidence, options, initial=None):
    ceiling = bounded(options, "solver_states", 256, 4096)
    initial = initial or tuple(pin.candidates[0] for pin in workspace.pins)
    visited: set[tuple[str, ...]] = set()
    metadata_error = None
    # A baseline is fixed for this solve. Index availability once; the final
    # artifact audit separately binds every selected URL and digest.
    baseline_versions = {
        identity[:3] for identity in getattr(evidence, "baseline", set())
    }

    def revisions(selected, conflict, witness):
        order = tuple(reversed(conflict))
        if witness:
            _, source, target, peer, requirement = witness
            incoming = []
            for manifest, refs in workspace.refs.items():
                for other in refs.values():
                    pin = workspace.pins[other]
                    info = evidence.get(pin.name)[1].get(selected[other])
                    peers = (
                        info.get("peerDependencies", {})
                        if isinstance(info, Mapping)
                        else {}
                    )
                    if not isinstance(peers, Mapping):
                        continue
                    for name, bound in peers.items():
                        if refs.get(name) == target and not peer_ignored(
                            options, manifest, pin.name, name
                        ):
                            try:
                                incoming.append(NpmSpec(peer_range(bound)))
                            except (TypeError, ValueError):
                                pass
            # Conflicting current sources cannot be repaired by a target-only
            # change. Prefer a source repair before revisiting target versions.
            if not any(
                all(Version(value) in bound for bound in incoming)
                for value in workspace.pins[target].candidates
            ):
                order = source, target
            del incoming
        # Prefer changes that repair this exact witness. Keep other participant
        # changes as fallbacks: some solutions need both endpoints to change.
        for direct in (True, False) if witness else (False,):
            for index in order:
                for candidate in workspace.pins[index].candidates:
                    if candidate == selected[index]:
                        continue
                    revised_values = list(selected)
                    revised_values[index] = candidate
                    revised = tuple(revised_values)
                    if revised in visited:
                        continue
                    if direct:
                        if index == target:
                            if Version(candidate) not in NpmSpec(requirement):
                                continue
                        else:
                            # This is ordering evidence only. Validate all peer
                            # metadata when the candidate is actually visited;
                            # unused ancient releases cannot poison the search.
                            info = evidence.get(workspace.pins[index].name)[1].get(
                                candidate
                            )
                            peers = (
                                info.get("peerDependencies", {})
                                if isinstance(info, Mapping)
                                else None
                            )
                            if not isinstance(peers, Mapping):
                                continue
                            bound = peers.get(peer)
                            if bound is not None:
                                try:
                                    if Version(selected[target]) not in NpmSpec(
                                        peer_range(bound)
                                    ):
                                        continue
                                except (TypeError, ValueError):
                                    continue
                    yield revised

    # Lazy depth-first expansion spends the bound on visited states, never on
    # hundreds of queued siblings that prevent a promising repair from advancing.
    frontier = [iter([tuple(initial)])]
    while frontier:
        try:
            selected = next(frontier[-1])
        except StopIteration:
            frontier.pop()
            continue
        if selected in visited:
            continue
        if len(visited) >= ceiling:
            raise ValueError(
                "JavaScript peer solver exhausted its explicit state bound"
                + (f": {metadata_error}" if metadata_error else "")
            )
        visited.add(selected)
        witness = None
        conflict = next(
            (
                pair
                for pair in workspace.duplicates
                if selected[pair[0]] != selected[pair[1]]
            ),
            None,
        )
        for manifest, refs in workspace.refs.items():
            if conflict:
                break
            for source in refs.values():
                pin = workspace.pins[source]
                try:
                    peers, peer_metadata = evidence.peers(
                        pin.name, selected[source], manifest=manifest
                    )
                except ValueError as error:
                    # Reject this release; a valid older source may still solve
                    # the graph. The final selection never skips peer validation.
                    if metadata_error is None:
                        metadata_error = (
                            f"Invalid peers for {pin.name}@{selected[source]}: {error}"
                        )
                    conflict = (source,)
                    break
                for peer, requirement in peers.items():
                    if peer_ignored(options, manifest, pin.name, peer):
                        continue
                    if peer not in refs:
                        if (
                            peer_metadata.get(peer, {}).get("optional") is True
                            or peer in workspace.locals
                        ):
                            continue
                        releases = evidence.get(peer)[0]
                        eligible = registry.maturity(
                            "npm", releases, evidence.policy, peer, evidence.now
                        ) + registry.active_exceptions(
                            "npm", releases, evidence.policy, peer, evidence.now
                        )
                        eligible += [
                            r
                            for r in releases
                            if ("npm", peer, r.version) in baseline_versions
                        ]
                        if not any(
                            Version(r.version) in NpmSpec(requirement) for r in eligible
                        ):
                            conflict = (source,)
                            break
                        continue
                    target = refs[peer]
                    if Version(selected[target]) not in NpmSpec(requirement):
                        conflict = source, target
                        witness = manifest, source, target, peer, requirement
                        break
                if conflict:
                    break
            if conflict:
                break
        if conflict is None:
            for index, pin in enumerate(workspace.pins):
                if not any(
                    e.get("package") == "npm:" + pin.name
                    for e in evidence.policy.get("exceptions", [])
                ):
                    continue
                scoped = selected_policy(workspace, pin, selected, evidence, options)
                releases = evidence.get(pin.name)[0]
                allowed = registry.maturity(
                    "npm", releases, scoped, pin.name, evidence.now
                ) + registry.active_exceptions(
                    "npm", releases, scoped, pin.name, evidence.now
                )
                if (
                    selected[index] not in {r.version for r in allowed}
                    and ("npm", pin.name, selected[index]) not in baseline_versions
                ):
                    conflict = (index,)
                    break
            if conflict is None:
                return selected
        # Full compatibility, maturity and exception checks above remain the
        # acceptance oracle; revision ordering cannot authorize a selection.
        frontier.append(revisions(selected, conflict, witness))
    raise ValueError(
        "No eligible JavaScript versions satisfy the scoped peer constraints"
        + (f": {metadata_error}" if metadata_error else "")
    )


def lock_spec(spec):
    return {**spec, "ecosystem": "npm"}


def locked_identities(workspace):
    if workspace.manager == "npm":
        import javascript_npm

        return javascript_npm.identities(workspace)
    return updates.lock_identities(
        workspace.root, ["javascript"], specs={"javascript": lock_spec(workspace.spec)}
    )


def baseline_maturity_exclusions(before, evidence):
    return sorted(
        {
            f"{name}@{version}"
            for provider, name, version, *_ in before["identities"]
            if provider == "npm"
            and any(
                r.version == version
                and (evidence.now - r.published).total_seconds()
                < registry.minimum_age(evidence.policy) * 86400
                for r in evidence.get(name)[0]
            )
        }
    )


def snapshot(root: Path, spec: dict) -> dict:
    workspace = Workspace(root, spec)
    identities = locked_identities(workspace)
    return {
        "schema": 1,
        "identities": [list(identity) for identity in sorted(identities)],
        "patches": {
            selector: {
                "path": path,
                "sha256": hashlib.sha256(workspace.original[path]).hexdigest(),
            }
            for selector, path in sorted(workspace.patches.items())
        },
        "requirements": [
            {
                "file": p.file,
                "pointer": list(p.pointer),
                "name": p.name,
                "requirement": p.requirement,
            }
            for p in workspace.pins
        ],
    }


def has_local_resolution(lock: inputs.PnpmLock) -> bool:
    local = False
    for item in lock["packages"].values():
        resolution = item["resolution"]
        if "directory" in resolution or resolution.get("type") == "directory":
            if (
                set(resolution) != {"directory", "type"}
                or resolution["type"] != "directory"
            ):
                raise ValueError("Unrecognized npm lock resolution source")
            local = True
    if local:
        return True
    for importer in lock["importers"].values():
        for section in SECTIONS:
            for edge in importer.get(section, {}).values():
                if edge["version"].startswith(("file:", "link:", "workspace:")):
                    return True
    for snapshot in lock["snapshots"].values():
        for section in SECTIONS:
            for raw in snapshot.get(section, {}).values():
                if raw.startswith(("file:", "link:", "workspace:")):
                    return True
    return False


def local_registry_entries(
    workspace: Workspace,
    lock: inputs.PnpmLock,
    entries: dict[str, inputs.PnpmPackage],
) -> dict[str, inputs.PnpmPackage]:
    """Local directory packages have no registry identity; validate their binding."""
    remaining = dict(entries)
    local_keys = {}
    for key, item in entries.items():
        resolution = item.get("resolution", {})
        if "directory" not in resolution and resolution.get("type") != "directory":
            continue
        if (
            set(resolution) != {"directory", "type"}
            or resolution["type"] != "directory"
        ):
            raise ValueError(
                "Local pnpm resolution must contain only its directory and type"
            )
        alias, _, raw = key.partition("(")[0].rpartition("@file:")
        if not alias or raw != resolution["directory"]:
            raise ValueError("Local pnpm resolution disagrees with its package key")
        manifest = workspace.local_manifest(alias, "file:" + raw)
        if item.get(
            "version", workspace.documents[manifest][0].get("version")
        ) != workspace.documents[manifest][0].get("version"):
            raise ValueError("Local pnpm package version disagrees with its manifest")
        local_keys[alias + "@file:" + raw] = manifest
        del remaining[key]
    for importer, info in lock["importers"].items():
        manifest = "package.json" if importer == "." else importer + "/package.json"
        if manifest not in workspace.manifests:
            raise ValueError("Lockfile contains an undeclared workspace importer")
        for section in SECTIONS:
            for alias, edge in info.get(section, {}).items():
                raw = edge["version"]
                if raw.startswith("link:"):
                    target = workspace.local_manifest(alias, raw, importer)
                elif raw.startswith("file:"):
                    target = workspace.local_manifest(alias, raw.partition("(")[0])
                    if alias + "@" + raw.partition("(")[0] not in local_keys:
                        raise ValueError(
                            "Local pnpm importer lacks its directory package"
                        )
                else:
                    continue
                declared = workspace.documents[manifest][0].get(section, {}).get(alias)
                if (
                    not isinstance(declared, str)
                    or not declared.startswith(("workspace:", "file:", "link:"))
                    or workspace.local_manifest(alias, declared, importer) != target
                ):
                    raise ValueError(
                        "Local pnpm importer differs from its manifest declaration"
                    )
    for context, node in lock["snapshots"].items():
        for section in SECTIONS:
            for alias, raw in node.get(section, {}).items():
                if raw.startswith("link:"):
                    workspace.local_manifest(alias, raw)
                elif raw.startswith("file:"):
                    workspace.local_manifest(alias, raw.partition("(")[0])
                    if alias + "@" + raw.partition("(")[0] not in local_keys:
                        raise ValueError(
                            "Local pnpm snapshot lacks its directory package"
                        )
        if "@file:" in context:
            if context.partition("(")[0] not in local_keys:
                raise ValueError("Undeclared local pnpm snapshot")
    if any(key not in lock["snapshots"] for key in local_keys):
        raise ValueError("Local pnpm package lacks its snapshot")
    return remaining


type LockTarget = tuple[str, str, str]


def lock_target(
    alias: str, raw: object, workspace: Workspace | None = None
) -> LockTarget | None:
    if not isinstance(raw, str):
        raise ValueError("pnpm lock dependency resolution must be a string")
    if raw.startswith(("link:", "workspace:")):
        return None
    if raw.startswith("https://codeload.github.com/") and workspace is not None:
        context = alias + "@" + raw
        source = getattr(workspace, "source_contents", {}).get(context)
        if source is None:
            raise ValueError(
                "Undeclared or unaudited retained source dependency context"
            )
        return alias, source["version"], context
    if raw.startswith("file:"):
        if workspace is None:
            raise ValueError(
                "Local pnpm dependency requires a declared workspace context"
            )
        manifest = workspace.local_manifest(alias, raw.partition("(")[0])
        version = workspace.documents[manifest][0].get("version")
        if (
            not isinstance(version, str)
            or registry.lock_version("npm", version) is None
        ):
            raise ValueError("Local pnpm package requires a valid declared version")
        return alias, version, alias + "@" + raw
    base = raw.partition("(")[0]
    if registry.lock_version("npm", base) is not None:
        return alias, base, f"{alias}@{raw}"
    name, separator, version = base.rpartition("@")
    if separator and registry.lock_version("npm", version) is not None:
        package_name(name)
        return name, version, raw
    raise ValueError("Unrecognized pnpm dependency context")


def effective_requirements(workspace, pin, *, parent=None, versions=None):
    """A declared override can supersede a direct range, including an npm alias."""
    allowed = [(pin.name, pin.requirement)]
    overrides = {
        **workspace.documents["package.json"][0].get("pnpm", {}).get("overrides", {}),
        **workspace.settings.get("overrides", {}),
    }
    # pnpm gives a matching parent-specific override precedence over generic
    # overrides, regardless of their declaration order.
    for selector, replacement in sorted(
        overrides.items(),
        key=lambda item: bool(re.search(r">(?=@?[A-Za-z_~])", item[0])),
    ):
        if re.search(r">(?=@?[A-Za-z_~])", selector):
            scope, selector = re.split(r">(?=@?[A-Za-z_~])", selector, maxsplit=1)
            if parent is None:
                continue
            parent_match = re.fullmatch(rf"({NAME})(?:@(.+))?", scope)
            if (
                not parent_match
                or parent_match[1] != parent[0]
                or (
                    parent_match[2]
                    and Version(parent[1]) not in NpmSpec(parent_match[2])
                )
            ):
                continue
        match = re.fullmatch(rf"({NAME})(?:@(.+))?", selector)
        if not match or match[1] not in (pin.alias, pin.name):
            continue
        if parent is not None and match[2]:
            inventory = versions() if callable(versions) else (versions or [])
            if not any(
                registry.lock_version("npm", value) is not None
                and Version(value) in NpmSpec(pin.requirement)
                and Version(value) in NpmSpec(match[2])
                for value in inventory
            ):
                continue
        if replacement.startswith("$"):
            name = replacement[1:]
            index = workspace.refs["package.json"].get(name)
            if index is None:
                raise ValueError("Override references a missing root dependency")
            reference = workspace.pins[index]
            if parent is not None:
                allowed = []
            allowed.append((reference.name, reference.requirement))
        elif replacement == "-":
            if parent is not None:
                allowed = []
        else:
            parsed = parse_requirement(pin.alias, replacement)
            if parsed:
                if parent is not None:
                    allowed = []
                allowed.append((parsed[0], parsed[2]))
    return allowed


def audit_registry_children(workspace, evidence, parent, children):
    """pnpm's frozen lock check does not verify registry dependency ranges."""
    info = evidence.get(parent[0])[1][parent[1]]
    required = info.get("dependencies", {})
    optional = info.get("optionalDependencies", {})
    if not isinstance(required, Mapping) or not isinstance(optional, Mapping):
        raise ValueError("Registry dependencies must be objects of named ranges")
    bundled = info.get("bundleDependencies", info.get("bundledDependencies", []))
    if type(bundled) is bool:
        bundled = list(required) + list(optional) if bundled else []
    if not isinstance(bundled, list) or any(
        not isinstance(name, str) for name in bundled
    ):
        raise ValueError("Bundled dependencies must be names or a boolean")
    for alias, requirement in {**required, **optional}.items():
        package_name(alias)
        target = children.get(alias)
        # A bundled child is part of its parent's hashed archive, not a separate
        # remote artifact or registry edge in pnpm's lock.
        if alias not in children and (alias in optional or alias in bundled):
            continue
        # Retained source bindings have their own exact parent, declaration,
        # override and archive checks, completed before this graph traversal.
        if target and target[2] in workspace.source_contents:
            continue
        if target is None and alias in children and alias in workspace.locals:
            # Deep workspace links have already been bound to their contained
            # manifests. Their declared versions still have to satisfy the edge.
            local_version = workspace.documents[workspace.locals[alias]][0].get(
                "version"
            )
            if registry.lock_version("npm", local_version) is not None:
                target = (alias, local_version, "")
        if not isinstance(requirement, str):
            raise ValueError("Registry dependency requirements must be strings")
        try:
            parsed = parse_requirement(alias, requirement)
        except ValueError:
            parsed = None
        # A declared override may replace or remove an upstream source that we
        # would not otherwise resolve. An unsuperseded value still has to pass
        # the registry identity/range check below.
        name, prefix, bound, operator = parsed or (alias, "", requirement, None)
        pin = Pin("", (), alias, name, requirement, prefix, bound, operator)
        allowed = effective_requirements(
            workspace, pin, parent=parent, versions=lambda: evidence.get(name)[1]
        )
        if alias not in children and not allowed:
            continue
        if target is None or not any(
            target[0] == actual and Version(target[1]) in NpmSpec(bound)
            for actual, bound in allowed
        ):
            raise ValueError(
                f"Registry dependency {parent[0]}>{alias} violates its declared range or override"
            )


def audit_peers(
    workspace: Workspace, evidence: Evidence, options: dict
) -> dict[tuple[str, str], list[str]]:
    import javascript_sources

    lock = inputs.pnpm_lock(
        document(
            Path(workspace.lock),
            tc.regular_input(workspace.directory, workspace.lock).decode(),
        )[0]
    )
    packages, snapshots = lock["packages"], lock["snapshots"]
    scopes: dict[tuple[str, str], list[str]] = {}
    for importer, info in lock["importers"].items():
        manifest = "package.json" if importer == "." else importer + "/package.json"
        if manifest not in workspace.manifests:
            raise ValueError("Lockfile contains an undeclared workspace importer")
        roots: dict[str, LockTarget | None] = {}
        for section in SECTIONS:
            for alias, value in info.get(section, {}).items():
                raw = value["version"]
                roots[alias] = (
                    None
                    if javascript_sources.is_target(
                        workspace.spec, manifest, alias, raw
                    )
                    else lock_target(alias, raw, workspace)
                )
        queue, visited = [t for t in roots.values() if t], set()
        while queue:
            actual, version, context = queue.pop()
            if context in visited:
                continue
            visited.add(context)
            if len(visited) > 100000:
                raise ValueError(
                    "JavaScript lock dependency graph exceeds its audit bound"
                )
            for rule in options.get("prefix_constraints", []):
                if (
                    actual.startswith(rule["prefix"])
                    and actual not in rule.get("exclude", [])
                    and Version(version) not in NpmSpec(rule_range(rule))
                ):
                    raise ValueError(
                        "Resolved transitive dependency violates JavaScript prefix compatibility"
                    )
            local = "@file:" in context
            source_content = getattr(workspace, "source_contents", {}).get(context)
            if (
                not local
                and source_content is None
                and f"{actual}@{version}" not in packages
            ) or context not in snapshots:
                raise ValueError("pnpm lock lacks the resolved package context")
            node = snapshots[context]
            dependencies = {
                **node.get("dependencies", {}),
                **node.get("optionalDependencies", {}),
            }
            children = {
                alias: lock_target(alias, raw, workspace)
                for alias, raw in dependencies.items()
            }
            if source_content is not None:
                peers, metadata = (
                    source_content.get("peerDependencies", {}),
                    source_content.get("peerDependenciesMeta", {}),
                )
                declared = {
                    **source_content.get("dependencies", {}),
                    **source_content.get("optionalDependencies", {}),
                    **peers,
                }
                if set(dependencies) - set(declared) or set(
                    source_content.get("dependencies", {})
                ) - set(dependencies):
                    raise ValueError(
                        "Retained source dependency graph differs from its archive manifest"
                    )
                for alias, target in children.items():
                    parsed = parse_requirement(alias, declared[alias])
                    if parsed is None or target is None:
                        raise ValueError(
                            "Retained source children require audited registry identities"
                        )
                    name, prefix, requirement, operator = parsed
                    pin = Pin(
                        "",
                        (),
                        alias,
                        name,
                        declared[alias],
                        prefix,
                        requirement,
                        operator,
                    )
                    if not any(
                        target[0] == name and Version(target[1]) in NpmSpec(bound)
                        for name, bound in effective_requirements(
                            workspace,
                            pin,
                            parent=(actual, version),
                            versions=evidence.get(pin.name)[1],
                        )
                    ):
                        raise ValueError(
                            "Retained source child violates its archive range or declared override"
                        )
            elif local:
                source = workspace.locals[actual]
                content = workspace.documents[source][0]
                peers, metadata = (
                    content.get("peerDependencies", {}),
                    content.get("peerDependenciesMeta", {}),
                )
                declared = {
                    **content.get("dependencies", {}),
                    **content.get("optionalDependencies", {}),
                    **peers,
                }
                if set(dependencies) - set(declared) or set(
                    content.get("dependencies", {})
                ) - set(dependencies):
                    raise ValueError(
                        "Local pnpm dependency graph differs from its manifest"
                    )
                for alias, raw in dependencies.items():
                    requirement = declared[alias]
                    if requirement.startswith(("workspace:", "file:", "link:")):
                        expected = workspace.local_manifest(
                            alias, requirement, str(Path(source).parent)
                        )
                        observed = workspace.local_manifest(
                            alias, raw.partition("(")[0]
                        )
                        if expected != observed:
                            raise ValueError(
                                "Local pnpm graph points to a different workspace"
                            )
                    else:
                        pin = workspace.pins[workspace.refs[source][alias]]
                        target = children[alias]
                        if target is None or not any(
                            target[0] == name and Version(target[1]) in NpmSpec(bound)
                            for name, bound in effective_requirements(workspace, pin)
                        ):
                            raise ValueError(
                                "Local pnpm dependency violates its manifest range"
                            )
            else:
                peers, metadata = evidence.peers(actual, version, manifest=manifest)
                audit_registry_children(
                    workspace, evidence, (actual, version), children
                )
            for peer, requirement in peers.items():
                requirement = peer_range(requirement)
                if peer_ignored(options, manifest, actual, peer):
                    continue
                target = children.get(peer)
                if target is None:
                    # Workspace links may provide a local peer whose declared
                    # package version is the relevant compatibility contract.
                    local_manifest = workspace.locals.get(peer)
                    if peer in children and local_manifest:
                        target_version = workspace.documents[local_manifest][0].get(
                            "version"
                        )
                    elif metadata.get(peer, {}).get("optional") is True:
                        continue
                    else:
                        raise ValueError(
                            f"Missing required peer {actual}>{peer} in {manifest}"
                        )
                else:
                    target_version = target[1]
                    scopes.setdefault(target[:2], []).append(requirement)
                if (
                    not isinstance(target_version, str)
                    or registry.lock_version("npm", target_version) is None
                    or Version(target_version) not in NpmSpec(requirement)
                ):
                    raise ValueError(
                        f"Incompatible resolved peer {actual}>{peer} in {manifest}"
                    )
            queue.extend(t for t in children.values() if t)
    return scopes


def audit_artifacts(workspace, before, policy, now, scopes):
    identities = locked_identities(workspace)
    old = identity_inventory(before["identities"])
    exclusions = set()
    groups: dict[tuple[str, tuple[str, ...]], set[Identity]] = {}
    for identity in identities:
        provider, name, version, *_ = identity
        bounds = list(scopes.get((name, version), [])) if provider == "npm" else []
        if provider == "npm":
            for rule in policy.get("javascript", {}).get("prefix_constraints", []):
                if name.startswith(rule["prefix"]) and name not in rule.get(
                    "exclude", []
                ):
                    bounds.append(rule_range(rule))
        key = (name if bounds else "", tuple(sorted(set(bounds))))
        groups.setdefault(key, set()).add(identity)
    for (name, group_bounds), group in groups.items():
        scoped = scoped_policy(policy, name, group_bounds) if group_bounds else policy
        updates.audit_identities(workspace.root, group, old, scoped, now)
        for identity in group:
            if identity[0] != "npm":
                continue
            for release in registry.active_exceptions(
                "npm", registry.releases("npm", identity[1]), scoped, identity[1], now
            ):
                if release.version == identity[2]:
                    exclusions.add(identity[1] + "@" + release.version)
    return sorted(exclusions)


def direct_scope(pin, spec, options, version, before):
    bounds = compatibility(pin, options)
    if pin.operator is None or pin.held:
        bounds.append(pin.requirement)
    if (
        spec.get("mode", options.get("mode", "aggressive")) == "compatible"
        and not pin.held
    ):
        bound = compatible_scope(pin, before, spec=spec, check_declaration=True)
        if Version(version) not in NpmSpec(bound):
            raise ValueError(
                "Resolved dependency escaped its original compatible range"
            )
        bounds.append(bound)
    return bounds


def audit_details(root: Path, spec: dict, before: dict, policy: dict, now: datetime):
    if before.get("schema") != 1:
        raise ValueError("Unsupported JavaScript audit baseline")
    workspace = Workspace(root, spec)
    reconcile_policy(workspace, policy, check=True)
    patches = {
        selector: {
            "path": path,
            "sha256": hashlib.sha256(workspace.original[path]).hexdigest(),
        }
        for selector, path in sorted(workspace.patches.items())
    }
    if patches != before.get("patches", {}):
        raise ValueError(
            "JavaScript updates must not silently alter version-bound patches"
        )
    if workspace.manager == "npm":
        import javascript_npm

        return javascript_npm.audit(workspace, before, policy, now)
    import javascript_sources

    lock = inputs.pnpm_lock(
        document(Path(workspace.lock), workspace.original[workspace.lock].decode())[0]
    )
    workspace.source_contents = javascript_sources.audit(workspace, before, policy, now)
    evidence = Evidence(policy, now)
    options = policy.get("javascript", {})
    if spec.get("mode", options.get("mode", "aggressive")) == "compatible":
        for pin in workspace.pins:
            if "overrides" in pin.pointer and not pin.held:
                compatible_scope(pin, before, spec=spec, check_declaration=True)
    scopes = audit_peers(workspace, evidence, options)
    # Recheck policy at actual locked versions; ranges/wildcards may resolve to
    # another version from the planner's preferred candidate.
    for manifest, refs in workspace.refs.items():
        importer = str(Path(manifest).parent)
        info = lock["importers"].get(importer)
        if info is None:
            raise ValueError("JavaScript lock is missing a declared workspace importer")
        for alias, index in refs.items():
            pin = workspace.pins[index]
            targets = [
                lock_target(alias, section[alias]["version"])
                for name in SECTIONS
                if alias in (section := info.get(name, {}))
            ]
            if not targets:
                raise ValueError("JavaScript lock is missing a declared dependency")
            for target in targets:
                if target is None or not any(
                    target[0] == name and Version(target[1]) in NpmSpec(bound)
                    for name, bound in effective_requirements(workspace, pin)
                ):
                    raise ValueError(
                        "Locked dependency disagrees with its manifest or override"
                    )
                if any(
                    Version(target[1]) not in NpmSpec(bound)
                    for bound in compatibility(pin, options)
                ):
                    raise ValueError(
                        "Resolved dependency violates scoped JavaScript compatibility"
                    )
                scopes.setdefault(target[:2], []).extend(
                    direct_scope(pin, spec, options, target[1], before)
                )
    return audit_artifacts(workspace, before, policy, now, scopes)


def audit(root: Path, spec: dict, before: dict, policy: dict, now: datetime) -> None:
    audit_details(root, spec, before, policy, now)


def fallback(workspace, selected, message, temporary):
    if not re.search(r"minimumReleaseAge|minimum-release-age", message):
        return None
    parent = re.search(
        rf"dependencies of ({NAME})@([0-9]+\.[0-9]+\.[0-9]+)(?:\s|$)", message
    )
    candidates = []
    if parent:
        candidates = [
            i
            for i, pin in enumerate(workspace.pins)
            if (pin.name, selected[i]) == parent.groups()
        ]
    else:
        direct = re.search(r"direct dependency of ([^\r\n]+)", message)
        blocked = re.search(
            rf"Version [^\r\n]+ of ({NAME}) does not meet the minimumReleaseAge constraint",
            message,
        )
        if direct and blocked:
            path = Path(direct[1].strip())
            try:
                importer = path.relative_to(temporary).as_posix()
            except ValueError:
                return None
            manifest = "package.json" if importer == "." else importer + "/package.json"
            index = workspace.refs.get(manifest, {}).get(blocked[1])
            if index is not None:
                candidates = [index]
    # Ambiguous attribution must not downgrade unrelated packages or workspaces.
    if len(candidates) != 1:
        return None
    index = candidates[0]
    current = selected[index]
    older = [
        v for v in workspace.pins[index].candidates if Version(v) < Version(current)
    ]
    if not older:
        return None
    workspace.pins[index].candidates = older
    revised = list(selected)
    revised[index] = older[0]
    return tuple(revised)


def normalization_graph(lock):
    """Retain every graph field while excluding native declaration metadata."""
    graph = deepcopy(lock)
    graph.pop("overrides", None)
    for importer in graph.get("importers", {}).values():
        for section in SECTIONS:
            for entry in importer.get(section, {}).values():
                if isinstance(entry, MutableMapping):
                    entry.pop("specifier", None)
    for catalog in graph.get("catalogs", {}).values():
        for entry in catalog.values():
            if isinstance(entry, MutableMapping):
                entry.pop("specifier", None)
    return graph


def resolve(root: Path, spec: dict, policy: dict, now: datetime) -> dict:
    workspace = Workspace(root, spec)
    before = snapshot(root, spec)
    reconcile_policy(workspace, policy)
    evidence, selected = plan(workspace, policy, now, before=before)
    if workspace.manager == "npm":
        import javascript_npm

        return javascript_npm.resolve(
            workspace, before, evidence, selected, policy, now
        )
    options = policy.get("javascript", {})
    attempts = bounded(options, "fallback_attempts", 20, 100)
    if not workspace.settings:
        # pnpm's release policy belongs in a workspace file even for one package.
        workspace.documents[workspace.workspace] = document(
            Path(workspace.workspace), "{}\n"
        )
        workspace.settings = workspace.documents[workspace.workspace][0]
    workspace.settings["minimumReleaseAge"] = registry.minimum_age(policy) * 1440
    workspace.settings["minimumReleaseAgeIgnoreMissingTime"] = False
    excludes: list[str] = []
    for exception in policy.get("exceptions", []):
        provider, _, name = exception.get("package", "").partition(":")
        if provider == "npm":
            excludes.extend(
                f"{name}@{r.version}"
                for r in registry.active_exceptions(
                    "npm", evidence.get(name)[0], policy, name, now
                )
            )
            excludes.append(f"{name}@{exception['version']}")
    workspace.settings["minimumReleaseAgeExclude"] = sorted(set(excludes))
    baseline_excludes = baseline_maturity_exclusions(before, evidence)
    parent = tc.contained(root, ".cache/toolchain/work")
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="javascript-update-", dir=parent
    ) as temporary_name:
        temporary = Path(temporary_name)
        for _attempt in range(attempts):
            workspace.settings["minimumReleaseAgeExclude"] = sorted(
                set(excludes + baseline_excludes)
            )
            content = workspace.render(selected, resolver_pins=True)
            for relative, data in content.items():
                if relative != workspace.lock:
                    tc.atomic_bytes(
                        tc.contained(temporary, relative),
                        data,
                        workspace.modes.get(relative, 0o644),
                    )
            tc.contained(temporary, workspace.lock).unlink(missing_ok=True)
            result = chainman.execute(
                root,
                spec.get("profile", "javascript"),
                [
                    "pnpm",
                    "install",
                    "--lockfile-only",
                    "--ignore-scripts",
                    "--strict-peer-dependencies=false",
                ],
                cwd=temporary,
                env=tc.environment(root),
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if result.returncode == 0:
                break
            revised = fallback(
                workspace, selected, result.stdout + "\n" + result.stderr, temporary
            )
            if revised is None:
                codes = sorted(
                    set(
                        re.findall(
                            r"ERR_PNPM_[A-Z_]+", result.stderr + "\n" + result.stdout
                        )
                    )
                )
                raise ValueError(
                    f"pnpm resolution failed (exit {result.returncode}; {', '.join(codes) or 'unclassified error'}) without a narrowly attributable maturity fallback"
                )
            selected = solve(workspace, evidence, options, revised)
        else:
            raise ValueError(
                "pnpm resolution exhausted its explicit maturity-fallback bound"
            )
        for relative, planned in content.items():
            if (
                relative != workspace.lock
                and tc.regular_input(temporary, relative) != planned
            ):
                raise ValueError(
                    "pnpm changed a declared resolver input outside the planned dependency edits"
                )
        # Normalization remains resolution: retain the exact baseline allowances
        # until the full artifact audit determines the final exclusion set.
        workspace.settings["minimumReleaseAgeExclude"] = sorted(
            set(excludes + baseline_excludes)
        )
        content = workspace.render(selected)
        for relative, data in content.items():
            if relative != workspace.lock:
                tc.atomic_bytes(
                    tc.contained(temporary, relative),
                    data,
                    workspace.modes.get(relative, 0o644),
                )
        import javascript_sources

        # Native override and catalog semantics determine the effective importer
        # specifiers. Normalize restored ranges during resolution, before freeze;
        # neither a new identity nor an edge change may escape this step.
        lock_path = tc.contained(temporary, workspace.lock)
        resolved_lock = document(
            lock_path, tc.regular_input(temporary, workspace.lock).decode()
        )[0]
        inputs.pnpm_lock(resolved_lock)
        normalized = chainman.execute(
            root,
            spec.get("profile", "javascript"),
            [
                "pnpm",
                "install",
                "--lockfile-only",
                "--ignore-scripts",
                "--strict-peer-dependencies=false",
                "--no-frozen-lockfile",
            ],
            cwd=temporary,
            env=tc.environment(root),
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if normalized.returncode:
            raise ValueError("pnpm lock normalization rejected restored declarations")
        normalized_lock = document(
            lock_path, tc.regular_input(temporary, workspace.lock).decode()
        )[0]
        inputs.pnpm_lock(normalized_lock)
        if normalization_graph(resolved_lock) != normalization_graph(normalized_lock):
            raise ValueError(
                "pnpm lock normalization changed the selected dependency graph"
            )
        for relative, planned in content.items():
            if (
                relative != workspace.lock
                and tc.regular_input(temporary, relative) != planned
            ):
                raise ValueError(
                    "pnpm lock normalization changed a declared resolver input"
                )
        javascript_sources.bind(workspace, temporary)
        workspace.settings["minimumReleaseAgeExclude"] = audit_details(
            temporary, {**spec, "directory": "."}, before, policy, now
        )
        content = workspace.render(selected)
        for relative, data in content.items():
            if relative != workspace.lock:
                tc.atomic_bytes(
                    tc.contained(temporary, relative),
                    data,
                    workspace.modes.get(relative, 0o644),
                )
        checked = chainman.execute(
            root,
            spec.get("profile", "javascript"),
            [
                "pnpm",
                "install",
                "--lockfile-only",
                "--ignore-scripts",
                "--strict-peer-dependencies=false",
                "--frozen-lockfile",
            ],
            cwd=temporary,
            env=tc.environment(root),
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if checked.returncode:
            raise ValueError(
                "pnpm frozen verification rejected the restored manifest ranges"
            )
        copied_spec = {**spec, "directory": "."}
        # The disposable source root is used only for inspection. Actual commands
        # always enter the project's declared, pinned Nix profile above.
        audit(temporary, copied_spec, before, policy, now)
        for relative, planned in content.items():
            if (
                relative != workspace.lock
                and tc.regular_input(temporary, relative) != planned
            ):
                raise ValueError(
                    "pnpm changed a declared resolver input outside the planned dependency edits"
                )
        final = {
            relative: tc.regular_input(temporary, relative)
            for relative in content
            if relative != workspace.lock
        }
        final[workspace.lock] = tc.regular_input(temporary, workspace.lock)
        if not tc.contained(workspace.directory, workspace.workspace).exists():
            final[workspace.workspace] = tc.regular_input(
                temporary, workspace.workspace
            )
        for relative, original in workspace.original.items():
            if tc.regular_input(workspace.directory, relative) != original:
                raise ValueError(
                    "JavaScript resolver inputs changed concurrently; refusing to install its plan"
                )
        for relative in final.keys() - workspace.original.keys():
            if tc.contained(workspace.directory, relative).exists():
                raise ValueError("JavaScript resolver output appeared concurrently")
        changed = []
        for relative, data in final.items():
            if workspace.original.get(relative) != data:
                tc.atomic_bytes(
                    tc.contained(workspace.directory, relative),
                    data,
                    workspace.modes.get(relative, 0o644),
                )
                changed.append(
                    (workspace.directory / relative).relative_to(root).as_posix()
                )
    return {
        "changed_files": sorted(changed),
        "selected": {
            f"{p.file}:{'/'.join(p.pointer)}": v
            for p, v in zip(workspace.pins, selected, strict=True)
        },
        "resolution_attempts": _attempt + 1,
    }
