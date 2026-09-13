"""Persisted checkpoint compatibility and decoding before operational decisions."""

from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import sys
import unittest

from hypothesis import given, settings, strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from transaction_state import State


def checkpoint():
    # A schema-1 checkpoint in the pre-typing, flat representation. This is an
    # independent wire fixture, not output from the serializer under test.
    return {
        "schema": 1,
        "root": "/original with spaces",
        "candidate": "/transaction/candidate",
        "identity": ["refs/heads/main", "original-commit"],
        "before": {"input.lock": "old-fingerprint"},
        "index": {"input.lock": ["100644", "old-object"]},
        "patterns": ["*.lock"],
        "options": {
            "format": False,
            "staged": False,
            "preview": False,
            "no_commit": False,
            "json": True,
            "message": "update selected dependencies",
            "only_chainman": False,
            "skip_chainman": True,
            "extra": ["--targets", "library"],
        },
        "runtime_files": ["chainman.lock"],
        "verify": ["run", "check"],
        "at": "2025-01-01T00:00:00+00:00",
        "source": False,
        "candidate_identity": ["refs/heads/main", "candidate-commit"],
        "candidate_before": {"input.lock": "candidate-fingerprint"},
        "candidate_index": {"input.lock": ["100755", "candidate-object"]},
        "candidate_modes": {"input.lock": 0o755},
        "candidate_git": {".git": "administration-fingerprint"},
    }


class TransactionStateTests(unittest.TestCase):
    @settings(max_examples=80, derandomize=True, deadline=None)
    @given(
        st.booleans(),
        st.booleans(),
        st.dictionaries(
            st.text(min_size=1, max_size=10), st.text(max_size=20), max_size=8
        ),
    )
    def test_old_checkpoint_round_trip_preserves_each_boundary(
        self, inspected, preview, snapshot
    ):
        wire = checkpoint()
        wire["options"]["preview"] = preview
        wire["before"] = snapshot
        if inspected:
            wire.update(updated={"input.lock": "new-fingerprint"}, paths=["input.lock"])
        untouched = deepcopy(wire)
        state = State.decode(wire)
        self.assertEqual(state.root, wire["root"])
        self.assertEqual(state.candidate, wire["candidate"])
        self.assertEqual(state.identity, ("refs/heads/main", "original-commit"))
        self.assertEqual(state.index, {"input.lock": ("100644", "old-object")})
        self.assertEqual(state.before, snapshot)
        self.assertEqual(
            state.candidate_identity, ("refs/heads/main", "candidate-commit")
        )
        self.assertEqual(
            state.candidate_index, {"input.lock": ("100755", "candidate-object")}
        )
        self.assertEqual(
            state.candidate_before, {"input.lock": "candidate-fingerprint"}
        )
        self.assertEqual(state.candidate_modes, {"input.lock": 0o755})
        self.assertEqual(state.at, datetime.fromisoformat(wire["at"]))
        self.assertEqual(state.options.extra, ["--targets", "library"])
        self.assertEqual(state.options.preview, preview)
        if inspected:
            self.assertEqual(state.require_inspection().updated, wire["updated"])
            self.assertEqual(state.require_inspection().paths, wire["paths"])
        else:
            with self.assertRaisesRegex(ValueError, "not been inspected"):
                state.require_inspection()
        self.assertEqual(json.loads(json.dumps(state.encode())), untouched)
        # Decoding owns its mutable leaves; later edits to parsed JSON cannot
        # retroactively change the admitted snapshot or selected arguments.
        wire["options"]["extra"].append("changed")
        wire["before"]["added"] = "changed"
        self.assertEqual(json.loads(json.dumps(state.encode())), untouched)

    def test_each_required_checkpoint_field_is_checked(self):
        for name in checkpoint().keys() - {"source"}:
            value = checkpoint()
            del value[name]
            with self.subTest(field=name), self.assertRaises(ValueError):
                State.decode(value)
        for name in checkpoint()["options"]:
            value = checkpoint()
            del value["options"][name]
            with self.subTest(option=name), self.assertRaises(ValueError):
                State.decode(value)

    def test_incomplete_inspection_and_invalid_scalar_shapes_are_rejected(self):
        for change in (
            {"schema": True},
            {"at": "2025-01-01"},
            {"identity": ["only-one"]},
            {"index": {"input.lock": ["100644", 42]}},
            {"candidate_modes": {"input.lock": True}},
            {"candidate_modes": {"input.lock": 0o100755}},
            {"before": {"input.lock": False}},
            {"verify": "run check"},
            {"paths": []},
            {"updated": {}},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                State.decode({**checkpoint(), **change})

    def test_staged_checkpoint_requires_its_selection_and_consistent_options(self):
        value = checkpoint()
        value["options"].update(format=True, staged=True, no_commit=True, extra=[])
        with self.assertRaises(ValueError):
            State.decode(value)
        value["selected"] = []
        self.assertEqual(State.decode(value).selected, [])
        for name, changed in (
            ("preview", True),
            ("no_commit", False),
            ("format", False),
            ("skip_chainman", False),
        ):
            invalid = deepcopy(value)
            invalid["options"][name] = changed
            with (
                self.subTest(option=name),
                self.assertRaisesRegex(ValueError, "inconsistent options"),
            ):
                State.decode(invalid)


if __name__ == "__main__":
    unittest.main()
