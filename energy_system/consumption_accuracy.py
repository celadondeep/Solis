"""Immutable next-day predictions, evaluated only after complete actual days."""
from datetime import date, datetime, timedelta, timezone
from math import sqrt
from statistics import fmean
from energy_system.consumption_forecast import integrate, instant


def freeze(ledger, *, now, forecast, baseline, method, trained_through, interval=None):
    target = str(now.date()+timedelta(days=1))
    if date.fromisoformat(trained_through) >= now.date():
        raise ValueError('Training data leaks into issue day')
    key = target+'|'+method
    if key in ledger:
        return False
    ledger[key] = dict(target_date=target, issued_at=now.isoformat(),
                       forecast_kwh=float(forecast), baseline_kwh=float(baseline),
                       method=method, trained_through=trained_through)
    if interval is not None:
        ledger[key]['interval'] = dict(interval)
    return True


def score(ledger, *, today, days, invalid, method, window_days=30):
    start = today-timedelta(days=window_days)
    scored, pending, rejected = [], [], []
    for row in ledger.values():
        target = date.fromisoformat(row['target_date'])
        if row['method'] != method or not start <= target < today:
            continue
        item = days.get(str(target))
        if str(target) in invalid:
            rejected.append(str(target))
        elif item is None:
            pending.append(str(target))
        else:
            # Do not remove a high actual just because training would filter it.
            actual = item['kwh']
            scored.append(dict(date=str(target), actual_kwh=actual,
                               error=row['forecast_kwh']-actual,
                               baseline_error=row['baseline_kwh']-actual))
            interval = row.get('interval') or {}
            if interval.get('lower_kwh') is not None and interval.get('upper_kwh') is not None:
                scored[-1]['interval_hit'] = interval['lower_kwh'] <= actual <= interval['upper_kwh']
    n = len(scored)
    actual_sum = sum(r['actual_kwh'] for r in scored)
    intervals = [r['interval_hit'] for r in scored if 'interval_hit' in r]
    return dict(source='frozen_next_day_forecasts', method=method, sample_days=n,
                status='ready' if n >= 14 else 'collecting',
                window_start=str(start), window_end=str(today-timedelta(days=1)),
                mae_kwh=round(fmean(abs(r['error']) for r in scored), 3) if n else None,
                rmse_kwh=round(sqrt(fmean(r['error']**2 for r in scored)), 3) if n else None,
                bias_kwh=round(fmean(r['error'] for r in scored), 3) if n else None,
                wape_percent=round(100*sum(abs(r['error']) for r in scored)/actual_sum, 2) if actual_sum else None,
                baseline_mae_kwh=round(fmean(abs(r['baseline_error']) for r in scored), 3) if n else None,
                interval_sample_days=len(intervals),
                interval_coverage_percent=round(100*sum(intervals)/len(intervals),2) if intervals else None,
                missing_actual_dates=sorted(pending), rejected_actual_dates=sorted(rejected))


def freeze_hour(ledger, *, now, forecast, baseline, active, applied=None):
    """First prediction in the five minutes BEFORE a physical hour begins."""
    current=instant(now)
    if current.minute < 55:
        return False
    start=(current+timedelta(hours=1)).replace(minute=0,second=0,microsecond=0)
    end=start+timedelta(hours=1)
    key=start.isoformat()
    if key in ledger:
        return False
    first,last=start.astimezone(now.tzinfo),end.astimezone(now.tzinfo)
    ledger[key]=dict(target_start=key, target_date=str(first.date()), hour=first.hour,
                     issued_at=now.isoformat(), active=bool(active),
                     forecast_kwh=integrate(forecast,first,last), baseline_kwh=integrate(baseline,first,last),
                     applied_kwh=integrate(applied or forecast,first,last))
    cutoff=current-timedelta(days=14)
    for old in list(ledger):
        if instant(old)<cutoff:
            del ledger[old]
    return True


def score_hours(ledger, *, today, days, invalid):
    scored=[]
    for row in ledger.values():
        if not today-timedelta(days=14)<=date.fromisoformat(row['target_date'])<today or row['target_date'] in invalid:
            continue
        day=days.get(row['target_date']);hour=row['hour']
        # Recorder folds the repeated DST hour; it cannot score each physical
        # hour separately from this daily summary. Never invent those actuals.
        if day is None or day['counts'][hour]!=1:
            continue
        actual=day['hours'][hour]
        scored.append(dict(error=abs(row['forecast_kwh']-actual),
                           baseline=abs(row['baseline_kwh']-actual), active=row['active'], date=row['target_date'],
                           applied=abs(row.get('applied_kwh',row['forecast_kwh'])-actual)))
    active=[r for r in scored if r['active']]
    return dict(source='frozen_hour_ahead_forecasts', sample_hours=len(scored), active_hours=len(active),
                active_days=len({r['date'] for r in active}),
                mae_kwh=round(fmean(r['error'] for r in scored),4) if scored else None,
                applied_mae_kwh=round(fmean(r['applied'] for r in scored),4) if scored else None,
                baseline_mae_kwh=round(fmean(r['baseline'] for r in scored),4) if scored else None,
                active_mae_kwh=round(fmean(r['error'] for r in active),4) if active else None,
                active_baseline_mae_kwh=round(fmean(r['baseline'] for r in active),4) if active else None)
