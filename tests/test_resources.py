"""Build budgets honor available memory, cgroups and explicit tool overrides."""

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import resources


class ResourceTests(unittest.TestCase):
    def test_memory_cpu_and_configured_maximum_bound_jobs(self):
        policy = {"max_jobs": 4, "memory_per_job_gib": 3}
        for cpus, memory, expected in [
            (16, 30 * resources.GIB, 4),
            (2, 30 * resources.GIB, 2),
            (16, 7 * resources.GIB, 2),
            (16, 0, 1),
            (16, None, 4),
        ]:
            self.assertEqual(resources.budget(policy, cpus, memory), expected)

    def test_nested_cgroup_and_parent_limits_are_both_applied(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            proc = root / "proc"
            cg = root / "cg"
            (proc / "self").mkdir(parents=True)
            (cg / "user/app").mkdir(parents=True)
            (proc / "meminfo").write_text("MemAvailable: 40000000 kB\n")
            (proc / "self/cgroup").write_text("0::/user/app\n")
            (cg / "user/memory.max").write_text(str(8 * resources.GIB))
            (cg / "user/memory.current").write_text(str(5 * resources.GIB))
            (cg / "user/app/memory.max").write_text(str(6 * resources.GIB))
            (cg / "user/app/memory.current").write_text(str(resources.GIB))
            (cg / "user/cpu.max").write_text("200000 100000")
            (cg / "user/app/cpu.max").write_text("max 100000")
            self.assertEqual(resources.linux_limits(proc, cg), (3 * resources.GIB, 2))

    def test_explicit_override_is_preserved_and_other_tool_gets_budget(self):
        env = {"CARGO_BUILD_JOBS": "7"}
        with patch.object(resources, "detected", return_value=(16, 6 * resources.GIB)):
            resources.apply(
                {
                    "job_variables": ["CARGO_BUILD_JOBS", "CMAKE_BUILD_PARALLEL_LEVEL"],
                    "memory_per_job_gib": 3,
                },
                env,
            )
        self.assertEqual(
            env, {"CARGO_BUILD_JOBS": "7", "CMAKE_BUILD_PARALLEL_LEVEL": "2"}
        )

    def test_ordinary_commands_do_not_probe_resources(self):
        with patch.object(resources, "detected") as detect:
            resources.apply({}, {})
        detect.assert_not_called()

    def test_invalid_policy_fails_without_exporting_values(self):
        for setting in [
            {"max_jobs": 0},
            {"memory_per_job_gib": float("nan")},
            {"memory_per_job_gib": -1},
        ]:
            env = {}
            with self.assertRaises(ValueError):
                resources.apply({"job_variables": ["CARGO_BUILD_JOBS"], **setting}, env)
            self.assertFalse(env)

    def test_falsey_non_tables_are_not_silently_treated_as_no_policy(self):
        for setting in (False, 0, "", [], None):
            env = {"KEEP": "unchanged"}
            with (
                self.subTest(setting=setting),
                patch.object(resources, "detected") as detect,
            ):
                with self.assertRaisesRegex(ValueError, "resource policy"):
                    resources.apply(setting, env)
                self.assertEqual(env, {"KEEP": "unchanged"})
                detect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
