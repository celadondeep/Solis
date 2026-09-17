"""Calendar edges, DST, missing data, anomalies and safe AppDaemon recovery."""
import copy
from datetime import date, datetime, time, timedelta, timezone
import importlib
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'work/appdaemon/apps') if (ROOT/'work').exists() else str(ROOT))
from energy_system.consumption_rolling import rolling_daily_statistics, recorder_days, rolling_hourly_statistics

TODAY=date(2026,9,17)
TZ=ZoneInfo('Europe/Vilnius')


def rows_for(day, value=1):
    first=datetime.combine(day,time.min,TZ).astimezone(timezone.utc)
    last=datetime.combine(day+timedelta(days=1),time.min,TZ).astimezone(timezone.utc)
    result=[]
    while first<last:
        result.append({'start':first.isoformat(),'end':(first+timedelta(hours=1)).isoformat(),'change':value})
        first+=timedelta(hours=1)
    return result


def history(values, today=TODAY):
    return [{'date':(today-timedelta(days=len(values)-i)).isoformat(),'kwh':v} for i,v in enumerate(values)]


class RollingMath(unittest.TestCase):
    def test_calendar_window_drops_old_and_incomplete_today(self):
        records=history([1000]*10+[10]*30)+[{'date':str(TODAY),'kwh':1000}]
        result=rolling_daily_statistics(records,today=TODAY)
        self.assertEqual(result['daily_sample_days'],30)
        self.assertEqual(result['daily_mean_kwh'],10)
        self.assertEqual(result['window_start'],'2026-08-18')
        tomorrow=rolling_daily_statistics(records,today=TODAY+timedelta(days=1))
        self.assertEqual(tomorrow['window_start'],'2026-08-19')

    def test_missing_days_do_not_extend_window_or_count_as_zero(self):
        records=history([10]*60)[::2]
        result=rolling_daily_statistics(records,today=TODAY)
        self.assertEqual(result['daily_sample_days'],15)
        self.assertEqual(result['daily_mean_kwh'],10)

    def test_isolated_spike_with_zero_iqr_is_excluded(self):
        result=rolling_daily_statistics(history([10]*28+[50,111]),today=TODAY)
        self.assertEqual(result['daily_mean_kwh'],10)
        self.assertEqual(len(result['anomaly_days']),2)

    def test_genuine_sustained_change_adapts(self):
        result=rolling_daily_statistics(history([10]*15+[20]*15),today=TODAY)
        self.assertEqual(result['daily_mean_kwh'],15)
        self.assertEqual(result['anomaly_days'],[])

    def test_weekday_means_are_actual_not_forecast_factors(self):
        records=history([10]*30)
        for record in records:
            if date.fromisoformat(record['date']).weekday()==5:record['kwh']=16
        result=rolling_daily_statistics(records,today=TODAY)
        self.assertEqual(result['weekday_kwh'][5],16)
        self.assertGreater(result['weekday_factors']['5'],1)
        self.assertLess(result['daily_mean_kwh']*result['weekday_factors']['5'],16)

    def test_invalid_values_and_short_history(self):
        result=rolling_daily_statistics(history([float('nan'),float('inf'),-1,0,10]),today=TODAY)
        self.assertIsNone(result['daily_mean_kwh'])
        self.assertEqual(result['daily_status'],'insufficient_data')

    def test_duplicate_day_is_idempotent(self):
        records=history([10]*30)
        self.assertEqual(rolling_daily_statistics(records,today=TODAY),rolling_daily_statistics(records*2,today=TODAY))

    def test_missing_hour_excludes_entire_day(self):
        rows=rows_for(TODAY-timedelta(days=1))[:-1]
        days,incomplete=recorder_days(rows,today=TODAY)
        self.assertFalse(days)
        self.assertIn('2026-09-16',incomplete)

    def test_duplicate_hour_does_not_add_energy(self):
        rows=rows_for(TODAY-timedelta(days=1))
        days,_=recorder_days(rows*2,today=TODAY)
        self.assertEqual(days['2026-09-16']['kwh'],24)

    def test_conflicting_hour_excludes_day(self):
        rows=rows_for(TODAY-timedelta(days=1));rows.append(dict(rows[0],change=2))
        days,_=recorder_days(rows,today=TODAY)
        self.assertFalse(days)

    def test_dst_days_conserve_energy_and_hour_counts(self):
        for day,hours in [(date(2026,3,29),23),(date(2026,10,25),25)]:
            days,_=recorder_days(rows_for(day),today=day+timedelta(days=1))
            self.assertEqual(days[str(day)]['kwh'],hours)
            self.assertEqual(sum(days[str(day)]['counts']),hours)

    def test_hourly_mean_uses_only_complete_accepted_days(self):
        rows=[]
        for i in range(1,8):rows+=rows_for(TODAY-timedelta(days=i),i)
        days,_=recorder_days(rows,today=TODAY)
        result=rolling_hourly_statistics(days,excluded_days=['2026-09-16'])
        self.assertEqual(result['hourly_sample_days'],6)
        self.assertEqual(result['hourly_kwh'],[4.5]*24)
        self.assertEqual(result['hourly_counts'],[6]*24)

    def test_zero_day_does_not_train_false_no_consumption(self):
        days,_=recorder_days(rows_for(TODAY-timedelta(days=1),0),today=TODAY)
        result=rolling_hourly_statistics(days,min_days=1)
        self.assertEqual(result['hourly_status'],'insufficient_data')


