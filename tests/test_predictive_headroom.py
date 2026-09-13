"""Offline regression tests: no HA services or cloud requests."""
import sys
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from energy_system.horizon import HorizonPolicy, Slot, plan_horizon
from energy_system.soc_buffer import preferred_policy, daytime_buffer, grid_present
from energy_system.dawn import morning_budget, plan_dawn
from energy_system.planner import PlannerPolicy, PlannerInput, decide_plan
from energy_system.profiles import HOME_ENERGY, EIMO_ENERGY


class PredictiveHeadroomTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 13, 11, 0, tzinfo=ZoneInfo('Europe/Vilnius'))
        self.policy = HorizonPolicy(hard_floor=13, kwh_per_soc=.16,
                                    storage_ceiling=77, comfort_soc=27,
                                    charge_kw=6.4, export_kw=1)

    def slots(self, powers, load=.3, low_factor=.85):
        return [Slot(self.now+timedelta(minutes=30*i), .5, p, p*low_factor, load)
                for i, p in enumerate(powers)]

    def evaluate(self, soc=70, slots=None, **kwargs):
        slots = self.slots([.4, 5, 5, 5, 2, 1, .5, .2]) if slots is None else slots
        guidance = plan_horizon(slots, soc, self.policy)
        return daytime_buffer(guidance, slots, self.now, soc, self.policy,
                              connected=True, execution_minutes=30, **kwargs)

    def test_both_sites_get_eight_extra_points_not_hardware_writes(self):
        for profile, hw_min in [(HOME_ENERGY, 12), (EIMO_ENERGY, 5)]:
            base = HorizonPolicy(hard_floor=profile['PLAN_HARD_FLOOR'], kwh_per_soc=profile['KWH_PER_SOC'])
            old = preferred_policy(base, hw_min, 100)
            new = preferred_policy(base, hw_min, 100, profile['EXTRA_HEADROOM_SOC'])
            self.assertEqual(old.storage_ceiling-new.storage_ceiling, 8)
            self.assertEqual(new.comfort_soc, old.comfort_soc)
            self.assertEqual(base.storage_ceiling, 95)

    def test_preempt_before_85_and_before_77(self):
        r = self.evaluate()
        self.assertTrue(r['predictive_buffer_due'])
        self.assertTrue(r['export_now'])
        self.assertLess(r['cutoff_soc'], 70)
        self.assertGreaterEqual(r['cutoff_soc'], r['buffer_protected_soc'])
        self.assertLessEqual((70-r['cutoff_soc'])*.16, .75)

    def test_cloud_setup_lead_is_larger_without_faster_writes(self):
        self.assertEqual(HOME_ENERGY['HEADROOM_EXECUTION_MINUTES'], 5)
        self.assertEqual(EIMO_ENERGY['HEADROOM_EXECUTION_MINUTES'], 30)
        self.assertEqual(EIMO_ENERGY['TACTICAL_INTERVAL'], 60)

    def test_ordinary_cloudy_day_does_not_discharge_for_a_fixed_percentage(self):
        r = self.evaluate(soc=70, slots=self.slots([.2]*8))
        self.assertFalse(r['export_now'])
        self.assertFalse(r['predictive_buffer_due'])

    def test_protect_night_and_morning_not_only_sunny_four_hours(self):
        slots = self.slots([.4, 5, 5, 5]+[0]*36, load=1, low_factor=.02)
        r = self.evaluate(soc=70, slots=slots)
        self.assertFalse(r['predictive_buffer_due'])
        self.assertFalse(r['export_now'])

    def test_no_grid_no_forced_export_even_high_soc(self):
        s = self.slots([5]*8)
        r = daytime_buffer(plan_horizon(s, 95, self.policy), s, self.now, 95,
                           self.policy, connected=False)
        self.assertFalse(r['export_now'])
        self.assertFalse(r['solar_export_priority'])

    def test_zero_export_limit_disables_preempt(self):
        p = replace(self.policy, export_kw=0)
        s = self.slots([5]*8)
        r = daytime_buffer(plan_horizon(s, 70, p), s, self.now, 70, p, connected=True)
        self.assertFalse(r['export_now'])

    def test_charge_power_clipping_alone_cannot_trigger_preexport(self):
        self.policy = replace(self.policy, charge_kw=.2)
        r = self.evaluate(soc=40, slots=self.slots([5]*4))
        self.assertFalse(r['predictive_buffer_due'])

    def test_hold_pending_cutoff_while_soc_rises(self):
        first = self.evaluate(soc=81)
        next_ = self.evaluate(soc=84, already_buffering=True, previous_cutoff=first['cutoff_soc'])
        self.assertEqual(next_['cutoff_soc'], first['cutoff_soc'])

    def test_pending_cutoff_never_overrides_new_higher_reserve(self):
        r = self.evaluate(soc=82, already_buffering=True, previous_cutoff=20)
        self.assertGreaterEqual(r['cutoff_soc'], 77)

    def test_manual_export_floor_protected(self):
        r = self.evaluate(soc=70, export_floor=69)
        # The existing horizon path is clamped by the final atomic planner too.
        inputs = PlannerInput(False, False, 70, 77, True, 69, 0, r)
        plan = decide_plan(inputs, PlannerPolicy(hard_floor=13))
        self.assertGreaterEqual(plan.slot_cutoff_soc, 69)

    def test_invalid_configuration_is_rejected(self):
        for extra in [-1, 11, float('nan')]:
            with self.assertRaises(ValueError):
                preferred_policy(self.policy, 12, 100, extra)

    def test_dawn_budget_has_more_capacity_margin(self):
        s = self.slots([.2, 1, 3, 5, 5, 3, 1, 0])
        old = morning_budget(s, replace(self.policy, storage_ceiling=85))
        new = morning_budget(s, self.policy)
        self.assertLessEqual(new['target_soc'], old['target_soc'])
        self.assertGreaterEqual(new['target_soc'], new['reserve_soc'])

    def test_planner_preserves_manual_storm_and_hard_floor(self):
        guidance = self.evaluate()
        p = PlannerPolicy(hard_floor=13)
        inputs = PlannerInput(False, False, 70, 77, True, 13, 0, guidance)
        self.assertTrue(decide_plan(inputs, p).slot_active)
        self.assertFalse(decide_plan(replace(inputs, manual=True), p).actionable)
        self.assertEqual(decide_plan(replace(inputs, storm=True), p).priority, 'storm')
        self.assertFalse(decide_plan(replace(inputs, soc=13), p).slot_active)
        self.assertFalse(decide_plan(replace(inputs, soc=None), p).actionable)


if __name__ == '__main__':
    unittest.main()
