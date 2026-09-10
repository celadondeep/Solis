from __future__ import annotations

import unittest

from energy_system.power_quality import (
    PowerQualityInput,
    analyze_history,
    as_float,
    evaluate_power_quality,
    magnitude_spread_pct,
    phase_deviation_pct,
)


def observation(**changes):
    values = {
        "pcc_voltage_v": [230.0, 231.0, 229.0],
        "inverter_voltage_v": [232.0, 233.0, 231.0],
        "pcc_active_w": [1000.0, 990.0, 1010.0],
        "pcc_reactive_var": [50.0, 50.0, 50.0],
        "pcc_apparent_va": [1001.25, 991.26, 1011.24],
        "inverter_current_a": [4.3, 4.3, 4.3],
        "frequency_hz": 50.0,
        "source_age_seconds": 5.0,
    }
    values.update(changes)
    return PowerQualityInput(**values)


class PowerQualityTests(unittest.TestCase):
    def test_parsing_rejects_non_finite_and_ha_placeholders(self):
        for value in (None, True, "", "unknown", "unavailable", "nan", float("inf")):
            self.assertIsNone(as_float(value))
        self.assertEqual(as_float("12.5"), 12.5)

    def test_balanced_nominal_observation_is_ok(self):
        result = evaluate_power_quality(observation())
        self.assertEqual(result.state, "ok")
        self.assertEqual(result.issues, ())
        self.assertAlmostEqual(result.pcc_voltage_spread_pct, 2 / 230 * 100, places=4)
        self.assertGreater(result.derived_power_factor, 0.99)

    def test_spread_is_magnitude_range_not_formal_vuf(self):
        self.assertAlmostEqual(magnitude_spread_pct([250, 230, 210]), 40 / 230 * 100)

    def test_eimo_historical_extreme_is_critical(self):
        # Exact, time-aligned example observed on 2026-09-07 07:11 local.
        result = evaluate_power_quality(observation(
            pcc_voltage_v=[247.2, 232.5, 200.9],
            inverter_voltage_v=[250.3, 236.6, 200.4],
            pcc_active_w=[-28, -53, -1552],
            pcc_reactive_var=[147, -15, 92],
            pcc_apparent_va=[150, 55, 1555],
        ))
        self.assertEqual(result.state, "critical")
        self.assertIn("voltage_critical_low", result.issues)
        self.assertIn("phase_magnitude_spread_critical", result.issues)

    def test_high_and_low_voltage_are_independently_reported(self):
        result = evaluate_power_quality(observation(pcc_voltage_v=[254, 230, 206]))
        self.assertIn("voltage_critical_low", result.issues)
        self.assertIn("voltage_critical_high", result.issues)

    def test_stale_source_supersedes_physical_classification(self):
        result = evaluate_power_quality(observation(pcc_voltage_v=[260, 230, 200], stale=True))
        self.assertEqual(result.state, "stale")
        self.assertEqual(result.issues, ("telemetry_stale",))

    def test_missing_phase_is_insufficient(self):
        result = evaluate_power_quality(observation(pcc_voltage_v=[230, "unknown", 231]))
        self.assertEqual(result.state, "insufficient")

    def test_low_load_does_not_manufacture_bad_power_factor(self):
        result = evaluate_power_quality(observation(
            pcc_active_w=[5, 5, 5],
            pcc_reactive_var=[10, 10, 10],
            pcc_apparent_va=[12, 12, 12],
        ))
        self.assertIsNone(result.derived_power_factor)
        self.assertFalse(any("power_factor" in issue for issue in result.issues))

    def test_reactive_share_and_power_factor_are_observational(self):
        result = evaluate_power_quality(observation(
            pcc_active_w=[200, 200, 200],
            pcc_reactive_var=[250, 250, 250],
            pcc_apparent_va=[320, 320, 320],
        ))
        self.assertEqual(result.state, "warning")
        self.assertIn("power_factor_warning", result.issues)
        self.assertIn("reactive_ratio_warning", result.issues)

    def test_pcc_and_inverter_imbalance_are_separate_metrics(self):
        result = evaluate_power_quality(observation(
            pcc_active_w=[1000, 1000, 1000],
            inverter_current_a=[1, 10, 1],
        ))
        self.assertLess(result.pcc_active_imbalance_pct, 1)
        self.assertGreater(result.inverter_current_imbalance_pct, 100)
        self.assertIn("inverter_current_imbalance_warning", result.issues)

    def test_voltage_rise_is_phase_by_phase(self):
        result = evaluate_power_quality(observation(
            pcc_voltage_v=[230, 230, 230],
            inverter_voltage_v=[232, 235, 241],
        ))
        self.assertEqual(result.voltage_rise_v, (2.0, 5.0, 11.0))
        self.assertIn("voltage_rise_critical", result.issues)

    def test_incoherent_power_triangle_is_data_warning(self):
        result = evaluate_power_quality(observation(
            pcc_active_w=[300, 300, 300],
            pcc_reactive_var=[0, 0, 0],
            pcc_apparent_va=[600, 600, 600],
        ))
        self.assertIn("power_triangle_incoherent", result.data_warnings)

    def test_signed_phase_deviation_detects_opposing_flow(self):
        self.assertGreater(phase_deviation_pct([500, 500, -500], 100), 100)

    def test_history_analysis_shows_association_without_causality_claim(self):
        samples = []
        for index in range(30):
            voltage = 225.0 + index
            samples.append({
                "pcc_voltage_mean_v": voltage,
                "pcc_voltage_max_v": voltage + 1,
                "pcc_active_total_w": 1000 + (index % 4) * 100,
                "pcc_reactive_total_var": -500 + index * 30,
            })
        result = analyze_history(samples)
        self.assertEqual(result["samples"], 30)
        self.assertGreater(result["corr_voltage_reactive"], 0.99)
        self.assertEqual(result["interpretation"], "association_only_q_sign_not_calibrated")


if __name__ == "__main__":
    unittest.main()
