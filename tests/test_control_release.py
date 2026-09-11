from datetime import datetime, timezone
import unittest

from scripts.control_release import check


class ControlReleaseTests(unittest.TestCase):
    def test_exact_maturity_boundary(self):
        sources = {
            "minimum_age_days": 30,
            "backend": {"version": "1.0.0", "published": "2026-08-17T23:01:55Z"},
        }
        with self.assertRaisesRegex(ValueError, "not mature"):
            check(sources, datetime(2026, 9, 16, 23, 1, 54, tzinfo=timezone.utc))
        check(sources, datetime(2026, 9, 16, 23, 1, 55, tzinfo=timezone.utc))
        with self.assertRaisesRegex(ValueError, "at least 30"):
            check(dict(sources, minimum_age_days=0), datetime.now(timezone.utc))
