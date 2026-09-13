"""Named identities preserve the old tuple/JSON contract and reject bad records."""

import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from datetime import datetime, timezone

from hypothesis import given, settings, strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import dependency_identity as subject
import updates


class IdentityTests(unittest.TestCase):
    @settings(max_examples=150, derandomize=True, deadline=None)
    @given(st.tuples(*(st.text(max_size=30) for _ in range(5))))
    def test_named_fields_preserve_wire_order_and_set_membership(self, fields):
        value = subject.decode(fields)
        self.assertEqual(value.provider, fields[0])
        self.assertEqual(value.package, fields[1])
        self.assertEqual(value.version, fields[2])
        self.assertEqual(value.url, fields[3])
        self.assertEqual(value.digest, fields[4])
        self.assertEqual(value, fields)
        self.assertEqual(hash(value), hash(fields))
        wire = json.loads(json.dumps([value]))
        self.assertEqual(wire, [list(fields)])
        self.assertEqual(subject.inventory(wire + wire), {value})
        with self.assertRaises(AttributeError):
            value.version = "changed"

    def test_malformed_baseline_is_rejected_before_registry_work(self):
        good = ("npm", "sample", "1.0.0", "", "sha256:" + "a" * 64)
        for bad in (None, "abcde", {}, [[]], [good[:4]], [[*good[:4], None]], [42]):
            with (
                self.subTest(bad=bad),
                patch.object(updates.registry, "releases") as releases,
            ):
                with self.assertRaises(ValueError):
                    # Empty current inventory must still validate the baseline.
                    updates.audit_identities(
                        Path("."),
                        set(),
                        bad,
                        {},
                        datetime(2025, 1, 1, tzinfo=timezone.utc),
                    )
                releases.assert_not_called()


if __name__ == "__main__":
    unittest.main()
