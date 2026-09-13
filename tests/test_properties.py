"""Bounded generated contracts with small oracles independent of the implementation.

These run in the ordinary unit gate, without network, Nix or package managers.
Fixed generation keeps the gate reproducible; Hypothesis still shrinks failures.
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path
import sys
import unittest

from hypothesis import given, settings, strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import configuration
import registry
import resources
import workflows

GENERATED = settings(max_examples=200, derandomize=True, deadline=None)
TEXT = st.text(alphabet="abc xyz_-", max_size=12)
STRINGS = st.lists(TEXT, max_size=4)
ENVIRONMENT = st.dictionaries(st.sampled_from(["A", "B", "C"]), TEXT)
TASK_LAYER = st.fixed_dictionaries(
    {},
    optional={
        "commands": st.lists(st.lists(TEXT, min_size=1, max_size=3), max_size=3),
        "setup": STRINGS,
        "environment": ENVIRONMENT,
        "transport": st.fixed_dictionaries(
            {}, optional={"ports": STRINGS, "host_access": st.booleans()}
        ),
    },
)


@st.composite
def workflow_graphs(draw):
    count = draw(st.integers(min_value=1, max_value=8))
    acyclic = draw(st.booleans())
    graph = {}
    for index in range(count):
        # DAGs supply successful closures; arbitrary graphs also contain cycles
        # and one possible missing name. Repeated edges and requests are allowed.
        choices = list(range(index if acyclic else count + 1))
        dependencies = (
            draw(st.lists(st.sampled_from(choices), max_size=6)) if choices else []
        )
        graph[f"task{index}"] = {
            "depends_on": [f"task{dependency}" for dependency in dependencies]
        }
    requests = draw(st.lists(st.integers(min_value=0, max_value=count), max_size=6))
    return graph, [f"task{index}" for index in requests]


class WorkflowProperties(unittest.TestCase):
    @GENERATED
    @given(workflow_graphs())
    def test_order_covers_only_requested_closure_once_after_dependencies(self, case):
        graph, requested = case
        original = deepcopy(graph)
        # Compute reachability with a worklist, then remove ready vertices. This
        # is independent of the implementation's recursive depth-first walk.
        reachable, pending = set(), list(requested)
        missing = False
        while pending:
            key = pending.pop()
            if key in reachable:
                continue
            reachable.add(key)
            if key not in graph:
                missing = True
            else:
                pending.extend(graph[key]["depends_on"])
        remaining = set(reachable)
        while not missing and remaining:
            ready = {
                key
                for key in remaining
                if not remaining.intersection(graph[key]["depends_on"])
            }
            if not ready:
                break
            remaining.difference_update(ready)
        if missing or remaining:
            with self.assertRaises(ValueError):
                workflows.order(graph, requested)
        else:
            result = workflows.order(graph, requested)
            self.assertEqual(set(result), reachable)
            self.assertEqual(len(result), len(reachable))
            positions = {key: index for index, key in enumerate(result)}
            for key in result:
                for dependency in graph[key]["depends_on"]:
                    self.assertLess(positions[dependency], positions[key])
            self.assertEqual(workflows.order(graph, requested), result)
        self.assertEqual(graph, original)


class CompositionProperties(unittest.TestCase):
    @GENERATED
    @given(st.lists(TASK_LAYER, min_size=1, max_size=6))
    def test_inheritance_matches_field_oracle_and_keeps_outputs_independent(
        self, layers
    ):
        # The reference handles this deliberately small task vocabulary by field;
        # it neither calls nor reimplements the production recursive merge.
        expected, origins, templates = {}, {}, {}
        for index, layer in enumerate(layers):
            location = f"templates.tasks.layer{index}"
            template = deepcopy(layer)
            if index:
                template["extends"] = f"layer{index - 1}"
            templates[f"layer{index}"] = template
            for key, value in layer.items():
                if key in {"environment", "transport"}:
                    if key not in expected:
                        expected[key] = {}
                        origins[key] = location
                    for child, item in value.items():
                        expected[key][child] = deepcopy(item)
                        origins[f"{key}.{child}"] = location
                    if expected[key]:
                        origins.pop(key, None)
                    elif not value:
                        origins[key] = location
                else:
                    expected[key] = deepcopy(value)
                    origins[key] = location
        source = {
            "schema": 3,
            "templates": {"tasks": templates},
            "tasks": {
                name: {"extends": f"layer{len(layers) - 1}"} for name in ("one", "two")
            },
        }
        before = deepcopy(source)
        compiled, actual_origins = configuration.compile(source)
        self.assertEqual(
            compiled, {"schema": 3, "tasks": {"one": expected, "two": expected}}
        )
        self.assertEqual(actual_origins, {"tasks.one": origins, "tasks.two": origins})
        self.assertEqual(source, before)
        # A caller can modify a returned task without editing its sibling, its
        # cached template or the source document, including lists nested in tables.
        one = compiled["tasks"]["one"]
        for key in ("commands", "setup"):
            if key in one:
                one[key].append(["changed"] if key == "commands" else "changed")
        if "environment" in one:
            one["environment"]["A"] = "changed"
        if "transport" in one and "ports" in one["transport"]:
            one["transport"]["ports"].append("changed")
        self.assertEqual(compiled["tasks"]["two"], expected)
        self.assertEqual(source, before)
        self.assertEqual(configuration.compile(source)[0]["tasks"]["one"], expected)

    @GENERATED
    @given(
        st.integers(min_value=1, max_value=30),
        st.sampled_from(tuple(configuration.FIELDS)),
    )
    def test_unused_inheritance_cycles_are_rejected(self, length, kind):
        templates = {
            f"n{i}": {"extends": f"n{(i + 1) % length}"} for i in range(length)
        }
        with self.assertRaisesRegex(ValueError, "inheritance cycle"):
            configuration.compile({"schema": 3, "templates": {kind: templates}})


NOW = datetime(2025, 1, 1, tzinfo=timezone.utc)
VERSION = st.tuples(*(st.integers(min_value=0, max_value=12) for _ in range(3)))
PUBLICATION = st.tuples(
    VERSION,
    st.sampled_from(
        [0, 1, 29 * 86400, 30 * 86400 - 1, 30 * 86400, 31 * 86400, 90 * 86400]
    ),
    st.booleans(),  # deprecated
    st.booleans(),  # prerelease
)


class SelectionProperties(unittest.TestCase):
    @GENERATED
    @given(
        VERSION,
        st.integers(min_value=1, max_value=365),
        st.integers(min_value=1, max_value=86400),
    )
    def test_every_observation_must_cross_the_age_boundary(
        self, version, days, seconds
    ):
        value = ".".join(map(str, version))
        cutoff = NOW - timedelta(days=days)
        releases = [
            registry.Release(value, cutoff - timedelta(seconds=seconds)),
            registry.Release(value, cutoff + timedelta(seconds=seconds)),
        ]
        policy = {"minimum_age_days": days}
        for ordered in (releases, list(reversed(releases))):
            with self.assertRaisesRegex(ValueError, "No eligible stable release"):
                registry.select("npm", ordered, policy, "sample", NOW)
            # Advancing by exactly the newer observation's offset admits it.
            self.assertEqual(
                registry.select(
                    "npm", ordered, policy, "sample", NOW + timedelta(seconds=seconds)
                ).version,
                value,
            )

    @GENERATED
    @given(
        st.lists(PUBLICATION, max_size=30),
        st.sampled_from([0, 1, 30, 60]),
        st.sampled_from(["npm", "pypi", "crates", "pub", "go", "maven"]),
    )
    def test_latest_eligible_version_matches_numeric_inventory(
        self, inventory, days, provider
    ):
        releases = [
            registry.Release(
                ".".join(map(str, version)) + ("-alpha.1" if prerelease else ""),
                NOW - timedelta(seconds=age),
                deprecated=deprecated,
            )
            for version, age, deprecated, prerelease in inventory
        ]
        # All observations of an exact version must be old enough. Numeric
        # triples avoid using the production version parser as our oracle.
        expected = {
            version
            for version, age, deprecated, prerelease in inventory
            if not prerelease
            and not deprecated
            and all(
                other_age >= days * 86400
                for other_version, other_age, _, other_pre in inventory
                if other_version == version and not other_pre
            )
        }
        for ordered in (releases, list(reversed(releases)), releases + releases):
            policy = {"minimum_age_days": days}
            if not expected:
                with self.assertRaisesRegex(ValueError, "No eligible stable release"):
                    registry.select(provider, ordered, policy, "sample", NOW)
            else:
                chosen = registry.select(provider, ordered, policy, "sample", NOW)
                self.assertEqual(chosen.version, ".".join(map(str, max(expected))))
                self.assertFalse(chosen.deprecated)


class ResourceProperties(unittest.TestCase):
    @GENERATED
    @given(
        st.integers(min_value=1, max_value=64),
        st.integers(min_value=1, max_value=128),
        st.integers(min_value=0, max_value=2**44),
        st.floats(
            min_value=2**-10, max_value=128, allow_nan=False, allow_infinity=False
        ),
    )
    def test_budget_matches_exact_capacity_and_is_monotone(
        self, cpus, maximum, memory, per_job
    ):
        policy = {"max_jobs": maximum, "memory_per_job_gib": per_job}
        capacity = Fraction(per_job) * resources.GIB
        # Random abundant memory mostly exercises the CPU cap. Deliberately
        # straddle a job's byte boundary as well, so rounding faults are tested.
        boundary = int(max(1, min(cpus, maximum) // 2) * capacity)
        for available in {memory, max(0, boundary - 1), boundary, boundary + 1}:
            expected = max(
                [
                    1,
                    *(
                        n
                        for n in range(1, min(cpus, maximum) + 1)
                        if n * capacity <= available
                    ),
                ]
            )
            jobs = resources.budget(policy, cpus, available)
            self.assertEqual(jobs, expected)
            self.assertLessEqual(jobs, resources.budget(policy, cpus + 1, available))
            self.assertLessEqual(
                jobs, resources.budget(policy, cpus, available + resources.GIB)
            )
        self.assertEqual(resources.budget(policy, cpus, None), min(cpus, maximum))


if __name__ == "__main__":
    unittest.main()
