import copy
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'work/appdaemon/apps') if (ROOT/'work').exists() else str(ROOT))
from energy_system.consumption_hybrid import capture, projection, hybrid_reader, replay_errors, uncertainty, component_fractions, validate_plan, nowcast_gate
from energy_system.consumption_forecast import integrate, forecast_reader
from energy_system.consumption_accuracy import freeze, score, freeze_hour, score_hours
from energy_system.horizon import forecast_slots, ha_attributes
from test_consumption_forecast import record, TZ

NOW=datetime(2026,9,18,12,2,tzinfo=TZ)


def power(at,kw=1):
    return dict(state=kw*1000,last_updated=at.isoformat(),attributes={'unit_of_measurement':'W'})


def samples(kw=2):
    return [dict(at=(NOW-timedelta(minutes=i)).isoformat(),kw=kw) for i in (15,10,5,0)]


def plan(now=NOW, slots=None):
    return dict(attributes=dict(generated_at=now.isoformat(),valid_until=(now+timedelta(hours=1)).isoformat(),
        covered_dates=[str(now.date()),str(now.date()+timedelta(days=1))],slots=slots or []))


class SourceQuality(unittest.TestCase):
    def test_refresh_does_not_duplicate_cloud_measurement(self):
        values,status=capture([],power(NOW),str(NOW.timestamp()),NOW)
        self.assertEqual(status,'fresh');self.assertEqual(len(values),1)
        values,status=capture(values,power(NOW+timedelta(minutes=1)),NOW.timestamp(),NOW+timedelta(minutes=1))
        self.assertEqual(status,'duplicate_source');self.assertEqual(len(values),1)

    def test_cloud_milliseconds_and_iso_modbus_are_equivalent(self):
        for stamp in (str(NOW.timestamp()),NOW.timestamp()*1000,NOW.isoformat()):
            values,status=capture([],power(NOW),stamp,NOW)
            self.assertEqual(status,'fresh');self.assertEqual(values[0]['kw'],1)

    def test_fresh_ha_state_cannot_refresh_stale_source(self):
        values,status=capture([],power(NOW),NOW-timedelta(minutes=20),NOW)
        self.assertEqual(status,'stale_source');self.assertEqual(values,[])

    def test_old_power_with_new_heartbeat_is_rejected(self):
        values,status=capture([],power(NOW-timedelta(minutes=20)),NOW,NOW)
        self.assertEqual(status,'stale_source');self.assertEqual(values,[])

    def test_future_unknown_units_and_nan_are_rejected(self):
        for state,hb in [(power(NOW),NOW+timedelta(minutes=1)),(power(NOW,float('nan')),NOW),
                         (dict(power(NOW),attributes={'unit_of_measurement':'A'}),NOW)]:
            values,status=capture([],state,hb,NOW)
            self.assertNotEqual(status,'fresh');self.assertEqual(values,[])

    def test_observation_requires_duration_and_no_gaps(self):
        self.assertEqual(projection(samples()[-2:],NOW,lambda t:1)['status'],'warming_up')
        data=samples();data[1]['at']=(NOW-timedelta(minutes=14)).isoformat()
        self.assertEqual(projection(data,NOW,lambda t:1,max_gap=360)['status'],'gap_in_samples')
        self.assertEqual(projection(samples(),NOW+timedelta(minutes=20),lambda t:1)['status'],'stale_source')

    def test_single_kettle_spike_is_not_extrapolated(self):
        data=samples(1);data[-1]['kw']=8
        self.assertEqual(projection(data,NOW,lambda t:1)['power_delta_kw'],0)
        self.assertAlmostEqual(projection(samples(2),NOW,lambda t:1)['power_delta_kw'],.35)

    def test_extreme_persistent_load_is_bounded(self):
        self.assertEqual(projection(samples(20),NOW,lambda t:1)['power_delta_kw'],1.5)


