import copy
from datetime import date, datetime, time, timedelta
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'work/appdaemon/apps') if (ROOT/'work').exists() else str(ROOT))
from energy_system.consumption_forecast import rates_for_day, forecast_reader, integrate, intraday_ratio, fresh_daily_value, day_quality
from energy_system.consumption_accuracy import freeze, score

TZ=ZoneInfo('Europe/Vilnius')
NOW=datetime(2026,9,18,23,tzinfo=TZ)


def record(now=NOW, totals=(24,48)):
    forecasts={}
    for delta,total in enumerate(totals):
        day=now.date()+timedelta(days=delta)
        forecasts[str(day)]=dict(daily_kwh=total, hourly_kw=rates_for_day([1]*24,total,day,'Europe/Vilnius'),
            daily_latest_sample=str(now.date()-timedelta(days=1)), hourly_latest_sample=str(now.date()-timedelta(days=1)))
    return dict(last_updated=now.isoformat(), attributes=dict(model_version=4, forecast_status='ok', forecasts=forecasts))


class ForecastContract(unittest.TestCase):
    def test_observed_means_are_not_today_forecast(self):
        old=dict(last_updated=NOW.isoformat(),attributes=dict(model_version=3,hourly_kwh=[1]*24,forecast_today_kwh=36))
        reader=forecast_reader(old,NOW,tomorrow=72)
        self.assertEqual(reader(NOW),1.5)
        self.assertEqual(reader(NOW+timedelta(hours=2)),3)

    def test_date_transition_and_partial_hour(self):
        reader=forecast_reader(record(),NOW)
        self.assertEqual(integrate(reader,NOW+timedelta(minutes=30),NOW+timedelta(hours=2)),2.5)

    def test_dst_daily_total_is_conserved(self):
        for day in (date(2026,3,29),date(2026,10,25)):
            first=datetime.combine(day,time.min,TZ);last=datetime.combine(day+timedelta(days=1),time.min,TZ)
            data=record(first,totals=(24,48));reader=forecast_reader(data,first)
            self.assertAlmostEqual(integrate(reader,first,last),24)

    def test_stale_training_is_not_refreshed_by_republishing(self):
        data=record()
        data['attributes']['forecasts'][str(NOW.date())]['hourly_latest_sample']='2026-08-01'
        with self.assertRaises(ValueError):forecast_reader(data,NOW)

    def test_future_sample_is_rejected(self):
        data=record();data['attributes']['forecasts'][str(NOW.date())]['daily_latest_sample']=str(NOW.date())
        with self.assertRaises(ValueError):forecast_reader(data,NOW)

    def test_missing_tomorrow_does_not_repeat_today(self):
        data=record();del data['attributes']['forecasts'][str(NOW.date()+timedelta(days=1))]
        with self.assertRaises(ValueError):forecast_reader(data,NOW)

    def test_invalid_v4_does_not_fall_back_to_graph(self):
        data=record();data['attributes']['forecasts'][str(NOW.date())]['hourly_kw'][0]=float('nan')
        data['attributes']['hourly_kwh']=[1]*24
        with self.assertRaises(ValueError):forecast_reader(data,NOW,24)

    def test_total_disagreement_is_rejected(self):
        data=record();data['attributes']['forecasts'][str(NOW.date())]['daily_kwh']=50
        with self.assertRaises(ValueError):forecast_reader(data,NOW)

    def test_quiet_correction_and_missing_or_stale_measurements(self):
        self.assertEqual(intraday_ratio(16,8,.25),1.25)
        self.assertEqual(intraday_ratio(16,8,0),1)
        self.assertEqual(intraday_ratio(None,8,.25),1)
        self.assertEqual(intraday_ratio(5,1,.25),1)
        self.assertEqual(fresh_daily_value(dict(state=0,last_updated=NOW.isoformat()),NOW),0)
        self.assertIsNone(fresh_daily_value(dict(state=10,last_updated=(NOW-timedelta(hours=2)).isoformat()),NOW))

    def test_yesterdays_counter_is_not_actual_today(self):
        now=NOW.replace(hour=0,minute=1)
        self.assertIsNone(fresh_daily_value(dict(state=24,last_updated=(now-timedelta(minutes=2)).isoformat()),now))

    def test_measurement_artifacts_are_separate_from_high_consumption(self):
        days={'2026-09-15':dict(kwh=0,hours=[0]*24),
              '2026-09-16':dict(kwh=111,hours=[109]+[2/23]*23),
              '2026-09-17':dict(kwh=48,hours=[2]*24)}
        self.assertEqual(set(day_quality(days)),{'2026-09-15','2026-09-16'})


