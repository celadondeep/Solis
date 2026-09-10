"""Preferred SOC band and bounded daytime PV buffer; no device writes.

The 15-point headroom is a planning preference, not a BMS/charge limit.
It avoids dwelling in the region where charging commonly tapers without
inventing a battery-specific current curve. Actual BMS current is used by
the adapter when available. Native TOU cutoff remains the offline guard.
"""
from dataclasses import replace
from math import ceil

from energy_system.horizon import finite, stamp
from energy_system.telemetry import as_utc


def preferred_policy(policy, soc_min, soc_max):
    low, high = finite(soc_min), finite(soc_max)
    if low is None or high is None or not 0 <= low < high <= 100:
        raise ValueError("SOCmin / SOCmax nepasiekiami")
    floor, ceiling = max(policy.hard_floor, low + 15), high - 15
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
                   export_floor=None, already_buffering=False, charge_acceptance_kw=None):
    """Keep a short, locally bounded TOU slot available through PV/load dips.

Existing forecast pre-export keeps its P10 reserve gate. Above the preferred
ceiling a buffer may discharge only the excess, with 2-point start hysteresis
and a <=0.75 kWh step. No discharge solely because it is daytime/high SOC:
there must be near-term solar surplus, or an already running buffer.
"""
    result = dict(guidance)
    upper, lower = policy.storage_ceiling, policy.comfort_soc
    result.update(preferred_soc_min=lower, preferred_soc_max=upper,
                  target_soc=max(lower, min(upper, finite(result.get("target_soc"),
                                                         result["reserve_soc"]))),
                  grid_connected=connected, soc_buffer_active=False,
                  charge_acceptance_kw=charge_acceptance_kw,
                  taper_headroom_soc=15)
    if not connected:
        result.update(export_now=False, solar_export_priority=False,
                      reason="Tinklo buvimas nepatvirtintas; priverstinis eksportas išjungtas")
        return result
    near_surplus = sum(max(0, s.pv-s.load) * s.hours for s in slots
                       if 0 <= (stamp(s.start)-stamp(now)).total_seconds() < 4*3600)
    start = soc >= upper + 2
    keep = already_buffering and soc > upper + 0.5
    if not (result.get("valid") is True and policy.export_kw > 0 and
            (start or keep) and (near_surplus >= 0.3 or keep)):
        return result
    floor = max(upper, finite(export_floor, policy.hard_floor),
                ceil(soc-policy.burst_kwh/policy.kwh_per_soc))
    if floor >= soc - 0.5:
        return result
    result.update(export_now=True, solar_export_priority=True, soc_buffer_active=True,
                  cutoff_soc=floor, reserve_soc=min(result["reserve_soc"], upper),
                  reason=f"SOC virš pageidaujamos {upper:.0f}% ribos; PV buferio eksportas iki {floor:.0f}%, kad liktų vietos saulės energijai ir krovimo srovės mažėjimui")
    return result
