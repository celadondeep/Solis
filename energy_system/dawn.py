"""Next-solar-day headroom and latest feasible night discharge. No HA writes.

Uses S6 Feed-in Priority for days at risk of clipping: load, capped PV export,
battery charge, clipping. The day dispatch function activates that actual mode.
Energy is DC kWh above the unchanged operational floor; grid/load are AC.
"""
from dataclasses import replace
from datetime import timedelta
from math import ceil

from energy_system.horizon import clamp, finite, simulate, stamp


def take_slots(slots, left, right):
    result = []
    for slot in slots:
        a = max(stamp(left), stamp(slot.start))
        b = min(stamp(right), stamp(slot.start) + timedelta(hours=slot.hours))
        if b > a:
            result.append(replace(slot, start=a, hours=(b-a).total_seconds()/3600))
    return result


def morning_budget(day, policy, export_floor_soc=None):
    """Choose dawn headroom from the central forecast; report P10 risk too.

The operational target uses expected dawn-to-net-production deficit plus a
margin. P10 remains visible as a cloudy-case risk metric, but cannot by itself
raise the dawn target enough to cancel all useful discharge.
"""
    deficit = peak_deficit = 0.0
    p10_deficit = p10_peak_deficit = 0.0
    central_repaid = p10_repaid = False
    for slot in day:
        # The central forecast determines useful dawn headroom. P10 is kept
        # as a risk estimate below; treating it as the mandatory SOC target
        # can veto all discharge on days where the low case never recovers.
        net = slot.pv - slot.load
        p10_net = slot.low_pv - slot.load
        if not central_repaid:
            if net >= 0:
                deficit -= min(net, policy.charge_kw)*slot.hours*policy.charge_eff
            else:
                deficit += min(-net, policy.discharge_kw)*slot.hours/policy.discharge_eff
            peak_deficit = max(peak_deficit, deficit)
            if net > 0 and deficit <= 0:
                central_repaid = True
        if not p10_repaid:
            if p10_net >= 0:
                p10_deficit -= min(p10_net, policy.charge_kw)*slot.hours*policy.charge_eff
            else:
                p10_deficit += min(-p10_net, policy.discharge_kw)*slot.hours/policy.discharge_eff
            p10_peak_deficit = max(p10_peak_deficit, p10_deficit)
            if p10_net > 0 and p10_deficit <= 0:
                p10_repaid = True
    operating_floor = max(policy.hard_floor, policy.comfort_soc,
                          finite(export_floor_soc, policy.hard_floor))
    floor_energy = max(0, (operating_floor-policy.hard_floor)*policy.kwh_per_soc)
    minimum = clamp(max(floor_energy, peak_deficit + policy.reserve_margin_kwh), 0, policy.capacity)
    p10_minimum = clamp(max(floor_energy, p10_peak_deficit + policy.reserve_margin_kwh), 0, policy.capacity)
    best = simulate(day, minimum, policy,pv_priority=True)
    full = simulate(day, policy.capacity, policy,pv_priority=True)
    lo, hi = minimum, policy.capacity
    if full['clipped_kwh'] <= best['clipped_kwh'] + 0.02:
        lo = hi
    else:
        for _ in range(22):
            mid = (lo+hi)/2
            if simulate(day, mid, policy,pv_priority=True)['clipped_kwh'] <= best['clipped_kwh'] + 0.02:
                lo = mid
            else:
                hi = mid
    # Round upwards so quantisation never causes extra battery discharge.
    target_soc = min(ceil(policy.storage_ceiling), ceil(policy.hard_floor+lo/policy.kwh_per_soc))
    target_soc = max(target_soc, ceil(policy.hard_floor+minimum/policy.kwh_per_soc))
    target_energy = (target_soc-policy.hard_floor)*policy.kwh_per_soc
    reference = simulate(day, target_energy, policy,pv_priority=True)
    cautious = simulate(day, target_energy, policy, low=True)
    return dict(target_soc=target_soc, target_energy=target_energy,
                reserve_soc=ceil(policy.hard_floor+minimum/policy.kwh_per_soc),
                p10_reserve_soc=ceil(policy.hard_floor+p10_minimum/policy.kwh_per_soc),
                p10_reserve_gap_kwh=round(max(0,p10_minimum-minimum),3),
                required_headroom_kwh=round(max(0,policy.capacity-target_energy),3),
                day_pv_kwh=round(sum(s.pv*s.hours for s in day),3),
                day_load_kwh=round(sum(s.load*s.hours for s in day),3),
                day_export_kwh=round(reference['export_kwh'],3),
                day_clipping_kwh=round(reference['clipped_kwh'],3),
                unavoidable_clipping_kwh=round(best['clipped_kwh'],3),
                day_low_import_kwh=round(cautious['import_kwh'],3))


