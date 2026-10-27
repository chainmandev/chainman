"""Pinned ecosystem audit tools, shared exception rules and explicit coverage."""

from collections.abc import Mapping, Sequence
from datetime import date
import json
from pathlib import Path
import subprocess
import tempfile
from typing import Literal, NotRequired, TypedDict

import chainman
import adapter_data as ad
import dependency_api
import registry
import toolchain as tc


class Finding(TypedDict):
    id: str
    package: str
    version: str | None
    paths: list[str]


class Lane(TypedDict):
    adapter: str
    status: Literal["unsupported", "unassessed", "failed", "passed"]
    directory: NotRequired[str]
    reason: NotRequired[str]
    findings: NotRequired[list[Finding]]
    reported_findings: NotRequired[list[Finding]]
    accepted: NotRequired[int]
    output: NotRequired[str]
    error: NotRequired[str]


class Report(TypedDict):
    schema: Literal[2]
    complete: bool
    passed: bool
    lanes: list[Lane]


def javascript(
    report: object, installed: Mapping[str, str] | None = None
) -> list[Finding]:
    if not isinstance(report, dict) or report.get("error"):
        raise ValueError("Package audit did not return a successful report")
    document = ad.table(report, "Package report")
    findings: list[Finding] = []
    if "advisories" in document:
        for key, raw in ad.table(document["advisories"], "Package advisories").items():
            entry = ad.table(raw, "Package advisory")
            occurrences = ad.array(entry.get("findings", []), "Advisory findings")
            for occurrence in occurrences or [{}]:
                value = ad.table(occurrence, "Advisory occurrence")
                findings.append(
                    dict(
                        id=key,
                        package=ad.nonempty(entry["module_name"], "Package name"),
                        version=ad.nonempty(value["version"], "Installed version")
                        if "version" in value
                        else None,
                        paths=ad.strings(value.get("paths", []), "Dependency paths"),
                    )
                )
        return findings
    if "vulnerabilities" in document:
        for raw in ad.table(document["vulnerabilities"], "Package inventory").values():
            value = ad.table(raw, "Package entry")
            for via in ad.array(value.get("via", []), "Package references"):
                if isinstance(via, dict):
                    entry = ad.table(via, "Package reference")
                    package = ad.nonempty(entry["name"], "Package name")
                    # npm's range describes affected versions, not installed ones.
                    # Only paths for this package can bind a lockfile version.
                    nodes = (
                        ad.strings(value.get("nodes", []), "Dependency paths")
                        if value.get("name") == package
                        else []
                    )
                    locations: list[str | None] = list(nodes) or [None]
                    for node in locations:
                        findings.append(
                            dict(
                                id=str(entry["source"]),
                                package=package,
                                version=(installed or {}).get(node)
                                if node is not None
                                else None,
                                paths=[node] if node is not None else [],
                            )
                        )
        if document["vulnerabilities"] and not findings:
            raise ValueError(
                "Package audit vulnerabilities lack advisory identity evidence"
            )
        return findings
    raise ValueError("Package audit report has no recognized vulnerability inventory")


def validate_waivers(exceptions: Sequence[Mapping[str, object]], today: date) -> None:
    seen: list[Mapping[str, object]] = []
    for entry in exceptions:
        required = {"id", "package", "versions", "reason", "review_after"}
        if "versions" not in entry:
            raise ValueError(
                'Legacy package-wide audit waiver: migrate by adding versions = ["exact.reviewed.version"] and optionally exact paths'
            )
        if set(entry) - required - {"paths"} or required - set(entry):
            raise ValueError(
                "Audit exceptions require id, package, versions, reason and review_after; only paths is optional"
            )
        for key in ("id", "package", "reason"):
            if not ad.nonempty(entry[key], "Audit exception " + key).strip():
                raise ValueError("Audit exception fields must not be blank")
        versions = ad.strings(entry["versions"], "Reviewed versions")
        if (
            not versions
            or len(versions) != len(set(versions))
            or any(registry.lock_version("npm", v) is None for v in versions)
        ):
            raise ValueError(
                "Audit exceptions require distinct exact reviewed versions"
            )
        if "paths" in entry:
            paths = ad.strings(entry["paths"], "Reviewed paths")
            if (
                not paths
                or len(paths) != len(set(paths))
                or any(not p.strip() for p in paths)
            ):
                raise ValueError("Audit exception paths must be distinct and nonempty")
        identity = f"{entry['id']} ({entry['package']})"
        for previous in seen:
            if (
                previous["id"] == entry["id"]
                and previous["package"] == entry["package"]
                and set(ad.strings(previous["versions"], "Versions")) & set(versions)
            ):
                if (
                    "paths" not in previous
                    or "paths" not in entry
                    or set(ad.strings(previous["paths"], "Paths"))
                    & set(ad.strings(entry["paths"], "Paths"))
                ):
                    raise ValueError(f"Duplicate audit exception: {identity}")
        if (
            date.fromisoformat(ad.text(entry["review_after"], "Exception review date"))
            <= today
        ):
            raise ValueError(
                f"Audit exception {identity} requires review: {entry['review_after']}"
            )
        seen.append(entry)


