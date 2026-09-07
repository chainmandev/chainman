"""Declared source updates with immutable identities and independently repeatable audits."""

from __future__ import annotations

import json
import re
import stat
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import chainman
import registry
import toolchain as tc
import yaml


def object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key in dependency input")
        result[key] = value
    return result


def read_json(root: Path, name: str):
    return json.loads(tc.regular_input(root, name), object_pairs_hook=object_pairs)


def repository_name(value: str) -> str:
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value)
        or any(part in (".", "..") for part in value.split("/"))
    ):
        raise ValueError("Expected an explicit public GitHub owner/repository")
    return value


def revision(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{40}", value):
        raise ValueError("Expected an immutable GitHub commit identity")
    return value


def commit_time(repository: str, commit: str) -> datetime:
    repository_name(repository)
    revision(commit)
    item = registry.data(f"https://api.github.com/repos/{repository}/commits/{commit}")
    if item.get("sha") != commit:
        raise ValueError("GitHub returned a different commit identity")
    return registry.timestamp(item["commit"]["committer"]["date"])


def cutoff(policy: dict, now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError("Update time must include a timezone")
    return now - timedelta(days=registry.minimum_age(policy))


def nix_candidate(repository: str, branch: str, policy: dict, now: datetime) -> dict:
    """Branch age is commit age; it is never presented as release publication age."""
    repository_name(repository)
    if not isinstance(branch, str) or not branch or any(c.isspace() for c in branch):
        raise ValueError("An explicit source branch is required")
    limit = cutoff(policy, now)
    query = urlencode({"sha": branch, "until": limit.isoformat(), "per_page": 1})
    entries = registry.data(
        f"https://api.github.com/repos/{repository}/commits?{query}"
    )
    if not isinstance(entries, list) or not entries:
        raise ValueError("No mature source revision with complete evidence")
    selected = revision(entries[0].get("sha"))
    published = commit_time(repository, selected)
    if published > limit:
        raise ValueError("Selected branch revision is younger than its age policy")
    return {"revision": selected, "published": published.isoformat()}


def action_version(tag: str):
    if not isinstance(tag, str) or not re.fullmatch(r"v?\d+(?:\.\d+){0,2}", tag):
        return None
    parts = tuple(int(part) for part in tag.removeprefix("v").split("."))
    return parts + (0,) * (3 - len(parts))


def action_releases(repository: str) -> list[registry.Release]:
    repository_name(repository)
    result = []
    for page in range(1, 101):
        entries = registry.data(
            f"https://api.github.com/repos/{repository}/releases?per_page=100&page={page}"
        )
        if not isinstance(entries, list):
            raise ValueError("Malformed GitHub release inventory")  # noqa: TRY004 - decoded external data
        for item in entries:
            rank = action_version(item.get("tag_name"))
            if item["draft"] or item["prerelease"] or rank is None:
                continue
            at = registry.timestamp(item.get("published_at"))
            tag = item["tag_name"]
            result.append(registry.Release(".".join(map(str, rank)), at, tag))
        if len(entries) < 100:
            return result
    raise ValueError("GitHub release inventory exceeded its pagination bound")


def select_action(
    repository: str,
    current: str,
    tracking: dict,
    policy: dict,
    now: datetime,
    *,
    advance_major: bool = True,
) -> dict:
    repository_name(repository)
    revision(current)
    if tracking["kind"] == "pin":
        return {"revision": current, "reason": "explicit immutable pin"}
    if tracking["kind"] == "channel":
        selected = nix_candidate(repository, tracking["channel"], policy, now)
        if commit_time(repository, current) >= registry.timestamp(
            selected["published"]
        ):
            return {"revision": current, "reason": "retained newer current revision"}
        return {**selected, "reason": "mature channel revision"}
    if tracking["kind"] != "release":
        raise ValueError("Unknown Actions tracking policy")
    releases = action_releases(repository)
    major = tracking.get("major")
    if not advance_major and major is not None:
        releases = [
            item for item in releases if action_version(item.version)[0] == major
        ]
    candidates = sorted(
        registry.eligible("github", releases, policy, repository, now),
        key=lambda item: action_version(item.version),
        reverse=True,
    )
    exceptions = {
        item.version
        for item in registry.active_exceptions(
            "github", releases, policy, repository, now
        )
    }
    for candidate in candidates:
        tag = candidate.identity
        commit = registry.github_commit(repository, tag)
        published = max(candidate.published, commit_time(repository, commit))
        if published > now:
            raise ValueError("Actions selected commit has future age evidence")
        if published <= cutoff(policy, now) or candidate.version in exceptions:
            chosen = registry.Release(candidate.version, published, tag)
            break
    else:
        raise ValueError("No eligible Actions release with mature immutable contents")
    rank = action_version(chosen.version)
    old_rank = tracking.get("version")
    if (
        (major is not None and rank[0] < major)
        or (old_rank is not None and rank < tuple(old_rank))
        or (
            (major is None or rank[0] == major)
            and commit != current
            and commit_time(repository, current) >= commit_time(repository, commit)
        )
    ):
        return {"revision": current, "reason": "retained newer current release"}
    return {
        "revision": commit,
        "tag": tag,
        "version": chosen.version,
        "published": chosen.published.isoformat(),
        "reason": "eligible stable release",
    }


ACTION = re.compile(
    r"(?m)^(?P<prefix>[ \t]*(?:-[ \t]+)?uses:[ \t]+)"
    r"(?P<quote>['\"]?)(?P<action>[^\s#'\"]+)@(?P<revision>[^\s#'\"]+)"
    r"(?P=quote)(?P<suffix>[^\r\n]*)$"
)


def action_tracking(suffix: str) -> dict:
    if "deps-update:" in suffix:
        text = suffix.split("deps-update:", 1)[1].strip()
        tokens = text.split()
        if "pin" in tokens:
            if any(t.startswith(("channel=", "release-major=")) for t in tokens):
                raise ValueError("Conflicting Actions pin and tracking declarations")
            return {"kind": "pin"}
        major = re.search(r"(?:^|\s)release-major=v(\d+)(?:$|\s)", text)
        channel = re.search(r"(?:^|\s)channel=([^\s]+)", text)
        if major and channel:
            raise ValueError("Conflicting Actions release and channel declarations")
        if major:
            return {"kind": "release", "major": int(major[1])}
        if channel:
            return {"kind": "channel", "channel": channel[1]}
        if re.fullmatch(r"tool=[^\s]+", text):
            return {"kind": "release"}
        raise ValueError(
            "Unsupported Actions annotation; declare release, channel, or pin"
        )
    version = re.fullmatch(r"[ \t]*#[ \t]*(v?\d+(?:\.\d+){0,2})[ \t]*", suffix)
    if version:
        return {
            "kind": "release",
            "major": action_version(version[1])[0],
            "version": list(action_version(version[1])),
        }
    return {"kind": "pin"}


def action_files(root: Path, spec: dict) -> list[str]:
    declared = spec.get("files")
    if not isinstance(declared, list) or not declared:
        raise ValueError("Actions requires declared workflow files or contained globs")
    result = set()
    for pattern in declared:
        if not isinstance(pattern, str):
            raise ValueError("Workflow paths must be strings")  # noqa: TRY004 - decoded external data
        tc.contained(root, pattern)
        matches = (
            sorted(root.glob(pattern))
            if any(c in pattern for c in "*?[")
            else [root / pattern]
        )
        for path in matches:
            name = path.relative_to(root).as_posix()
            tc.regular_input(root, name)
            result.add(name)
    if not result:
        raise ValueError("Declared workflow patterns have no inputs")
    return sorted(result)


def actions_snapshot(root: Path, spec: dict) -> dict:
    records = []
    files = action_files(root, spec)
    for name in files:
        source = tc.regular_input(root, name).decode()

        def external_uses(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key == "uses":
                        if not isinstance(item, str):
                            raise ValueError(
                                "Actions uses values must be literal strings"
                            )
                        if not item.startswith(("./", "docker://")):
                            yield item
                    else:
                        yield from external_uses(item)
            elif isinstance(value, list):
                for item in value:
                    yield from external_uses(item)

        parsed = list(external_uses(yaml.safe_load(source)))
        matched = [
            m["action"] + "@" + m["revision"]
            for m in ACTION.finditer(source)
            if not m["action"].startswith(("./", "docker://"))
        ]
        if sorted(parsed) != sorted(matched):
            raise ValueError(
                "External Actions uses entries must be standalone literal lines"
            )
        ordinals = {}
        for match in ACTION.finditer(source):
            action = match["action"]
            if action.startswith(("./", "docker://")):
                continue
            parts = action.split("/")
            if len(parts) < 2 or any(p in ("", ".", "..") for p in parts):
                raise ValueError("Unsupported Actions repository or subpath")
            repository = repository_name("/".join(parts[:2]))
            ordinal = ordinals.get(action, 0)
            ordinals[action] = ordinal + 1
            records.append(
                {
                    "file": name,
                    "action": action,
                    "ordinal": ordinal,
                    "repository": repository,
                    "revision": revision(match["revision"]),
                    "tracking": action_tracking(match["suffix"]),
                }
            )
    return {"adapter": "actions", "files": files, "records": records}


def action_plan(before: dict, spec: dict, policy: dict, now: datetime) -> list[dict]:
    if type(spec.get("advance_major", True)) is not bool:
        raise ValueError("advance_major must be boolean")
    return [
        {
            **record,
            "selected": select_action(
                record["repository"],
                record["revision"],
                record["tracking"],
                policy,
                now,
                advance_major=spec.get("advance_major", True)
                and spec.get("mode", "aggressive") != "compatible",
            ),
        }
        for record in before["records"]
    ]


def write_planned(root: Path, plans: list[tuple[str, bytes, bytes]]) -> list[str]:
    for name, old, _ in plans:
        if tc.regular_input(root, name) != old:
            raise ValueError(
                "Dependency input changed concurrently; changes are preserved"
            )
    changed = []
    for name, old, new in plans:
        if old != new:
            path = tc.contained(root, name)
            tc.atomic_bytes(path, new, stat.S_IMODE(path.stat().st_mode))
            changed.append(name)
    return changed


def resolve_actions(root: Path, spec: dict, policy: dict, now: datetime) -> dict:
    before = actions_snapshot(root, spec)
    decisions = action_plan(before, spec, policy, now)
    planned = []
    for name in before["files"]:
        old = tc.regular_input(root, name)
        by_key = {
            (d["action"], d["ordinal"]): d for d in decisions if d["file"] == name
        }
        counts = {}

        def replace(match, counts=counts, by_key=by_key):
            action = match["action"]
            if action.startswith(("./", "docker://")):
                return match[0]
            ordinal = counts.get(action, 0)
            counts[action] = ordinal + 1
            selected = by_key[action, ordinal]["selected"]
            suffix = match["suffix"]
            if "version" in selected:
                major = action_version(selected["version"])[0]
                if "deps-update:" in suffix:
                    suffix = re.sub(
                        r"release-major=v\d+", f"release-major=v{major}", suffix
                    )
                else:
                    suffix = re.sub(
                        r"(?<=#)\s*v?\d+(?:\.\d+){0,2}\s*$",
                        " v" + selected["version"],
                        suffix,
                    )
            return (
                match["prefix"]
                + match["quote"]
                + action
                + "@"
                + selected["revision"]
                + match["quote"]
                + suffix
            )

        planned.append((name, old, ACTION.sub(replace, old.decode()).encode()))
    return {"changed": write_planned(root, planned), "selected": decisions}


def oci_tag(tag: str):
    if not isinstance(tag, str):
        return None
    match = re.fullmatch(r"v?(\d+(?:\.\d+){0,2})(-[A-Za-z0-9][A-Za-z0-9._-]*)?", tag)
    if not match or re.search(
        r"(?:^|[._-])(?:alpha|beta|rc|pre|preview|nightly|dev|canary|snapshot)\d*(?:[._-]|$)",
        match[2] or "",
    ):
        return None
    parts = match[1].split(".")
    return (".".join(parts + ["0"] * (3 - len(parts))), len(parts), match[2] or "")


def oci_candidates(repository: str, source: str) -> list[registry.Release]:
    result = []
    if source == "dockerHub":
        if isinstance(repository, str):
            repository = repository.removeprefix("docker.io/")
        if not isinstance(repository, str) or not re.fullmatch(
            r"[a-z0-9_.-]+/[a-z0-9_.-]+", repository
        ):
            raise ValueError("Docker Hub requires an explicit namespace/repository")
        prefix = f"https://hub.docker.com/v2/repositories/{repository}/tags"
        url = prefix + "?page_size=100"
        visited = set()
        for _ in range(100):
            if url in visited or not (url.startswith((prefix + "?", prefix + "/?"))):
                raise ValueError("Unexpected Docker Hub pagination target")
            visited.add(url)
            body = registry.data(url)
            if not isinstance(body.get("results"), list):
                raise ValueError("Malformed Docker Hub tag inventory")  # noqa: TRY004 - decoded external data
            for entry in body["results"]:
                if oci_tag(entry.get("name")) is None:
                    continue
                result.append(
                    registry.Release(
                        entry["name"],
                        registry.timestamp(entry.get("last_updated")),
                        registry.digest(entry.get("digest")),
                    )
                )
            url = body.get("next")
            if not url:
                return result
        raise ValueError("Docker Hub pagination exceeded its bound")
    if source == "gcr":
        if (
            not isinstance(repository, str)
            or not re.fullmatch(r"(?:[a-z]+\.)?gcr\.io/[A-Za-z0-9_./-]+", repository)
            or any(p in ("", ".", "..") for p in repository.split("/"))
        ):
            raise ValueError("GCR requires an explicit public registry/repository")
        host, name = repository.split("/", 1)
        body = registry.data(f"https://{host}/v2/{name}/tags/list")
        if not isinstance(body.get("manifest"), dict):
            raise ValueError("GCR lacks manifest-bound publication evidence")
        seen = set()
        for identity, entry in body["manifest"].items():
            identity = registry.digest(identity)
            raw = entry.get("timeUploadedMs")
            if (
                not isinstance(raw, (str, int))
                or isinstance(raw, bool)
                or not re.fullmatch(r"\d+", str(raw))
            ):
                raise ValueError("GCR manifest lacks its upload time")
            at = datetime.fromtimestamp(int(raw) / 1000, timezone.utc)
            for tag in entry.get("tag", []):
                if oci_tag(tag) is None:
                    continue
                if tag in seen:
                    raise ValueError("GCR tag has conflicting immutable identities")
                seen.add(tag)
                result.append(registry.Release(tag, at, identity))
        return result
    raise ValueError("Unsupported OCI age-evidence source")


def oci_inventory(root: Path, spec: dict) -> dict:
    value = read_json(root, spec["file"])
    for part in spec.get("pointer", ["images"]):
        value = value[part]
    if not isinstance(value, dict) or not value:
        raise ValueError("OCI inventory must be a nonempty named image mapping")
    names = spec.get("names", list(value))
    if (
        not isinstance(names, list)
        or not names
        or len(set(names)) != len(names)
        or set(names) - value.keys()
    ):
        raise ValueError("OCI names must select distinct declared inventory entries")
    result = {}
    for name in names:
        entry = value[name]
        if not isinstance(entry, dict) or oci_tag(entry.get("tag")) is None:
            raise ValueError("OCI inventory requires explicit stable release tags")
        result[name] = {
            "repository": entry["repository"],
            "tag": entry["tag"],
            "digest": registry.digest(entry.get("digest")),
            "versionSource": entry.get("versionSource", "dockerHub"),
        }
    return result


def select_oci(current: dict, spec: dict, policy: dict, now: datetime) -> dict:
    parsed = oci_tag(current["tag"])
    candidates = oci_candidates(current["repository"], current["versionSource"])
    eligible = []
    for item in candidates:
        tag = oci_tag(item.version)
        if tag[1:] != parsed[1:]:
            continue
        if (
            spec.get("mode", "aggressive") == "compatible"
            and action_version(tag[0])[: min(parsed[1], 2)]
            != action_version(parsed[0])[: min(parsed[1], 2)]
        ):
            continue
        eligible.append(
            registry.Release(tag[0], item.published, item.version + "@" + item.identity)
        )
    if spec.get("mode", "aggressive") not in ("aggressive", "compatible"):
        raise ValueError("OCI mode must be aggressive or compatible")
    chosen = registry.select("docker", eligible, policy, current["repository"], now)
    if registry.version("docker", chosen.version) < registry.version(
        "docker", parsed[0]
    ):
        return {**current, "reason": "retained newer immutable image"}
    tag, digest = chosen.identity.rsplit("@", 1)
    return {
        **current,
        "tag": tag,
        "digest": digest,
        "published": chosen.published.isoformat(),
        "reason": "eligible manifest-bound image",
    }


def resolve_oci(root: Path, spec: dict, policy: dict, now: datetime) -> dict:
    before = oci_inventory(root, spec)
    decisions = {
        name: select_oci(value, spec, policy, now) for name, value in before.items()
    }
    old = tc.regular_input(root, spec["file"])
    document = json.loads(old, object_pairs_hook=object_pairs)
    inventory = document
    for part in spec.get("pointer", ["images"]):
        inventory = inventory[part]
    for name, selected in decisions.items():
        inventory[name].update(tag=selected["tag"], digest=selected["digest"])
    new = (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode()
    if all(
        all(selected[k] == before[name][k] for k in ("tag", "digest"))
        for name, selected in decisions.items()
    ):
        new = old
    return {
        "changed": write_planned(root, [(spec["file"], old, new)]),
        "selected": decisions,
    }


def nix_specs(root: Path, spec: dict) -> list[dict]:
    entries = spec.get("inputs")
    if not isinstance(entries, list) or not entries:
        raise ValueError("Nix updates require explicitly declared inputs")
    seen = set()
    for item in entries:
        directory = item.get("directory", ".")
        tc.regular_input(root, str(Path(directory) / "flake.nix"))
        repository_name(item.get("repository"))
        name = item.get("input")
        if not isinstance(name, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", name
        ):
            raise ValueError("Nix requires an exact input name or nested input path")
        key = (directory, name)
        if key in seen:
            raise ValueError("Duplicate Nix update target")
        seen.add(key)
    return entries


def nix_node(lock: dict, name: str) -> str:
    def follow(path, active):
        key = lock["root"]
        for part in path:
            ref = lock["nodes"][key]["inputs"][part]
            if isinstance(ref, list):
                token = tuple(ref)
                if token in active:
                    raise ValueError("Nix follows cycle")
                key = follow(ref, active | {token})
            elif isinstance(ref, str):
                key = ref
            else:
                raise ValueError("Malformed Nix input reference")  # noqa: TRY004 - decoded external data
        return key

    return follow(name.split("/"), set())


def nix_snapshot(root: Path, spec: dict) -> dict:
    inputs = nix_specs(root, spec)
    locks = {
        str(Path(item.get("directory", ".")) / "flake.lock"): None for item in inputs
    }
    for path in locks:
        locks[path] = read_json(root, path)
    return {"adapter": "nix", "locks": locks}


def nix_plan(before: dict, spec: dict, policy: dict, now: datetime) -> list[dict]:
    plans = []
    for item in spec["inputs"]:
        path = str(Path(item.get("directory", ".")) / "flake.lock")
        lock = before["locks"][path]
        node = nix_node(lock, item["input"])
        current = lock["nodes"][node]["locked"]
        repository = item["repository"]
        if (
            current.get("type") != "github"
            or current.get("owner", "") + "/" + current.get("repo", "") != repository
        ):
            raise ValueError("Nix input differs from its declared source repository")
        selected = nix_candidate(repository, item["branch"], policy, now)
        at = commit_time(repository, current.get("rev"))
        if at >= registry.timestamp(selected["published"]):
            selected = {"revision": current["rev"], "published": at.isoformat()}
        if any(
            p["file"] == path
            and p["node"] == node
            and p["revision"] != selected["revision"]
            for p in plans
        ):
            raise ValueError("Nix aliases select conflicting revisions for one source")
        plans.append({**item, "file": path, "node": node, **selected})
    return plans


def resolve_nix(root: Path, spec: dict, policy: dict, now: datetime) -> dict:
    before = nix_snapshot(root, spec)
    plans = nix_plan(before, spec, policy, now)
    changed = []
    # One invocation per flake keeps coordinated overrides in the same lock operation.
    for file in before["locks"]:
        selected = [p for p in plans if p["file"] == file]
        if all(
            before["locks"][file]["nodes"][p["node"]]["locked"]["rev"] == p["revision"]
            for p in selected
        ):
            continue
        argv = [
            "nix",
            "--extra-experimental-features",
            "nix-command flakes",
            "flake",
            "lock",
            "path:.",
        ]
        for item in selected:
            argv.extend(
                [
                    "--override-input",
                    item["input"],
                    f"github:{item['repository']}/{item['revision']}",
                ]
            )
        chainman.execute(
            root,
            spec.get("profile", "core"),
            argv,
            env=tc.environment(root),
            cwd=tc.contained(root, str(Path(file).parent)),
        )
        changed.append(file)
    audit_nix(root, spec, before, policy, now)
    return {"changed": changed, "selected": plans}


def nix_tree(root: Path, spec: dict, repository: str, commit: str) -> dict:
    repository_name(repository)
    revision(commit)
    owner, repo = repository.split("/")
    # All inserted values have a restricted literal alphabet; no Nix string
    # interpolation or project expression is accepted by this evidence query.
    expression = (
        'builtins.fetchTree { type = "github"; owner = "'
        + owner
        + '"; repo = "'
        + repo
        + '"; rev = "'
        + commit
        + '"; }'
    )
    result = chainman.execute(
        root,
        spec.get("profile", "core"),
        [
            "nix",
            "--extra-experimental-features",
            "nix-command flakes",
            "eval",
            "--impure",
            "--json",
            "--expr",
            expression,
        ],
        env=tc.environment(root),
        text=True,
        stdout=subprocess.PIPE,
    )
    return json.loads(result.stdout)


def audit_nix(
    root: Path, spec: dict, before: dict, policy: dict, now: datetime
) -> None:
    plans = nix_plan(before, spec, policy, now)
    current = nix_snapshot(root, spec)
    for file, old in before["locks"].items():
        new = current["locks"][file]
        expected = {p["node"]: p for p in plans if p["file"] == file}
        if (
            new.get("root") != old.get("root")
            or new["nodes"].keys() != old["nodes"].keys()
        ):
            raise ValueError("Nix changed undeclared lock structure")
        for key, node in old["nodes"].items():
            if key not in expected:
                if node != new["nodes"][key]:
                    raise ValueError("Nix changed an undeclared input")
                continue
            plan = expected[key]
            selected = new["nodes"][key]["locked"]
            if any(
                new["nodes"][key].get(k) != value
                for k, value in node.items()
                if k not in ("locked", "original")
            ):
                raise ValueError(
                    "Nix changed the selected input's dependency structure"
                )
            allowed_locked = {"rev", "lastModified", "narHash"}
            if {k: v for k, v in selected.items() if k not in allowed_locked} != {
                k: v for k, v in node["locked"].items() if k not in allowed_locked
            }:
                raise ValueError("Nix changed undeclared source attributes")
            if (
                nix_node(new, plan["input"]) != key
                or selected.get("rev") != plan["revision"]
                or selected.get("type") != "github"
                or selected.get("owner", "") + "/" + selected.get("repo", "")
                != plan["repository"]
            ):
                raise ValueError(
                    "Nix final input differs from its selected immutable source"
                )
            if selected != node["locked"]:
                if selected.get("lastModified") != int(
                    registry.timestamp(plan["published"]).timestamp()
                ) or not re.fullmatch(
                    r"sha256-[A-Za-z0-9+/]{43}=", selected.get("narHash", "")
                ):
                    raise ValueError(
                        "Nix input lacks consistent timestamp/hash evidence"
                    )
                tree = nix_tree(root, spec, plan["repository"], plan["revision"])
                if tree.get("narHash") != selected["narHash"]:
                    raise ValueError(
                        "Nix input content hash differs from its immutable source"
                    )


def snapshot(root: Path, spec: dict) -> dict:
    adapter = spec.get("adapter")
    if adapter == "actions":
        return actions_snapshot(root, spec)
    if adapter == "oci":
        return {"adapter": "oci", "images": oci_inventory(root, spec)}
    if adapter == "nix":
        return nix_snapshot(root, spec)
    if adapter == "go":
        import source_go

        return source_go.snapshot(root, spec)
    if adapter == "toolchain":
        import source_toolchain

        return source_toolchain.snapshot(root, spec)
    raise ValueError("Unsupported source update adapter")


def resolve(root: Path, spec: dict, policy: dict, now: datetime) -> dict:
    cutoff(policy, now)
    adapter = spec.get("adapter")
    if adapter == "actions":
        return resolve_actions(root, spec, policy, now)
    if adapter == "oci":
        return resolve_oci(root, spec, policy, now)
    if adapter == "nix":
        return resolve_nix(root, spec, policy, now)
    if adapter == "go":
        import source_go

        return source_go.resolve(root, spec, policy, now)
    if adapter == "toolchain":
        import source_toolchain

        return source_toolchain.resolve(root, spec, policy, now)
    raise ValueError("Unsupported source update adapter")


def audit(root: Path, spec: dict, before: dict, policy: dict, now: datetime) -> None:
    if before.get("adapter") != spec.get("adapter"):
        raise ValueError("Source audit snapshot belongs to a different adapter")
    cutoff(policy, now)
    if spec["adapter"] == "actions":
        actual = actions_snapshot(root, spec)
        expected = action_plan(before, spec, policy, now)
        if actual["files"] != before["files"] or len(actual["records"]) != len(
            expected
        ):
            raise ValueError("Actions target inventory changed during reconciliation")
        for current, planned in zip(actual["records"], expected, strict=True):
            if (
                any(
                    current[k] != planned[k]
                    for k in ("file", "action", "ordinal", "repository")
                )
                or current["revision"] != planned["selected"]["revision"]
            ):
                raise ValueError(
                    "Actions final identity differs from its verified selection"
                )
            old_tracking, new_tracking = planned["tracking"], current["tracking"]
            expected_tracking = dict(old_tracking)
            if "version" in planned["selected"]:
                version = list(action_version(planned["selected"]["version"]))
                if "major" in expected_tracking:
                    expected_tracking["major"] = version[0]
                if "version" in expected_tracking:
                    expected_tracking["version"] = version
            if expected_tracking != new_tracking:
                raise ValueError(
                    "Actions tracking policy changed during reconciliation"
                )
    elif spec["adapter"] == "oci":
        actual = oci_inventory(root, spec)
        expected = {
            name: select_oci(value, spec, policy, now)
            for name, value in before["images"].items()
        }
        if actual.keys() != expected.keys() or any(
            any(actual[name][k] != value[k] for k in actual[name])
            for name, value in expected.items()
        ):
            raise ValueError("OCI final identity differs from its verified selection")
    elif spec["adapter"] == "nix":
        audit_nix(root, spec, before, policy, now)
    elif spec["adapter"] == "go":
        import source_go

        source_go.audit(root, spec, before, policy, now)
    elif spec["adapter"] == "toolchain":
        import source_toolchain

        source_toolchain.audit(root, spec, before, policy, now)
    else:
        raise ValueError("Unsupported source update adapter")
