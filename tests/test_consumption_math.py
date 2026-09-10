from __future__ import annotations

import unittest

from energy_system.consumption_math import (
    remaining_profile_ratio,
    robust_consumption_statistics,
)


class RemainingProfileTests(unittest.TestCase):
    def test_current_hour_is_prorated(self):
        profile = {str(hour): 1.0 for hour in range(24)}
        self.assertAlmostEqual(remaining_profile_ratio(profile, 12, 0), 0.5)
        self.assertAlmostEqual(
            remaining_profile_ratio(profile, 12, 30),
            11.5 / 24,
        )

    def test_end_of_day_approaches_zero(self):
        profile = {str(hour): 1.0 for hour in range(24)}
        self.assertLess(remaining_profile_ratio(profile, 23, 59, 59), 0.001)


class RobustConsumptionTests(unittest.TestCase):
    def test_meter_spikes_do_not_distort_weekday_factor(self):
        normal = [
            {"date": "2026-07-20", "value": 10, "raw_kwh": 10},
            {"date": "2026-07-21", "value": 11, "raw_kwh": 11},
            {"date": "2026-07-22", "value": 9, "raw_kwh": 9},
            {"date": "2026-07-23", "value": 10, "raw_kwh": 10},
            {"date": "2026-07-24", "value": 10, "raw_kwh": 10},
            {"date": "2026-07-25", "value": 12, "raw_kwh": 12},
            {"date": "2026-07-26", "value": 10, "raw_kwh": 10},
            {"date": "2026-07-27", "value": 11, "raw_kwh": 11},
            {"date": "2026-07-28", "value": 10, "raw_kwh": 10},
            {"date": "2026-07-29", "value": 10, "raw_kwh": 10},
            {"date": "2026-07-30", "value": 11, "raw_kwh": 11},
            {"date": "2026-07-31", "value": 9, "raw_kwh": 9},
            {"date": "2026-08-01", "value": 11, "raw_kwh": 11},
            {"date": "2026-08-02", "value": 10, "raw_kwh": 10},
            {"date": "2026-08-03", "value": 100, "raw_kwh": 100},
            {"date": "2026-08-09", "value": 50, "raw_kwh": 50},
        ]
        stats = robust_consumption_statistics(normal)
        self.assertEqual(
            stats["anomaly_days"],
            ["2026-08-03", "2026-08-09"],
        )
        self.assertLess(stats["weekday_factors"]["0"], 1.3)
        self.assertGreater(stats["daily_avg"], 8)
        self.assertLess(stats["daily_avg"], 13)

    def test_duplicate_date_counts_once_and_last_value_wins(self):
        stats = robust_consumption_statistics([
            {"date": "2026-08-01", "value": 10},
            {"date": "2026-08-01", "value": 12},
            {"date": "2026-08-02", "value": 10},
            {"date": "2026-08-03", "value": 11},
        ])
        self.assertEqual(stats["history_days"], 3)
        self.assertEqual(stats["usable_days"], 3)

    def test_invalid_samples_are_ignored(self):
        stats = robust_consumption_statistics([
            {"date": "2026-08-01", "value": 10},
            {"date": "bad", "value": 12},
            {"date": "2026-08-02", "value": -1},
            {"date": "2026-08-03", "value": 11},
            {"date": "2026-08-04", "value": 9},
        ])
        self.assertEqual(stats["history_days"], 3)


if __name__ == "__main__":
    unittest.main()
