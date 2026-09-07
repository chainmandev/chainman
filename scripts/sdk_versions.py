"""Coordinate explicitly managed language targets with fresh selected SDKs."""

from __future__ import annotations

from collections.abc import Mapping
import fnmatch
import json
from pathlib import Path
import re
import subprocess
import tempfile
import tomllib

from packaging.specifiers import SpecifierSet
from packaging.version import Version
from semantic_version import NpmSpec, Version as Semver

import manifests
from toolchain import contained, environment, managed_run, module

KINDS = {
    "pnpm": "javascript",
    "node": "javascript",
    "go": "go",
    "python": "python",
    "ruff": "python",
    "dart": "flutter",
    "swift": "swift",
    "jdk": "compose",
    "rust": "rust",
}


def number(value: str) -> Version:
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]+(?:\.[0-9]+){1,2}", value
    ):
        raise ValueError("SDK probe did not return a supported stable numeric version")
    return Version(value)


def extracted(output: str, pattern: str) -> Version:
    matches = re.findall(pattern, output, re.MULTILINE)
    if len(matches) != 1:
        raise ValueError("Missing or ambiguous selected SDK version probe")
    return number(matches[0])


def probe(root: Path, spec: dict) -> dict[str, Version]:
    env = environment(root)
    env["TOOLCHAIN_FRESH"] = "1"

    def command(argv, *, json_output=False):
        # Probe outside project manifests: pnpm must not dispatch the old
        # packageManager pin, and Gradle must not configure/build the project.
        with tempfile.TemporaryDirectory(prefix="toolchain-sdk-probe-") as cwd:
            result = managed_run(
                [
                    str(root / "scripts/enter.sh"),
                    spec["profile"],
                    "sh",
                    "-eu",
                    "-c",
                    'cd "$1"; shift; exec "$@"',
                    "sh",
                    cwd,
                    *argv,
                ],
                cwd=root,
                env=env,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        return result.stdout if json_output else result.stdout + "\n" + result.stderr

    profile = spec["profile"]
    if profile == "javascript":
        return {
            "node": extracted(
                command(["node", "--version"]), r"^v([0-9]+\.[0-9]+\.[0-9]+)$"
            ),
            "pnpm": extracted(
                command(["pnpm", "--version"]), r"^([0-9]+\.[0-9]+\.[0-9]+)$"
            ),
        }
    if profile == "python":
        return {
            "python": extracted(
                command(["python3", "--version"]), r"^Python ([0-9]+\.[0-9]+\.[0-9]+)$"
            )
        }
    if profile == "go":
        return {
            "go": extracted(
                command(["go", "version"]),
                r"^go version go([0-9]+\.[0-9]+(?:\.[0-9]+)?) \S+$",
            )
        }
    if profile == "swift":
        return {
            "swift": extracted(
                command(["swift", "--version"]),
                r"^(?:Apple )?Swift version ([0-9]+\.[0-9]+(?:\.[0-9]+)?) .+$",
            )
        }
    if profile == "rust":
        return {
            "rust": extracted(
                command(["rustc", "--version"]),
                r"^rustc ([0-9]+\.[0-9]+\.[0-9]+) \(.+\)$",
            )
        }
    if profile == "flutter":
        body = json.loads(
            command(["flutter", "--version", "--machine"], json_output=True)
        )
        bundled = number(body.get("dartSdkVersion"))
        actual = extracted(
            command(["dart", "--version"]),
            r"^Dart SDK version: ([0-9]+\.[0-9]+\.[0-9]+) .+$",
        )
        if bundled != actual or body.get("channel") != "stable":
            raise ValueError("Flutter must expose its own stable bundled Dart SDK")
        return {"dart": actual}
    if profile == "compose":
        actual = extracted(
            command(["javac", "-version"]), r"^javac ([0-9]+\.[0-9]+(?:\.[0-9]+)?)$"
        )
        java = extracted(
            command(["java", "-version"]),
            r'^(?:openjdk|java) version "([0-9]+\.[0-9]+(?:\.[0-9]+)?)".*$',
        )
        gradle = extracted(
            command(["gradle", "--version"]),
            r"^Launcher JVM:\s+([0-9]+\.[0-9]+(?:\.[0-9]+)?) .+$",
        )
        if actual.major != java.major or actual.major != gradle.major:
            raise ValueError(
                "Gradle launcher and selected Java compiler/runtime must use the same JDK major"
            )
        return {"jdk": actual}
    raise ValueError("Unsupported selected SDK probe profile")


def supported(kind: str, value: str, sdk: Version) -> bool:
    if kind == "pnpm":
        return value == "pnpm@" + str(sdk)
    if kind == "node":
        return Semver(str(sdk)) in NpmSpec(value)
    if kind == "python":
        return sdk in SpecifierSet(value)
    if kind == "dart":
        return Semver(str(sdk)) in NpmSpec(value)
    if kind == "ruff":
        if not re.fullmatch(r"py[0-9][0-9]+", value):
            return False
        return number(value[2] + "." + value[3:]) <= sdk
    if kind == "jdk":
        return value == str(sdk.major)
    return number(value) <= sdk


def replacement(target: dict, old: str, sdk: Version) -> str:
    kind = target["kind"]
    held = target.get("hold_value")
    reason = target.get("hold_reason")
    if held is not None or reason is not None:
        if (
            not isinstance(held, str)
            or not isinstance(reason, str)
            or not reason.strip()
        ):
            raise ValueError("SDK holds require an exact existing value and a reason")
        if old != held or not supported(kind, held, sdk):
            raise ValueError(
                "SDK hold disagrees with the manifest or refreshed SDK; retain a compatible Nix SDK explicitly"
            )
        return old
    expected = {
        "pnpm": "pnpm@" + str(sdk),
        "node": f">={sdk.major} <{sdk.major + 1}",
        "go": str(sdk),
        "python": f">={sdk.major}.{sdk.minor}",
        "ruff": f"py{sdk.major}{sdk.minor}",
        "dart": f">={sdk} <{sdk.major + 1}.0.0",
        "swift": f"{sdk.major}.{sdk.minor}",
        "jdk": str(sdk.major),
    }[kind]
    shapes = {
        "pnpm": r"pnpm@[0-9]+\.[0-9]+\.[0-9]+",
        "node": r">=[0-9]+ <[0-9]+",
        "go": r"[0-9]+\.[0-9]+(?:\.[0-9]+)?",
        "python": r">=[0-9]+\.[0-9]+",
        "ruff": r"py[0-9][0-9]+",
        "dart": r">=[0-9]+\.[0-9]+\.[0-9]+ <[0-9]+\.0\.0",
        "swift": r"[0-9]+\.[0-9]+",
        "jdk": r"[0-9]+",
    }
    if not re.fullmatch(shapes[kind], old):
        raise ValueError(
            "SDK target is not in its declared managed form; use an explicit reasoned hold"
        )
    if kind in ("node", "dart"):
        lower, upper = old.split(" ")
        if int(upper[1:].split(".")[0]) != int(lower[2:].split(".")[0]) + 1:
            raise ValueError(
                "Custom SDK compatibility ranges require an explicit reasoned hold"
            )
    # Updating the selected environment must not silently lower a source contract.
    if kind == "pnpm":
        floor = number(old.removeprefix("pnpm@"))
    elif kind == "ruff":
        floor = number(old[2] + "." + old[3:])
    else:
        digits = re.search(r"[0-9]+(?:\.[0-9]+)*", old)[0]
        floor = Version(digits)
    if floor > sdk:
        raise ValueError(
            "Refreshed SDK is older than the declared language/runtime target"
        )
    return expected


def rust_contract(path: Path, workspace: dict, sdk: Version) -> None:
    body = tomllib.loads(path.read_text())
    package = body.get("package")
    if package is None:
        return

    def field(name, default=None):
        value = package.get(name, default)
        if isinstance(value, Mapping):
            if dict(value) != {"workspace": True} or name not in workspace:
                raise ValueError("Unsupported inherited Rust language contract")
            return workspace[name]
        return value

    edition = field("edition", "2015")
    minimum = {"2015": "1.0", "2018": "1.31", "2021": "1.56", "2024": "1.85"}.get(
        edition
    )
    if minimum is None or sdk < number(minimum):
        raise ValueError("Selected Rust SDK does not support the declared edition")
    msrv = field("rust-version")
    if msrv is not None and sdk < number(msrv):
        raise ValueError("Selected Rust SDK is below the declared MSRV")


def synchronize(root: Path, selected: list[str], *, check: bool = False) -> dict:
    config = tomllib.loads(contained(root, "sdk-versions.toml").read_text())
    if config.get("schema") != 1:
        raise ValueError("Unsupported SDK target configuration schema")
    declared = config.get("targets", [])
    if not isinstance(declared, list) or any(not isinstance(t, dict) for t in declared):
        raise ValueError("SDK targets must be a list of tables")
    for target in declared:
        files = target.get("files")
        if (
            not isinstance(files, list)
            or not files
            or any(not isinstance(p, str) or not p.strip() for p in files)
        ):
            raise ValueError(
                "SDK target files must be a nonempty list of nonempty patterns"
            )
    unmanaged = config.get("unmanaged_modules", {})
    if not isinstance(unmanaged, dict) or any(
        not isinstance(reason, str) or not reason.strip()
        for reason in unmanaged.values()
    ):
        raise ValueError("Unmanaged SDK modules require explicit nonempty reasons")
    targets = [t for t in declared if t.get("module") in selected]
    owned = {t.get("module") for t in declared}
    if owned & unmanaged.keys():
        raise ValueError("SDK module cannot be both targeted and explicitly unmanaged")
    missing = set(selected) & set(KINDS.values()) - owned - unmanaged.keys()
    if missing:
        raise ValueError(
            "Selected built-in SDK module has no targets or explicit unmanaged reason: "
            + ", ".join(sorted(missing))
        )
    specs, versions, bodies, seen = {}, {}, {}, set()
    python_ranges, ruff_targets = {}, {}
    for target in targets:
        name, kind = target["module"], target.get("kind")
        if kind not in KINDS:
            raise ValueError("Unsupported SDK target kind")
        spec = specs.setdefault(name, module(name, root))
        if spec.get("profile") != KINDS[kind]:
            raise ValueError("SDK target and selected module profile disagree")
        if name not in versions:
            versions[name] = probe(root, spec)
        sdk = versions[name].get("python" if kind == "ruff" else kind)
        if not isinstance(sdk, Version):
            raise ValueError("Missing selected SDK probe evidence")
        paths = set()
        for pattern in target["files"]:
            contained(root, pattern)
            found = list(root.glob(pattern))
            if not found:
                raise ValueError("Declared SDK target pattern matched no files")
            paths.update(found)
        workspace = {}
        if kind == "rust":
            workspace = (
                tomllib.loads(contained(root, target["workspace"]).read_text())
                .get("workspace", {})
                .get("package", {})
            )
        for path in sorted(paths):
            relative = str(path.relative_to(root))
            contained(root, relative)
            if not path.is_file() or not any(
                fnmatch.fnmatchcase(relative, p) for p in spec.get("update_outputs", [])
            ):
                raise ValueError("SDK target is not a declared regular update output")
            if (relative, kind) in seen:
                raise ValueError("Duplicate SDK version target")
            seen.add((relative, kind))
            if kind == "rust":
                rust_contract(path, workspace, sdk)
                continue
            old_body = bodies.get(path, path.read_text())
            if kind in ("go", "swift", "jdk"):
                pattern = {
                    "go": r"(?m)^go (?P<value>[0-9.]+)$",
                    "swift": r"(?m)^// swift-tools-version: (?P<value>[0-9.]+)$",
                    "jdk": r"\bjvmToolchain\((?P<value>[0-9]+)\)",
                }[kind]
                matches = list(re.finditer(pattern, old_body))
                if len(matches) != 1:
                    raise ValueError(
                        "SDK target must match exactly one version declaration"
                    )
                match = matches[0]
                value = replacement(target, match["value"], sdk)
                bodies[path] = (
                    old_body[: match.start("value")]
                    + value
                    + old_body[match.end("value") :]
                )
            else:
                pointer = {
                    "pnpm": ["packageManager"],
                    "node": ["engines", "node"],
                    "python": ["project", "requires-python"],
                    "ruff": ["tool", "ruff", "target-version"],
                    "dart": ["environment", "sdk"],
                }[kind]
                document, render = manifests.document(path, body=old_body)
                old = manifests.lookup(document, pointer)
                value = replacement(target, old, sdk)
                if kind == "python":
                    python_ranges.setdefault(name, []).append(value)
                elif kind == "ruff":
                    ruff_targets[name] = number(value[2] + "." + value[3:])
                if value != old:
                    manifests.assign(document, pointer, value)
                    bodies[path] = render()
    for name, ruff in ruff_targets.items():
        for requirement in python_ranges.get(name, []):
            floors = [
                number(s.version.removesuffix(".*"))
                for s in SpecifierSet(requirement)
                if s.operator in (">", ">=", "==", "~=")
            ]
            if not floors or ruff > max(floors):
                raise ValueError(
                    "Ruff language target exceeds the declared Python floor; coordinate explicit compatibility holds"
                )
    changed = [
        str(p.relative_to(root)) for p, body in bodies.items() if body != p.read_text()
    ]
    if check and changed:
        raise ValueError(
            "SDK declarations disagree with the refreshed selected profiles: "
            + ", ".join(changed)
        )
    if not check:
        for path, body in bodies.items():
            if path.read_text() != body:
                path.write_text(body)
    return {
        name: {kind: str(value) for kind, value in data.items()}
        for name, data in versions.items()
    }
