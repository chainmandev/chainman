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
import toolchain as tc


class Lane(TypedDict):
    adapter: str
    status: Literal["unsupported", "failed", "passed"]
    directory: NotRequired[str]
    reason: NotRequired[str]
    findings: NotRequired[dict[str, str]]
    accepted: NotRequired[int]
    output: NotRequired[str]
    error: NotRequired[str]


class Report(TypedDict):
    schema: Literal[1]
    complete: bool
    passed: bool
    lanes: list[Lane]


def javascript(report: object) -> dict[str, str]:
    if not isinstance(report, dict) or report.get("error"):
        raise ValueError("Package audit did not return a successful report")
    document = ad.table(report, "Package report")
    if "advisories" in document:
        return {
            key: ad.text(
                ad.table(value, "Package advisory")["module_name"], "Package name"
            )
            for key, value in ad.table(
                document["advisories"], "Package advisories"
            ).items()
        }
    if "vulnerabilities" in document:
        findings: dict[str, str] = {}
        for raw in ad.table(document["vulnerabilities"], "Package inventory").values():
            value = ad.table(raw, "Package entry")
            for via in ad.array(value.get("via", []), "Package references"):
                if isinstance(via, dict):
                    entry = ad.table(via, "Package reference")
                    findings[str(entry["source"])] = ad.text(
                        entry["name"], "Package name"
                    )
        return findings
    raise ValueError("Package audit report has no recognized vulnerability inventory")


def evaluate(
    findings: Mapping[str, str],
    exceptions: Sequence[Mapping[str, object]],
    today: date | None = None,
) -> dict[str, str]:
    today = today or date.today()
    ignored = set()
    for entry in exceptions:
        if (
            set(entry) != {"id", "package", "reason", "review_after"}
            or not ad.text(entry["reason"], "Exception reason").strip()
        ):
            raise ValueError(
                "Audit exceptions require id, package, reason and review_after"
            )
        key = str(entry["id"])
        if key in ignored or findings.get(key) != entry["package"]:
            raise ValueError(
                "Audit exception is duplicate, stale or belongs to another package"
            )
        if (
            date.fromisoformat(ad.text(entry["review_after"], "Exception review date"))
            <= today
        ):
            raise ValueError("Audit exception requires review")
        ignored.add(key)
    return {key: package for key, package in findings.items() if key not in ignored}


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


def run_retained(root: Path, arguments: list[str], roots: Path) -> int:
    import recipes

    cfg = tc.config(root)
    settings = dependency_api.inspection_policy(root)
    adapters = ad.table(settings.get("adapters", {}), "Dependency adapters")
    selected, _ = dependency_api.selection(
        settings, recipes.selection_options(arguments)
    )
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
    unsupported = audit.get("unsupported", {})
    if not isinstance(unsupported, dict) or any(
        name not in adapters or not isinstance(reason, str) or not reason.strip()
        for name, reason in unsupported.items()
    ):
        raise ValueError("Unsupported audit declarations require an adapter and reason")
    unsupported_reasons = ad.string_map(unsupported, "Unsupported audit declarations")
    rows: list[Lane] = []
    binaries: dict[str, Path] = {}
    for name in adapters:
        if name not in selected:
            continue
        spec = dependency_api.configured(root, name, settings)
        kind = ad.text(spec["adapter"], "Adapter kind")
        if kind not in {
            "javascript",
            "rust",
            "python",
            "go",
            "flutter",
            "swift",
            "gradle",
        }:
            continue
        if kind in {"flutter", "swift", "gradle"}:
            rows.append(
                dict(
                    adapter=name,
                    status="unsupported",
                    reason=unsupported_reasons.get(
                        name,
                        "No shared vulnerability scanner is configured for this ecosystem",
                    ),
                )
            )
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
                    findings = javascript(json.loads(result.stdout))
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
                            "Cargo.toml",
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
                rows.append(
                    dict(
                        adapter=name,
                        directory=directory,
                        status="passed" if result.returncode == 0 else "failed",
                        output=result.stdout + result.stderr,
                    )
                )
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
    report: Report = dict(
        schema=1,
        complete=not any(row["status"] == "unsupported" for row in rows),
        passed=all(row["status"] == "passed" for row in rows),
        lanes=rows,
    )
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1
