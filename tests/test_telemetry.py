from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from energy_system.telemetry import (
    as_utc,
    assess_freshness,
    numeric_value,
)


class TelemetryFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 5, 0, 0, tzinfo=timezone.utc)

    def test_iso_z_is_normalized(self):
        self.assertEqual(
            as_utc("2026-09-05T00:00:00Z"),
            self.now,
        )

    def test_fresh_and_stale_are_distinguished(self):
        fresh = assess_freshness(
            "50", self.now - timedelta(seconds=30),
            max_age_seconds=60, now=self.now,
        )
        stale = assess_freshness(
            "50", self.now - timedelta(seconds=61),
            max_age_seconds=60, now=self.now,
        )
        self.assertTrue(fresh.usable)
        self.assertEqual(fresh.quality, "fresh")
        self.assertFalse(stale.usable)
        self.assertEqual(stale.quality, "stale")

    def test_invalid_and_future_values_are_not_usable(self):
        invalid = assess_freshness(
            "unknown", self.now, max_age_seconds=60, now=self.now,
        )
        future = assess_freshness(
            50, self.now + timedelta(minutes=5),
            max_age_seconds=60, now=self.now,
        )
        self.assertEqual(invalid.quality, "missing")
        self.assertFalse(invalid.usable)
        self.assertEqual(future.quality, "future")
        self.assertFalse(future.usable)

    def test_unknown_timestamp_can_be_used_but_is_marked(self):
        value, freshness = numeric_value(
            42, None, max_age_seconds=60, now=self.now,
        )
        self.assertEqual(value, 42.0)
        self.assertEqual(freshness.quality, "unknown")
        self.assertTrue(freshness.usable)


if __name__ == "__main__":
    unittest.main()
