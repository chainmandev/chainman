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
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import chainman
import registry
import toolchain as tc
import updates
from ruamel.yaml import YAML
from semantic_version import NpmSpec, Version

SECTIONS = (
    "dependencies",
    "devDependencies",
    "optionalDependencies",
    "peerDependencies",
)
NAME = r"(?:@[a-z0-9_.~-]+/)?[a-z0-9_.~-]+"


def package_name(value):
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


def parse_requirement(alias, value):
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
    NpmSpec(value)
    simple = re.fullmatch(r"([~^]?)([0-9]+\.[0-9]+\.[0-9]+)", value)
    # Complex declared ranges are contracts, not templates to rewrite loosely.
    return actual, prefix, value, simple.group(1) if simple else None


def document(path, body):
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
    pointer: tuple
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

    def replacement(self, selected):
        if self.operator is None:
            # Preserve unions, bounded ranges and wildcards. The lock and the
            # post-resolution audit still enforce their selected compatibility.
            return self.original
        return self.prefix + self.operator + selected


class Workspace:
    def __init__(self, root, spec):
        self.root, self.spec = root, spec
        self.directory = tc.contained(root, spec.get("directory", "."))
        self.manager = spec.get("manager", "pnpm")
        if self.manager not in ("npm", "pnpm"):
            raise ValueError("JavaScript manager must be npm or pnpm")
        self.workspace = spec.get("workspace", "pnpm-workspace.yaml")
        self.lock = "package-lock.json" if self.manager == "npm" else "pnpm-lock.yaml"
        self.documents, self.original, self.modes = {}, {}, {}
        self.pins, self.refs, self.locals = [], {}, {}
        self.duplicates = []
        self.patches = {}
        root_manifest = self.load("package.json")
        workspace_path = tc.contained(self.directory, self.workspace)
        self.settings = (
            self.load(self.workspace)
            if self.manager == "pnpm" and workspace_path.exists()
            else {}
        )
        patterns = spec.get("manifests")
        if patterns is None:
            workspaces = self.settings.get(
                "packages", root_manifest.get("workspaces", [])
            )
            if isinstance(workspaces, Mapping):
                workspaces = workspaces.get("packages", [])
            patterns = ["package.json", *[p + "/package.json" for p in workspaces]]
        if not isinstance(patterns, list) or not all(
            isinstance(p, str) for p in patterns
        ):
            raise ValueError("JavaScript manifests must be a list of relative patterns")
        included, excluded = {"package.json"}, set()
        for pattern in patterns:
            negative = pattern.startswith("!")
            pattern = pattern.removeprefix("!")
            tc.contained(self.directory, pattern)
            found = {
                p.relative_to(self.directory).as_posix()
                for p in self.directory.glob(pattern)
            }
            (excluded if negative else included).update(found)
        self.manifests = sorted(included - excluded)
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
        for name, content in list(self.documents.items()):
            value = content[0]
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
            for path in self.directory.glob(pattern):
                if path.is_dir():
                    continue
                self.keep(path.relative_to(self.directory).as_posix())
        lock = tc.contained(self.directory, self.lock)
        if lock.exists():
            self.keep(self.lock)
        self.discover()
        for rule in spec.get("held_dependencies", []):
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

    def keep(self, relative):
        path = tc.contained(self.directory, relative)
        data = tc.regular_input(self.directory, relative)
        if len(data) > 16 * 1024 * 1024:
            raise ValueError("JavaScript resolver input exceeds 16 MiB")
        self.original[relative] = data
        self.modes[relative] = path.stat().st_mode & 0o777
        return data

    def load(self, relative):
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

    def add(self, file, pointer, alias, value, *, catalog=None, user=None):
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

    def discover(self):
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

    def override(self, file, pointer, selector, requirement):
        # Keep parent/version selectors intact while updating their replacement.
        target = selector.rsplit(">", 1)[-1].strip()
        match = re.fullmatch(rf"({NAME})(?:@(.+))?", target)
        if not match:
            raise ValueError("Unsupported pnpm override selector")
        if match[2]:
            NpmSpec(match[2])
        self.add(file, pointer, match[1], requirement)

    def npm_overrides(self, file, pointer, table, parent=None):
        for selector, requirement in table.items():
            target = parent if selector == "." else selector
            match = re.fullmatch(rf"({NAME})(?:@(.+))?", target or "")
            if not match:
                raise ValueError("Unsupported npm override selector")
            if isinstance(requirement, Mapping):
                self.npm_overrides(file, (*pointer, selector), requirement, target)
            else:
                self.add(file, (*pointer, selector), match[1], requirement)

    def render(self, selected, *, resolver_pins=False):
        for pin, version in zip(self.pins, selected):
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
    def __init__(self, policy, now):
        if now.tzinfo is None:
            raise ValueError("JavaScript update time requires a timezone")
        registry.minimum_age(policy)
        self.policy, self.now, self.cache = policy, now, {}

    def get(self, name):
        package_name(name)
        if name not in self.cache:
            body = registry.data(f"https://registry.npmjs.org/{quote(name, safe='')}")
            if body.get("name") != name:
                raise ValueError("Registry package identity disagrees with its request")
            releases = registry.releases("npm", name)
            versions = body.get("versions", {})
            for release in releases:
                if release.published > self.now:
                    raise ValueError("Future JavaScript registry publication time")
                if release.version not in versions:
                    raise ValueError(
                        "Registry release metadata disagrees with its manifest"
                    )
                info = versions[release.version]
                if info.get("name") != name or info.get("version") != release.version:
                    raise ValueError("Registry version manifest identity mismatch")
            self.cache[name] = releases, versions
        return self.cache[name]

    def peers(self, name, version):
        info = self.get(name)[1].get(version)
        if not isinstance(info, Mapping):
            raise ValueError("Selected package lacks registry dependency metadata")
        peers = info.get("peerDependencies", {})
        for peer, value in peers.items():
            package_name(peer)
            NpmSpec(value)
        return peers, info.get("peerDependenciesMeta", {})


