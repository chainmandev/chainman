"""Run-wide parallelism hints: formulas, inheritance, overrides and port blocks."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import parallelism
import recipes
import resources

GIB = resources.GIB


def unexpected() -> tuple[int, int | None]:
    raise AssertionError("inherited values must not be recomputed")


def no_port() -> int:
    raise AssertionError("no port block may be selected")


class FormulaTests(unittest.TestCase):
    def test_design_expected_values(self):
        for cpus, gib, mode, jobs, lanes in [
            (2, 8, "conservative", 1, 1),
            (2, 8, "aggressive", 2, 1),
            (7, 19, "conservative", 3, 1),
            (7, 19, "aggressive", 7, 2),
            (44, 256, "conservative", 22, 1),
            (44, 256, "aggressive", 44, 14),
        ]:
            with self.subTest(cpus=cpus, gib=gib, mode=mode):
                self.assertEqual(
                    parallelism.compute(mode, cpus, gib * GIB),
                    (jobs, lanes, max(1, jobs // lanes)),
                )

    def test_memory_bounds_jobs_and_lanes_with_configured_lane_memory(self):
        self.assertEqual(parallelism.compute("conservative", 16, 4 * GIB), (2, 1, 2))
        self.assertEqual(parallelism.compute("aggressive", 16, 6 * GIB), (4, 1, 4))
        self.assertEqual(
            parallelism.compute("aggressive", 30, 64 * GIB, lane_memory_gib=16),
            (30, 4, 7),
        )
        self.assertEqual(parallelism.compute("aggressive", 1, 0), (1, 1, 1))
        self.assertEqual(parallelism.compute("conservative", 9, None), (4, 1, 4))


class ApplyTests(unittest.TestCase):
    def apply(self, env, settings=None, cpus=7, gib=19, port=41200):
        return parallelism.apply(
            env, settings, detect=lambda: (cpus, gib * GIB), select=lambda: port
        )

    def test_host_nix_top_level_exports_all_values_and_banner(self):
        env = {"CHAINMAN_MODE": "host-nix"}
        banner = self.apply(env)
        self.assertEqual(
            parallelism.exported(env),
            {
                "CHAINMAN_PARALLELISM": "conservative",
                "CHAINMAN_JOBS": "3",
                "CHAINMAN_LANES": "1",
                "CHAINMAN_JOBS_PER_LANE": "3",
                "CHAINMAN_PORT_BASE": "41200",
            },
        )
        self.assertEqual(
            banner,
            "Chainman parallelism: conservative — jobs 3 (per lane 3), lanes 1, "
            "ports 41200–41299. Set CHAINMAN_PARALLELISM=aggressive to use more.",
        )

    def test_nested_commands_inherit_without_recomputing_or_banner(self):
        env = {"CHAINMAN_MODE": "host-nix"}
        self.apply(env, cpus=44, gib=256)
        inherited = dict(env)
        self.assertIsNone(parallelism.apply(env, detect=unexpected, select=no_port))
        self.assertEqual(env, inherited)

    def test_aggressive_mode_is_opt_in(self):
        env = {"CHAINMAN_MODE": "container-nix", "CHAINMAN_PARALLELISM": "aggressive"}
        banner = self.apply(env, settings={"lane_memory_gib": 9})
        self.assertEqual(
            (
                env["CHAINMAN_JOBS"],
                env["CHAINMAN_LANES"],
                env["CHAINMAN_JOBS_PER_LANE"],
            ),
            ("7", "2", "3"),
        )
        self.assertNotIn("Set CHAINMAN_PARALLELISM", banner or "")

    def test_explicit_values_win_and_derive_the_rest(self):
        env = {
            "CHAINMAN_MODE": "host-nix",
            "CHAINMAN_JOBS": "10",
            "CHAINMAN_LANES": "3",
        }
        self.apply(env, cpus=1, gib=1)
        self.assertEqual(env["CHAINMAN_JOBS"], "10")
        self.assertEqual(env["CHAINMAN_JOBS_PER_LANE"], "3")
        env = {
            "CHAINMAN_MODE": "host-nix",
            "CHAINMAN_JOBS": "4",
            "CHAINMAN_LANES": "2",
            "CHAINMAN_JOBS_PER_LANE": "4",
            "CHAINMAN_PORT_BASE": "30000",
        }
        self.assertIsNone(parallelism.apply(env, detect=unexpected, select=no_port))
        self.assertEqual(env["CHAINMAN_JOBS_PER_LANE"], "4")
        env = {"CHAINMAN_MODE": "host-nix", "CHAINMAN_PORT_BASE": "30000"}
        self.assertIn("ports 30000–30099", self.apply(env, port=1) or "")

    def test_invalid_values_fail_clearly(self):
        for env, message in [
            ({"CHAINMAN_PARALLELISM": "turbo"}, "CHAINMAN_PARALLELISM"),
            ({"CHAINMAN_JOBS": "0"}, "CHAINMAN_JOBS must"),
            ({"CHAINMAN_LANES": "two"}, "CHAINMAN_LANES must"),
            (
                {"CHAINMAN_MODE": "host-nix", "CHAINMAN_PORT_BASE": "65500"},
                "CHAINMAN_PORT_BASE",
            ),
        ]:
            with self.subTest(env=env), self.assertRaisesRegex(ValueError, message):
                self.apply(env)
        for setting in (0, -1, float("inf"), True, "6"):
            with self.subTest(setting=setting), self.assertRaises(ValueError):
                self.apply({}, settings={"lane_memory_gib": setting})

    def test_container_and_bare_host_modes_leave_ports_unset(self):
        for env in (
            {"CHAINMAN_MODE": "container-nix"},
            {"CHAINMAN_MODE": "host-nix", "TOOLCHAIN_CONTAINER": "1"},
            {"CHAINMAN_MODE": "host"},
        ):
            with self.subTest(env=env):
                banner = parallelism.apply(
                    env, detect=lambda: (7, 19 * GIB), select=no_port
                )
                self.assertNotIn("CHAINMAN_PORT_BASE", env)
                self.assertIn("ports fixed.", banner or "")

    def test_recipe_plan_exports_one_computation_to_every_task(self):
        cfg = {"tasks": {"a": {}, "b": {}}, "recipes": {"verify-lite": ["a", "b"]}}
        body = recipes.plan(cfg, "verify-lite", {"CHAINMAN_JOBS": "3"})
        lines = body.splitlines()
        self.assertEqual(lines[2], "export CHAINMAN_JOBS=3")
        self.assertTrue(lines[3].endswith("run a"))


class PortBlockTests(unittest.TestCase):
    def test_collision_selects_another_block(self):
        choices = iter([41200, 41200, 25000])
        base = parallelism.port_block(
            free=lambda port: not 41200 <= port < 41300,
            choose=lambda _: next(choices),
        )
        self.assertEqual(base, 25000)

    def test_no_free_block_is_a_clear_error(self):
        with self.assertRaisesRegex(ValueError, "No free 100-port loopback block"):
            parallelism.port_block(free=lambda port: port % 100 != 57)

    def test_selected_block_is_aligned_inside_the_range(self):
        base = parallelism.port_block(free=lambda port: True)
        self.assertEqual(base % 100, 0)
        self.assertTrue(20000 <= base <= 59900)

    def test_bound_loopback_port_is_not_free(self):
        import socket

        with socket.socket() as held:
            held.bind(("127.0.0.1", 0))
            held.listen()
            self.assertFalse(parallelism.bindable(held.getsockname()[1]))


if __name__ == "__main__":
    unittest.main()
