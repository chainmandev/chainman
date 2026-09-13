from datetime import datetime, timezone
import unittest

from scripts.control_release import check


class ControlReleaseTests(unittest.TestCase):
    def test_publication_record_requires_usable_dates_and_versions(self):
        now = datetime(2026, 10, 1, tzinfo=timezone.utc)
        for record in (
            None,
            {"version": 1, "published": "2026-08-17T23:01:55Z"},
            {"version": "1.0.0", "published": 0},
            {"version": "1.0.0", "published": "2026-08-17"},
        ):
            with self.subTest(record=record), self.assertRaises(ValueError):
                check({"minimum_age_days": 30, "backend": record}, now)

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
