from __future__ import annotations

import math
import unittest

from energy_system.planner import (
    PlannerInput,
    PlannerPolicy,
    decide_plan,
    target_soc_for_room,
)
from energy_system.profiles import EIMO_ENERGY, HOME_ENERGY


def legacy_action(inputs, policy):
    """2026-09-02 algoritmo veiksmo laukai validiems įėjimams."""
    target = max(policy.hard_floor, min(float(inputs.target_soc), 100.0))
    if inputs.storm:
        return ("storm", 100.0, False, 100.0, True)
    if inputs.manual:
        return ("manual", target, None, None, None)
    soc = float(inputs.soc)
    floor = max(float(inputs.export_floor), policy.hard_floor)
    if soc <= policy.hard_floor:
        return (
            "self_use", target, False, policy.hard_floor,
            inputs.in_production_hours,
        )
    if not inputs.in_production_hours:
        if soc <= max(target, policy.night_rest_soc):
            return ("self_use", target, False, None, False)
        return ("night_export", target, True, target, True)
    if soc < floor + 5:
        return ("self_use", target, False, None, True)
    if soc >= policy.ceiling:
        return ("feed_in", target, True, policy.ceiling, True)
    threshold = 3 if soc < 30 else 1
    if inputs.room_shortfall_kwh > threshold:
        return ("feed_in", target, True, floor, True)
    if soc >= target:
        return ("feed_in", target, False, None, True)
    return ("self_use", target, False, None, True)


def action_tuple(plan):
    return (
        plan.mode,
        plan.target_soc,
        plan.slot_active,
        plan.slot_cutoff_soc,
        plan.inverter_on,
    )


class PlannerParityTests(unittest.TestCase):
    def test_valid_grid_matches_previous_algorithm(self):
        for hard_floor in (12.0, 6.0):
            policy = PlannerPolicy(hard_floor=hard_floor)
            for production in (False, True):
                for soc in (
                    hard_floor - 1, hard_floor, hard_floor + 1,
                    17, 25, 29, 30, 50, 79, 80, 95,
                ):
                    for target in (20, 50, 80):
                        for shortfall in (0, 1, 1.1, 3, 3.1, 8):
                            inputs = PlannerInput(
                                storm=False,
                                manual=False,
                                soc=soc,
                                target_soc=target,
                                in_production_hours=production,
                                export_floor=hard_floor,
                                room_shortfall_kwh=shortfall,
                            )
                            with self.subTest(
                                hard_floor=hard_floor,
                                production=production,
                                soc=soc,
                                target=target,
                                shortfall=shortfall,
                            ):
                                self.assertEqual(
                                    action_tuple(decide_plan(inputs, policy)),
                                    legacy_action(inputs, policy),
                                )

    def test_storm_precedes_invalid_telemetry(self):
        plan = decide_plan(
            PlannerInput(True, False, None, 80, False, 12, 0),
            PlannerPolicy(hard_floor=12),
        )
        self.assertEqual(plan.mode, "storm")
        self.assertEqual(plan.priority, "storm")
        self.assertFalse(plan.actionable)

    def test_manual_precedes_invalid_telemetry(self):
        plan = decide_plan(
            PlannerInput(False, True, None, 70, False, 12, 0),
            PlannerPolicy(hard_floor=12),
        )
        self.assertEqual(plan.mode, "manual")
        self.assertFalse(plan.actionable)

    def test_unknown_soc_holds_physical_state(self):
        for soc in (None, math.nan, -1, 101):
            plan = decide_plan(
                PlannerInput(False, False, soc, 80, False, 12, 0),
                PlannerPolicy(hard_floor=12),
            )
            self.assertEqual(plan.mode, "hold")
            self.assertIsNone(plan.slot_active)
            self.assertIsNone(plan.inverter_on)
            self.assertFalse(plan.inputs_valid)
            self.assertFalse(plan.actionable)