class CoherentForecast(unittest.TestCase):
    def reader(self,now=NOW,**changes):
        p=projection(samples(2),NOW,lambda t:1)
        p.update(changes)
        return hybrid_reader(lambda t:1,now,{'projection':p})

    def test_nowcast_integral_and_expiration(self):
        r=self.reader()
        self.assertAlmostEqual(integrate(r,NOW,NOW+timedelta(hours=1)),1+.35/2)
        r=self.reader(now=NOW+timedelta(minutes=7))
        self.assertEqual(integrate(r,NOW,NOW+timedelta(hours=1)),1)
        self.assertFalse(r.active_projection)

    def test_tomorrow_and_past_are_never_nowcast(self):
        r=self.reader(cumulative_ratio=1.4)
        self.assertEqual(r(NOW-timedelta(minutes=1)),1)
        self.assertEqual(r(NOW+timedelta(days=1)),1)
        self.assertEqual(r(NOW+timedelta(hours=2)),1.4)

    def test_midnight_and_dst_conserve_baseline(self):
        for day in (date(2026,3,29),date(2026,10,25)):
            first=datetime.combine(day,time.min,TZ);last=datetime.combine(day+timedelta(days=1),time.min,TZ)
            data=record(first);data['attributes'].update(model_version=5,hybrid={})
            self.assertAlmostEqual(integrate(forecast_reader(data,first),first,last),24)
        now=NOW.replace(hour=23,minute=50)
        p=projection([],now,lambda t:1,daily_ratio=1.5)
        r=hybrid_reader(lambda t:1,now,{'projection':p})
        self.assertAlmostEqual(integrate(r,now,now+timedelta(minutes=20)),.25+1/6)

    def test_rate_never_negative(self):
        r=self.reader(power_delta_kw=-1.5)
        self.assertGreaterEqual(min(r(NOW+timedelta(minutes=i)) for i in range(60)),0)

    def test_planner_and_remaining_sensor_agree_for_partial_slots(self):
        reader=self.reader()
        start=NOW.replace(minute=0)
        end=start+timedelta(hours=2)
        rows=[dict(period_start=(start+timedelta(minutes=30*i)).isoformat(),pv_estimate=1,pv_estimate10=.5) for i in range(4)]
        slots,_,_=forecast_slots(rows,NOW,end,reader,lambda h:1,self_kw=.13)
        house=sum(s.hours*(s.load-.13) for s in slots)
        self.assertAlmostEqual(house,integrate(reader,NOW,end))

    def test_serialized_zero_values_round_trip(self):
        data=record(NOW);data['attributes'].update(model_version=5,hybrid={'projection':projection(samples(1),NOW,lambda t:1)})
        data['attributes']=ha_attributes(data['attributes'])
        self.assertEqual(forecast_reader(data,NOW)(NOW),1)

    def test_malformed_projection_falls_back(self):
        for change in ({'power_delta_kw':float('nan')},{'cumulative_ratio':9},{'valid_until':(NOW+timedelta(days=1)).isoformat()}):
            r=self.reader(**change)
            self.assertEqual(r(NOW),1);self.assertFalse(r.active_projection)


class ApplianceReplacement(unittest.TestCase):
    def reader(self,record,now=NOW):
        return hybrid_reader(lambda t:1,now,dict(appliances=[dict(max_kw=3,historical_fraction=[.25]*24,plan=record['attributes'])]))

    def test_explicit_off_replaces_component_without_removing_other_loads(self):
        self.assertEqual(self.reader(plan())(NOW),.75)

    def test_partial_hour_plan_adds_exact_energy_without_double_count(self):
        r=self.reader(plan(slots=[dict(start=(NOW+timedelta(minutes=7)).isoformat(),end=(NOW+timedelta(minutes=22)).isoformat(),power_kw=2)]))
        self.assertAlmostEqual(integrate(r,NOW,NOW+timedelta(hours=1)),.75+.5)

    def test_absent_stale_or_invalid_plan_keeps_historical_load(self):
        invalid=plan();invalid['attributes'].pop('slots')
        self.assertEqual(self.reader(invalid)(NOW),1)
        self.assertEqual(self.reader(plan(),now=NOW+timedelta(hours=2))(NOW),1)
        bad=plan(slots=[dict(start=NOW.isoformat(),end=(NOW+timedelta(minutes=5)).isoformat(),power_kw=4)])
        with self.assertRaises(ValueError):validate_plan(bad,NOW,max_kw=3)

    def test_overlapping_or_undated_plans_rejected(self):
        slot=dict(start=NOW.isoformat(),end=(NOW+timedelta(hours=1)).isoformat(),power_kw=1)
        with self.assertRaises(ValueError):validate_plan(plan(slots=[slot,slot]),NOW,max_kw=3)
        bad=plan();bad['attributes']['covered_dates']=[]
        with self.assertRaises(ValueError):validate_plan(bad,NOW,max_kw=3)

    def test_components_cannot_exceed_whole_meter(self):
        whole={str(NOW.date()-timedelta(days=i)):dict(hours=[1]*24) for i in range(1,16)}
        parts={d:dict(hours=[.2]*24) for d in whole}
        fractions,count=component_fractions(whole,parts,whole)
        self.assertEqual(count,15);self.assertAlmostEqual(fractions[0],.2)
        with self.assertRaises(ValueError):component_fractions(whole,parts,list(whole)[:3])
        parts={d:dict(hours=[2]*24) for d in whole}
        with self.assertRaises(ValueError):component_fractions(whole,parts,whole)


