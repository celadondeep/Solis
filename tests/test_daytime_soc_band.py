"""Keep the daytime SOC band independent of a predicted PV surplus."""
from dataclasses import replace
from datetime import datetime,timedelta
from pathlib import Path
import sys,unittest
from zoneinfo import ZoneInfo
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from energy_system.horizon import HorizonPolicy,Slot,plan_horizon
from energy_system.soc_buffer import daytime_buffer
from energy_system.planner import PlannerInput,PlannerPolicy,decide_plan
from energy_system.dawn import plan_dawn

class DaytimeBand(unittest.TestCase):
    def setUp(self):
        self.now=datetime(2026,9,18,17,19,tzinfo=ZoneInfo('Europe/Vilnius'))
        self.policy=HorizonPolicy(hard_floor=6,kwh_per_soc=.1433578947368421,storage_ceiling=77,comfort_soc=20,export_kw=1)
        self.slots=[Slot(self.now+timedelta(minutes=30*i),.5,.2 if i<2 else 0,.1 if i<2 else 0,.6) for i in range(8)]
    def evaluate(self,soc=100,**kw):
        options=dict(connected=True,production_on=True,export_floor=6)
        options.update(kw)
        return daytime_buffer(plan_horizon(self.slots,soc,self.policy),self.slots,self.now,soc,self.policy,**options)
    def test_full_battery_exports_with_production_but_zero_forecast_surplus(self):
        r=self.evaluate()
        self.assertEqual(r['buffer_near_pv_surplus_kwh'],0)
        self.assertTrue(r['buffer_preferred_band_due']);self.assertTrue(r['export_now'])
        self.assertTrue(r['solar_export_priority']);self.assertTrue(r['soc_buffer_active'])
        self.assertEqual(r['cutoff_soc'],95)
        self.assertLessEqual((100-r['cutoff_soc'])*self.policy.kwh_per_soc,.75)
    def test_both_transports_use_the_same_band_rule(self):
        for scale,floor in ((.16,13),(.1433578947368421,6)):
            self.policy=replace(self.policy,kwh_per_soc=scale,hard_floor=floor)
            r=self.evaluate()
            self.assertTrue(r['export_now']);self.assertGreaterEqual(r['cutoff_soc'],77)
    def test_stable_pending_cutoff_does_not_move_with_soc(self):
        r=self.evaluate(soc=99,already_buffering=True,previous_cutoff=95)
        self.assertEqual(r['cutoff_soc'],95)
    def test_starts_at_two_points_and_stops_at_half_point_hysteresis(self):
        self.assertFalse(self.evaluate(soc=78.9)['export_now'])
        self.assertTrue(self.evaluate(soc=79)['export_now'])
        self.assertTrue(self.evaluate(soc=78,already_buffering=True,previous_cutoff=77)['export_now'])
        self.assertFalse(self.evaluate(soc=77.5,already_buffering=True,previous_cutoff=77)['export_now'])
    def test_no_production_does_not_keep_daytime_export_running(self):
        self.assertFalse(self.evaluate(production_on=False,already_buffering=True,previous_cutoff=95)['export_now'])
    def test_grid_loss_and_export_floor_remain_guards(self):
        self.assertFalse(self.evaluate(connected=False)['export_now'])
        self.assertFalse(self.evaluate(export_floor=100)['export_now'])
        self.policy=replace(self.policy,export_kw=0)
        self.assertFalse(self.evaluate()['export_now'])
    def test_result_creates_feed_in_slot_and_storm_override_still_wins(self):
        r=self.evaluate()
        inp=PlannerInput(False,False,100,77,True,6,0,r)
        normal=decide_plan(inp,PlannerPolicy(hard_floor=6))
        self.assertEqual(normal.mode,'feed_in');self.assertTrue(normal.slot_active)
        self.assertEqual(normal.slot_cutoff_soc,95)
        self.assertEqual(decide_plan(replace(inp,storm=True),PlannerPolicy(hard_floor=6)).priority,'storm')
        self.assertFalse(decide_plan(replace(inp,manual=True),PlannerPolicy(hard_floor=6)).actionable)
    def test_night_sleep_plan_remains_authoritative(self):
        now=self.now.replace(hour=22)
        slots=[Slot(now+timedelta(minutes=30*i),.5,3 if 19<=i<=40 else 0,2 if 19<=i<=40 else 0,.3) for i in range(52)]
        r=plan_dawn(slots,now,47,self.policy,production_on=False,execution_margin_minutes=30)
        self.assertTrue(r['night_active']);self.assertFalse(r['export_now']);self.assertFalse(r['inverter_on'])

    def test_night_sleep_wins_over_legacy_production_window(self):
        guidance=dict(valid=True,night_active=True,inverter_on=False,
                      export_now=False,grid_connected=True,reason='Night sleep')
        for floor in (6,13):
            inp=PlannerInput(False,False,99,20,True,floor,0,guidance)
            policy=PlannerPolicy(hard_floor=floor)
            plan=decide_plan(inp,policy)
            self.assertEqual(plan.priority,'horizon_night')
            self.assertFalse(plan.slot_active)
            self.assertFalse(plan.inverter_on)
            awake=decide_plan(replace(inp,horizon={**guidance,'inverter_on':True}),policy)
            self.assertTrue(awake.inverter_on)
            no_grid=decide_plan(replace(inp,horizon={**guidance,'grid_connected':False}),policy)
            self.assertTrue(no_grid.inverter_on)
            self.assertTrue(decide_plan(replace(inp,storm=True),policy).inverter_on)

    def test_invalid_night_guidance_does_not_shut_down_during_production(self):
        inp=PlannerInput(False,False,99,20,True,6,0,
                         dict(valid=False,night_active=True,inverter_on=False))
        self.assertTrue(decide_plan(inp,PlannerPolicy(hard_floor=6)).inverter_on)

if __name__=='__main__':unittest.main()
