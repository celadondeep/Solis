from __future__ import annotations

import unittest
from pathlib import Path

from energy_system.planner import EnergyPlan
from energy_system.profiles import HOME_ENERGY
from energy_system.shadow import compare_plan, preview_execution


class ShadowTests(unittest.TestCase):
    def setUp(self):
        self.plan = EnergyPlan(
            mode="night_export",
            target_soc=80,
            slot_active=True,
            slot_cutoff_soc=80,
            inverter_on=True,
            reason="test",
        )

    def test_matching_active_plan_has_no_drift(self):
        result = compare_plan(
            self.plan,
            site="home",
            active_mode="night_export",
            active_attributes={
                "slot_active": "on",
                "slot_cutoff_soc": 80,
                "inverter_on": "on",
            },
        )
        self.assertEqual(result.status, "match")
        self.assertEqual(result.differences, ())

    def test_drift_is_explicit(self):
        result = compare_plan(
            self.plan,
            site="home",
            active_mode="self_use",
            active_attributes={"slot_active": "off", "inverter_on": "off"},
        )
        self.assertEqual(result.status, "drift")
        self.assertIn("mode:self_use!=night_export", result.differences)
        self.assertIn("slot:off!=on", result.differences)

    def test_preview_is_read_only_and_never_applies(self):
        actuator = HOME_ENERGY["ACTUATOR"]
        observed = {
            "executor": "on",
            "mode": "Self-Use",
            "slot": "off",
            "slot_cutoff_soc": "12",
            "exclusive_slots": {
                entity_id: "off"
                for entity_id in actuator["exclusive_off"]
            },
            "power": "off",
        }
        before = dict(observed)
        result = preview_execution(
            self.plan,
            observed,
            actuator,
            site="home",
            inverter_control_available=True,
        )
        self.assertEqual(result.status, "shadow_only")
        self.assertGreaterEqual(len(result.commands), 3)
        self.assertEqual(observed, before)

    def test_shadow_adapter_contains_no_service_writer(self):
        source = Path(
            "/homeassistant/appdaemon/apps/energy_system/shadow_manager.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("call_service", source)
        self.assertNotIn("automation.trigger", source)
        self.assertIn("writes_disabled", source)


if __name__ == "__main__":
    unittest.main()
