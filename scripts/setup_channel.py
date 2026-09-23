"""Private foreground consent messages, including across container VM mounts."""

import os
from pathlib import Path
import shutil
import time
import uuid


def _write(path: Path, value: str) -> None:
    temporary = path.with_name(path.name + ".new")
    temporary.write_text(value)
    os.replace(temporary, path)


def request(channel: Path, question: str) -> bool:
    name = uuid.uuid4().hex
    incoming = channel / "incoming" / name
    incoming.mkdir(mode=0o700)
    try:
        _write(incoming / "alive", "0")
        _write(incoming / "question", question)
        previous = ""
        changed = time.monotonic()
        counter = 0
        while True:
            current = (channel / "outgoing/alive").read_text()
            if current != previous:
                previous, changed = current, time.monotonic()
            if not current or time.monotonic() - changed > 10:
                raise OSError("Setup consent owner stopped responding")
            response = channel / "outgoing" / name
            try:
                return response.read_text().strip() == "yes"
            except FileNotFoundError:
                pass
            counter += 1
            _write(incoming / "alive", str(counter))
            time.sleep(0.25)
    finally:
        shutil.rmtree(incoming, ignore_errors=True)
