import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from energy_system.soc_buffer import preferred_policy, daytime_buffer, grid_present
from energy_system.horizon import HorizonPolicy, Slot, simulate, plan_horizon, read_guidance, ha_attributes
from energy_system.planner import PlannerPolicy, PlannerInput, decide_plan

NOW=datetime(2026,9,10,10,tzinfo=timezone.utc)

class SocBufferTests(unittest.TestCase):
    def setUp(self):
        self.policy=preferred_policy(HorizonPolicy(13,.16,charge_kw=6.4),12,100)
        self.slots=[Slot(NOW+timedelta(minutes=30*i),.5,4,3,.5) for i in range(8)]
        self.guidance=dict(valid=True,export_now=False,solar_export_priority=True,
                           reserve_soc=85,cutoff_soc=100,target_soc=95)

    def buffer(self,soc=92,**kw):
        return daytime_buffer(self.guidance,self.slots,NOW,soc,self.policy,connected=True,**kw)

    def plan(self,guidance,soc=92,**kw):
        inp=PlannerInput(False,False,soc,85,True,13,0,guidance)
        return decide_plan(replace(inp,**kw),PlannerPolicy(13))

    def test_band_tracks_configuration_and_keeps_hardware_floor(self):
        self.assertEqual((self.policy.comfort_soc,self.policy.storage_ceiling),(27,85))
        e=preferred_policy(HorizonPolicy(6,.143),5,100)
        self.assertEqual((e.comfort_soc,e.storage_ceiling),(20,85))
        self.assertEqual(preferred_policy(self.policy,15,95).storage_ceiling,80)
        self.assertEqual(self.policy.hard_floor,13)
        for lo,hi in [(None,100),(12,None),(40,60),(float('nan'),100)]:
            with self.assertRaises(ValueError):preferred_policy(self.policy,lo,hi)

    def test_pv_priority_does_not_suppress_buffer(self):
        g=self.buffer();p=self.plan(g)
        self.assertTrue(p.slot_active)
        self.assertEqual(p.priority,'horizon_soc_buffer')
        self.assertGreaterEqual(p.slot_cutoff_soc,85)
        self.assertLessEqual((92-p.slot_cutoff_soc)*.16,.75)
        self.assertEqual(g['target_soc'],85)

    def test_cloud_and_load_dip_keep_running_buffer_above_floor(self):
        dark=[replace(s,pv=0,low_pv=0,load=3) for s in self.slots]
        g=daytime_buffer(self.guidance,dark,NOW,86,self.policy,connected=True,already_buffering=True)
        self.assertTrue(self.plan(g,soc=86).slot_active)
        self.assertEqual(g['cutoff_soc'],85)
        g=daytime_buffer(self.guidance,dark,NOW,85,self.policy,connected=True,already_buffering=True)
        self.assertFalse(self.plan(g,soc=85).slot_active)

    def test_no_discharge_for_high_soc_without_solar_surplus(self):
        dark=[replace(s,pv=0,low_pv=0) for s in self.slots]
        g=daytime_buffer(self.guidance,dark,NOW,92,self.policy,connected=True)
        self.assertFalse(self.plan(g).slot_active)

    def test_hysteresis_manual_floor_and_zero_export(self):
        self.assertFalse(self.plan(self.buffer(86),soc=86).slot_active)
        self.assertTrue(self.plan(self.buffer(87),soc=87).slot_active)
        self.assertFalse(self.plan(self.buffer(export_floor=94)).slot_active)
        g=daytime_buffer(self.guidance,self.slots,NOW,92,replace(self.policy,export_kw=0),connected=True)
        self.assertFalse(self.plan(g).slot_active)

    def test_storm_manual_stale_have_priority(self):
        g=self.buffer()
        for kw in [dict(storm=True),dict(manual=True),dict(soc=None)]:
            p=self.plan(g,**kw)
            self.assertFalse(p.actionable)
            self.assertFalse(p.slot_active)
        self.assertEqual(self.plan(g,storm=True).target_soc,100)

    def test_no_grid_disables_all_forced_export(self):
        g=daytime_buffer({**self.guidance,'export_now':True},self.slots,NOW,92,self.policy,connected=False)
        self.assertFalse(self.plan(g).slot_active)
        self.assertEqual(self.plan(g).mode,'self_use')
        for hb,volts in [(NOW-timedelta(minutes=16),[230]*3),(NOW,[230,0,230]),(None,[230]*3)]:
            self.assertFalse(grid_present(NOW,hb,volts,50,900))
        self.assertTrue(grid_present(NOW,NOW.timestamp(),[230]*3,50,900))

    def test_bms_zero_and_reduced_current_change_expected_clipping(self):
        base=self.slots[:1]; e=(60-13)*.16
        normal=simulate(base,e,self.policy)
        taper=simulate([replace(base[0],charge_limit_kw=.1)],e,self.policy)
        stopped=simulate([replace(base[0],charge_limit_kw=0)],e,self.policy)
        self.assertGreater(taper['clipped_kwh'],normal['clipped_kwh'])
        self.assertGreater(stopped['clipped_kwh'],taper['clipped_kwh'])
        self.assertEqual(stopped['charged_kwh'],0)

    def test_existing_forecast_slot_also_survives_pv_priority(self):
        g=dict(valid=True,export_now=True,solar_export_priority=True,reserve_soc=30,cutoff_soc=75)
        self.assertTrue(self.plan(g,soc=80).slot_active)
        self.assertEqual(self.plan(g,soc=80).slot_cutoff_soc,75)

    def test_shadow_decodes_buffer_flag_and_expiry(self):
        g=ha_attributes({**self.buffer(),'expires_at':(NOW+timedelta(minutes=7)).isoformat()})
        self.assertEqual(self.plan(read_guidance(g,NOW)).priority,'horizon_soc_buffer')
        self.assertFalse(self.plan(read_guidance(g,NOW+timedelta(minutes=8))).slot_active)

    def test_forecast_preexport_protects_load_reserve(self):
        dark=[replace(s,pv=0,low_pv=0,load=4) for s in self.slots]
        self.assertFalse(plan_horizon(dark,70,self.policy)['export_now'])

if __name__=='__main__':unittest.main()
