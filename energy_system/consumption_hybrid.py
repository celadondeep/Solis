"""Transport-independent household hybrid. All inputs are cached HA readings.

Historical demand remains the fallback. A bounded median residual adjusts only
the next hour; explicit appliance plans replace their historical contribution.
No inverter actions, extra Cloud requests or third-party ML dependencies.
"""
from datetime import date, datetime, time, timedelta, timezone
from statistics import median

from energy_system.consumption_forecast import instant, number, integrate
from energy_system.consumption_math import _quantile
from energy_system.consumption_rolling import rolling_daily_statistics


def capture(samples, record, heartbeat, now, *, max_age=600, window=1200, max_kw=30):
    """Keep distinct physical observations, never repeated HA publications.

    The source heartbeat is required even when HA last_updated is recent.
    Old observations cannot be made fresh by an AppDaemon restart.
    """
    current = instant(now)
    kept = []
    for row in samples:
        try:
            age = (current-instant(row['at'])).total_seconds()
            value = number(row['kw'])
            if 0 <= age <= window and value is not None and 0 <= value <= max_kw:
                kept.append(dict(at=instant(row['at']).isoformat(), kw=value))
        except (KeyError, TypeError, ValueError):
            continue
    reason = 'fresh'
    try:
        numeric = number(heartbeat)
        stamp = datetime.fromtimestamp(numeric/(1000 if numeric>1e11 else 1),timezone.utc) if numeric is not None else instant(heartbeat)
        reported = instant(record.get('last_reported') or record.get('last_updated'))
        unit = (record.get('attributes') or {}).get('unit_of_measurement')
        value = number(record.get('state'))
        if value is None or unit not in ('W', 'kW'):
            raise ValueError('invalid_power')
        value /= 1000 if unit == 'W' else 1
        if not 0 <= value <= max_kw:
            raise ValueError('invalid_power')
        if not (0 <= (current-stamp).total_seconds() <= max_age and
                0 <= (current-reported).total_seconds() <= max_age):
            raise ValueError('stale_source')
        # Reject a new heartbeat paired with an old cached power value.
        if abs((stamp-reported).total_seconds()) > max_age:
            raise ValueError('unpaired_source')
        if kept and stamp <= instant(kept[-1]['at']):
            reason = 'duplicate_source'
        else:
            kept.append(dict(at=stamp.isoformat(), kw=value))
    except (TypeError, ValueError, OverflowError, OSError) as error:
        reason = str(error) if str(error) in ('invalid_power', 'stale_source', 'unpaired_source') else 'invalid_timestamp'
    return sorted(kept, key=lambda r: r['at'])[-60:], reason


def projection(samples, now, baseline, *, daily_ratio=1, gain=.35,
               max_age=600, max_gap=660, min_span=600, max_delta_kw=1.5):
    """Bounded, robust short-term correction. Expired projections are ignored."""
    current = instant(now)
    result = dict(as_of=now.isoformat(), valid_until=(current+timedelta(seconds=360)).isoformat(),
                  cumulative_ratio=max(.7,min(1.6,float(daily_ratio))),
                  power_delta_kw=0.0, horizon_seconds=3600, step_seconds=300,
                  status='warming_up', sample_count=len(samples), span_seconds=0,
                  gain=gain, source_at=None)
    if not samples:
        return result
    stamps = [instant(s['at']) for s in samples]
    result['source_at'] = stamps[-1].isoformat()
    span = (stamps[-1]-stamps[0]).total_seconds()
    result['span_seconds'] = round(span)
    if not 0 <= (current-stamps[-1]).total_seconds() <= max_age:
        result.update(status='stale_source', cumulative_ratio=1.0)
    elif any(not 0 < (b-a).total_seconds() <= max_gap for a,b in zip(stamps,stamps[1:])):
        result['status'] = 'gap_in_samples'
    elif len(samples) >= 3 and span >= min_span:
        residuals = [s['kw']-baseline(at.astimezone(now.tzinfo))*result['cumulative_ratio']
                     for s,at in zip(samples,stamps)]
        # Median suppresses a lone kettle spike. The cap limits battery impact.
        delta = median(residuals)*max(0,min(.5,float(gain)))
        result.update(status='active', power_delta_kw=round(max(-max_delta_kw,min(max_delta_kw,delta)),5))
    return result


