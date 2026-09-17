"""Calendar-window consumption estimates from local HA recorder statistics.

Missing hours are never zero-filled. Local-day coverage is checked in UTC so
23/25-hour DST days are accepted without inventing or dropping energy.
"""
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from math import isfinite
from statistics import fmean, median
from zoneinfo import ZoneInfo

from energy_system.consumption_math import _quantile


def window_bounds(today, window_days=30):
    today = date.fromisoformat(str(today))
    window_days = int(window_days)
    if not 7 <= window_days <= 120:
        raise ValueError("ROLLING_WINDOW_DAYS must be between 7 and 120")
    return today - timedelta(days=window_days), today


def _outliers(samples, multiplier=2.5):
    if len(samples) < 8:
        return []
    values = [v for _, v in samples]
    q1, q3 = _quantile(values, .25), _quantile(values, .75)
    # A constant baseline must still reject an isolated counter jump. The
    # relative floor also avoids over-filtering genuinely quiet households.
    spread = max(q3 - q1, median(values) * .25, .1)
    lower, upper = max(0, q1 - multiplier * spread), q3 + multiplier * spread
    return [day for day, value in samples if not lower <= value <= upper]


def rolling_daily_statistics(history, *, today, window_days=30, min_days=5):
    start, end = window_bounds(today, window_days)
    unique = {}
    for item in history:
        try:
            day = date.fromisoformat(str(item['date']))
            value = float(item['kwh'])
            if start <= day < end and isfinite(value) and value > 0:
                unique[day.isoformat()] = value
        except (KeyError, TypeError, ValueError):
            continue
    excluded = _outliers(sorted(unique.items()))
    valid = [(day, value) for day, value in sorted(unique.items()) if day not in excluded]
    weekdays = [[v for d, v in valid if date.fromisoformat(d).weekday() == wd] for wd in range(7)]
    avg = fmean(v for _, v in valid) if len(valid) >= min_days else None
    counts = [len(values) for values in weekdays]
    means = [fmean(values) if values else None for values in weekdays]
    # Four or five examples per weekday are useful, but not sufficient for
    # large day-specific forecast corrections: shrink towards the common mean.
    factors = {str(i): 1.0 if avg is None or means[i] is None else
               1 + (means[i] / avg - 1) * counts[i] / (counts[i] + 3.0)
               for i in range(7)}
    return {
        'window_days': int(window_days), 'window_start': start.isoformat(),
        'window_end': (end - timedelta(days=1)).isoformat(),
        'daily_sample_days': len(valid), 'daily_observed_days': len(unique),
        'daily_mean_kwh': avg, 'weekday_kwh': means, 'weekday_counts': counts,
        'weekday_factors': factors, 'anomaly_days': excluded,
        'valid_dates': [d for d, _ in valid],
        'daily_status': 'ok' if avg is not None else 'insufficient_data',
    }


def _datetime(value):
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, (float, int)):
        result = datetime.fromtimestamp(value / (1000 if value > 1e11 else 1), timezone.utc)
    else:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Recorder timestamp must have an explicit timezone')
    return result.astimezone(timezone.utc)


def recorder_days(rows, *, today, window_days=30, timezone_name='Europe/Vilnius'):
    """Return complete days and incomplete dates; duplicate hours are idempotent."""
    start, end = window_bounds(today, window_days)
    tz = ZoneInfo(timezone_name)
    points, invalid = {}, set()
    for row in rows:
        try:
            stamp = _datetime(row['start'])
            if not start <= stamp.astimezone(tz).date() < end:
                continue
            finish = _datetime(row['end']) if row.get('end') is not None else stamp + timedelta(hours=1)
            value = float(row['change'])
            if not isfinite(value) or value < 0 or finish - stamp != timedelta(hours=1) or stamp.minute or stamp.second or stamp.microsecond:
                invalid.add(stamp)
                continue
            if stamp in points and abs(points[stamp] - value) > 1e-8:
                invalid.add(stamp)
            points[stamp] = value
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    complete, incomplete = {}, []
    day = start
    while day < end:
        first = datetime.combine(day, time.min, tz).astimezone(timezone.utc)
        last = datetime.combine(day + timedelta(days=1), time.min, tz).astimezone(timezone.utc)
        expected = [first + timedelta(hours=i) for i in range(int((last-first).total_seconds()/3600))]
        key = day.isoformat()
        if any(stamp not in points or stamp in invalid for stamp in expected):
            incomplete.append(key)
        else:
            hours, counts = [0.0]*24, [0]*24
            for stamp in expected:
                hour = stamp.astimezone(tz).hour
                hours[hour] += points[stamp]
                counts[hour] += 1
            complete[key] = {'kwh': sum(hours), 'hours': hours, 'counts': counts}
        day += timedelta(days=1)
    return complete, incomplete


def rolling_hourly_statistics(days, *, excluded_days=(), min_days=5, min_total=1.0):
    eligible = [(d, item['kwh']) for d, item in days.items() if item['kwh'] >= min_total]
    anomalies = set(excluded_days) | set(_outliers(eligible))
    valid = [(d, item) for d, item in sorted(days.items()) if item['kwh'] >= min_total and d not in anomalies]
    counts = [sum(item['counts'][h] for _, item in valid) for h in range(24)]
    means = [sum(item['hours'][h] for _, item in valid)/counts[h] if counts[h] else None for h in range(24)]
    ok = len(valid) >= min_days and all(value is not None for value in means)
    total = sum(means) if ok else 0
    return {
        'hourly_status': 'ok' if ok and total > 0 else 'insufficient_data',
        'hourly_sample_days': len(valid), 'hourly_counts': counts,
        'hourly_kwh': means if ok else [None]*24,
        'hourly_profile': {str(h): means[h]*24/total for h in range(24)} if ok and total > 0 else None,
        'hourly_excluded_days': sorted(d for d in days if d in anomalies or days[d]['kwh'] < min_total),
    }
