"""Preferred SOC band and bounded daytime PV buffer; no device writes.

The 15-point headroom is a planning preference, not a BMS/charge limit.
It avoids dwelling in the region where charging commonly tapers without
inventing a battery-specific current curve. Actual BMS current is used by
the adapter when available. Native TOU cutoff remains the offline guard.
"""
from dataclasses import replace
from math import ceil

from energy_system.horizon import finite, stamp, simulate, required_reserve
from energy_system.telemetry import as_utc


def preferred_policy(policy, soc_min, soc_max, extra_headroom_soc=0):
    low, high = finite(soc_min), finite(soc_max)
    if low is None or high is None or not 0 <= low < high <= 100:
        raise ValueError("SOCmin / SOCmax nepasiekiami")
    extra = finite(extra_headroom_soc)
    if extra is None or not 0 <= extra <= 10:
        raise ValueError("Papildoma talpos atsarga turi būti 0–10 p. p.")
    floor, ceiling = max(policy.hard_floor, low + 15), high - 15 - extra
    if floor >= ceiling:
        raise ValueError("SOC ribos per siauros 15 p. p. atsargai")
    return replace(policy, comfort_soc=floor, storage_ceiling=ceiling)


def grid_present(now, heartbeat, voltages, frequency, max_age):
    at = as_utc(heartbeat)
    if at is None or not -60 <= (stamp(now) - at).total_seconds() <= max_age:
        return False
    return (len(voltages) == 3 and all(finite(v) is not None and 180 < float(v) < 270
                                    for v in voltages)
            and finite(frequency) is not None and 45 < float(frequency) < 55)


def daytime_buffer(guidance, slots, now, soc, policy, *, connected,
                   export_floor=None, already_buffering=False, charge_acceptance_kw=None,
                   previous_cutoff=None, execution_minutes=5, extra_headroom_soc=0,
                   production_on=False):
    """Keep a short, locally bounded TOU slot available through PV/load dips.

Predict capacity pressure four hours ahead, including PV's share of the grid
export limit. Below the preferred ceiling protect P10 household demand until
the next day's credible recharge, plus comfort SOC. A second cloudy evening
does not veto all useful headroom today. Each new target frees <=0.75 kWh;
an outstanding target is held stable rather than chasing every SOC increase.
Above the preferred ceiling, ongoing production is sufficient to restore the
SOC band. A lack of forecast surplus must not strand a full battery until night.
The adapter's separate night plan owns export/sleep after production ends.
"""
    result = dict(guidance)
    upper, lower = policy.storage_ceiling, policy.comfort_soc
    result.update(preferred_soc_min=lower, preferred_soc_max=upper,
                  target_soc=max(lower, min(upper, finite(result.get("target_soc"),
                                                         result["reserve_soc"]))),
                  grid_connected=connected, soc_buffer_active=False,
                  charge_acceptance_kw=charge_acceptance_kw,
                  taper_headroom_soc=15, extra_headroom_soc=extra_headroom_soc,
                  predictive_buffer_due=False, buffer_required_kwh=0,
                  buffer_preferred_band_due=False, buffer_near_pv_surplus_kwh=0,
                  buffer_first_pressure_at=None, buffer_execution_minutes=execution_minutes)
    if not connected:
        result.update(export_now=False, solar_export_priority=False,
                      reason="Tinklo buvimas nepatvirtintas; priverstinis eksportas išjungtas")
        return result
    if (result.get("valid") is not True or finite(soc) is None or not 0 <= soc <= 100
            or not slots or policy.export_kw <= 0):
        return result
    near = []
    for s in slots:
        lead = (stamp(s.start)-stamp(now)).total_seconds()/3600
        if 0 <= lead < 4:
            near.append(replace(s, hours=min(s.hours, 4-lead)))
    near_surplus = sum(max(0, s.pv-s.load) * s.hours for s in near)
    # Protect tonight and the next morning under P10, not only four sunny hours.
    protected = []
    for s in slots:
        if (stamp(s.start).astimezone(now.tzinfo).date() > now.date()
                and s.low_pv > s.load*1.1 + 0.1):
            break
        protected.append(s)
    reserve = max(lower, policy.hard_floor + required_reserve(protected, policy)/policy.kwh_per_soc,
                  finite(export_floor, policy.hard_floor))
    energy = max(0, (soc-policy.hard_floor)*policy.kwh_per_soc)
    baseline = simulate(near, energy, policy, pv_priority=True)
    removable = max(0, (soc-reserve)*policy.kwh_per_soc)
    best = simulate(near, energy-removable, policy, pv_priority=True)
    useful = max(0, baseline['clipped_kwh']-best['clipped_kwh'])
    first = baseline['first_spill']
    lead_capacity = sum(min(policy.discharge_kw, max(0, policy.export_kw-max(0,s.pv-s.load)))
                        *s.hours for s in near[:first]) if first is not None else 0
    needed = min(removable, useful*policy.charge_eff)
    early = (first is not None and needed >= (0.15 if already_buffering else policy.start_kwh)
             and lead_capacity <= needed*policy.discharge_eff + policy.export_kw*execution_minutes/60)
    result.update(buffer_required_kwh=round(needed, 3),
                  buffer_first_pressure_at=near[first].start.isoformat() if first is not None else None,
                  buffer_protected_soc=ceil(reserve), predictive_buffer_due=early)
    start = soc >= upper + 2
    keep = already_buffering and soc > upper + 0.5
    band_due = (start or keep) and (production_on or near_surplus >= 0.3)
    result.update(buffer_preferred_band_due=band_due,
                  buffer_near_pv_surplus_kwh=round(near_surplus, 3))
    if not (early or band_due):
        return result
    safe_floor = max(reserve if early and soc <= upper+2 else upper,
                     finite(export_floor, policy.hard_floor))
    floor = max(ceil(safe_floor), ceil(soc-policy.burst_kwh/policy.kwh_per_soc))
    old_cutoff = finite(previous_cutoff)
    if already_buffering and old_cutoff is not None and safe_floor <= old_cutoff < soc-0.5:
        floor = old_cutoff
    if floor >= soc - 0.5:
        return result
    result.update(export_now=True, solar_export_priority=True, soc_buffer_active=True,
                  cutoff_soc=floor, reserve_soc=min(result["reserve_soc"], safe_floor),
                  reason=(f"Artėja talpos trūkumas ({needed:.2f} kWh); ankstyvas PV buferis iki {floor:.0f}%"
                          if early and soc < upper+2 else
                          f"Dienos SOC virš pageidaujamos {upper:.0f}% ribos; iškrovimas iki {floor:.0f}%"))
    return result