class AppRecovery(unittest.TestCase):
    def setUp(self):
        self.saved={name:sys.modules.get(name) for name in ['appdaemon','appdaemon.plugins','appdaemon.plugins.hass','appdaemon.plugins.hass.hassapi']}
        for name in self.saved:sys.modules[name]=types.ModuleType(name)
        sys.modules['appdaemon.plugins.hass.hassapi'].Hass=object
        sys.modules.pop('energy_system.consumption',None)
        from energy_system.consumption import build_consumption_model
        from energy_system.site_registry import get_site
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.profile=get_site('home')['consumption'];self.profile['MODEL_FILE']=self.tmp.name+'/model.json'
        self.profile['ESO_CSV_FILE']=self.tmp.name+'/missing.csv'
        self.app=build_consumption_model(self.profile)()
        self.app.local_now=lambda:datetime(2026,9,17,12,tzinfo=TZ)
        self.app.log=lambda *args,**kwargs:None
        self.app.model=self.app.load_model();self.app.model['history']=history([10]*30)
        self.app.retry_timer=None;self.calls=[];self.published={}
        self.app.get_state=lambda *args,**kwargs:{}
        self.app.set_state=lambda entity,**kwargs:self.published.update({entity:kwargs})
        self.app.enrich_history_with_weather=lambda:None
        self.app.run_in=lambda *args,**kwargs:self.calls.append((args,kwargs)) or 'timer'

    def tearDown(self):
        for name,value in self.saved.items():
            if value is None:sys.modules.pop(name,None)
            else:sys.modules[name]=value
        sys.modules.pop('energy_system.consumption',None)

    def response(self,rows):
        return {'success':True,'result':{'response':{'statistics':{self.profile['SENSOR']['today_consumption']:rows}}}}

    def test_ha_response_parsing_and_no_repeat_queries(self):
        calls=[]
        rows=sum((rows_for(TODAY-timedelta(days=i),.5) for i in range(1,31)),[])
        self.app.call_service=lambda *a,**kw:calls.append((a,kw)) or self.response(rows)
        self.app.update_model({});self.app.update_model({})
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0][0],('recorder/get_statistics',))
        self.assertEqual(self.app.model['daily_avg'],12)
        self.assertEqual(self.app.model['profile_days'],30)
        self.assertEqual(self.published[self.profile['OUTPUT']['daily_avg']]['state'],'12.0')
        self.assertTrue(Path(self.profile['MODEL_FILE']).exists())

    def test_recorder_error_keeps_shape_and_schedules_one_retry(self):
        old=copy.deepcopy(self.app.model['hourly_profile'])
        self.app.call_service=lambda *a,**kw:{'success':False}
        self.app.update_model({});self.app.update_model({})
        self.assertEqual(self.app.model['hourly_profile'],old)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.app.model['statistics_status'],'cached_after_error')
        self.assertEqual(self.app.model['rolling']['hourly_status'],'insufficient_data')
        self.assertEqual(self.app.model['daily_avg'],10)

    def test_retry_does_not_schedule_endless_chain(self):
        self.app.call_service=lambda *a,**kw:{'success':False}
        self.app.update_model({'statistics_retry':True})
        self.assertEqual(self.calls,[])

    def test_failed_persistence_preserves_previous_file(self):
        self.app.save_model();path=Path(self.profile['MODEL_FILE']);old=path.read_text()
        self.app.model['bad_value']=float('nan');self.app.save_model()
        self.assertEqual(path.read_text(),old)
        self.assertFalse(Path(str(path)+'.tmp').exists())


if __name__=='__main__':unittest.main()
