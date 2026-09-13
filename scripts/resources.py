"""Resource-aware build concurrency without replacing ecosystem executables."""

from __future__ import annotations

import math
from collections.abc import MutableMapping
from dataclasses import dataclass
import os
from pathlib import Path
import platform
import re
import subprocess

GIB = 1024**3


def positive_integer(text: str) -> int | None:
    try:
        value = int(text)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def linux_limits(
    proc: Path = Path("/proc"), cgroups: Path = Path("/sys/fs/cgroup")
) -> tuple[int | None, int | None]:
    memory: list[int] = []
    cpu: list[int] = []
    for line in read(proc / "meminfo").splitlines():
        if line.startswith("MemAvailable:"):
            parts = line.split()
            if len(parts) > 1 and parts[1].isdecimal():
                memory.append(int(parts[1]) * 1024)
    paths = [cgroups]
    for line in read(proc / "self/cgroup").splitlines():
        if line.startswith("0::"):
            relative = Path(line[3:].lstrip("/"))
            if ".." not in relative.parts:
                current = cgroups / relative
                while current != cgroups:
                    paths.append(current)
                    current = current.parent
    for directory in paths:
        limit = read(directory / "memory.max")
        used = read(directory / "memory.current")
        if limit.isdecimal() and used.isdecimal():
            memory.append(max(0, int(limit) - int(used)))
        quota = read(directory / "cpu.max").split()
        if len(quota) == 2:
            amount, period = map(positive_integer, quota)
            if amount and period:
                cpu.append(max(1, math.ceil(amount / period)))
    # Retain support for the v1 container layout used by older engines.
    v1_limit = positive_integer(read(cgroups / "memory/memory.limit_in_bytes"))
    used = read(cgroups / "memory/memory.usage_in_bytes")
    if v1_limit and used.isdecimal():
        memory.append(max(0, v1_limit - int(used)))
    return (min(memory) if memory else None, min(cpu) if cpu else None)


def detected() -> tuple[int, int | None]:
    try:
        cpus = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        cpus = os.cpu_count() or 1
    memory = None
    if platform.system() == "Linux":
        memory, quota = linux_limits()
        if quota is not None:
            cpus = min(cpus, quota)
    elif platform.system() == "Darwin":
        try:
            memory = positive_integer(
                subprocess.check_output(
                    ["/usr/sbin/sysctl", "-n", "hw.memsize"], text=True, timeout=5
                )
            )
        except (OSError, subprocess.SubprocessError):
            pass
    return max(1, cpus), memory


@dataclass(frozen=True)
class Policy:
    maximum: int | None
    per_job: int | float | None
    variables: tuple[str, ...]


def policy(settings: object, *, require_variables: bool = True) -> Policy:
    """Decode once so resource detection and export consume validated values."""
    if not isinstance(settings, dict) or set(settings) - {
        "max_jobs",
        "memory_per_job_gib",
        "job_variables",
    }:
        raise ValueError("Unknown resource policy field")
    variables: object = settings.get("job_variables", [])
    if not isinstance(variables, list) or (
        settings and require_variables and not variables
    ):
        raise ValueError("Resource policy requires explicit job_variables")
    names: list[str] = []
    for variable in variables:
        if (
            not isinstance(variable, str)
            or not re.fullmatch("[A-Z][A-Z0-9_]*", variable)
            or variable.startswith(("CHAINMAN_", "TOOLCHAIN_"))
        ):
            raise ValueError("Invalid resource job variable")
        names.append(variable)
    maximum: int | None = None
    if "max_jobs" in settings:
        value: object = settings["max_jobs"]
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError("resources.max_jobs must be a positive integer")
        maximum = value
    per_job: object = settings.get("memory_per_job_gib")
    if per_job is not None and (
        not isinstance(per_job, (int, float))
        or isinstance(per_job, bool)
        or (isinstance(per_job, float) and not math.isfinite(per_job))
        or per_job <= 0
    ):
        raise ValueError("resources.memory_per_job_gib must be positive and finite")
    return Policy(maximum, per_job, tuple(names))


def _budget(policy: Policy, cpus: int, memory: int | None) -> int:
    jobs = min(policy.maximum or max(1, cpus), max(1, cpus))
    per_job = policy.per_job
    if per_job is not None and memory is not None:
        jobs = min(jobs, max(1, int(memory // (per_job * GIB))))
    return jobs


def budget(settings: object, cpus: int, memory: int | None) -> int:
    return _budget(policy(settings, require_variables=False), cpus, memory)


def validate(settings: object) -> None:
    policy(settings)


def apply(settings: object, env: MutableMapping[str, str]) -> None:
    configured = policy(settings)
    if not configured.variables:
        return
    for variable in configured.variables:
        if variable in env and positive_integer(env[variable]) is None:
            raise ValueError(
                f"{variable} must be a positive integer when explicitly configured"
            )
    jobs = _budget(configured, *detected())
    for variable in configured.variables:
        env.setdefault(variable, str(jobs))
