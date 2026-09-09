from datetime import datetime, timedelta


def _ceil_to_minute(value: datetime) -> datetime:
    """Round a datetime up to the first whole minute at/after value."""
    rounded = value.replace(second=0, microsecond=0)
    if rounded < value:
        rounded += timedelta(minutes=1)
    return rounded


def forecast_threshold_time(
    detailed_forecast,
    threshold_kw: float,
    interval_minutes: int = 30,
):
    """Estimate the first rising threshold crossing from Solcast intervals.

    Solcast pv_estimate values represent average power for a forecast interval.
    For a 30-minute interval, the best point representation is the interval
    midpoint. The crossing is linearly interpolated between adjacent midpoint
    samples and rounded UP to the first whole minute where forecast >= threshold.
    """
    points = []
    midpoint_offset = timedelta(minutes=interval_minutes / 2)

    for period in detailed_forecast or []:
        try:
            start = datetime.fromisoformat(str(period["period_start"]))
            power_kw = float(period.get("pv_estimate", 0.0))
        except (KeyError, TypeError, ValueError):
            continue
        points.append((start + midpoint_offset, power_kw))

    if not points:
        return None

    points.sort(key=lambda item: item[0])

    prev_time, prev_power = points[0]
    if prev_power >= threshold_kw:
        return _ceil_to_minute(prev_time)

    for current_time, current_power in points[1:]:
        if prev_power < threshold_kw <= current_power:
            delta_power = current_power - prev_power
            if delta_power <= 0:
                crossing = current_time
            else:
                fraction = (threshold_kw - prev_power) / delta_power
                crossing = prev_time + (current_time - prev_time) * fraction
            return _ceil_to_minute(crossing)

        prev_time = current_time
        prev_power = current_power

    return None
