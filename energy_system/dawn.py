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
    """Largest dawn SOC with no additional avoidable capacity clipping.

The minimum retains the P10 dawn-to-net-production deficit and a small
margin. A second cloudy evening cannot demand an 80% reserve at dawn and
thereby veto all useful headroom. P10 full-day outcomes remain diagnostic.
"""
    deficit = peak_deficit = 0.0
    for slot in day:
        net = slot.low_pv - slot.load
        if net >= 0:
            deficit -= min(net, policy.charge_kw)*slot.hours*policy.charge_eff
        else:
            deficit += min(-net, policy.discharge_kw)*slot.hours/policy.discharge_eff
        peak_deficit = max(peak_deficit, deficit)
        if net > 0 and deficit <= 0:
            break
    floor_energy = max(0, ((export_floor_soc or policy.hard_floor)-policy.hard_floor)*policy.kwh_per_soc)
    minimum = clamp(max(floor_energy, peak_deficit + policy.reserve_margin_kwh), 0, policy.capacity)
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
    target_energy = (target_soc-policy.hard_floor)*policy.kwh_per_soc
    reference = simulate(day, target_energy, policy,pv_priority=True)
    cautious = simulate(day, target_energy, policy, low=True)
    return dict(target_soc=target_soc, target_energy=target_energy,
                reserve_soc=ceil(policy.hard_floor+minimum/policy.kwh_per_soc),
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


def plan_dawn(slots, now, soc, policy, *, production_on=False,
              power_control=True, idle_kw=0.13, off_kw=0.03,
              pv_threshold_kw=0.1, wake_margin_minutes=30,
              execution_margin_minutes=5, min_sleep_minutes=20,
              already_exporting=False, power_on=True, export_floor_soc=None):
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
            wake_margin_minutes,execution_margin_minutes,min_sleep_minutes)):
        raise ValueError('invalid night policy')
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
    night = take_slots(slots,now_utc,dawn)
    energy = max(0,(soc-policy.hard_floor)*policy.kwh_per_soc)
    target = budget['target_energy']
    natural = sum(max(0,s.load-s.pv)*s.hours/policy.discharge_eff for s in night)
    needed = max(0,energy-target)
    off_drain = off_kw*sum(s.hours for s in night) if power_control else 0
    forced = max(0,needed-off_drain) if power_control else max(0,needed-natural)
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
    # Explicit zero export limit must never activate TOU.
    planned = forced >= (0.05 if already_exporting else policy.start_kwh) and policy.export_kw > 0
    start = max(now_utc,ideal_start-timedelta(minutes=execution_margin_minutes)) if planned and ideal_start else None
    wake = max(now_utc,dawn-timedelta(minutes=wake_margin_minutes))
    if start:
        wake = min(wake,start)
    # If waking for solar before a short discharge window, its household drain
    # creates some headroom too. Recompute at wake/SOC changes; the hardware
    # cutoff is the final dawn target, never a moving 0.75 kWh burst.
    due = bool(start and start <= now_utc+timedelta(seconds=1) and soc > budget['target_soc']+0.5)
    wait_seconds = (wake-now_utc).total_seconds()
    can_sleep = power_control and wait_seconds > 1 and (
        not power_on or wait_seconds >= min_sleep_minutes*60)
    state = 'export' if due else 'sleep' if can_sleep else 'self_use'
    sleep_hours = max(0,(wake-now_utc).total_seconds()/3600) if can_sleep else 0
    off_slots = take_slots(night,now_utc,wake) if can_sleep else []
    standby_saved = max(0,idle_kw-off_kw)*sleep_hours
    house_grid = sum(max(0,s.load-idle_kw)*s.hours for s in off_slots)
    if due:
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
                cutoff_soc=budget['target_soc'],
                pv_start_at=local_dawn.isoformat(),
                discharge_start_at=start.astimezone(now.tzinfo).isoformat() if start else None,
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
