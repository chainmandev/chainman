"""Pinned ecosystem audit tools, shared exception rules and explicit coverage."""

from datetime import date
import json
from pathlib import Path
import subprocess
import tempfile

import chainman
import dependency_api
import toolchain as tc


def javascript(report):
    if not isinstance(report, dict) or report.get("error"):
        raise ValueError("Package audit did not return a successful report")
    if "advisories" in report:
        return {
            str(key): value["module_name"]
            for key, value in report["advisories"].items()
        }
    if "vulnerabilities" in report:
        findings = {}
        for value in report["vulnerabilities"].values():
            for via in value.get("via", []):
                if isinstance(via, dict):
                    findings[str(via["source"])] = via["name"]
        return findings
    raise ValueError("Package audit report has no recognized vulnerability inventory")


def evaluate(findings, exceptions, today=None):
    today = today or date.today()
    ignored = set()
    for entry in exceptions:
        if (
            set(entry) != {"id", "package", "reason", "review_after"}
            or not entry["reason"].strip()
        ):
            raise ValueError(
                "Audit exceptions require id, package, reason and review_after"
            )
        key = str(entry["id"])
        if key in ignored or findings.get(key) != entry["package"]:
            raise ValueError(
                "Audit exception is duplicate, stale or belongs to another package"
            )
        if date.fromisoformat(entry["review_after"]) <= today:
            raise ValueError("Audit exception requires review")
        ignored.add(key)
    return {key: package for key, package in findings.items() if key not in ignored}


def tools_path(root, kind, *, gc_root):
    return (
        Path(
            tc.managed_run(
                [
                    tc.nix_command(),
                    "--extra-experimental-features",
                    "nix-command flakes",
                    "build",
                    f"path:{chainman.RUNTIME}/nix#audit-{kind}",
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


def run(root, arguments):
    with tc.nix_temporary_directory("chainman-audit-tools-") as directory:
        return run_retained(root, arguments, Path(directory))


def run_retained(root, arguments, roots):
    import recipes

    cfg = tc.config(root)
    settings = dependency_api.inspection_policy(root)
    selected, _ = dependency_api.selection(
        settings, recipes.selection_options(arguments)
    )
    audit = cfg.get("audits", {})
    if not isinstance(audit, dict) or set(audit) - {"exceptions", "unsupported"}:
        raise ValueError(
            "Audit configuration supports exceptions and unsupported declarations"
        )
    exceptions = audit.get("exceptions", {})
    if not isinstance(exceptions, dict):
        raise ValueError("Audit exceptions must be keyed by adapter")
    for name, entries in exceptions.items():
        if settings.get("adapters", {}).get(name, {}).get(
            "adapter"
        ) != "javascript" or not isinstance(entries, list):
            raise ValueError(
                "Shared audit exceptions require a declared JavaScript adapter; native scanners use their project policy"
            )
    unsupported = audit.get("unsupported", {})
    if not isinstance(unsupported, dict) or any(
        name not in settings.get("adapters", {})
        or not isinstance(reason, str)
        or not reason.strip()
        for name, reason in unsupported.items()
    ):
        raise ValueError("Unsupported audit declarations require an adapter and reason")
    rows, binaries = [], {}
    for name in settings.get("adapters", {}):
        if name not in selected:
            continue
        spec = dependency_api.configured(root, name, settings)
        kind = spec["adapter"]
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
                    reason=audit.get("unsupported", {}).get(
                        name,
                        "No shared vulnerability scanner is configured for this ecosystem",
                    ),
                )
            )
            continue
        directories = spec.get("directories", [spec.get("directory", ".")])
        for directory in directories:
            try:
                cwd = tc.contained(root, directory)

                def execute(argv):
                    result = chainman.execute(
                        root,
                        spec["profile"],
                        argv,
                        cwd=cwd,
                        env=tc.environment(root),
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    return result

                if kind == "javascript":
                    result = execute([spec.get("manager", "pnpm"), "audit", "--json"])
                    if result.returncode not in (0, 1):
                        raise ValueError("Package audit tool failed")
                    findings = javascript(json.loads(result.stdout))
                    remaining = evaluate(
                        findings, audit.get("exceptions", {}).get(name, [])
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
    report = dict(
        schema=1,
        complete=not any(row["status"] == "unsupported" for row in rows),
        passed=all(row["status"] == "passed" for row in rows),
        lanes=rows,
    )
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1
