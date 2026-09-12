"""Pure service addresses; engine DNS owns container address discovery."""

import hashlib
import re

import toolchain as tc


def owner(root, name):
    services = tc.config(root).get("services", {})
    visited = set()
    while True:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name) or name not in services:
            raise ValueError("Endpoint requires a declared service")
        if name in visited:
            raise ValueError("Service network cycle")
        visited.add(name)
        spec = services[name]
        peer = spec.get("network_service")
        if not peer:
            return name, spec
        name = peer


def alias(root, name):
    name, spec = owner(root, name)
    scope = "repository" if spec.get("scope") == "repository" else str(root.resolve())
    return "cm-" + hashlib.sha256((scope + "\0" + name).encode()).hexdigest()[:24]


def address(root, name, port, container):
    if not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise ValueError("Endpoint requires a TCP port from 1 to 65535")
    name, spec = owner(root, name)
    if container:
        return alias(root, name) + ":" + port
    if "container" not in spec:
        return "127.0.0.1:" + port
    matches = []
    for binding in spec["container"].get("ports", []):
        match = re.fullmatch(r"127\.0\.0\.1:([0-9]+):([0-9]+)(?:/tcp)?", binding)
        if match and int(match[2]) == int(port) and 1 <= int(match[1]) <= 65535:
            matches.append(match[1])
    if len(matches) != 1:
        raise ValueError(
            f"Service {name} needs one loopback TCP publication for port {port}"
        )
    return "127.0.0.1:" + matches[0]