def replay_errors(history, *, today, strength=1, window=30, min_days=5, max_samples=60, target_dates=None):
    """Next-day issue schedule: target D only learns observations through D-2."""
    unique = {str(r['date']):float(r['kwh']) for r in history
              if date.fromisoformat(str(r['date'])) < today and number(r.get('kwh')) is not None and float(r['kwh']) > 0}
    rows = [dict(date=d,kwh=v) for d,v in sorted(unique.items())]
    errors = []
    for target, actual in sorted(unique.items()):
        if target_dates is not None and target not in target_dates:
            continue
        day = date.fromisoformat(target)
        issue = day-timedelta(days=1)
        stats = rolling_daily_statistics(rows,today=issue,window_days=window,min_days=min_days)
        if stats['daily_mean_kwh'] is None or not stats['valid_dates']:
            continue
        if (issue-date.fromisoformat(max(stats['valid_dates']))).days > 7:
            continue
        factor = 1+(stats['weekday_factors'][str(day.weekday())]-1)*strength
        prediction = round(stats['daily_mean_kwh']*factor,2)
        errors.append(dict(target_date=target, error_kwh=actual-prediction,
                           predicted_kwh=prediction, actual_kwh=actual,
                           trained_through=max(stats['valid_dates'])))
    return errors[-max_samples:]


def uncertainty(point, errors, *, minimum=14):
    values = [float(r['error_kwh']) for r in errors]
    if len(values) >= minimum:
        low,high = _quantile(values,.1),_quantile(values,.9)
        source = 'causal_replay_residuals'
    else:
        low,high = -max(2,point*.35),max(2,point*.35)
        source = 'uncalibrated_fallback'
    return dict(lower_kwh=round(max(0,min(point,point+low)),2),
                upper_kwh=round(max(point,point+high),2),
                nominal_percent=80, source=source, sample_days=len(values),
                status='historical_estimate' if len(values)>=minimum else 'insufficient_calibration',
                live_coverage_percent=None)


def nowcast_gate(scores, *, previously_enabled=False):
    """Prospective admission with hysteresis, evaluated on identical hours.

    Seven distinct completed days and 48 active-hour pairs are a minimum;
    they are not a significance claim. Requiring a 5% gain reduces needless
    switching. A previously admitted model is withdrawn after >2% regression.
    """
    n=int(scores.get('active_hours',0)); days=int(scores.get('active_days',0))
    candidate=number(scores.get('active_mae_kwh'))
    baseline=number(scores.get('active_baseline_mae_kwh'))
    result=dict(enabled=False,status='collecting',sample_hours=n,sample_days=days,
                required_hours=48,required_days=7,candidate_mae_kwh=candidate,baseline_mae_kwh=baseline)
    if n>=48 and days>=7 and candidate is not None and baseline is not None and baseline>0:
        enabled=candidate<=baseline*(1.02 if previously_enabled else .95)
        result.update(enabled=enabled,status='accepted' if enabled else 'baseline_better')
    return result


def component_fractions(whole_days, component_days, accepted_dates, *, minimum=14):
    """Matched complete dates only; a component must fit within its whole meter."""
    dates = sorted(set(accepted_dates)&set(whole_days)&set(component_days))
    if len(dates) < minimum:
        raise ValueError('insufficient_component_history')
    whole = [sum(whole_days[d]['hours'][h] for d in dates) for h in range(24)]
    part = [sum(component_days[d]['hours'][h] for d in dates) for h in range(24)]
    if any(not 0 <= p <= w+1e-6 for w,p in zip(whole,part)):
        raise ValueError('component_exceeds_household')
    return [p/w if w else 0.0 for w,p in zip(whole,part)], len(dates)


