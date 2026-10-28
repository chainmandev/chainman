"""Download this CI run's exact runtime artifact using the pinned Python client."""

import argparse
import hashlib
from http.client import HTTPMessage
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import IO
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import zipfile

import ci_runtime_closure as closure


MAX_ZIP_BYTES = closure.MAX_BYTES + 64 * 1024**2


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> None:
        return None


def headers(token: str = "") -> dict[str, str]:
    result = {
        "User-Agent": "chainman-runtime-transfer",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        result["Authorization"] = "Bearer " + token
    return result


def validate_metadata(
    value: object, artifact: int, run: int, candidate: str, digest: str
) -> int:
    if not isinstance(value, dict):
        raise ValueError("invalid runtime artifact metadata")
    workflow = value.get("workflow_run")
    size = value.get("size_in_bytes")
    if (
        type(value.get("id")) is not int
        or value["id"] != artifact
        or value.get("expired") is not False
        or value.get("digest") != "sha256:" + digest
        or not isinstance(workflow, dict)
        or type(workflow.get("id")) is not int
        or workflow["id"] != run
        or workflow.get("head_sha") != candidate
        or type(size) is not int
        or not 0 < size <= MAX_ZIP_BYTES
    ):
        raise ValueError("runtime artifact identity, digest, expiry or size mismatch")
    return size


def metadata(url: str, token: str) -> object:
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        with opener.open(Request(url, headers=headers(token)), timeout=60) as response:
            body = response.read(1024**2 + 1)
    except HTTPError as error:
        error.close()
        raise ValueError("could not read runtime artifact metadata") from None
    except URLError:
        raise ValueError("could not read runtime artifact metadata") from None
    if len(body) > 1024**2:
        raise ValueError("runtime artifact metadata exceeds the size limit")
    result: object = json.loads(body)
    return result


def download_zip(
    url: str, token: str, destination: Path, size: int, digest: str
) -> None:
    opener = build_opener(ProxyHandler({}), NoRedirect())
    for hop in range(5):
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or (hop == 0 and parsed.hostname != "api.github.com")
        ):
            raise ValueError("unsafe runtime artifact redirect")
        # The credential is sent only to the original GitHub API request.
        request = Request(url, headers=headers(token if hop == 0 else ""))
        try:
            with opener.open(request, timeout=60) as response:
                actual = hashlib.sha256()
                received = 0
                with destination.open("xb") as stream:
                    while block := response.read(1024**2):
                        received += len(block)
                        if received > size or received > MAX_ZIP_BYTES:
                            raise ValueError(
                                "runtime artifact download exceeds its declared size"
                            )
                        actual.update(block)
                        stream.write(block)
                if received != size or actual.hexdigest() != digest:
                    raise ValueError("runtime artifact ZIP size or SHA-256 mismatch")
                return
        except HTTPError as error:
            location = error.headers.get("Location")
            code = error.code
            error.close()
            if code not in {301, 302, 303, 307, 308} or not location:
                raise ValueError("runtime artifact download failed") from None
            url = location
        except URLError:
            raise ValueError("runtime artifact download failed") from None
    raise ValueError("too many runtime artifact redirects")


def extract_runtime(archive: Path, destination: Path, digest: str) -> None:
    with zipfile.ZipFile(archive) as bundle:
        members = bundle.infolist()
        if len(members) != 1:
            raise ValueError("expected exactly one runtime archive member")
        member = members[0]
        mode = stat.S_IFMT(member.external_attr >> 16)
        if (
            member.filename != closure.ARCHIVE
            or member.is_dir()
            or mode not in {0, stat.S_IFREG}
            or member.flag_bits & 1
            or not 0 < member.file_size <= closure.MAX_BYTES
        ):
            raise ValueError("invalid runtime archive member")
        with bundle.open(member) as source, destination.open("xb") as target:
            copied = 0
            while block := source.read(1024**2):
                copied += len(block)
                if copied > member.file_size or copied > closure.MAX_BYTES:
                    raise ValueError("runtime archive member exceeds its declared size")
                target.write(block)
        if copied != member.file_size or closure.archive_digest(destination) != digest:
            raise ValueError("runtime archive SHA-256 mismatch")


def download_runtime(
    directory: Path, candidate: str, artifact: str, nar_digest: str, zip_digest: str
) -> None:
    closure.check_source(closure.ROOT, candidate)
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    run = os.environ.get("GITHUB_RUN_ID", "")
    token = os.environ.get("GH_TOKEN", "")
    if (
        not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
        or not re.fullmatch(r"[1-9][0-9]*", run)
        or not re.fullmatch(r"[1-9][0-9]*", artifact)
        or not re.fullmatch(r"[0-9a-f]{64}", nar_digest)
        or not re.fullmatch(r"[0-9a-f]{64}", zip_digest)
        or not token
        or len(token) > 4096
        or not re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", token)
    ):
        raise ValueError("missing or invalid runtime transfer context")
    url = (
        "https://api.github.com/repos/" + repository + "/actions/artifacts/" + artifact
    )
    size = validate_metadata(
        metadata(url, token), int(artifact), int(run), candidate, zip_digest
    )
    if directory.exists() or directory.is_symlink():
        raise ValueError("runtime download destination already exists")
    directory.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="chainman-runtime-", dir=directory.parent
    ) as temporary:
        work = Path(temporary)
        archive = work / "artifact.zip"
        download_zip(url + "/zip", token, archive, size, zip_digest)
        ready = work / "ready"
        ready.mkdir()
        extract_runtime(archive, ready / closure.ARCHIVE, nar_digest)
        closure.check_source(closure.ROOT, candidate)
        ready.rename(directory)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("candidate")
    parser.add_argument("artifact")
    parser.add_argument("nar_digest")
    parser.add_argument("zip_digest")
    args = parser.parse_args()
    download_runtime(
        args.directory, args.candidate, args.artifact, args.nar_digest, args.zip_digest
    )
    print("Verified this run's exact runtime artifact and both archive digests")


if __name__ == "__main__":
    main()
