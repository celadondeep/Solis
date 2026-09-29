"""Dated household forecasts shared by the model and all planning consumers.

The historical hourly_kwh chart is an observation, never a dated forecast.
Rates are kW; integrating them over a local 23/24/25-hour day gives daily_kwh.
"""
from datetime import date, datetime, time, timedelta, timezone
from math import isfinite
from zoneinfo import ZoneInfo


def number(value):
    try:
        value = float(value)
        return value if isfinite(value) else None
    except (ValueError, TypeError):
        return None


def instant(value):
    result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Timestamp needs a timezone')
    return result.astimezone(timezone.utc)


def integrate(load_at, start, end):
    """Walk physical hours and optional forecast breakpoints in UTC."""
    cursor, end = instant(start), instant(end)
    boundaries = sorted({instant(v) for v in getattr(load_at, 'boundaries', ()) if cursor < instant(v) < end})
    index = 0
    total = 0.0
    while cursor < end:
        nxt = min(end, (cursor + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0))
        while index < len(boundaries) and boundaries[index] <= cursor:
            index += 1
        if index < len(boundaries):
            nxt = min(nxt, boundaries[index])
        value = number(load_at(cursor.astimezone(start.tzinfo)))
        if value is None or value < 0:
            raise ValueError('Invalid household rate')
        total += value * (nxt-cursor).total_seconds()/3600
        cursor = nxt
    return total


def rates_for_day(shape, total, day, timezone_name):
    values = [number(shape.get(str(h))) for h in range(24)] if isinstance(shape, dict) else [number(v) for v in shape]
    total = number(total)
    if len(values) != 24 or any(v is None or v < 0 for v in values) or total is None or total <= 0:
        raise ValueError('Invalid household shape or daily total')
    day, tz = date.fromisoformat(str(day)), ZoneInfo(timezone_name)
    first = datetime.combine(day, time.min, tz)
    last = datetime.combine(day+timedelta(days=1), time.min, tz)
    mass = integrate(lambda at: values[at.hour], first, last)
    if mass <= 0:
        raise ValueError('Empty household shape')
    return [v*total/mass for v in values]


def forecast_reader(record, now, tomorrow=None, max_training_age_days=7):
    """Validate once, then return a date-aware rate lookup. Fail closed on v4."""
    attrs = record.get('attributes') or {}
    age = (instant(now)-instant(record.get('last_updated'))).total_seconds()
    if not -300 <= age <= 48*3600:
        raise ValueError('Stale household publication')
    dates = [now.date(), now.date()+timedelta(days=1)]
    if (number(attrs.get('model_version')) or 0) >= 4:
        if attrs.get('forecast_status') not in ('ok', 'cached'):
            raise ValueError('Household model is not ready')
        forecasts = attrs.get('forecasts') or {}
        values = {}
        for day in dates:
            row = forecasts.get(str(day)) or {}
            for key in ('daily_latest_sample', 'hourly_latest_sample'):
                last = date.fromisoformat(str(row.get(key)))
                if not 1 <= (now.date()-last).days <= max_training_age_days:
                    raise ValueError('Stale household training data')
            rates = [number(v) for v in row.get('hourly_kw', [])]
            total = number(row.get('daily_kwh'))
            if len(rates) != 24 or any(v is None or v < 0 for v in rates) or total is None or total <= 0:
                raise ValueError('Missing or invalid dated household forecast')
            first = datetime.combine(day, time.min, now.tzinfo)
            last = datetime.combine(day+timedelta(days=1), time.min, now.tzinfo)
            if abs(integrate(lambda at: rates[at.hour], first, last)-total) > .02:
                raise ValueError('Household forecast total differs from hourly rates')
            values[str(day)] = rates
    else:
        # Migration support for v3; preserve the explicit weekday-corrected total.
        shape = attrs.get('hourly_kwh')
        if not isinstance(shape, list) or len(shape) != 24:
            raise ValueError('Missing household profile')
        today = attrs.get('forecast_today_kwh')
        if today is None and all(number(v) is not None for v in shape):
            today = sum(float(v) for v in shape)
        values = {str(day): rates_for_day(shape, total, day, str(now.tzinfo))
                  for day, total in zip(dates, (today, tomorrow))}

    def load_at(local):
        try:
            return values[str(local.date())][local.hour]
        except KeyError as exc:
            raise ValueError('Household forecast date outside horizon') from exc
    if (number(attrs.get('model_version')) or 0) >= 5:
        from energy_system.consumption_hybrid import hybrid_reader
        return hybrid_reader(load_at, now, attrs.get('hybrid') or {})
    return load_at


def intraday_ratio(actual, expected, gain=0.0):
    actual, expected, gain = number(actual), number(expected), number(gain)
    if actual is None or actual < 0 or expected is None or expected < 2 or gain is None:
        return 1.0
    weight = min(max(0, min(.75, gain)), expected/5 * max(0, min(.75, gain)))
    return max(.7, min(1.6, 1+(actual/expected-1)*weight))


def fresh_daily_value(record, now, max_age=1800):
    try:
        at = instant(record.get('last_reported') or record.get('last_updated'))
        age = (instant(now)-at).total_seconds()
        value = number(record.get('state'))
        if at.astimezone(now.tzinfo).date() == now.date() and 0 <= age <= max_age and value is not None and value >= 0:
            return value
    except (TypeError, ValueError):
        pass
    return None


def day_quality(days):
    """Explicit acquisition failures, separate from statistical training outliers."""
    issues = {}
    for day, item in days.items():
        total = item['kwh']
        if total <= 0:
            issues[day] = 'zero_total_unverified'
        previous = days.get(str(date.fromisoformat(day)-timedelta(days=1)))
        if previous and previous['kwh'] == 0 and total > 20 and max(item['hours'])/total > .9:
            issues[day] = 'concentrated_after_zero_day'
    return issues