def peer_ignored(options, manifest, source, peer):
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


def plan(workspace, policy, now):
    options = policy.get("javascript", {})
    mode = workspace.spec.get("mode", options.get("mode", "aggressive"))
    if mode not in ("aggressive", "compatible"):
        raise ValueError("JavaScript update mode must be aggressive or compatible")
    evidence = Evidence(policy, now)
    baseline = locked_identities(workspace)
    for pin in workspace.pins:
        pin.ranges = compatibility(pin, options)
        if pin.operator is None or "overrides" in pin.pointer:
            pin.ranges.append(pin.requirement)
        if pin.held:
            pin.ranges.append(pin.requirement)
        floor_match = re.match(
            r"(?:[~^]|>=?)?([0-9]+)(?:\.([0-9]+))?(?:\.([0-9]+))?", pin.requirement
        )
        floor = (
            Version(".".join(x or "0" for x in floor_match.groups()))
            if floor_match
            else None
        )
        if mode == "compatible":
            if floor is None:
                raise ValueError(
                    "Compatible updates require an explicit current version or lower bound"
                )
            pin.ranges.append(f">={floor.major}.0.0 <{floor.major + 1}.0.0")
        for selector in workspace.patches:
            match = re.fullmatch(rf"({NAME})@(.+)", selector)
            if match and match[1] == pin.name and floor and floor in NpmSpec(match[2]):
                pin.ranges.append(match[2])
        releases = evidence.get(pin.name)[0]
        eligible = registry.maturity(
            "npm", releases, policy, pin.name, now
        ) + registry.active_exceptions("npm", releases, policy, pin.name, now)
        # An already locked immutable artifact can be retained when the mature
        # selection is older. It receives no exemption from identity or range
        # auditing, and no new young artifact can inherit this allowance.
        if floor and any(i[1:3] == (pin.name, str(floor)) for i in baseline):
            bound = registry.constraint("npm", policy, pin.name)
            if not bound or floor in NpmSpec(bound):
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
    queue, visited, scheduled = [tuple(initial)], set(), {tuple(initial)}
    truncated = False
    while queue:
        selected = queue.pop(0)
        if selected in visited:
            continue
        visited.add(selected)
        if len(visited) > ceiling:
            raise ValueError(
                "JavaScript peer solver exhausted its explicit state bound"
            )
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
                peers, peer_metadata = evidence.peers(pin.name, selected[source])
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
                        if not any(
                            Version(r.version) in NpmSpec(requirement) for r in eligible
                        ):
                            conflict = (source,)
                            break
                        continue
                    target = refs[peer]
                    if Version(selected[target]) not in NpmSpec(requirement):
                        conflict = source, target
                        break
                if conflict:
                    break
            if conflict:
                break
        if conflict is None:
            return selected
        # Change only a participant in the witnessed conflict. Never weaken a
        # compatibility/age rule to make the package manager return success.
        for index in reversed(conflict):
            for candidate in workspace.pins[index].candidates:
                if candidate == selected[index]:
                    continue
                revised = list(selected)
                revised[index] = candidate
                revised = tuple(revised)
                if revised not in scheduled:
                    if len(scheduled) >= ceiling:
                        truncated = True
                    else:
                        scheduled.add(revised)
                        queue.append(revised)
    if truncated:
        raise ValueError("JavaScript peer solver exhausted its explicit state bound")
    raise ValueError(
        "No eligible JavaScript versions satisfy the scoped peer constraints"
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


def lock_target(alias, raw):
    if not isinstance(raw, str):
        raise ValueError("pnpm lock dependency resolution must be a string")
    if raw.startswith(("link:", "workspace:")):
        return None
    base = raw.partition("(")[0]
    if registry.version("npm", base) is not None:
        return alias, base, f"{alias}@{raw}"
    name, separator, version = base.rpartition("@")
    if separator and registry.version("npm", version) is not None:
        package_name(name)
        return name, version, raw
    raise ValueError("Unrecognized pnpm dependency context")


def effective_requirements(workspace, pin):
    """A declared override can supersede a direct range, including an npm alias."""
    allowed = [(pin.name, pin.requirement)]
    overrides = {
        **workspace.documents["package.json"][0].get("pnpm", {}).get("overrides", {}),
        **workspace.settings.get("overrides", {}),
    }
    for selector, replacement in overrides.items():
        if ">" in selector:
            continue  # Parent-scoped rules apply inside that parent's graph.
        match = re.fullmatch(rf"({NAME})(?:@(.+))?", selector)
        if not match or match[1] not in (pin.alias, pin.name):
            continue
        if replacement.startswith("$"):
            name = replacement[1:]
            index = workspace.refs["package.json"].get(name)
            if index is None:
                raise ValueError("Override references a missing root dependency")
            reference = workspace.pins[index]
            allowed.append((reference.name, reference.requirement))
        elif replacement != "-":
            parsed = parse_requirement(pin.alias, replacement)
            if parsed:
                allowed.append((parsed[0], parsed[2]))
    return allowed


def audit_peers(workspace, evidence, options):
    lock = document(
        Path(workspace.lock),
        tc.regular_input(workspace.directory, workspace.lock).decode(),
    )[0]
    packages, snapshots = lock.get("packages", {}), lock.get("snapshots", {})
    if not str(lock.get("lockfileVersion", "")).startswith("9"):
        raise ValueError("JavaScript peer audit requires pnpm lockfile version 9")
    for importer, info in lock.get("importers", {}).items():
        manifest = "package.json" if importer == "." else importer + "/package.json"
        if manifest not in workspace.manifests:
            raise ValueError("Lockfile contains an undeclared workspace importer")
        roots = {}
        for section in SECTIONS:
            for alias, value in info.get(section, {}).items():
                roots[alias] = lock_target(alias, value.get("version"))
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
            if f"{actual}@{version}" not in packages or context not in snapshots:
                raise ValueError("pnpm lock lacks the resolved package context")
            node = snapshots[context]
            dependencies = {
                **node.get("dependencies", {}),
                **node.get("optionalDependencies", {}),
            }
            children = {
                alias: lock_target(alias, raw) for alias, raw in dependencies.items()
            }
            peers, metadata = evidence.peers(actual, version)
            for peer, requirement in peers.items():
                if peer_ignored(options, manifest, actual, peer):
                    continue
                target = children.get(peer)
                if target is None:
                    # Workspace links may provide a local peer whose declared
                    # package version is the relevant compatibility contract.
                    local = workspace.locals.get(peer)
                    if peer in children and local:
                        target_version = workspace.documents[local][0].get("version")
                    elif metadata.get(peer, {}).get("optional") is True:
                        continue
                    else:
                        raise ValueError(
                            f"Missing required peer {actual}>{peer} in {manifest}"
                        )
                else:
                    target_version = target[1]
                if registry.version("npm", target_version) is None or Version(
                    target_version
                ) not in NpmSpec(requirement):
                    raise ValueError(
                        f"Incompatible resolved peer {actual}>{peer} in {manifest}"
                    )
            queue.extend(t for t in children.values() if t)


def audit(root: Path, spec: dict, before: dict, policy: dict, now: datetime) -> None:
    if before.get("schema") != 1:
        raise ValueError("Unsupported JavaScript audit baseline")
    workspace = Workspace(root, spec)
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

        javascript_npm.audit(workspace, before, policy, now)
        return
    updates.audit_locks(
        root,
        ["javascript"],
        {tuple(identity) for identity in before["identities"]},
        policy,
        now,
        specs={"javascript": lock_spec(spec)},
    )
    evidence = Evidence(policy, now)
    options = policy.get("javascript", {})
    # Recheck policy at actual locked versions; ranges/wildcards may resolve to
    # another version from the planner's preferred candidate.
    lock = document(Path(workspace.lock), workspace.original[workspace.lock].decode())[
        0
    ]
    for manifest, refs in workspace.refs.items():
        importer = str(Path(manifest).parent)
        info = lock.get("importers", {}).get(importer)
        if not isinstance(info, Mapping):
            raise ValueError("JavaScript lock is missing a declared workspace importer")
        for alias, index in refs.items():
            pin = workspace.pins[index]
            targets = [
                lock_target(alias, section[alias].get("version"))
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
                mode = spec.get("mode", options.get("mode", "aggressive"))
                if mode == "compatible" and not pin.held:
                    previous = next(
                        (
                            p
                            for p in before.get("requirements", [])
                            if p["file"] == pin.file
                            and tuple(p["pointer"]) == pin.pointer
                            and p["name"] == pin.name
                        ),
                        None,
                    )
                    floor = (
                        re.match(r"(?:[~^]|>=?)?([0-9]+)", previous["requirement"])
                        if previous
                        else None
                    )
                    if floor is None or Version(target[1]).major != int(floor[1]):
                        raise ValueError(
                            "Resolved dependency escaped the compatible-mode major"
                        )
    audit_peers(workspace, evidence, options)


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


def restore_lock_specifiers(workspace, directory):
    """Restore declared ranges after solving with exact temporary candidates.

    Only specifier metadata changes; selected versions and artifact identities stay
    untouched. A real frozen pnpm pass subsequently validates this lock against the
    restored manifests, followed by independent age, identity and peer audits.
    """
    path = tc.contained(directory, workspace.lock)
    lock, render = document(path, tc.regular_input(directory, workspace.lock).decode())
    for manifest in workspace.manifests:
        importer = lock.get("importers", {}).get(str(Path(manifest).parent), {})
        for section in SECTIONS:
            for alias, requirement in (
                workspace.documents[manifest][0].get(section, {}).items()
            ):
                entry = importer.get(section, {}).get(alias)
                if entry is not None:
                    entry["specifier"] = requirement
    for pin in workspace.pins:
        if pin.catalog:
            entry = lock.get("catalogs", {}).get(pin.catalog, {}).get(pin.alias)
            if entry is not None:
                requirement = workspace.documents[pin.file][0]
                for component in pin.pointer:
                    requirement = requirement[component]
                entry["specifier"] = requirement
    if "overrides" in lock:
        lock["overrides"] = {
            **workspace.documents["package.json"][0]
            .get("pnpm", {})
            .get("overrides", {}),
            **workspace.settings.get("overrides", {}),
        }
    tc.atomic_bytes(path, render().encode(), 0o644)


def resolve(root: Path, spec: dict, policy: dict, now: datetime) -> dict:
    workspace = Workspace(root, spec)
    before = snapshot(root, spec)
    evidence, selected = plan(workspace, policy, now)
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
    excludes = []
    for exception in policy.get("exceptions", []):
        provider, _, name = exception.get("package", "").partition(":")
        if provider == "npm":
            excludes.extend(
                f"{name}@{r.version}"
                for r in registry.active_exceptions(
                    "npm", evidence.get(name)[0], policy, name, now
                )
            )
    workspace.settings["minimumReleaseAgeExclude"] = sorted(set(excludes))
    baseline_excludes = baseline_maturity_exclusions(before, evidence)
    parent = tc.contained(root, ".cache/toolchain/work")
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="javascript-update-", dir=parent
    ) as temporary_name:
        temporary = Path(temporary_name)
        for attempt in range(attempts):
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
        workspace.settings["minimumReleaseAgeExclude"] = sorted(set(excludes))
        content = workspace.render(selected)
        for relative, data in content.items():
            if relative != workspace.lock:
                tc.atomic_bytes(
                    tc.contained(temporary, relative),
                    data,
                    workspace.modes.get(relative, 0o644),
                )
        restore_lock_specifiers(workspace, temporary)
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
            for p, v in zip(workspace.pins, selected)
        },
        "resolution_attempts": attempt + 1,
    }
