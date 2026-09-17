"""Minimal X11 authentication projection for a verified helper container.

This module never starts project code or a display server. Its input is a bounded
read-only authority file; its output belongs to one bootstrap invocation.
"""

import os
from pathlib import Path
import re
import stat
import struct

LIMIT = 1024 * 1024


def display_number(display: str) -> bytes:
    match = re.fullmatch(r"(?:unix/|unix)?:([0-9]{1,5})(?:\.[0-9]+)?", display)
    if match is None:
        raise ValueError("X11 transport requires a local DISPLAY such as :0 or unix/:0")
    return str(int(match[1])).encode("ascii")


def authentication(data: bytes, display: str, hostname: str) -> bytes:
    number = display_number(display)
    if len(data) > LIMIT:
        raise ValueError("X11 authority exceeds the 1 MiB limit")
    offset = 0
    selected: bytes | None = None

    def word() -> int:
        nonlocal offset
        if offset + 2 > len(data):
            raise ValueError("Truncated X11 authority record")
        value = struct.unpack_from("!H", data, offset)[0]
        offset += 2
        return int(value)

    def field() -> bytes:
        nonlocal offset
        size = word()
        if offset + size > len(data):
            raise ValueError("Truncated X11 authority field")
        value = data[offset : offset + size]
        offset += size
        return value

    while offset < len(data):
        family = word()
        address, screen, protocol, secret = (field() for _ in range(4))
        if (
            (family == 65535 or (family == 256 and address == hostname.encode()))
            and screen == number
            and protocol == b"MIT-MAGIC-COOKIE-1"
            and len(secret) == 16
        ):
            record = struct.pack("!H", 65535)
            for value in (b"", number, protocol, secret):
                record += struct.pack("!H", len(value)) + value
            # Prefer a hostname-specific record over a wildcard record.
            if selected is None or family == 256:
                selected = record
    if selected is None:
        raise ValueError("No matching local MIT-MAGIC-COOKIE-1 authority for DISPLAY")
    return selected


def prepare(source: Path, destination: Path, display: str, hostname: str) -> None:
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("X11 authority must be a regular file")
        selected = authentication(handle.read(LIMIT + 1), display, hostname)
    descriptor = os.open(
        destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(selected)
