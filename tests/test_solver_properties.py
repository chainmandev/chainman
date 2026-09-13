"""Compare bounded peer solving with exhaustive finite-domain enumeration.

The oracle uses integer membership and equality, not the production SemVer
parser, conflict selection or search order. Registry and workspace decoding are
deliberately outside this solver-only model and have separate native tests.
"""

from itertools import product
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

from hypothesis import example, given, settings, strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import javascript_updates as js


@st.composite
def peer_graphs(draw):
    count = draw(st.integers(min_value=2, max_value=4))
    domains = [tuple(range(1, draw(st.integers(1, 3)) + 1)) for _ in range(count)]
    peers = {}
    for source, domain in enumerate(domains):
        targets = [target for target in range(count) if target != source]
        for version in domain:
            peers[source, version] = draw(
                st.dictionaries(
                    st.sampled_from(targets),
                    st.sets(st.integers(1, 4), min_size=1, max_size=3),
                    max_size=len(targets),
                )
            )
    duplicate = draw(st.booleans())
    if duplicate:
        domains.append(
            tuple(sorted(draw(st.sets(st.sampled_from(domains[0]), min_size=1))))
        )
    return domains, peers, duplicate


def version_text(value):
    return f"{value}.0.0"


class PeerSolverProperties(unittest.TestCase):
    @settings(max_examples=200, derandomize=True, deadline=None)
    @given(peer_graphs())
    @example(
        (
            [(1, 2), (1, 2)],
            {(0, 1): {1: {2}}, (0, 2): {1: {1}}, (1, 1): {0: {1}}, (1, 2): {0: {1}}},
            False,
        )
    )
    @example(([(1,), (1,)], {(0, 1): {1: {2}}, (1, 1): {}}, False))
    @example(
        (
            [(1, 2), (1, 2), (1,)],
            {(0, 1): {1: {2}}, (0, 2): {}, (1, 1): {}, (1, 2): {0: {1}}},
            True,
        )
    )
    def test_solver_finds_a_solution_exactly_when_finite_oracle_does(self, graph):
        domains, peers, duplicate = graph
        count = len(domains) - int(duplicate)
        names = [f"package-{index}" for index in range(count)]
        slot_names = [*names, *([names[0]] if duplicate else [])]
        scopes = [dict(enumerate(range(count)))]
        duplicates = [(0, count)] if duplicate else []
        if duplicate:
            scopes.append({**scopes[0], 0: count})

        # Exhaust every assignment. No search heuristic or production constraint
        # helper participates in deciding satisfiability.
        solutions = set()
        for candidate in product(*domains):
            if any(candidate[left] != candidate[right] for left, right in duplicates):
                continue
            if all(
                candidate[scope[target]] in allowed
                for scope in scopes
                for source, slot in scope.items()
                for target, allowed in peers[source, candidate[slot]].items()
            ):
                solutions.add(tuple(map(version_text, candidate)))

        pins = [
            js.Pin(
                file=f"manifest-{index}.json",
                pointer=("dependencies", name),
                alias=name,
                name=name,
                original="*",
                prefix="",
                requirement="*",
                operator=None,
                candidates=[version_text(value) for value in reversed(domain)],
            )
            for index, (name, domain) in enumerate(
                zip(slot_names, domains, strict=True)
            )
        ]
        workspace = SimpleNamespace(
            pins=pins,
            duplicates=duplicates,
            locals=set(),
            refs={
                f"scope-{index}.json": {
                    names[name]: slot for name, slot in scope.items()
                }
                for index, scope in enumerate(scopes)
            },
        )
        metadata = {
            name: {
                version_text(version): {
                    "peerDependencies": {
                        names[target]: " || ".join(map(version_text, sorted(allowed)))
                        for target, allowed in peers[index, version].items()
                    }
                }
                for version in domains[index]
            }
            for index, name in enumerate(names)
        }

        class Evidence:
            policy = {}
            baseline = set()

            def get(self, name):
                return [], metadata[name]

            def peers(self, name, version, *, manifest):
                return metadata[name][version]["peerDependencies"], {}

        # At most 3**5 = 243 assignments; the bound cannot truncate this model.
        options = {"solver_states": 256}
        if not solutions:
            with self.assertRaisesRegex(ValueError, "No eligible JavaScript versions"):
                js.solve(workspace, Evidence(), options)
        else:
            selected = js.solve(workspace, Evidence(), options)
            self.assertIn(selected, solutions)
            self.assertEqual(js.solve(workspace, Evidence(), options), selected)


if __name__ == "__main__":
    unittest.main()
