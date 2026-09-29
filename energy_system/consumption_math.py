"""Gryna, testuojama robust vartojimo statistika."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from math import isfinite
from statistics import median


def _quantile(values, fraction):
    ordered = sorted(values)
    if not ordered:
        raise ValueError("values negali būti tuščias")
    position = (len(ordered) - 1) * float(fraction)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return ordered[low] + (ordered[high] - ordered[low]) * weight


def remaining_profile_ratio(profile, hour, minute=0, second=0):
    """Likusios paros profilio dalis, įskaitant tik likusią einamos valandos dalį."""
    weights = {str(key): float(value) for key, value in profile.items()}
    total = sum(weights.values())
    if total <= 0:
        return 0.0
    hour = max(0, min(int(hour), 23))
    minute = max(0, min(int(minute), 59))
    second = max(0, min(int(second), 59))
    fraction_left = 1.0 - (minute * 60 + second) / 3600.0
    remaining = weights.get(str(hour), 1.0) * fraction_left
    remaining += sum(weights.get(str(h), 1.0) for h in range(hour + 1, 24))
    return max(0.0, min(remaining / total, 1.0))


def robust_consumption_statistics(
    samples,
    *,
    median_window_days=14,
    shrinkage=3.0,
    iqr_multiplier=2.5,
):
    """Mokosi iš medianų, o akivaizdžius skaitiklio šuolius izoliuoja.

    Laukas value turi būti jau normalizuotas pagal sezoną. Pasikartojus tai
    pačiai datai naudojamas paskutinis įrašas.
    """
    unique = {}
    for item in samples:
        try:
            day = datetime.strptime(str(item["date"]), "%Y-%m-%d").date()
            value = float(item["value"])
            raw_kwh = float(item.get("raw_kwh", value))
            if not isfinite(value) or value <= 0:
                continue
        except (KeyError, TypeError, ValueError):
            continue
        unique[day.isoformat()] = {
            "date": day.isoformat(),
            "weekday": str(day.weekday()),
            "value": value,
            "raw_kwh": raw_kwh,
        }

    ordered = [unique[key] for key in sorted(unique)]
    if len(ordered) < 3:
        return None

    values = [item["value"] for item in ordered]
    anomaly_days = []
    usable = ordered
    if len(values) >= 8:
        q1 = _quantile(values, 0.25)
        q3 = _quantile(values, 0.75)
        iqr = q3 - q1
        if iqr > 0:
            lower = max(0.0, q1 - iqr_multiplier * iqr)
            upper = q3 + iqr_multiplier * iqr
            anomaly_days = [
                item["date"] for item in ordered
                if item["value"] < lower or item["value"] > upper
            ]
            candidate = [
                item for item in ordered if item["date"] not in anomaly_days
            ]
            if len(candidate) >= 3:
                usable = candidate
            else:
                anomaly_days = []

    overall = median(item["value"] for item in usable)
    if not isfinite(overall) or overall <= 0:
        return None

    by_weekday = defaultdict(list)
    for item in usable:
        by_weekday[item["weekday"]].append(item["value"])

    factors = {}
    for weekday in map(str, range(7)):
        values_for_day = by_weekday.get(weekday, [])
        if not values_for_day:
            factors[weekday] = 1.0
            continue
        learned = median(values_for_day) / overall
        count = len(values_for_day)
        weight = count / (count + float(shrinkage))
        factors[weekday] = round(1.0 + (learned - 1.0) * weight, 3)

    window = usable[-max(1, int(median_window_days)):]
    baselines = [
        item["value"] / (factors.get(item["weekday"], 1.0) or 1.0)
        for item in window
    ]
    daily_avg = median(baselines)

    return {
        "daily_avg": daily_avg,
        "weekday_factors": factors,
        "anomaly_days": anomaly_days,
        "usable_days": len(usable),
        "history_days": len(ordered),
    }