def evaluate(
    findings: Sequence[Finding],
    exceptions: Sequence[Mapping[str, object]],
    today: date | None = None,
) -> list[Finding]:
    validate_waivers(exceptions, today or date.today())
    remaining: list[Finding] = [{**f} for f in findings]
    for entry in exceptions:
        matched = False
        next_findings: list[Finding] = []
        for finding in remaining:
            if (
                finding["id"] != entry["id"]
                or finding["package"] != entry["package"]
                or finding["version"] not in ad.strings(entry["versions"], "Versions")
            ):
                next_findings.append(finding)
                continue
            if "paths" in entry:
                paths = ad.strings(entry["paths"], "Paths")
                if not finding["paths"]:
                    next_findings.append(finding)
                    continue
                unmatched = [p for p in finding["paths"] if p not in paths]
                matched = matched or len(unmatched) < len(finding["paths"])
                if unmatched:
                    next_findings.append({**finding, "paths": unmatched})
            else:
                matched = True
        if not matched:
            raise ValueError(
                f"Stale audit exception {entry['id']} ({entry['package']}): no finding has matching reviewed version/path evidence"
            )
        remaining = next_findings
    return remaining


def npm_installed(root: Path) -> dict[str, str]:
    path = root / "package-lock.json"
    if not path.is_file():
        return {}
    lock = ad.table(json.loads(tc.regular_input(root, "package-lock.json")), "npm lock")
    return {
        name: ad.nonempty(item["version"], "Installed version")
        for name, raw in ad.table(lock.get("packages", {}), "npm packages").items()
        for item in [ad.table(raw, "npm package")]
        if "version" in item
    }


