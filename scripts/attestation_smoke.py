"""Pack deterministic, disposable smoke bytes without contacting a registry."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPOSITORY = "chainmandev/chainman"
WORKFLOW = ".github/workflows/attestation-smoke.yml"
SMOKE_REF = "refs/heads/codex/attestation-smoke-20261109"
NAME = "@chainman/attestation-smoke"
VERSION = "0.0.0-smoke.0"
SUBJECT = f"pkg:npm/%40chainman/attestation-smoke@{VERSION}"


def write_json(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("pack", "create"))
    parser.add_argument("destination", type=Path)
    parser.add_argument("--commit", default=os.environ.get("GITHUB_SHA", ""))
    parser.add_argument("--run", default=os.environ.get("GITHUB_RUN_ID", ""))
    parser.add_argument("--attempt", default=os.environ.get("GITHUB_RUN_ATTEMPT", ""))
    parser.add_argument("--ref", default=os.environ.get("GITHUB_REF", ""))
    args = parser.parse_args()
    if (
        not re.fullmatch(r"[a-f0-9]{40}", args.commit)
        or not re.fullmatch(r"[1-9][0-9]*", args.run)
        or not re.fullmatch(r"[1-9][0-9]*", args.attempt)
        or args.ref != SMOKE_REF
    ):
        raise ValueError(
            "The fixture requires a full source commit, numeric run/attempt "
            "and the exact disposable smoke ref"
        )
    if args.action == "create" and any(
        os.environ.get(variable) != value
        for variable, value in (
            ("GITHUB_SHA", args.commit),
            ("GITHUB_RUN_ID", args.run),
            ("GITHUB_RUN_ATTEMPT", args.attempt),
            ("GITHUB_REF", args.ref),
        )
    ):
        raise ValueError("Signing fixture arguments must equal the Actions environment")
    npm = os.environ.get("CHAINMAN_ATTEST_NPM", "")
    if not npm or not Path(npm).is_absolute() or not Path(npm).is_file():
        raise ValueError("Run this fixture inside Chainman's pinned attest shell")
    output = args.destination.absolute()
    output.mkdir(mode=0o700)
    with tempfile.TemporaryDirectory(prefix=".fixture-", dir=output) as temporary:
        fixture = Path(temporary)
        disabled = "node -e \"throw new Error('Smoke fixture lifecycle must remain disabled')\""
        write_json(
            fixture / "package.json",
            {
                "name": NAME,
                "version": VERSION,
                "description": "Disposable Chainman attestation smoke fixture",
                "type": "module",
                "license": "MIT",
                "repository": {
                    "type": "git",
                    "url": f"git+https://github.com/{REPOSITORY}.git",
                },
                "files": ["index.js", "package.json"],
                "scripts": {
                    "prepack": disabled,
                    "prepare": disabled,
                    "prepublishOnly": disabled,
                    "publish": disabled,
                },
                "publishConfig": {"access": "public", "tag": "next"},
            },
        )
        (fixture / "index.js").write_text(
            "export const smoke = 'chainman-attestation-smoke';\n", encoding="utf-8"
        )
        for path in (fixture / "package.json", fixture / "index.js"):
            path.chmod(0o644)
        user_config = fixture / ".npmrc.user"
        global_config = fixture / ".npmrc.global"
        for path in (user_config, global_config):
            path.write_text("", encoding="utf-8")
        packed = subprocess.run(
            [
                npm,
                "pack",
                str(fixture),
                "--ignore-scripts",
                "--offline",
                "--update-notifier=false",
                f"--userconfig={user_config}",
                f"--globalconfig={global_config}",
                "--pack-destination",
                str(output),
                "--json",
            ],
            check=True,
            capture_output=True,
            text=True,
            cwd=fixture,
        )
        data = json.loads(packed.stdout)
        record = data[0] if isinstance(data, list) and len(data) == 1 else data[NAME]
        filename = record["filename"]
        if (
            not isinstance(filename, str)
            or Path(filename).name != filename
            or not filename.endswith(".tgz")
            or record["name"] != NAME
            or record["version"] != VERSION
        ):
            raise ValueError("Unexpected pinned npm pack result")
    tarball = output / filename
    digest = hashlib.sha512(tarball.read_bytes()).hexdigest()
    manifest = output / "manifest.json"
    policy = output / "policy.json"
    write_json(
        manifest,
        {
            "version": 1,
            "sourceCommit": args.commit,
            "artifacts": [
                {"path": filename, "subject": SUBJECT, "digest": {"sha512": digest}}
            ],
        },
    )
    write_json(
        policy,
        {
            "provider": "github-actions",
            "repository": REPOSITORY,
            "workflow": WORKFLOW,
            "ref": args.ref,
            "requiredJobs": ["SDK smoke"],
            "artifactName": "qualified-release-{runId}-{runAttempt}",
            "tag": "next",
        },
    )
    manifest.chmod(0o600)
    policy.chmod(0o600)
    if args.action == "create":
        subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                str(Path(__file__).resolve().with_name("isolated.py")),
                "attestation_entry.py",
                "attest",
                "create",
                str(manifest),
                "--run",
                args.run,
                "--policy",
                str(policy),
            ],
            check=True,
        )
    print(f"tarball={tarball}")
    print(f"sha512={digest}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.CalledProcessError,
    ) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
