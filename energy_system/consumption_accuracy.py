"""Immutable next-day predictions, evaluated only after complete actual days."""
from datetime import date, timedelta
from math import sqrt
from statistics import fmean


def freeze(ledger, *, now, forecast, baseline, method, trained_through):
    target = str(now.date()+timedelta(days=1))
    if date.fromisoformat(trained_through) >= now.date():
        raise ValueError('Training data leaks into issue day')
    key = target+'|'+method
    if key in ledger:
        return False
    ledger[key] = dict(target_date=target, issued_at=now.isoformat(),
                       forecast_kwh=float(forecast), baseline_kwh=float(baseline),
                       method=method, trained_through=trained_through)
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
    n = len(scored)
    actual_sum = sum(r['actual_kwh'] for r in scored)
    return dict(source='frozen_next_day_forecasts', method=method, sample_days=n,
                status='ready' if n >= 14 else 'collecting',
                window_start=str(start), window_end=str(today-timedelta(days=1)),
                mae_kwh=round(fmean(abs(r['error']) for r in scored), 3) if n else None,
                rmse_kwh=round(sqrt(fmean(r['error']**2 for r in scored)), 3) if n else None,
                bias_kwh=round(fmean(r['error'] for r in scored), 3) if n else None,
                wape_percent=round(100*sum(abs(r['error']) for r in scored)/actual_sum, 2) if actual_sum else None,
                baseline_mae_kwh=round(fmean(abs(r['baseline_error']) for r in scored), 3) if n else None,
                missing_actual_dates=sorted(pending), rejected_actual_dates=sorted(rejected))