def day_dispatch(slots,now,soc,policy):
    """Enable the PV export assumed by the dawn budget; TOU discharge OFF."""
    end=(now+timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0)
    day=take_slots(slots,now,end)
    energy=max(0,(soc-policy.hard_floor)*policy.kwh_per_soc)
    own=simulate(day,energy,policy)
    selling=simulate(day,energy,policy,pv_priority=True)
    priority=(policy.export_kw>0 and own['clipped_kwh']-selling['clipped_kwh']>0.05)
    return dict(solar_export_priority=priority,
        day_self_use_clipping_kwh=round(own['clipped_kwh'],3),
        day_pv_priority_clipping_kwh=round(selling['clipped_kwh'],3),
        day_dispatch="load_grid_battery" if priority else "load_battery_grid")


def _retarget_budget(day, policy, budget, target_soc):
    """Refresh all scenario metrics when night hysteresis holds a target."""
    target_soc = int(clamp(ceil(target_soc), budget['reserve_soc'], ceil(policy.storage_ceiling)))
    energy = (target_soc-policy.hard_floor)*policy.kwh_per_soc
    central = simulate(day, energy, policy, pv_priority=True)
    cautious = simulate(day, energy, policy, low=True)
    return dict(budget, target_soc=target_soc, target_energy=energy,
        required_headroom_kwh=round(max(0,policy.capacity-energy),3),
        day_export_kwh=round(central['export_kwh'],3),
        day_clipping_kwh=round(central['clipped_kwh'],3),
        day_low_import_kwh=round(cautious['import_kwh'],3))