class FrozenAccuracy(unittest.TestCase):
    def test_retraining_cannot_rewrite_an_issued_prediction(self):
        ledger={}
        args=dict(now=NOW,forecast=10,baseline=12,method='v4',trained_through='2026-09-17')
        self.assertTrue(freeze(ledger,**args));before=copy.deepcopy(ledger)
        self.assertFalse(freeze(ledger,**dict(args,forecast=99)))
        self.assertEqual(ledger,before)

    def test_training_cutoff_is_strictly_before_issue_date(self):
        with self.assertRaises(ValueError):freeze({},now=NOW,forecast=1,baseline=1,method='v4',trained_through=str(NOW.date()))

    def test_no_fake_accuracy_before_actuals(self):
        ledger={};freeze(ledger,now=NOW,forecast=10,baseline=12,method='v4',trained_through='2026-09-17')
        r=score(ledger,today=NOW.date(),days={},invalid={},method='v4')
        self.assertEqual(r['sample_days'],0);self.assertIsNone(r['mae_kwh'])

    def test_high_valid_actual_is_scored_and_not_silently_filtered(self):
        ledger={};freeze(ledger,now=NOW,forecast=10,baseline=12,method='v4',trained_through='2026-09-17')
        r=score(ledger,today=date(2026,9,20),days={'2026-09-19':{'kwh':100}},invalid={},method='v4')
        self.assertEqual(r['mae_kwh'],90);self.assertEqual(r['bias_kwh'],-90)
        self.assertEqual(r['baseline_mae_kwh'],88)

    def test_quality_failure_is_visible_without_becoming_zero_error(self):
        ledger={};freeze(ledger,now=NOW,forecast=10,baseline=12,method='v4',trained_through='2026-09-17')
        r=score(ledger,today=date(2026,9,20),days={'2026-09-19':{'kwh':0}},invalid={'2026-09-19':'zero'},method='v4')
        self.assertEqual(r['sample_days'],0);self.assertEqual(r['rejected_actual_dates'],['2026-09-19'])


class NightConsumer(unittest.TestCase):
    def app(self, data):
        from energy_system.night import build_night_mixin
        from energy_system.site_registry import get_site
        app=build_night_mixin(get_site('home')['energy'])()
        app.get_state=lambda *a,**k:data
        app.get_daily_consumption=lambda:48+.13*24
        return app

    def test_fallback_adds_standby_only_once(self):
        with patch('energy_system.night.datetime') as clock:
            clock.now.return_value=NOW
            self.assertEqual(self.app({}).consumption_until_time(NOW+timedelta(hours=1)),2.13)

    def test_night_uses_two_dated_forecasts(self):
        with patch('energy_system.night.datetime') as clock:
            clock.now.return_value=NOW
            self.assertEqual(self.app(record()).consumption_until_time(NOW+timedelta(hours=2)),3.26)


class DailyInput(unittest.TestCase):
    def test_both_plants_use_their_own_fresh_daily_counter(self):
        from energy_system.horizon_adapter import build_horizon_mixin
        from energy_system.site_registry import get_site
        for site in ('home','eimo'):
            profile=get_site(site)['energy'];app=build_horizon_mixin(profile)()
            app._read_state_record=lambda entity: dict(state=8,last_updated=NOW.isoformat()) if entity==profile['CONSUMPTION_DAILY_SENSOR'] else {}
            self.assertEqual(app._consumed_today(NOW),8)
            app._read_state_record=lambda entity:dict(state=8,last_updated=(NOW-timedelta(hours=2)).isoformat())
            self.assertIsNone(app._consumed_today(NOW))


if __name__=='__main__':unittest.main()
