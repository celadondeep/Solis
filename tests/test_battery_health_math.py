from __future__ import annotations

import unittest

from energy_system.battery_health_math import (
    battery_efficiency_percent,
    is_plausible_efficiency,
    plausible_efficiency_rows,
)


class BatteryHealthMathTests(unittest.TestCase):
    def test_energy_balance_uses_soc_change(self):
        self.assertAlmostEqual(
            battery_efficiency_percent(10, 6, 0.5, 3),
            95.0,
        )

    def test_impossible_efficiency_is_not_plausible(self):
        self.assertFalse(is_plausible_efficiency(118.6))
        self.assertTrue(is_plausible_efficiency(95.0))

    def test_bad_input_does_not_raise(self):
        self.assertIsNone(battery_efficiency_percent("unknown", 5, 1, 0))

    def test_explicitly_invalid_history_row_stays_excluded(self):
        rows = [
            {"efficiency": 95.0},
            {"efficiency": 118.6},
            {"efficiency": 94.0, "valid": False},
        ]
        self.assertEqual(plausible_efficiency_rows(rows), [{"efficiency": 95.0}])


if __name__ == "__main__":
    unittest.main()
