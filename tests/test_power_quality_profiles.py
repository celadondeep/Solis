from __future__ import annotations

import unittest

from energy_system.power_quality_profiles import POWER_QUALITY_PROFILES


class PowerQualityProfileTests(unittest.TestCase):
    def test_each_plant_has_complete_three_phase_inputs(self):
        for key, profile in POWER_QUALITY_PROFILES.items():
            entities = profile["entities"]
            for group in (
                "pcc_voltage_v", "inverter_voltage_v", "pcc_active_w",
                "pcc_reactive_var", "pcc_apparent_va", "inverter_current_a",
            ):
                self.assertEqual(len(entities[group]), 3, f"{key}:{group}")
                self.assertEqual(len(set(entities[group])), 3, f"{key}:{group}")

    def test_outputs_do_not_cross_between_plants(self):
        home = set(POWER_QUALITY_PROFILES["home"]["outputs"].values())
        eimo = set(POWER_QUALITY_PROFILES["eimo"]["outputs"].values())
        self.assertFalse(home & eimo)

    def test_monitor_is_explicitly_slow_and_bounded(self):
        for profile in POWER_QUALITY_PROFILES.values():
            self.assertGreaterEqual(profile["sample_interval_seconds"], 60)
            self.assertGreaterEqual(profile["publish_interval_seconds"], 300)
            self.assertLessEqual(profile["history_days"], 14)

    def test_eimo_deduplicates_unchanged_cloud_snapshots(self):
        self.assertTrue(POWER_QUALITY_PROFILES["eimo"]["append_only_on_new_heartbeat"])
        self.assertFalse(POWER_QUALITY_PROFILES["home"]["append_only_on_new_heartbeat"])


if __name__ == "__main__":
    unittest.main()
