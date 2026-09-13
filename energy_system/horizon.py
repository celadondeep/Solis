"""Receding-horizon energy budget. Pure Python; no HA or actuator writes.

Battery energy is DC kWh above the site's hard floor. Forecasts/load/export
are AC kW. Time is integrated in UTC; local time selects the household profile.
Only demonstrably useful pre-export is permitted, in bounded increments.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import ceil, isfinite

VERSION = "4.2-soc-buffer"


def ha_attributes(value):
    """AppDaemon 4.5.13 drops False/0/None in nested REST kwargs.

Explicit strings also overwrite previous attributes when a signal turns off.
"""
    if isinstance(value, dict):
        return {k: ha_attributes(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [ha_attributes(v) for v in value]
    if isinstance(value, bool):
        return "on" if value else "off"
    if value is None:
        return "unknown"
    if isinstance(value, (int, float)) and value == 0:
        return "0"
    return value


def read_guidance(attrs, now):
    """Read the explicitly encoded, expiring guidance from HA for the shadow."""
    result = dict(attrs or {})
    try:
        if stamp(result.get("expires_at")) < stamp(now):
            raise ValueError("expired")
        for key in ("valid", "export_now", "night_active", "inverter_on", "solar_export_priority", "soc_buffer_active", "grid_connected"):
            result[key] = result.get(key) is True or result.get(key) in ("on", "true")
    except (TypeError, ValueError):
        return {"valid": False, "export_now": False, "night_active": False,
                "reason": "Prognozė nepasiekiama; savas vartojimas"}
    return result


def finite(value, default=None):
    try:
        value = float(value)
        return value if isfinite(value) else default
    except (ValueError, TypeError):
        return default


def clamp(value, low, high):
    return max(low, min(high, value))


def stamp(value):
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value))
    if result.tzinfo is None:
        raise ValueError("timestamp without timezone")
    return result.astimezone(timezone.utc)


@dataclass(frozen=True)
class HorizonPolicy:
    hard_floor: float
    kwh_per_soc: float
    storage_ceiling: float = 95.0  # planning buffer, NOT a hardware charge setting
    comfort_soc: float = 20.0
    reserve_margin_kwh: float = 0.5
    export_kw: float = 1.0
    charge_kw: float = 3.0
    discharge_kw: float = 3.0
    charge_eff: float = 0.95
    discharge_eff: float = 0.95
    burst_kwh: float = 0.75
    start_kwh: float = 0.5

    def __post_init__(self):
        values = self.__dict__
        if not all(finite(v) is not None for v in values.values()):
            raise ValueError("nonfinite horizon policy")
        if not 0 <= self.hard_floor < self.storage_ceiling <= 100:
            raise ValueError("SOC bounds")
        if self.kwh_per_soc <= 0 or min(self.charge_kw, self.discharge_kw) <= 0:
            raise ValueError("capacity/power bounds")
        if not 0 < min(self.charge_eff, self.discharge_eff) <= max(self.charge_eff, self.discharge_eff) <= 1:
            raise ValueError("efficiency bounds")
        if min(self.export_kw, self.reserve_margin_kwh, self.burst_kwh, self.start_kwh) < 0:
            raise ValueError("negative limits")

    @property
    def capacity(self):
        return (self.storage_ceiling - self.hard_floor) * self.kwh_per_soc


@dataclass(frozen=True)
class Slot:
    start: datetime
    hours: float
    pv: float
    low_pv: float
    load: float
    charge_limit_kw: float | None = None  # measured BMS acceptance, current interval only


def forecast_slots(records, now, end, hourly_load, hourly_factor,
                   today_ratio=1.0, load_ratio=1.0, tomorrow_load_ratio=1.0,
                   self_kw=0.14):
    """Validate complete 30-minute coverage, clip current partial interval.