def tools_path(root: Path, kind: str, *, gc_root: Path) -> Path:
    return (
        Path(
            tc.managed_run(
                [
                    tc.nix_command(),
                    "--extra-experimental-features",
                    "nix-command flakes",
                    "build",
                    tc.nix_path_reference(chainman.RUNTIME / "nix", f"audit-{kind}"),
                    "--out-link",
                    str(gc_root),
                    "--print-out-paths",
                    "--no-write-lock-file",
                ],
                cwd=root,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
        )
        / "bin"
    )


def run(root: Path, arguments: list[str]) -> int:
    with tc.nix_temporary_directory("chainman-audit-tools-") as directory:
        return run_retained(root, arguments, Path(directory))


SCANNERS = {"javascript", "rust", "python", "go"}


def configuration(
    cfg: Mapping[str, object], adapters: Mapping[str, object]
) -> tuple[ad.Table, dict[str, str]]:
    audit = cfg.get("audits", {})
    if not isinstance(audit, dict) or set(audit) - {"exceptions", "unsupported"}:
        raise ValueError(
            "Audit configuration supports exceptions and unsupported declarations"
        )
    audit = ad.table(audit, "Audit configuration")
    exceptions = audit.get("exceptions", {})
    if not isinstance(exceptions, dict):
        raise ValueError("Audit exceptions must be keyed by adapter")
    exception_entries = ad.table(exceptions, "Audit exceptions")
    for name, entries in exception_entries.items():
        if ad.table(adapters.get(name, {}), "Adapter").get(
            "adapter"
        ) != "javascript" or not isinstance(entries, list):
            raise ValueError(
                "Shared audit exceptions require a declared JavaScript adapter; native scanners use their project policy"
            )
        validate_waivers(
            [ad.table(entry, "Audit exception") for entry in entries], date.today()
        )
    unsupported = audit.get("unsupported", {})
    if not isinstance(unsupported, dict) or any(
        name not in adapters or not isinstance(reason, str) or not reason.strip()
        for name, reason in unsupported.items()
    ):
        raise ValueError("Unsupported audit declarations require an adapter and reason")
    unsupported_reasons = ad.string_map(unsupported, "Unsupported audit declarations")
    return exception_entries, unsupported_reasons


def unavailable(
    name: str, spec: Mapping[str, object], unsupported: Mapping[str, str]
) -> Lane | None:
    """Classify coverage without executing a scanner, shared by audit and report."""
    kind = ad.text(spec["adapter"], "Adapter kind")
    if name in unsupported or kind in {"flutter", "swift", "gradle"}:
        return dict(
            adapter=name,
            status="unsupported",
            reason=unsupported.get(
                name, "No shared vulnerability scanner is configured for this ecosystem"
            ),
        )
    if kind not in SCANNERS:
        return dict(
            adapter=name,
            status="unassessed",
            reason="No vulnerability assessment is implemented for this adapter",
        )
    if not ad.strings(
        spec.get("directories", [spec.get("directory", ".")]), "Adapter directories"
    ):
        return dict(
            adapter=name,
            status="unassessed",
            reason="The selected adapter has an empty audit scope",
        )
    return None


def run_retained(root: Path, arguments: list[str], roots: Path) -> int:
    import recipes

    cfg = tc.config(root)
    settings = dependency_api.inspection_policy(root)
    adapters = ad.table(settings.get("adapters", {}), "Dependency adapters")
    selected = (
        dependency_api.selection(settings, recipes.selection_options(arguments))[0]
        if adapters or arguments
        else set()
    )
    exception_entries, unsupported_reasons = configuration(cfg, adapters)
    rows: list[Lane] = []
    binaries: dict[str, Path] = {}
    for name in adapters:
        if name not in selected:
            continue
        spec = dependency_api.configured(root, name, settings)
        kind = ad.text(spec["adapter"], "Adapter kind")
        missing = unavailable(name, spec, unsupported_reasons)
        if missing is not None:
            rows.append(missing)
            continue
        directories = ad.strings(
            spec.get("directories", [spec.get("directory", ".")]), "Adapter directories"
        )
        for directory in directories:
            try:
                cwd = tc.contained(root, directory)

                def execute(argv: list[str]) -> subprocess.CompletedProcess[str]:
                    result = chainman.execute(
                        root,
                        ad.text(spec["profile"], "Adapter profile"),
                        argv,
                        cwd=cwd,
                        env=tc.environment(root),
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    return result

                if kind == "javascript":
                    result = execute(
                        [
                            ad.text(spec.get("manager", "pnpm"), "JavaScript manager"),
                            "audit",
                            "--json",
                        ]
                    )
                    if result.returncode not in (0, 1):
                        raise ValueError("Package audit tool failed")
                    findings = javascript(
                        json.loads(result.stdout),
                        npm_installed(cwd) if spec.get("manager") == "npm" else None,
                    )
                    if result.returncode and not findings:
                        raise ValueError(
                            "Package audit failed without structured findings"
                        )
                    remaining = evaluate(
                        findings,
                        [
                            ad.table(entry, "Audit exception")
                            for entry in ad.array(
                                exception_entries.get(name, []), "Audit exceptions"
                            )
                        ],
                    )
                    rows.append(
                        dict(
                            adapter=name,
                            directory=directory,
                            status="failed" if remaining else "passed",
                            findings=remaining,
                            reported_findings=findings,
                            accepted=len(findings) - len(remaining),
                        )
                    )
                    continue
                if kind not in binaries:
                    binaries[kind] = tools_path(root, kind, gc_root=roots / kind)
                bin_path = binaries[kind]
                if kind == "rust":
                    result = execute(
                        [
                            str(bin_path / "cargo-deny"),
                            "--manifest-path",
                            str(cwd / "Cargo.toml"),
                            "check",
                            "advisories",
                        ]
                    )
                elif kind == "go":
                    result = execute([str(bin_path / "govulncheck"), "./..."])
                else:
                    with tempfile.TemporaryDirectory(
                        dir=tc.environment(root).get("TMPDIR"), prefix="audit-"
                    ) as temporary:
                        requirements = str(Path(temporary) / "requirements.txt")
                        exported = execute(
                            [
                                "uv",
                                "export",
                                "--locked",
                                "--no-emit-project",
                                "--no-hashes",
                                "--format",
                                "requirements.txt",
                                "--output-file",
                                requirements,
                            ]
                        )
                        if exported.returncode:
                            raise ValueError("Locked Python dependency export failed")
                        result = execute(
                            [
                                str(bin_path / "pip-audit"),
                                "--disable-pip",
                                "--no-deps",
                                "-r",
                                requirements,
                            ]
                        )
                lane: Lane = dict(
                    adapter=name,
                    directory=directory,
                    status="passed" if result.returncode == 0 else "failed",
                    output=result.stdout + result.stderr,
                )
                if result.returncode:
                    lane["error"] = (
                        f"Native scanner exited with status {result.returncode}; "
                        "text output cannot establish whether assessment completed"
                    )
                rows.append(lane)
            except (
                OSError,
                ValueError,
                KeyError,
                subprocess.CalledProcessError,
            ) as error:
                rows.append(
                    dict(
                        adapter=name,
                        directory=directory,
                        status="failed",
                        error=str(error),
                    )
                )
    complete = bool(rows) and all(
        row["status"] in {"passed", "failed"} and "error" not in row for row in rows
    )
    report: Report = dict(
        schema=2,
        complete=complete,
        passed=complete and all(row["status"] == "passed" for row in rows),
        lanes=rows,
    )
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1