def plan_dawn(slots, now, soc, policy, *, production_on=False,
              power_control=True, idle_kw=0.13, off_kw=0.03,
              pv_threshold_kw=0.1, wake_margin_minutes=30,
              execution_margin_minutes=5, min_sleep_minutes=20,
              already_exporting=False, power_on=True, export_floor_soc=None,
              discharge_committed=False, previous_plan=None,
              evening_fraction=0.4, evening_quiet_hour=23.0,
              minimum_phase_kwh=1.0, target_decrease_hysteresis_soc=2.0):
    """Recompute an explicit target/start/deadline from current SOC.

When power control is available the inverter sleeps until the latest
    discharge window. Houses draw from the grid; off_kw is conservatively
    budgeted as battery standby (live off-state still reports ~20 W DC).
Without power control natural household discharge is deducted first.
The export ceiling is shared with PV and never added on top of PV export.
"""
    if finite(soc) is None or not 0 <= soc <= 100:
        raise ValueError('invalid dawn SOC')
    if any(finite(v) is None or v < 0 for v in (idle_kw,off_kw,pv_threshold_kw,
            wake_margin_minutes,execution_margin_minutes,min_sleep_minutes,
            evening_quiet_hour,minimum_phase_kwh,target_decrease_hysteresis_soc)):
        raise ValueError('invalid night policy')
    if finite(evening_fraction) is None or not 0 < evening_fraction < 1:
        raise ValueError('evening_fraction must be between 0 and 1')
    if production_on:
        return None
    now_utc = stamp(now)
    candidates = [s for s in slots if s.pv >= pv_threshold_kw]
    if not candidates:
        raise ValueError('no next PV production window')
    dawn = stamp(candidates[0].start)
    if dawn <= now_utc:
        return None
    local_dawn = dawn.astimezone(now.tzinfo)
    end = (local_dawn+timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0)
    day = take_slots(slots,dawn,end)
    if not day or stamp(day[-1].start)+timedelta(hours=day[-1].hours) < stamp(end)-timedelta(seconds=1):
        raise ValueError('incomplete next solar day')
    budget = morning_budget(day,policy,export_floor_soc)
    prior = previous_plan if isinstance(previous_plan, dict) else {}
    prior_date = prior.get('night_plan_date')
    same_night = prior_date == local_dawn.date().isoformat()
    # The operational target is recomputed from every valid forecast. In
    # particular, a worsening dawn forecast must be allowed to protect more
    # energy; a previous target is execution history, not a safety limit.
    # A small improvement is ignored until it is at least the configured
    # SOC deadband below the last accepted target. Safety increases are
    # immediate, so this cannot preserve an obsolete low reserve.
    prior_target = finite(prior.get('night_plan_target_soc')) if same_night else None
    if (prior_target is not None and budget['target_soc'] < prior_target
            and prior_target-budget['target_soc'] < target_decrease_hysteresis_soc):
        budget = _retarget_budget(day, policy, budget, prior_target)
    night = take_slots(slots,now_utc,dawn)
    energy = max(0,(soc-policy.hard_floor)*policy.kwh_per_soc)
    target = budget['target_energy']
    natural = sum(max(0,s.load-s.pv)*s.hours/policy.discharge_eff for s in night)
    needed = max(0,energy-target)
    off_drain = off_kw*sum(s.hours for s in night) if power_control else 0
    forced = max(0,needed-off_drain) if power_control else max(0,needed-natural)
    split_kwh = max(0.0, needed)
    evening_kwh = split_kwh*evening_fraction
    morning_kwh = split_kwh-evening_kwh
    split_enabled = evening_kwh >= minimum_phase_kwh and morning_kwh >= minimum_phase_kwh
    current_local = now.astimezone(local_dawn.tzinfo)
    quiet = (local_dawn-timedelta(days=1)).replace(hour=int(evening_quiet_hour),
                                  minute=round((evening_quiet_hour%1)*60),
                                  second=0, microsecond=0)
    previous_evening_target = finite(prior.get('evening_target_soc')) if same_night else None
    if same_night and previous_evening_target is not None:
        evening_target = max(budget['target_soc'], previous_evening_target)
    else:
        evening_target = max(budget['target_soc'], ceil(soc-evening_kwh/policy.kwh_per_soc))
    evening_done = bool(same_night and prior.get('evening_done') in (True, 'on', 'true'))
    # An achieved target is recoverable from current SOC too, so a restart
    # between polling cycles cannot start the same evening stage again.
    if split_enabled and soc <= evening_target+0.5:
        evening_done = True
    evening_window = (split_enabled and not evening_done
                      and current_local.date() < local_dawn.date()
                      and current_local.hour + current_local.minute/60 < evening_quiet_hour
                      and current_local.hour >= 12)
    remaining = forced
    ideal_start = None
    for s in reversed(night):
        # Grid export + household + running losses, constrained by battery power.
        natural_kw = min(policy.discharge_kw,max(0,s.load-s.pv))
        export_kw = min(max(0,policy.export_kw-max(0,s.pv-s.load)),
                        max(0,policy.discharge_kw-natural_kw))
        drain_kw = max(0,(natural_kw+export_kw)/policy.discharge_eff-off_kw) \
            if power_control else export_kw/policy.discharge_eff
        if remaining > 1e-8 and drain_kw > 0:
            duration = min(s.hours,remaining/drain_kw)
            remaining -= duration*drain_kw
            ideal_start = stamp(s.start)+timedelta(hours=s.hours-duration)
    feasible = remaining <= 0.02
    morning_start = ideal_start
    if split_enabled and not evening_done and quiet < local_dawn:
        morning_remaining = max(0, (evening_target-budget['target_soc'])*policy.kwh_per_soc
                                - off_kw*(dawn-stamp(quiet)).total_seconds()/3600)
        morning_start = None
        for s in reversed(take_slots(slots, quiet, dawn)):
            natural_kw = min(policy.discharge_kw,max(0,s.load-s.pv))
            export_kw = min(max(0,policy.export_kw-max(0,s.pv-s.load)),
                            max(0,policy.discharge_kw-natural_kw))
            drain_kw = max(0,(natural_kw+export_kw)/policy.discharge_eff-off_kw) \
                if power_control else export_kw/policy.discharge_eff
            if morning_remaining > 1e-8 and drain_kw > 0:
                duration = min(s.hours,morning_remaining/drain_kw)
                morning_remaining -= duration*drain_kw
                morning_start = stamp(s.start)+timedelta(hours=s.hours-duration)
    if morning_start is not None:
        morning_start = max(now_utc, morning_start-timedelta(minutes=execution_margin_minutes))
    # The evening stage uses its own fixed SOC cutoff and must end at quiet
    # time. The remaining energy is recalculated from actual SOC for dawn.
    phase = 'morning'
    if evening_window and policy.export_kw > 0:
        phase = 'evening'
        planned = True
        start = now_utc
        due = soc > evening_target+0.5
        if due:
            wake = now_utc
    else:
        # Explicit zero export limit must never activate TOU.
        planned = forced >= (0.05 if already_exporting or discharge_committed else policy.start_kwh) and policy.export_kw > 0
        start = max(now_utc,ideal_start-timedelta(minutes=execution_margin_minutes)) if planned and ideal_start else None
        # If waking for solar before a short discharge window, its household drain
        # creates some headroom too. Recompute at wake/SOC changes; the hardware
        # cutoff is the final dawn target, never a moving 0.75 kWh burst.
        due = bool(planned and (discharge_committed or start and start <= now_utc+timedelta(seconds=1))
                   and soc > budget['target_soc']+0.5)
        if due:
            # Once the current night's discharge was committed, falling SOC must
            # not move its calculated start into the future and put it back to sleep.
            wake = now_utc
            start = now_utc
    if phase != 'evening':
        wake = max(now_utc,dawn-timedelta(minutes=wake_margin_minutes))
        if start:
            wake = min(wake,start)
    wait_seconds = (wake-now_utc).total_seconds()
    can_sleep = power_control and wait_seconds > 1 and (
        not power_on or wait_seconds >= min_sleep_minutes*60)
    state = 'export' if due else 'sleep' if can_sleep else 'self_use'
    sleep_hours = max(0,(wake-now_utc).total_seconds()/3600) if can_sleep else 0
    off_slots = take_slots(night,now_utc,wake) if can_sleep else []
    standby_saved = max(0,idle_kw-off_kw)*sleep_hours
    house_grid = sum(max(0,s.load-idle_kw)*s.hours for s in off_slots)
    if due:
        if phase == 'evening':
            reason = f"Vakarinis etapas iki {evening_target}%; sustabdyti iki {quiet:%H:%M}, ryto tikslas {budget['target_soc']}%"
        else:
            reason = f"Iškrovimas prieš rytą iki {budget['target_soc']}%, baigti iki {local_dawn:%H:%M}"
    elif can_sleep:
        reason = (f"Nakties miegas; įjungti {wake.astimezone(now.tzinfo):%H:%M}, "
                  f"ryto tikslas {budget['target_soc']}% / {budget['required_headroom_kwh']:.1f} kWh vietos")
    else:
        reason = f"Pasiruošimas rytinei gamybai {local_dawn:%H:%M}; tikslas {budget['target_soc']}%"
    if not feasible and planned:
        reason += f"; iki termino gali trūkti {remaining:.2f} kWh vietos"
    return dict(**budget, state=state, reason=reason, night_active=True,
                inverter_on=not can_sleep, export_now=due,
                cutoff_soc=evening_target if phase == 'evening' else budget['target_soc'],
                discharge_phase=phase,
                night_plan_date=local_dawn.date().isoformat(),
                night_plan_target_soc=budget['target_soc'],
                night_split_enabled=split_enabled,
                discharge_committed=bool(discharge_committed or (phase == 'morning' and due)),
                night_split_total_kwh=round(split_kwh,3),
                evening_kwh=round(evening_kwh if split_enabled else 0,3),
                morning_kwh=round(morning_kwh if split_enabled else split_kwh,3),
                evening_target_soc=evening_target if split_enabled else None,
                evening_quiet_at=quiet.isoformat(),
                evening_done=evening_done or (phase == 'evening' and not due),
                pv_start_at=local_dawn.isoformat(),
                discharge_start_at=start.astimezone(now.tzinfo).isoformat() if start else None,
                morning_planned_start_at=morning_start.astimezone(now.tzinfo).isoformat() if morning_start else None,
                discharge_deadline=local_dawn.isoformat(),
                wake_at=wake.astimezone(now.tzinfo).isoformat(),
                required_discharge_kwh=round(needed,3),
                required_preexport_kwh=round(forced if planned else 0,3),
                natural_discharge_if_on_kwh=round(natural,3),
                feasible=feasible, unmet_headroom_kwh=round(max(0,remaining),3),
                standby_saved_kwh=round(standby_saved,3),
                sleep_grid_import_kwh=round(house_grid,3),
                sleep_battery_standby_kwh=round(off_kw*sleep_hours,3),
                idle_w=round(idle_kw*1000),off_w=round(off_kw*1000),
                power_control_available=power_control, measured_soc=soc)