class Calibration(unittest.TestCase):
    def test_candidate_requires_fresh_prospective_evidence_and_recovers_on_regression(self):
        good=dict(active_hours=60,active_days=7,active_mae_kwh=.18,active_baseline_mae_kwh=.2)
        self.assertTrue(nowcast_gate(good)['enabled'])
        self.assertFalse(nowcast_gate(dict(good,active_days=6))['enabled'])
        self.assertFalse(nowcast_gate(dict(good,active_hours=47))['enabled'])
        self.assertFalse(nowcast_gate(dict(good,active_mae_kwh=.21),previously_enabled=True)['enabled'])
        self.assertTrue(nowcast_gate(dict(good,active_mae_kwh=.202),previously_enabled=True)['enabled'])
        self.assertFalse(nowcast_gate(dict(good,active_mae_kwh=.202))['enabled'])

    def test_hour_forecast_frozen_before_target_and_dst_actual_not_invented(self):
        ledger={}
        args=dict(forecast=lambda t:2,baseline=lambda t:1,active=True,applied=lambda t:1)
        self.assertFalse(freeze_hour(ledger,now=NOW,**args))
        issue=NOW.replace(minute=56)
        self.assertTrue(freeze_hour(ledger,now=issue,**args))
        self.assertFalse(freeze_hour(ledger,now=issue+timedelta(minutes=1),**args))
        row=next(iter(ledger.values()))
        self.assertLess(datetime.fromisoformat(row['issued_at']),datetime.fromisoformat(row['target_start']))
        day=dict(hours=[1]*24,counts=[1]*24)
        result=score_hours(ledger,today=NOW.date()+timedelta(days=1),days={str(NOW.date()):day},invalid={})
        self.assertEqual(result['sample_hours'],1);self.assertEqual(result['applied_mae_kwh'],0)
        self.assertEqual(result['active_mae_kwh'],1)
        day['counts'][13]=2
        self.assertEqual(score_hours(ledger,today=NOW.date()+timedelta(days=1),days={str(NOW.date()):day},invalid={})['sample_hours'],0)

    def history(self):
        return [dict(date=str(NOW.date()-timedelta(days=i)),kwh=10+i%4) for i in range(1,81)]

    def test_future_actual_cannot_change_earlier_prediction(self):
        h=self.history();first=replay_errors(h,today=NOW.date())
        h[2]['kwh']=1000
        second=replay_errors(h,today=NOW.date())
        target=str(NOW.date()-timedelta(days=3))
        self.assertEqual([r['predicted_kwh'] for r in first if r['target_date']<=target],
                         [r['predicted_kwh'] for r in second if r['target_date']<=target])
        for row in first:self.assertLess(date.fromisoformat(row['trained_through']),date.fromisoformat(row['target_date'])-timedelta(days=1))

    def test_range_contains_point_and_has_no_fake_live_coverage(self):
        r=uncertainty(12,replay_errors(self.history(),today=NOW.date()))
        self.assertLessEqual(r['lower_kwh'],12);self.assertGreaterEqual(r['upper_kwh'],12)
        self.assertIsNone(r['live_coverage_percent'])
        self.assertEqual(uncertainty(12,[])['status'],'insufficient_calibration')

    def test_frozen_interval_and_prospective_coverage(self):
        ledger={};bounds=uncertainty(12,[])
        freeze(ledger,now=NOW,forecast=12,baseline=11,method='v5',trained_through='2026-09-17',interval=bounds)
        original=copy.deepcopy(ledger);bounds['upper_kwh']=100
        self.assertEqual(ledger,original)
        r=score(ledger,today=date(2026,9,20),days={'2026-09-19':dict(kwh=12)},invalid={},method='v5')
        self.assertEqual(r['interval_sample_days'],1);self.assertEqual(r['interval_coverage_percent'],100)


if __name__=='__main__':unittest.main()