class SafetyPolicyTests(unittest.TestCase):
    def test_hard_floor_is_off_at_night_and_on_during_pv_window(self):
        policy = PlannerPolicy(hard_floor=12, night_rest_soc=16)
        night = decide_plan(
            PlannerInput(False, False, 12, 20, False, 12, 0), policy
        )
        day = decide_plan(
            PlannerInput(False, False, 12, 20, True, 12, 0), policy
        )
        self.assertFalse(night.inverter_on)
        self.assertTrue(day.inverter_on)
        self.assertEqual(night.priority, "hard_floor")
        self.assertEqual(day.priority, "hard_floor")

    def test_night_rest_band_prevents_export_churn(self):
        policy = PlannerPolicy(hard_floor=12, night_rest_soc=16)
        plan = decide_plan(
            PlannerInput(False, False, 15, 12, False, 12, 0), policy
        )
        self.assertEqual(plan.mode, "self_use")
        self.assertFalse(plan.slot_active)
        self.assertFalse(plan.inverter_on)
        self.assertEqual(plan.priority, "night_idle")


class TargetTests(unittest.TestCase):
    def test_zero_room_stops_at_health_ceiling(self):
        policy = PlannerPolicy(hard_floor=12)
        self.assertEqual(target_soc_for_room(0, 0.16, policy), 80)

    def test_sunny_day_can_go_below_preferred_floor(self):
        home = PlannerPolicy(
            hard_floor=HOME_ENERGY["PLAN_HARD_FLOOR"],
            preferred_floor=HOME_ENERGY["PLAN_TARGET_BAND_FLOOR"],
            ceiling=HOME_ENERGY["PLAN_TARGET_BAND_CEILING"],
        )
        self.assertEqual(target_soc_for_room(13.12, 0.16, home), 18)

    def test_target_never_crosses_site_hard_floor(self):
        home = PlannerPolicy(hard_floor=12)
        eimo = PlannerPolicy(hard_floor=6)
        self.assertEqual(target_soc_for_room(100, 0.16, home), 12)
        self.assertEqual(target_soc_for_room(100, 13.619 / 95, eimo), 6)

    def test_invalid_room_is_rejected(self):
        with self.assertRaises(ValueError):
            target_soc_for_room(-1, 0.16, PlannerPolicy(hard_floor=12))


class IsolationTests(unittest.TestCase):
    def test_private_entity_names_do_not_cross_sites(self):
        home_values = tuple(v for v in HOME_ENERGY["SENSOR"].values() if v)
        eimo_values = tuple(v for v in EIMO_ENERGY["SENSOR"].values() if v)
        self.assertFalse(any("1033300254190112" in v for v in home_values))
        self.assertFalse(any("_eimo" in v for v in home_values))
        self.assertFalse(any("solis_s6_eh3p" in v for v in eimo_values))

    def test_output_entities_are_disjoint(self):
        self.assertTrue(
            set(HOME_ENERGY["OUTPUT"].values()).isdisjoint(
                EIMO_ENERGY["OUTPUT"].values()
            )
        )

    def test_overdischarge_is_not_an_actuator(self):
        # The requested band reads configured SOC limits; it never writes them.
        values = repr((HOME_ENERGY["ACTUATOR"], EIMO_ENERGY["ACTUATOR"])).lower()
        self.assertNotIn("overdischarge", values)

    def test_site_specific_battery_sign_and_floor(self):
        self.assertEqual(HOME_ENERGY["BATTERY_DISCHARGE_SIGN"], 1.0)
        self.assertEqual(EIMO_ENERGY["BATTERY_DISCHARGE_SIGN"], -1.0)
        self.assertEqual(HOME_ENERGY["PLAN_HARD_FLOOR"], 12)
        self.assertEqual(EIMO_ENERGY["PLAN_HARD_FLOOR"], 6)
        self.assertEqual(HOME_ENERGY["NIGHT_REST_SOC"], 16)
        self.assertEqual(EIMO_ENERGY["NIGHT_REST_SOC"], 6)


if __name__ == "__main__":
    unittest.main()
