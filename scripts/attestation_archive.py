"""Select expected signed bundle bytes from an untrusted GitHub ZIP."""

import io
import stat
import zipfile
from collections.abc import Sequence
from pathlib import PurePosixPath

from attestation_model import relative_path


def bundle_archive(data: bytes, expected: Sequence[str]) -> dict[str, bytes]:
    """Archive manifests and tarballs have no authority over local artifacts."""
    if len(set(expected)) != len(expected):
        raise ValueError("Ambiguous bundle basenames in local manifest")
    seen: set[str] = set()
    selected: dict[str, bytes] = {}
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for count, entry in enumerate(archive.infolist(), 1):
                directory = entry.orig_filename.endswith("/")
                name = relative_path(
                    entry.orig_filename[:-1] if directory else entry.orig_filename
                )
                if name in seen or count > 10000:
                    raise ValueError("Duplicate or excessive qualified archive entries")
                seen.add(name)
                mode = stat.S_IFMT(entry.external_attr >> 16)
                if mode not in (0, stat.S_IFDIR if directory else stat.S_IFREG):
                    raise ValueError(
                        "Qualified archive contains a symlink or special file"
                    )
                if entry.flag_bits & 1:
                    raise ValueError(
                        "Encrypted qualified archive entries are not supported"
                    )
                key = PurePosixPath(name).name
                if directory or key not in expected:
                    continue
                if (
                    key in selected
                    or entry.file_size > 16 * 1024 * 1024
                    or total + entry.file_size > 64 * 1024 * 1024
                ):
                    raise ValueError("Ambiguous or excessive qualified bundle bytes")
                total += entry.file_size
                with archive.open(entry) as stream:
                    value = stream.read(entry.file_size + 1)
                if len(value) != entry.file_size:
                    raise ValueError("ZIP bundle size mismatch")
                selected[key] = value
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError) as error:
        raise ValueError("Invalid qualified bundle ZIP archive") from error
    if len(selected) != len(expected):
        raise ValueError("Qualified archive is missing expected signed bundles")
    return selected
