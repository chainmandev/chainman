"""Resource-aware build concurrency without replacing ecosystem executables."""

from __future__ import annotations

import math
import os
from pathlib import Path
import platform
import re
import subprocess

GIB = 1024**3


def positive_integer(text):
    try:
        value = int(text)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def read(path):
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def linux_limits(proc=Path("/proc"), cgroups=Path("/sys/fs/cgroup")):
    memory, cpu = [], []
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
    limit = positive_integer(read(cgroups / "memory/memory.limit_in_bytes"))
    used = read(cgroups / "memory/memory.usage_in_bytes")
    if limit and used.isdecimal():
        memory.append(max(0, limit - int(used)))
    return (min(memory) if memory else None, min(cpu) if cpu else None)


def detected():
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


def budget(settings, cpus, memory):
    maximum = settings.get("max_jobs", cpus)
    per_job = settings.get("memory_per_job_gib")
    if type(maximum) is not int or maximum < 1:
        raise ValueError("resources.max_jobs must be a positive integer")
    jobs = min(maximum, max(1, cpus))
    if per_job is not None:
        if (
            type(per_job) not in (int, float)
            or not math.isfinite(per_job)
            or per_job <= 0
        ):
            raise ValueError("resources.memory_per_job_gib must be positive and finite")
        if memory is not None:
            jobs = min(jobs, max(1, int(memory // (per_job * GIB))))
    return jobs


def apply(settings, env):
    if not settings:
        return
    if not isinstance(settings, dict) or set(settings) - {
        "max_jobs",
        "memory_per_job_gib",
        "job_variables",
    }:
        raise ValueError("Unknown resource policy field")
    variables = settings.get("job_variables", [])
    if not isinstance(variables, list) or not variables:
        raise ValueError("Resource policy requires explicit job_variables")
    for variable in variables:
        if (
            not isinstance(variable, str)
            or not re.fullmatch("[A-Z][A-Z0-9_]*", variable)
            or variable.startswith(("CHAINMAN_", "TOOLCHAIN_"))
        ):
            raise ValueError("Invalid resource job variable")
        if variable in env and positive_integer(env[variable]) is None:
            raise ValueError(
                f"{variable} must be a positive integer when explicitly configured"
            )
    jobs = budget(settings, *detected())
    for variable in variables:
        env.setdefault(variable, str(jobs))
