"""Run-wide parallelism hints computed once and inherited by nested commands."""

from __future__ import annotations

from collections.abc import Callable, Mapping, MutableMapping
import random
import socket
import sys

import resources

MODES = ("conservative", "aggressive")
PORT_FIRST = 20000
PORT_LIMIT = 60000
PORT_BLOCK = 100
PORT_ATTEMPTS = 32
RANDOM = random.SystemRandom()
VARIABLES = (
    "CHAINMAN_PARALLELISM",
    "CHAINMAN_JOBS",
    "CHAINMAN_LANES",
    "CHAINMAN_JOBS_PER_LANE",
    "CHAINMAN_PORT_BASE",
)


def compute(
    mode: str, cpus: int, memory: int | None, lane_memory_gib: float = 6
) -> tuple[int, int, int]:
    """Return jobs, lanes and jobs per lane for logical CPUs and total memory."""
    gib = None if memory is None else memory / resources.GIB
    cpus = max(1, cpus)
    if mode == "aggressive":
        jobs = cpus if gib is None else min(cpus, int(gib / 1.5))
        lanes = cpus // 3 if gib is None else min(cpus // 3, int(gib / lane_memory_gib))
    else:
        jobs = cpus // 2 if gib is None else min(cpus // 2, int(gib / 2))
        lanes = 1
    jobs, lanes = max(1, jobs), max(1, lanes)
    return jobs, lanes, max(1, jobs // lanes)


def bindable(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def port_block(
    free: Callable[[int], bool] = bindable,
    choose: Callable[[range], int] = RANDOM.choice,
) -> int:
    """Select a random 100-port loopback block whose ports are all bindable now."""
    bases = range(PORT_FIRST, PORT_LIMIT, PORT_BLOCK)
    tried: set[int] = set()
    for _ in range(PORT_ATTEMPTS):
        base = choose(bases)
        if base in tried:
            continue
        tried.add(base)
        if all(free(port) for port in range(base, base + PORT_BLOCK)):
            return base
    raise ValueError(
        f"No free {PORT_BLOCK}-port loopback block found in "
        f"{PORT_FIRST}-{PORT_LIMIT - 1}; free ports or set CHAINMAN_PORT_BASE"
    )


def _positive(env: Mapping[str, str], name: str) -> int | None:
    if name not in env:
        return None
    value = resources.positive_integer(env[name])
    if value is None:
        raise ValueError(f"{name} must be a positive integer")
    return value


def apply(
    env: MutableMapping[str, str],
    settings: object = None,
    *,
    detect: Callable[[], tuple[int, int | None]] = lambda: resources.detected(
        total=True
    ),
    select: Callable[[], int] = port_block,
) -> str | None:
    """Fill unset values; return the run banner only when this call computed any."""
    mode = env.setdefault("CHAINMAN_PARALLELISM", "conservative")
    if mode not in MODES:
        raise ValueError("CHAINMAN_PARALLELISM must be conservative or aggressive")
    jobs = _positive(env, "CHAINMAN_JOBS")
    lanes = _positive(env, "CHAINMAN_LANES")
    per_lane = _positive(env, "CHAINMAN_JOBS_PER_LANE")
    computed = False
    if jobs is None or lanes is None:
        computed = True
        derived = compute(mode, *detect(), resources.lane_memory(settings or {}))
        jobs = derived[0] if jobs is None else jobs
        lanes = derived[1] if lanes is None else lanes
    if per_lane is None:
        computed = True
        per_lane = max(1, jobs // lanes)
    env.update(
        CHAINMAN_JOBS=str(jobs),
        CHAINMAN_LANES=str(lanes),
        CHAINMAN_JOBS_PER_LANE=str(per_lane),
    )
    ports = "fixed"
    # Container and bare-host workloads keep their fixed ports for now.
    if env.get("CHAINMAN_MODE") == "host-nix" and env.get("TOOLCHAIN_CONTAINER") != "1":
        base = _positive(env, "CHAINMAN_PORT_BASE")
        if base is None:
            computed = True
            base = select()
            env["CHAINMAN_PORT_BASE"] = str(base)
        elif base + PORT_BLOCK - 1 > 65535:
            raise ValueError("CHAINMAN_PORT_BASE must leave room for 100 ports")
        ports = f"{base}–{base + PORT_BLOCK - 1}"
    if not computed:
        return None
    hint = (
        " Set CHAINMAN_PARALLELISM=aggressive to use more."
        if mode == "conservative"
        else ""
    )
    return (
        f"Chainman parallelism: {mode} — jobs {jobs} (per lane {per_lane}), "
        f"lanes {lanes}, ports {ports}.{hint}"
    )


def announce(env: MutableMapping[str, str], settings: object = None) -> None:
    banner = apply(env, settings)
    if banner:
        print(banner, file=sys.stderr, flush=True)


def exported(env: Mapping[str, str]) -> dict[str, str]:
    """Values recorded in verification reports and inherited by gates."""
    return {name: env[name] for name in VARIABLES if name in env}
