from __future__ import annotations

import unittest

from energy_system.boiler import soc_drop_rate_10min
from energy_system.planner import EnergyPlan
from energy_system.profiles import EIMO_ENERGY, HOME_ENERGY
from energy_system.supervisor import evaluate_execution


class BoilerRateTests(unittest.TestCase):
    def test_rate_uses_real_elapsed_time(self):
        self.assertAlmostEqual(
            soc_drop_rate_10min([(0.0, 50.0), (60.0, 49.0)]),
            10.0,
        )

    def test_rate_is_zero_without_elapsed_window(self):
        self.assertEqual(soc_drop_rate_10min([]), 0.0)
        self.assertEqual(
            soc_drop_rate_10min([(10.0, 50.0), (10.0, 49.0)]),
            0.0,
        )


class ExecutionSupervisorTests(unittest.TestCase):
    @staticmethod
    def exclusive_off_states(actuator):
        return {entity_id: "off" for entity_id in actuator.get("exclusive_off", ())}

    def setUp(self):
        self.plan = EnergyPlan(
            mode="self_use",
            target_soc=80,
            slot_active=False,
            slot_cutoff_soc=None,
            inverter_on=True,
            reason="test",
        )

    def test_home_matching_actuators_are_ok(self):
        a = HOME_ENERGY["ACTUATOR"]
        result = evaluate_execution(
            self.plan,
            {
                "executor": "on",
                "mode": "Self-Use",
                "slot": "off",
                "slot_cutoff_soc": "30",
                "exclusive_slots": self.exclusive_off_states(a),
                "power": "on",
            },
            a,
            True,
        )
        self.assertEqual(result.state, "ok")
        self.assertEqual(result.mismatches, ())

    def test_write_mismatch_is_visible(self):
        a = HOME_ENERGY["ACTUATOR"]
        result = evaluate_execution(
            self.plan,
            {
                "executor": "on",
                "mode": "Feed-in Priority",
                "slot": "on",
                "slot_cutoff_soc": "30",
                "exclusive_slots": self.exclusive_off_states(a),
                "power": "off",
            },
            a,
            True,
        )
        self.assertEqual(result.state, "degraded")
        self.assertEqual(set(result.mismatches), {"mode", "slot", "power"})

    def test_eimo_power_is_monitor_only(self):
        a = EIMO_ENERGY["ACTUATOR"]
        result = evaluate_execution(
            self.plan,
            {
                "executor": "on",
                "mode": "Self-Use",
                "slot": "off",
                "slot_cutoff_soc": "80",
                "exclusive_slots": self.exclusive_off_states(a),
                "power": "off",
            },
            a,
            False,
        )
        self.assertEqual(result.state, "ok")

    def test_shadow_slot_is_visible(self):
        a = EIMO_ENERGY["ACTUATOR"]
        exclusive = self.exclusive_off_states(a)
        shadow_slot = a["exclusive_off"][-1]
        exclusive[shadow_slot] = "on"
        result = evaluate_execution(
            self.plan,
            {
                "executor": "on",
                "mode": "Self-Use",
                "slot": "off",
                "slot_cutoff_soc": "80",
                "exclusive_slots": exclusive,
                "power": "on",
            },
            a,
            False,
        )
        self.assertEqual(result.state, "degraded")
        self.assertIn(f"shadow_slot_on:{shadow_slot}", result.mismatches)


if __name__ == "__main__":
    unittest.main()