def validate_plan(record, now, *, max_kw, max_age=3600):
    """Explicit dated coverage: an empty slots array means deliberately off.

    Missing/stale plans mean keep the historical contribution. Plan producers
    must provide generated_at, valid_until, covered_dates and nonoverlapping
    slots with timezone-aware start/end and power_kw. Partial coverage is never
    interpreted as zero household demand.
    """
    max_kw=number(max_kw)
    if max_kw is None or max_kw<=0:
        raise ValueError('invalid_appliance_power_limit')
    attrs = record.get('attributes') or {}
    generated, expires = instant(attrs.get('generated_at')), instant(attrs.get('valid_until'))
    current = instant(now)
    if not 0 <= (current-generated).total_seconds() <= max_age or expires <= current:
        raise ValueError('stale_plan')
    dates = attrs.get('covered_dates')
    allowed = {str(now.date()+timedelta(days=i)) for i in (0,1)}
    if not isinstance(dates,list) or not dates or len(dates)!=len(set(dates)) or not set(dates)<=allowed:
        raise ValueError('invalid_plan_dates')
    raw = attrs.get('slots')
    if not isinstance(raw,list) or len(raw)>96:
        raise ValueError('invalid_plan_slots')
    slots=[]
    for row in raw:
        start,end = instant(row['start']),instant(row['end'])
        power=number(row['power_kw'])
        if end<=start or power is None or not 0<=power<=max_kw:
            raise ValueError('invalid_plan_slot')
        cursor=start
        while cursor<end:
            local=cursor.astimezone(now.tzinfo)
            if str(local.date()) not in dates:
                raise ValueError('uncovered_plan_slot')
            cursor=min(end,instant(datetime.combine(local.date()+timedelta(days=1),time.min,now.tzinfo)))
        slots.append(dict(start=start.isoformat(),end=end.isoformat(),power_kw=power))
    slots.sort(key=lambda r:r['start'])
    if any(instant(a['end'])>instant(b['start']) for a,b in zip(slots,slots[1:])):
        raise ValueError('overlapping_plan_slots')
    return dict(generated_at=generated.isoformat(),valid_until=min(expires,generated+timedelta(seconds=max_age)).isoformat(),
                covered_dates=dates,slots=slots)


def hybrid_reader(base, now, data):
    """Add validated optional components and a time-limited nowcast to a base.

    Rates are constant inside explicit 5-minute projection steps. Supplying all
    breakpoints to integrate() conserves energy in partial hours and DST days.
    Stale optional inputs fall back independently to historical demand.
    """
    current=instant(now)
    parts=[]
    for part in data.get('appliances',[]):
        try:
            fractions=[number(x) for x in part['historical_fraction']]
            if len(fractions)!=24 or any(x is None or not 0<=x<=1 for x in fractions):
                continue
            plan=validate_plan({'attributes':part['plan']},now,max_kw=float(part['max_kw']))
            # Multiple component meters may overlap; reject any over-subtraction.
            if any(sum(p['fraction'][h] for p in parts)+fractions[h]>1+1e-6 for h in range(24)):
                continue
            parts.append(dict(fraction=fractions,plan=plan))
        except (KeyError,TypeError,ValueError):
            continue
    p=data.get('projection') or {}
    try:
        as_of,until=instant(p['as_of']),instant(p['valid_until'])
        ratio,delta=number(p['cumulative_ratio']),number(p['power_delta_kw'])
        if (not as_of<=current<until or (until-as_of).total_seconds()>600 or
            ratio is None or not .7<=ratio<=1.6 or delta is None or abs(delta)>1.5 or
            p.get('horizon_seconds')!=3600 or p.get('step_seconds')!=300):
            raise ValueError('invalid_projection')
    except (KeyError,TypeError,ValueError):
        as_of,ratio,delta=None,1.0,0.0
    boundaries=[]
    if as_of is not None:
        boundaries=[as_of+timedelta(seconds=300*i) for i in range(13)]
    for part in parts:
        for slot in part['plan']['slots']:
            boundaries.extend([instant(slot['start']),instant(slot['end'])])

    def rate(local):
        value=base(local)
        at=instant(local)
        # Subtract only on dates explicitly covered by a validated plan.
        planned=0.0;fraction=0.0
        for part in parts:
            if str(local.date()) in part['plan']['covered_dates']:
                fraction+=part['fraction'][local.hour]
                planned+=sum(s['power_kw'] for s in part['plan']['slots'] if instant(s['start'])<=at<instant(s['end']))
        background=value*max(0,1-fraction)
        if as_of is not None and at>=as_of and local.date()==as_of.astimezone(now.tzinfo).date():
            lead=(at-as_of).total_seconds()
            weight=max(0,1-(int(lead//300)+.5)*300/3600)
            background=max(0,background*ratio+delta*weight)
        return background+planned

    rate.boundaries=sorted(set(boundaries))
    rate.hybrid=True
    rate.active_projection=as_of is not None and (p.get('status')=='active' or ratio!=1)
    rate.appliance_count=len(parts)
    return rate