No silent zero-fill for missing daylight data. Same-day bias decays over
four hours; it never changes the next day's independent forecast.
"""
    utc_now, utc_end = stamp(now), stamp(end)
    by_start = {}
    expected_today = elapsed_load = 0.0
    local_today = now.date()
    for row in records:
        start = stamp(row["period_start"])
        if start in by_start:
            raise ValueError("duplicate forecast interval")
        pv = finite(row.get("pv_estimate"))
        low = finite(row.get("pv_estimate10"), pv * 0.65 if pv is not None else None)
        if pv is None or low is None or min(pv, low) < 0:
            raise ValueError("invalid PV forecast")
        local = start.astimezone(now.tzinfo)
        factor = finite(hourly_factor(local.hour))
        if factor is None or factor <= 0:
            raise ValueError("invalid calibration")
        by_start[start] = (pv * factor, min(pv, low) * factor, local)
        if local.date() == local_today and start < utc_now:
            duration = max(0.0, min(0.5, (utc_now - start).total_seconds() / 3600))
            expected_today += pv * factor * duration
            elapsed_load += hourly_load(local) * duration

    slots, cursor = [], utc_now
    for start, (pv, low, local) in sorted(by_start.items()):
        finish = start + timedelta(minutes=30)
        if finish <= utc_now or start >= utc_end:
            continue
        left, right = max(start, utc_now), min(finish, utc_end)
        if abs((left - cursor).total_seconds()) > 1:
            raise ValueError("gap/overlap in forecast")
        dt = (right - left).total_seconds() / 3600
        lead = max(0.0, (left - utc_now).total_seconds() / 3600)
        same_day = local.date() == local_today
        bias = 1 + (today_ratio - 1) * max(0.0, 1 - lead / 4) if same_day else 1.0
        demand_bias = load_ratio if same_day else tomorrow_load_ratio
        load = hourly_load(local) * demand_bias + self_kw
        if finite(load) is None or load < 0:
            raise ValueError("invalid load")
        slots.append(Slot(left, dt, pv * bias, low * bias, load))
        cursor = right
    if not slots or abs((cursor - utc_end).total_seconds()) > 1:
        raise ValueError("incomplete forecast horizon")
    return slots, expected_today, elapsed_load


def simulate(slots, energy, policy, low=False, pv_priority=False):
    """Self-use reference, with power limits, conversion losses and grid cap."""
    e = max(0.0, energy)
    imported = exported = clipped = charged = discharged = 0.0
    first_spill = None
    trajectory = []
    for i, slot in enumerate(slots):
        net = (slot.low_pv if low else slot.pv) - slot.load
        if net >= 0:
            pv_export = min(net,policy.export_kw)*slot.hours if pv_priority else 0.0
            remaining_pv = net*slot.hours-pv_export
            charge_kw = policy.charge_kw if slot.charge_limit_kw is None else min(policy.charge_kw, slot.charge_limit_kw)
            accepted = min(remaining_pv, charge_kw * slot.hours,
                           max(0.0, policy.capacity - e) / policy.charge_eff)
            gain = accepted * policy.charge_eff
            e += gain
            charged += gain
            out = pv_export + min(max(0.0,remaining_pv-accepted),
                                  max(0.0,policy.export_kw*slot.hours-pv_export))
            exported += out
            spill = max(0.0, net * slot.hours - accepted - out)
            # Discharge cannot fix clipping caused solely by the charge power limit.
            unavoidable = max(0.0, net - charge_kw - policy.export_kw) * slot.hours
            if spill > unavoidable + 0.01 and first_spill is None:
                first_spill = i
            clipped += spill
        else:
            supplied = min(-net * slot.hours, policy.discharge_kw * slot.hours,
                           e * policy.discharge_eff)
            drain = supplied / policy.discharge_eff
            e -= drain
            discharged += drain
            imported += max(0.0, -net * slot.hours - supplied)
        trajectory.append(round(policy.hard_floor + e / policy.kwh_per_soc, 2))
    return dict(import_kwh=imported, export_kwh=exported, clipped_kwh=clipped,
                charged_kwh=charged, discharged_kwh=discharged, end_energy=e,
                first_spill=first_spill, soc=trajectory)


def required_reserve(slots, policy):
    """Backward energy requirement under P10 and 10% higher household demand.

