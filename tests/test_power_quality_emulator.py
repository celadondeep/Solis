from __future__ import annotations

import unittest

from energy_system.power_quality_emulator import run_scenarios


class PowerQualityEmulatorTests(unittest.TestCase):
    def test_all_builtin_scenarios_match_expected_state(self):
        report = run_scenarios()
        self.assertGreaterEqual(len(report), 10)
        self.assertTrue(all(row["passed"] for row in report), report)

    def test_real_eimo_extremes_stay_in_regression_suite(self):
        rows = {row["scenario"]: row for row in run_scenarios()}
        self.assertEqual(rows["eimo_2026_09_07_0711"]["actual"], "critical")
        self.assertEqual(rows["eimo_2026_09_06_1725"]["actual"], "critical")


if __name__ == "__main__":
    unittest.main()
