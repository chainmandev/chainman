"""Supported dependency queries and ordered, project-declared update adapters."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import tomllib

import chainman
import registry
import toolchain as tc
import updates


def merge(base: dict, extra: dict) -> dict:
    result = deepcopy(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def policy(root: Path) -> dict:
    result = deepcopy(tc.config(root).get("updates", {}))
    if result.get("policy_file"):
        declared = tomllib.loads(tc.regular_input(root, result["policy_file"]).decode())
        result = merge(result, declared)
    registry.minimum_age(result)
    return result


def instant() -> datetime:
    value = os.environ.get("CHAINMAN_UPDATE_AT")
    return registry.timestamp(value) if value else datetime.now(timezone.utc)


@contextmanager
def transaction_environment(root: Path, now: datetime):
    values = {
        "CHAINMAN_UPDATE_ACTIVE": "1",
        "CHAINMAN_UPDATE_AT": now.isoformat(),
        "CHAINMAN_ROOT": str(root),
        "CHAINMAN_PROJECT_ROOT": str(root),
        "CHAINMAN_RUNTIME": str(chainman.RUNTIME),
    }
    before = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in before.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def implementation(spec: dict):
    adapter = spec.get("adapter")
    if adapter == "javascript":
        import javascript_updates

        return javascript_updates
    if adapter in {"actions", "oci", "nix", "go", "toolchain"}:
        import source_updates

        return source_updates
    if adapter == "artifact":
        import source_artifacts

        return source_artifacts
    if adapter in {"rust", "python", "flutter", "swift", "gradle"}:
        import ecosystem_updates

        return ecosystem_updates
    raise ValueError(f"Unknown dependency adapter: {adapter!r}")


def configured(root: Path, name: str, settings: dict | None = None):
    settings = policy(root) if settings is None else settings
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name):
        raise ValueError(
            "Adapter names require letters, digits, underscores or hyphens"
        )
    spec = settings.get("adapters", {}).get(name)
    if not isinstance(spec, dict):
        raise ValueError(f"Dependency adapter {name!r} is not configured")
    result = deepcopy(spec)
    result.setdefault(
        "profile", tc.config(root).get("project", {}).get("default_profile", "default")
    )
    implementation(result)
    return result


def effective_policy(settings: dict, spec: dict) -> dict:
    result = merge(settings, spec.get("policy", {}))
    registry.minimum_age(result)
    return result


def selection(settings: dict, extra: list[str]) -> tuple[set[str], dict[str, str]]:
    parser = argparse.ArgumentParser(prog="deps-update --")
    parser.add_argument("--targets", default="all")
    parser.add_argument("--policy", choices=("aggressive", "compatible"))
    parser.add_argument("--target-policy", action="append", default=[])
    args = parser.parse_args(extra)
    names = target_names(settings)
    automatic = {
        name
        for name in names
        if not settings.get("adapters", {}).get(name, {}).get("explicit_only", False)
    }
    for spec in settings.get("adapters", {}).values():
        if type(spec.get("explicit_only", False)) is not bool:
            raise ValueError("Adapter explicit_only must be a boolean")
    groups = settings.get("target_groups", {})
    targets = set()
    for name in args.targets.split(","):
        if name == "all":
            targets.update(automatic)
        elif name in groups:
            targets.update(groups[name])
        else:
            targets.add(name)
    if not targets or targets - names:
        raise ValueError(
            "Update targets must name configured adapters, hooks or target groups"
        )
    modes = {name: args.policy for name in targets if args.policy}
    for item in args.target_policy:
        name, sep, mode = item.partition("=")
        if not sep or name not in targets or mode not in {"aggressive", "compatible"}:
            raise ValueError(
                "Target policy requires a selected adapter=aggressive|compatible"
            )
        modes[name] = mode
    return targets, modes


def target_names(settings: dict) -> set[str]:
    hooks = settings.get("targets", [])
    if not isinstance(hooks, list) or any(
        not isinstance(name, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name)
        for name in hooks
    ):
        raise ValueError("Hook targets require a list of simple names")
    adapters = set(settings.get("adapters", {}))
    if len(hooks) != len(set(hooks)) or adapters.intersection(hooks):
        raise ValueError("Hook target names must be unique and distinct from adapters")
    return adapters | set(hooks)


def run_steps(root: Path, settings: dict, now: datetime, extra: list[str]):
    """Resolve in order, then audit every selected adapter after all project hooks."""
    steps = settings.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("Configured adapters require a nonempty updates.steps list")
    names, modes = selection(settings, extra)
    all_names = target_names(settings)
    adapters, seen, covered, phase = {}, set(), set(), 0
    for step in steps:
        if not isinstance(step, dict) or ("resolve" in step) == ("commands" in step):
            raise ValueError("Each update step must declare resolve or commands")
        targets = step.get("targets", [])
        if not isinstance(targets, list) or set(targets) - all_names:
            raise ValueError("Hook targets must name configured adapters or hooks")
        if "resolve" not in step:
            covered.update(targets)
            continue
        name = step["resolve"]
        spec = configured(root, name, settings)
        if name in seen:
            raise ValueError("An adapter must occur exactly once in updates.steps")
        seen.add(name)
        covered.add(name)
        if name not in names:
            continue
        if name in modes:
            spec["mode"] = modes[name]
        next_phase = {"nix": 0, "toolchain": 1}.get(spec["adapter"], 2)
        if next_phase < phase:
            raise ValueError(
                "Nix inputs and toolchain synchronization must precede package resolution"
            )
        phase = next_phase
        adapters[name] = (spec, effective_policy(settings, spec))
    if names.intersection(settings.get("adapters", {})) - seen or names - covered:
        raise ValueError("Selected update targets are missing from updates.steps")
    # Capture all pre-update identities before any resolver or generator can run.
    baselines = {
        name: implementation(spec).snapshot(root, spec)
        for name, (spec, _) in adapters.items()
    }
    with transaction_environment(root, now):
        for step in steps:
            if "resolve" in step:
                name = step["resolve"]
                if name in adapters:
                    spec, chosen_policy = adapters[name]
                    result = implementation(spec).resolve(
                        root,
                        spec,
                        chosen_policy,
                        now,
                        **(
                            {"before": baselines[name]}
                            if spec["adapter"] == "toolchain"
                            else {}
                        ),
                    )
                    if isinstance(result, dict):
                        baselines[name]["resolution"] = result
                continue
            if step.get("targets") and not names.intersection(step["targets"]):
                continue
            env = tc.environment(root)
            env.update(
                TOOLCHAIN_FRESH="1",
                CHAINMAN_MINIMUM_RELEASE_AGE_DAYS=str(registry.minimum_age(settings)),
                CHAINMAN_UPDATE_TARGETS=json.dumps(sorted(names)),
                CHAINMAN_UPDATE_POLICIES=json.dumps(
                    {
                        name: modes.get(
                            name,
                            adapters[name][0].get("mode", "aggressive")
                            if name in adapters
                            else "aggressive",
                        )
                        for name in names
                    },
                    sort_keys=True,
                ),
            )
            chainman.run_hook(
                root,
                step["commands"],
                name=step.get("profile", settings.get("profile", "default")),
                env=env,
            )
        for name, (spec, chosen_policy) in adapters.items():
            implementation(spec).audit(root, spec, baselines[name], chosen_policy, now)


def resolve_command(root: Path, args: list[str]):
    if os.environ.get("CHAINMAN_UPDATE_ACTIVE") != "1":
        raise ValueError("deps-resolve must run inside deps-update")
    parser = argparse.ArgumentParser(prog="deps-resolve")
    parser.add_argument("name")
    parser.add_argument("--policy", choices=("aggressive", "compatible"))
    opts = parser.parse_args(args)
    settings = policy(root)
    spec = configured(root, opts.name, settings)
    if opts.policy:
        spec["mode"] = opts.policy
    selected_policy = effective_policy(settings, spec)
    engine = implementation(spec)
    now = instant()
    before = engine.snapshot(root, spec)
    result = engine.resolve(
        root,
        spec,
        selected_policy,
        now,
        **({"before": before} if spec["adapter"] == "toolchain" else {}),
    )
    if isinstance(result, dict):
        before["resolution"] = result
    engine.audit(root, spec, before, selected_policy, now)
    return result


def release_record(release: registry.Release) -> dict:
    return {
        "version": release.version,
        "identity": release.identity,
        "published": release.published.isoformat(),
        "artifacts": [
            {
                "url": item.url,
                "digest": item.digest,
                "published": item.published.isoformat(),
            }
            for item in release.artifacts
        ],
    }


def query(root: Path, request: dict, *, now: datetime | None = None) -> dict:
    if (
        not isinstance(request, dict)
        or type(request.get("schema")) is not int
        or request["schema"] != 1
    ):
        raise ValueError("Dependency requests require schema=1")
    now = instant() if now is None else now
    settings = policy(root)
    if request.get("adapter"):
        spec = configured(root, request["adapter"], settings)
        settings = effective_policy(settings, spec)
    operation = request.get("operation")
    if operation in {"artifact-metadata", "artifact-audit"}:
        import source_artifact

        url, digest = request.get("url"), request.get("digest")
        result = (
            source_artifact.audit(
                url, digest, settings, now, max_bytes=request.get("max_bytes")
            )
            if operation == "artifact-audit"
            else source_artifact.inspect(
                url, digest, now, max_bytes=request.get("max_bytes")
            )
        )
        return {"schema": 1, "operation": operation, **result}
    if operation in {"select", "metadata"}:
        provider, package = request.get("provider"), request.get("package")
        if provider not in {
            "npm",
            "pypi",
            "crates",
            "pub",
            "github",
            "docker",
            "go",
            "swift",
            "maven",
        }:
            raise ValueError("Unsupported registry provider")
        if not isinstance(package, str) or not package:
            raise ValueError("Selection requires a package identity")
        tag_pattern = request.get("tag_pattern")
        if tag_pattern is not None and provider != "github":
            raise ValueError("Tag patterns are supported only for GitHub releases")
        if tag_pattern is not None:
            import source_github

            candidates = source_github.releases(package, tag_pattern)
        elif provider == "go":
            import lock_adapters

            candidates = lock_adapters.go_candidates(root, package)
        else:
            candidates = registry.releases(provider, package)
        if operation == "select" and request.get("constraint"):
            bound = request["constraint"]
            if (
                not isinstance(bound, dict)
                or not isinstance(bound.get("range"), str)
                or not isinstance(bound.get("reason"), str)
                or not bound["reason"].strip()
            ):
                raise ValueError("A query constraint requires a range and reason")
            candidates = [
                candidate
                for candidate in candidates
                if registry.compatible(provider, candidate.version, bound["range"])
            ]
        if request.get("mode") == "compatible" and not request.get("constraint"):
            raise ValueError("A compatible query must declare its actual constraint")
        if request.get("mode", "aggressive") not in {"aggressive", "compatible"}:
            raise ValueError("Query mode must be aggressive or compatible")
        if operation == "metadata":
            matches = [
                candidate
                for candidate in candidates
                if candidate.version == request.get("version")
                or (
                    tag_pattern is not None
                    and candidate.identity == request.get("version")
                )
            ]
            if not matches:
                raise ValueError("Exact release metadata is unavailable")
            chosen = max(matches, key=lambda candidate: candidate.published)
        else:
            chosen = registry.select(provider, candidates, settings, package, now)
        extra = {}
        if tag_pattern is not None:
            if operation == "select":
                tagged = source_github.select(
                    package, tag_pattern, settings, now, values=candidates
                )
            else:
                tagged = {
                    "release": source_github.bind(package, chosen),
                    "tag": chosen.identity,
                }
            chosen = tagged["release"]
            extra["tag"] = tagged["tag"]
        elif provider == "github":
            import source_updates

            identity = registry.github_commit(package, chosen.version)
            published = max(
                chosen.published, source_updates.commit_time(package, identity)
            )
            chosen = registry.Release(
                chosen.version, published, identity, chosen.python, chosen.artifacts
            )
            if operation == "select" and not registry.eligible(
                provider, [chosen], settings, package, now
            ):
                raise ValueError("Selected release tag now points to immature contents")
        elif provider == "docker":
            # Discovery may retain legacy tags with no digest, but public evidence
            # (including metadata and retained eligible_candidate) must bind one.
            registry.digest(chosen.identity)
        if operation == "metadata":
            return {
                "schema": 1,
                "disposition": "metadata",
                **release_record(chosen),
                **extra,
            }
        current = request.get("current")
        rank = registry.version(provider, current) if isinstance(current, str) else None
        if rank is not None and registry.version(provider, chosen.version) <= rank:
            return {
                "schema": 1,
                "disposition": "retained",
                "version": current,
                "reason": "No newer eligible version",
                "eligible_candidate": {**release_record(chosen), **extra},
            }
        return {
            "schema": 1,
            "disposition": "selected",
            **release_record(chosen),
            **extra,
        }
    if operation == "audit":
        items = request.get("artifacts")
        if not isinstance(items, list) or any(
            not isinstance(item, list)
            or len(item) != 5
            or any(not isinstance(part, str) for part in item)
            for item in items
        ):
            raise ValueError(
                "Audit artifacts require provider/package/version/url/digest arrays"
            )
        updates.audit_identities(
            root, {tuple(item) for item in items}, set(), settings, now
        )
        return {"schema": 1, "disposition": "audited", "artifacts": len(items)}
    raise ValueError("Dependency operation must be select, metadata or audit")


def query_command(root: Path, args: list[str]):
    if args:
        raise ValueError(
            "deps-query reads one schema-versioned JSON request from stdin"
        )
    body = sys.stdin.read(4 * 1024 * 1024 + 1)
    if len(body) > 4 * 1024 * 1024:
        raise ValueError("Dependency request exceeds 4 MiB")
    return query(root, json.loads(body))
