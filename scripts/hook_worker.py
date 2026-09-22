"""Managed hook preparation and tools. Real-repository Git belongs to the host."""

import base64
import json
from pathlib import Path
import subprocess

import chainman
import configuration_files
import config_inspection
import formatters
import hooks
import staged_format
import toolchain as tc
from adapter_data import strings, table


def export(root: Path, args: list[str]) -> int:
    if len(args) != 5:
        raise ValueError("Invalid native hook export")
    output, target, git, launcher, action = args
    destination = Path(output)
    if (
        not destination.is_absolute()
        or destination.is_symlink()
        or not destination.is_dir()
    ):
        raise ValueError("Native hooks require a private output directory")
    if target not in {"linux-arm64", "linux-amd64", "darwin-arm64", "darwin-amd64"}:
        raise ValueError("Unsupported native hook platform")
    cfg = config_inspection.validated(root)
    enabled = hooks.declaration(cfg).get("enabled", False)
    tc.atomic_bytes(destination / "enabled", b"1\n" if enabled else b"0\n")
    if action == "setup" and not enabled:
        return 0
    name = "chainman.toml" if (root / "chainman.toml").exists() else "toolchain.toml"
    authority = configuration_files.read(root, name).documents
    if (root / "chainman.lock").exists():
        authority["chainman.lock"] = tc.regular_input(root, "chainman.lock")
    config = hooks.effective(root, destination)
    packages = [("task", "chainman-control")]
    if action in {"hooks", "setup"}:
        packages.append(("hook-lefthook", "lefthook"))
    for package, executable in packages:
        with tc.nix_temporary_directory("chainman-hook-export-") as temporary:
            store = subprocess.run(
                [
                    tc.nix_command(),
                    "--extra-experimental-features",
                    "nix-command flakes",
                    "build",
                    tc.nix_path_reference(
                        chainman.RUNTIME / "nix", f"{package}-{target}"
                    ),
                    "--out-link",
                    str(Path(temporary) / "package"),
                    "--print-out-paths",
                    "--no-write-lock-file",
                ],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.strip()
            if not store.startswith("/nix/store/") or "\n" in store:
                raise ValueError("Invalid native hook package output")
            source = Path(store) / "bin" / executable
            if source.is_symlink() or not source.is_file():
                raise ValueError("Native hook output must be a regular executable")
            tc.atomic_bytes(destination / executable, source.read_bytes(), 0o700)
    tc.atomic_json(
        destination / "plan.json",
        {
            "root": str(root),
            "git": git,
            "launcher": launcher,
            "lefthook": str(destination / "lefthook"),
            "directory": str(destination),
            "enabled": enabled,
            "config": str(config),
            "authority": {
                name: base64.b64encode(body).decode()
                for name, body in authority.items()
            },
        },
    )
    return 0


def run(root: Path, args: list[str]) -> int:
    if len(args) != 2:
        raise ValueError("Invalid managed hook phase")
    phase, raw = args
    directory = Path(raw)
    if not directory.is_absolute() or directory.is_symlink() or not directory.is_dir():
        raise ValueError("Hook phase requires a real operation directory")
    if phase == "format":
        inputs = table(
            json.loads(tc.regular_input(directory, "input.json")),
            "Hook formatting inputs",
        )
        cfg = tc.config(root)
        paths = strings(inputs["paths"], "Changed staged paths")
        selected = sorted(
            {
                path
                for spec in formatters.declarations(cfg).values()
                for path in formatters.selected(spec, paths)
            }
        )
        if not formatters.declarations(cfg):
            raise ValueError("Staged formatting needs [formatters.NAME] declarations")
        before = staged_format.inventory(root)

        def prepared() -> None:
            nonlocal before
            installed = staged_format.inventory(root)
            if any(installed.get(path) != value for path, value in before.items()):
                raise ValueError("Formatter setup changed staged source files")
            before = installed

        formatters.execute(root, cfg, selected, prepared=prepared)
        after = staged_format.inventory(root)
        touched = sorted(
            path
            for path in before.keys() | after.keys()
            if before.get(path) != after.get(path)
        )
        if any(
            path not in selected
            or path not in after
            or before[path][1] != after[path][1]
            for path in touched
        ):
            raise ValueError(
                f"Formatter changed files outside selected content: {touched!r}"
            )
        tc.atomic_json(
            directory / "result" / "manifest.json",
            {"touched": touched, "selected": selected},
        )
        return 0
    if phase in {"scan-select", "scan-check"}:
        import trojan_source

        return trojan_source.worker(root, directory, phase)
    raise ValueError("Unknown managed hook phase")


if __name__ == "__main__":
    import sys

    selected = Path(sys.argv[1])
    if sys.argv[2] == "export":
        raise SystemExit(export(selected, sys.argv[3:]))
    raise SystemExit(run(selected, sys.argv[2:]))
