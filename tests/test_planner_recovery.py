import unittest
import ast
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from energy_system.planner import PlannerInput, PlannerPolicy, decide_plan, EnergyPlan
from energy_system.supervisor import evaluate_execution
from energy_system.dawn import plan_dawn
from energy_system.horizon import HorizonPolicy, Slot
from energy_system.soc_buffer import daytime_buffer
from dataclasses import replace


class NightRegressionTests(unittest.TestCase):
    def test_unchanged_targets_publish_current_reason_and_new_lease_without_new_revision(self):
        import energy_system
        path=Path(energy_system.__file__).parent/'manager.py'
        tree=ast.parse(path.read_text())
        fn=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='_publish_plan')
        now=[datetime(2026,9,15,22,tzinfo=ZoneInfo('Europe/Vilnius'))]
        class Clock:
            @staticmethod
            def now():return now[0]
        class Outputs(dict):
            def __missing__(self,key):return key
        ns=dict(datetime=Clock,timedelta=timedelta,monotonic=lambda:1000,
                OUTPUT=Outputs(),SITE_KEY='eimo',SITE_LABEL='Eimo',TELEMETRY_REQUIRED=(),
                INVERTER_CONTROL_AVAILABLE=True)
        exec(compile(ast.fix_missing_locations(ast.Module(body=[fn],type_ignores=[])),str(path),'exec'),ns)
        states={}
        fake=SimpleNamespace(last_plan_mode=None,last_plan_signature=None,plan_revision=0,
              telemetry_status={},core_state='normal',last_coordinator_decision=None,config_issues=[],
              set_state=lambda entity,**kw:states.update({entity:kw}),log=lambda *args:None)
        plan=EnergyPlan('self_use',13,False,None,False,'Wake 03:00')
        ns['_publish_plan'](fake,plan)
        first=states['plan']['attributes']
        now[0]+=timedelta(minutes=1)
        ns['_publish_plan'](fake,replace(plan,reason='Wake 03:05'))
        latest=states['plan']['attributes']
        self.assertEqual(latest['revision'],first['revision'])
        self.assertEqual(latest['committed_at'],first['committed_at'])
        self.assertEqual(latest['reason'],'Wake 03:05')
        self.assertNotEqual(latest['valid_until'],first['valid_until'])
        self.assertEqual(datetime.fromisoformat(latest['valid_until'])-now[0],timedelta(seconds=180))

    def setUp(self):
        self.now = datetime(2026,9,15,22,tzinfo=ZoneInfo('Europe/Vilnius'))
        self.policy = HorizonPolicy(hard_floor=6,kwh_per_soc=.1433579,export_kw=1,storage_ceiling=77)
        self.slots = [Slot(self.now+timedelta(minutes=30*i), .5,
                      0 if i < 19 or i > 40 else 3, 0 if i < 19 or i > 40 else 2, .3)
                      for i in range(52)]

    def test_committed_night_discharge_does_not_sleep_again_as_soc_falls(self):
        sleeping=plan_dawn(self.slots,self.now,47,self.policy,execution_margin_minutes=30)
        self.assertFalse(sleeping['export_now'])
        ongoing=plan_dawn(self.slots,self.now,47,self.policy,execution_margin_minutes=30,
                          discharge_committed=True)
        self.assertTrue(ongoing['export_now'])
        self.assertTrue(ongoing['inverter_on'])
        at_target=plan_dawn(self.slots,self.now,ongoing['target_soc'],self.policy,
                           discharge_committed=True)
        self.assertFalse(at_target['export_now'])

    def test_no_grid_never_shuts_down_even_at_hard_floor(self):
        for soc in (5,47):
            p=decide_plan(PlannerInput(False,False,soc,13,False,6,0,
                {'valid':True,'night_active':True,'inverter_on':False,'grid_connected':False}),
                PlannerPolicy(hard_floor=6))
            self.assertTrue(p.inverter_on); self.assertFalse(p.slot_active)

    def test_sleeping_inverter_does_not_require_a_mode_write(self):
        plan=EnergyPlan('self_use',13,False,None,False,'sleep')
        actuator={'mode_by_plan':{'self_use':'Self-Use'}}
        actual={'executor':'on','slot':'off','power':'off','mode':'Feed-In Priority'}
        self.assertEqual(evaluate_execution(plan,actual,actuator,True).state,'ok')

    def test_day_buffer_cannot_persist_without_future_pv_surplus(self):
        dark=[Slot(self.now,.5,0,0,.3)]
        r=daytime_buffer({'valid':True,'export_now':False,'reserve_soc':77},dark,self.now,85,
            self.policy,connected=True,already_buffering=True,previous_cutoff=77)
        self.assertFalse(r['export_now'])
        self.assertFalse(r['soc_buffer_active'])


if __name__ == '__main__':
    unittest.main()