This is a budget protected from forced export, not a BMS protection setting.
Terminal reserve avoids treating tomorrow midnight as permission to empty.
"""
    required = max(0.0, (policy.comfort_soc - policy.hard_floor) * policy.kwh_per_soc)
    for slot in reversed(slots):
        net = slot.low_pv - slot.load * 1.10
        if net >= 0:
            charge_kw = policy.charge_kw if slot.charge_limit_kw is None else min(policy.charge_kw, slot.charge_limit_kw)
            required -= min(net, charge_kw) * slot.hours * policy.charge_eff
        else:
            required += min(-net, policy.discharge_kw) * slot.hours / policy.discharge_eff
        required = clamp(required, 0, policy.capacity)
    comfort = max(0.0, (policy.comfort_soc - policy.hard_floor) * policy.kwh_per_soc)
    return clamp(max(comfort, required + policy.reserve_margin_kwh), 0, policy.capacity)


def plan_horizon(slots, soc, policy):
    if finite(soc) is None or not 0 <= soc <= 100:
        raise ValueError("invalid SOC")
    if not slots or any(
        any(finite(v) is None or v < 0 for v in (s.hours, s.pv, s.low_pv, s.load))
        or not 0 < s.hours <= 1 or s.low_pv > s.pv
        or (s.charge_limit_kw is not None and (finite(s.charge_limit_kw) is None or s.charge_limit_kw < 0)) for s in slots
    ):
        raise ValueError("invalid horizon slots")
    energy = max(0.0, (soc - policy.hard_floor) * policy.kwh_per_soc)
    reserve = required_reserve(slots, policy)
    baseline = simulate(slots, energy, policy)
    cautious = simulate(slots, energy, policy, low=True)
    removable = max(0.0, energy - reserve)
    best = simulate(slots, energy - removable, policy)
    useful = max(0.0, baseline["clipped_kwh"] - best["clipped_kwh"])
    needed = 0.0
    if useful >= policy.start_kwh and removable > 0:
        lo, hi = 0.0, removable
        # Smallest pre-export obtaining the achievable curtailment reduction.
        for _ in range(18):
            mid = (lo + hi) / 2
            trial = simulate(slots, energy - mid, policy)
            if trial["clipped_kwh"] <= best["clipped_kwh"] + 0.02:
                hi = mid
            else:
                lo = mid
        needed = hi
        low_trial = simulate(slots, energy - needed, policy, low=True)
        # Do not sell energy if the low-solar scenario then has to buy it back.
        if low_trial["import_kwh"] > cautious["import_kwh"] + 0.05:
            needed = 0.0
    first = baseline["first_spill"]
    lead_capacity = 0.0
    if first is not None:
        for slot in slots[:first]:
            pv_export = min(max(0.0, slot.pv - slot.load), policy.export_kw)
            lead_capacity += max(0.0, policy.export_kw - pv_export) * slot.hours
    due = needed > 0 and policy.export_kw > 0 and (
        lead_capacity <= needed * policy.discharge_eff + policy.export_kw * 0.75
    )
    burst = min(needed, policy.burst_kwh, removable) if due else 0.0
    cutoff = max(policy.hard_floor, ceil(soc - burst / policy.kwh_per_soc))
    allowed = burst >= 0.15 and cutoff < soc - 0.5
    reserve_soc = ceil(policy.hard_floor + reserve / policy.kwh_per_soc)
    if allowed:
        reason = f"Prognozuojama netilpsianti PV {useful:.1f} kWh; trumpas eksportas iki {cutoff}%"
    elif needed > 0:
        reason = "Vietos rytoj gali reikėti; eksportas atidėtas, dabar baterija naudojama namams"
    elif useful > 0 or baseline["first_spill"] is not None:
        reason = "Išsaugomas vartojimo rezervas pagal atsargesnę gamybos prognozę"
    else:
        reason = "Vartojimas ir leistinas eksportas sukuria pakankamai vietos; baterija priverstinai neiškraunama"
    return dict(version=VERSION, valid=True, export_now=allowed,
                reserve_soc=reserve_soc, cutoff_soc=cutoff,
                reason=reason, required_preexport_kwh=round(needed, 3),
                useful_headroom_kwh=round(useful, 3),
                predicted_import_kwh=round(baseline["import_kwh"], 3),
                cautious_import_kwh=round(cautious["import_kwh"], 3),
                predicted_export_kwh=round(baseline["export_kwh"], 3),
                predicted_clipping_kwh=round(baseline["clipped_kwh"], 3),
                predicted_charge_kwh=round(baseline["charged_kwh"], 3),
                predicted_discharge_kwh=round(baseline["discharged_kwh"], 3),
                pv_kwh=round(sum(s.pv * s.hours for s in slots), 3),
                pv_low_kwh=round(sum(s.low_pv * s.hours for s in slots), 3),
                load_kwh=round(sum(s.load * s.hours for s in slots), 3),
                first_spill_at=slots[first].start.isoformat() if first is not None else None,
                horizon_hours=round(sum(s.hours for s in slots), 2),
                soc_at_end=baseline["soc"][-1],
                trajectory=[dict(at=s.start.isoformat(), soc=baseline["soc"][i],
                                 soc_low=cautious["soc"][i])
                            for i, s in enumerate(slots) if i % 2 == 0])
