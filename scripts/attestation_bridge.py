"""Private boundary to pinned signing SDK and native npm metadata semantics."""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
import tempfile

from attestation_common import keys, object_value, parse, string
from attestation_model import Artifact
import toolchain as tc

MAX_RESPONSE = 16 * 1024 * 1024


def executable(variable: str) -> str:
    value = os.environ.get(variable, "")
    if not value or not Path(value).is_absolute() or not Path(value).is_file():
        raise ValueError(f"The pinned attest profile must supply {variable}")
    return value


def npm_executable() -> str:
    return executable("CHAINMAN_ATTEST_NPM")


def invoke(request: dict[str, object]) -> dict[str, object]:
    program = executable("CHAINMAN_ATTEST_ADAPTER")
    with tempfile.TemporaryDirectory(prefix="chainman-attest-adapter-") as temporary:
        response = Path(temporary) / "response.json"
        result = tc.managed_run(
            [program, "--response-file", str(response)],
            input=json.dumps(request),
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise ValueError(f"Attestation adapter failed ({result.returncode})")
        descriptor = os.open(response, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > MAX_RESPONSE
            ):
                raise ValueError("Invalid private attestation adapter response")
            content = stream.read(MAX_RESPONSE + 1)
        if len(content) > MAX_RESPONSE:
            raise ValueError("Attestation adapter response exceeds size limit")
        data = object_value(parse(content))
        if (
            type(data.get("protocol")) is not int
            or data["protocol"] != 1
            or data.get("operation") != request["operation"]
        ):
            raise ValueError("Attestation adapter response does not match the request")
        return data


def attest_provenance(artifact: Artifact) -> object:
    response = invoke(
        {
            "protocol": 1,
            "operation": "attest-provenance",
            "subject": {
                "name": artifact.subject,
                "digest": {"sha512": artifact.sha512},
            },
        }
    )
    keys(response, ["protocol", "operation", "bundle"])
    return object_value(response.get("bundle"))


@dataclass(frozen=True)
class PackageMetadata:
    manifest: dict[str, object]
    subject: str
    repository: str | None


def inspect_package(path: Path) -> PackageMetadata:
    response = invoke(
        {"protocol": 1, "operation": "npm-inspect", "tarball": str(path.absolute())}
    )
    keys(response, ["protocol", "operation", "manifest", "subject", "repository"])
    repository = response.get("repository")
    return PackageMetadata(
        object_value(response.get("manifest")),
        string(response.get("subject")),
        string(repository) if repository is not None else None,
    )
