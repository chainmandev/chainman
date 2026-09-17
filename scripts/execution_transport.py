"""Compose declarative container access without reading credentials or running tools."""

from collections.abc import Mapping
import os
from pathlib import Path

from adapter_data import Table, array, strings, table, text
import project_environment


def layers(
    cfg: Mapping[str, object], spec: Mapping[str, object], *, profile: str | None = None
) -> list[tuple[str, Table]]:
    result = [("container", table(cfg.get("container", {}), "Container transport"))]
    if "container" not in spec:
        name = profile or text(
            spec.get(
                "profile",
                table(cfg.get("project", {}), "Project").get(
                    "default_profile", "default"
                ),
            ),
            "Profile",
        )
        selected = table(
            table(cfg.get("profiles", {}), "Profiles").get(name, {}), "Profile"
        )
        result.append(
            (
                f"profiles.{name}.transport",
                table(selected.get("transport", {}), "Profile transport"),
            )
        )
    result.append(
        ("execution.transport", table(spec.get("transport", {}), "Execution transport"))
    )
    return result


def compose(declarations: list[tuple[str, Table]]) -> Table:
    mounts: list[object] = []
    targets: dict[str, Table] = {}
    ports: list[str] = []
    bindings: dict[str, str] = {}
    result: Table = {"mounts": mounts, "ports": ports}
    for origin, spec in declarations:
        project_environment.transport(spec)
        for raw in array(spec.get("mounts", []), "Mounts"):
            mount = dict(table(raw, "Mount"))
            mount.setdefault("read_only", True)
            mount.setdefault("optional", False)
            target = text(
                mount.get("target", "env:" + str(mount.get("source_env", ""))), "Target"
            )
            if target in targets and targets[target] != mount:
                raise ValueError(
                    f"Conflicting transport mount target {target} at {origin}"
                )
            if target not in targets:
                targets[target] = mount
                mounts.append(mount)
        for port in strings(spec.get("ports", []), "Ports"):
            port = canonical_port(port)
            host, _, protocol = port.partition("/")
            binding = ":".join(host.split(":")[:2]) + "/" + (protocol or "tcp")
            if binding in bindings and bindings[binding] != port:
                raise ValueError(
                    f"Conflicting transport port binding {binding} at {origin}"
                )
            bindings[binding] = port
            if port not in ports:
                ports.append(port)
        if spec.get("host_access"):
            result["host_access"] = True
        if spec.get("display"):
            result["display"] = spec["display"]
    return result


def canonical_port(port: str) -> str:
    value = project_environment.transport_port(port)
    return value if "/" in value else value + "/tcp"


def equivalent(left: Mapping[str, object], right: Mapping[str, object]) -> bool:
    """Ignore independent ordering; preserve ordering where mounts overlap."""

    def signature(value: Mapping[str, object]) -> tuple[object, ...]:
        normalized = compose([("comparison", dict(value))])
        mounts = [table(m, "Mount") for m in array(normalized["mounts"], "Mounts")]
        identities = [tuple(sorted(m.items())) for m in mounts]
        targets = [
            str(m.get("target", "env:" + str(m.get("source_env", "")))) for m in mounts
        ]
        overlaps = []
        for index, target in enumerate(targets):
            for other in range(index + 1, len(targets)):
                # An implicit target is not resolved by credential inspection.
                if (
                    target.startswith("env:")
                    or targets[other].startswith("env:")
                    or target.startswith(targets[other].rstrip("/") + "/")
                    or targets[other].startswith(target.rstrip("/") + "/")
                ):
                    overlaps.append((identities[index], identities[other]))
        return (
            frozenset(identities),
            frozenset(overlaps),
            frozenset(strings(normalized["ports"], "Ports")),
            bool(normalized.get("host_access")),
            normalized.get("display"),
        )

    return signature(left) == signature(right)


def effective(
    cfg: Mapping[str, object], spec: Mapping[str, object], *, profile: str | None = None
) -> Table:
    return compose(layers(cfg, spec, profile=profile))


def external_options(root: Path) -> Table:
    selected = os.environ.get("CHAINMAN_INSPECTION_OPTIONS") or os.environ.get(
        "CHAINMAN_CONTAINER_OPTIONS_FILE"
    )
    if not selected:
        return {"status": "not supplied", "options": []}
    path = Path(selected)
    if not path.is_absolute():
        path = root / path
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 1024 * 1024:
        return {
            "status": "unavailable",
            "source_env": "CHAINMAN_CONTAINER_OPTIONS_FILE",
        }
    lines = iter(path.read_text().splitlines())
    options: list[object] = []
    for option in lines:
        if not option:
            continue
        if option not in {
            "--mount",
            "--volume",
            "-v",
            "--publish",
            "-p",
            "--network",
            "--platform",
            "--add-host",
            "--hostname",
            "--label",
            "--name",
        }:
            raise ValueError("Unsupported container option in inspection")
        value = next(lines, None)
        if value is None:
            raise ValueError("Container option lacks a value")
        options.append(
            {
                "option": option,
                "value": value
                if option
                in {
                    "--mount",
                    "--volume",
                    "-v",
                    "--publish",
                    "-p",
                    "--network",
                    "--platform",
                    "--add-host",
                }
                else "<redacted>",
            }
        )
    return {
        "status": "supplied",
        "source_env": "CHAINMAN_CONTAINER_OPTIONS_FILE",
        "options": options,
    }


def inspect(
    root: Path,
    cfg: Mapping[str, object],
    spec: Mapping[str, object],
    *,
    profile: str | None = None,
    label: str = "execution",
) -> Table:
    declarations = layers(cfg, spec, profile=profile)
    combined = compose(declarations)
    # Values selected by environment names may be credentials themselves. Report
    # names and resolution state, never their values or the contents of paths.
    records: list[object] = []
    on_host = (
        os.environ.get("CHAINMAN_ACTIVE_MODE") != "container-nix"
        and os.environ.get("CHAINMAN_BOOTSTRAP_CONTAINER") != "1"
    )
    for origin, declaration in declarations:
        if not declaration:
            continue
        record: Table = {
            "origin": origin.replace("execution", label),
            "declaration": declaration,
        }
        mounts: list[object] = []
        for raw in array(declaration.get("mounts", []), "Mounts"):
            mount = table(raw, "Mount")
            source = (
                os.environ.get(str(mount["source_env"]))
                if "source_env" in mount
                else str(mount["source"])
            )
            path = Path(source) if source else None
            if path is not None and not path.is_absolute():
                path = root / path
            state = (
                "not checked on host"
                if not on_host
                else "unresolved"
                if path is None
                else "present"
                if path.exists()
                else "omitted"
                if mount.get("optional")
                else "missing"
            )
            mounts.append(dict(mount, status=state))
        record["mounts"] = mounts
        records.append(record)
    return {
        "applies_in": "container-nix",
        "mount_status_context": "host filesystem"
        if on_host
        else "host filesystem unavailable",
        "effective": combined,
        "layers": records,
        "display": {"kind": combined.get("display"), "authentication": "not inspected"},
        "external_options": external_options(root),
    }
