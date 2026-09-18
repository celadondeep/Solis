"""Gryna, nuo Home Assistant nepriklausoma energijos plano logika."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Optional


@dataclass(frozen=True)
class PlannerPolicy:
    """Vienos elektrinės saugos ir eksporto ribos."""

    hard_floor: float
    night_rest_soc: float = 0.0
    preferred_floor: float = 20.0
    ceiling: float = 80.0
    low_soc_recovery_margin: float = 5.0
    low_soc_shortfall_boundary: float = 30.0
    low_soc_shortfall_kwh: float = 3.0
    normal_shortfall_kwh: float = 1.0


@dataclass(frozen=True)
class PlannerInput:
    """Vieno planavimo ciklo normalizuoti įėjimai."""

    storm: bool
    manual: bool
    soc: Optional[float]
    target_soc: float
    in_production_hours: bool
    export_floor: float
    room_shortfall_kwh: float
    horizon: Optional[dict] = None


@dataclass(frozen=True)
class EnergyPlan:
    """Atominis planas, kurį fizinis Executor vykdo kaip vieną vienetą."""

    mode: str
    target_soc: float
    slot_active: Optional[bool]
    slot_cutoff_soc: Optional[float]
    inverter_on: Optional[bool]
    reason: str
    actionable: bool = True
    inputs_valid: bool = True
    priority: str = "normal"


def _finite(value: object) -> bool:
    try:
        return isfinite(float(value))
    except (TypeError, ValueError):
        return False


def target_soc_for_room(
    required_room_kwh: float,
    kwh_per_soc: float,
    policy: PlannerPolicy,
) -> int:
    """Paverčia reikalingą vietą į SOC tikslą.

    20 % yra pageidaujamos sveikatos juostos apačia, o ne saugos dugnas.
    Jei labai saulėta diena reikalauja daugiau vietos, tikslas gali leistis
    žemiau 20 %, tačiau niekada žemiau konkretaus inverterio sloto ribos.
    Viršutinė 80 % riba lieka baterijos tausojimo saugikliu.
    """

    if not _finite(required_room_kwh) or float(required_room_kwh) < 0:
        raise ValueError("required_room_kwh turi būti baigtinis ir >= 0")
    if not _finite(kwh_per_soc) or float(kwh_per_soc) <= 0:
        raise ValueError("kwh_per_soc turi būti baigtinis ir > 0")

    raw_target = int(round(100.0 - float(required_room_kwh) / float(kwh_per_soc)))
    return int(max(policy.hard_floor, min(raw_target, policy.ceiling)))


def decide_plan(inputs: PlannerInput, policy: PlannerPolicy) -> EnergyPlan:
    """Vienintelė sprendimų funkcija abiem elektrinėms."""

    target = (
        max(policy.hard_floor, min(float(inputs.target_soc), 100.0))
        if _finite(inputs.target_soc)
        else policy.ceiling
    )

    if inputs.storm:
        return EnergyPlan(
            mode="storm", target_soc=100.0, slot_active=False,
            slot_cutoff_soc=100.0, inverter_on=True,
            reason="Audros / ESO rezervas", actionable=False,
            priority="storm",
        )

    if inputs.manual:
        return EnergyPlan(
            mode="manual", target_soc=target, slot_active=None,
            slot_cutoff_soc=None, inverter_on=None,
            reason="Rankinis valdymas — automatinis Executor pristabdytas",
            actionable=False, priority="manual",
        )

    if not _finite(inputs.soc) or not 0.0 <= float(inputs.soc) <= 100.0:
        return EnergyPlan(
            mode="hold", target_soc=target, slot_active=None,
            slot_cutoff_soc=None, inverter_on=None,
            reason="SOC telemetrija nepasiekiama — fizinė būsena nekeičiama",
            actionable=False, inputs_valid=False, priority="telemetry_hold",
        )

    soc = float(inputs.soc)
    export_floor = (
        max(policy.hard_floor, min(float(inputs.export_floor), 100.0))
        if _finite(inputs.export_floor)
        else policy.hard_floor
    )
    shortfall = (
        float(inputs.room_shortfall_kwh)
        if _finite(inputs.room_shortfall_kwh)
        else 0.0
    )

    if inputs.horizon is not None and inputs.horizon.get("grid_connected") is False:
        return EnergyPlan(mode="self_use", target_soc=target, slot_active=False,
            slot_cutoff_soc=None, inverter_on=True,
            reason="Tinklo buvimas nepatvirtintas; eksportas ir nakties išjungimas uždrausti",
            priority="grid_guard")

    if soc <= policy.hard_floor:
        inverter_on = inputs.in_production_hours
        return EnergyPlan(
            mode="self_use", target_soc=target, slot_active=False,
            slot_cutoff_soc=policy.hard_floor, inverter_on=inverter_on,
            reason=(
                f"Kritinis dugnas: SOC {soc:.0f}% <= "
                f"{policy.hard_floor:.0f}% — "
                + ("PV priėmimas paliktas aktyvus" if inverter_on
                   else "naktį inverteris ilsinamas")
            ),
            priority="hard_floor",
        )

    if inputs.horizon is not None:
        # The forward model protects a consumption budget. SOC above that
        # budget alone is never sufficient reason to export the battery.
        guidance = inputs.horizon
        reserve = guidance.get("reserve_soc", 100.0)
        cutoff = guidance.get("cutoff_soc", 100.0)
        valid = guidance.get("valid") is True
        if valid and guidance.get("solar_export_priority") is True and guidance.get("export_now") is not True:
            return EnergyPlan(mode="feed_in",target_soc=target,slot_active=False,
                slot_cutoff_soc=None,inverter_on=True,
                reason=str(guidance.get("reason","Dienos PV eksportas ir kaupimas")),
                priority="horizon_pv_export")
        safe_cutoff = max(export_floor, float(reserve), float(cutoff)) if (
            _finite(reserve) and _finite(cutoff)
        ) else 100.0
        if (valid and guidance.get("export_now") is True
                and policy.hard_floor <= safe_cutoff < soc - 0.5):
            return EnergyPlan(
                mode="feed_in" if inputs.in_production_hours else "night_export",
                target_soc=target, slot_active=True,
                slot_cutoff_soc=safe_cutoff, inverter_on=True,
                reason=str(guidance.get("reason", "Prognozuotas vietos poreikis")),
                priority="horizon_soc_buffer" if guidance.get("soc_buffer_active") is True else "horizon_export",
            )
        return EnergyPlan(
            mode="self_use", target_soc=target, slot_active=False,
            slot_cutoff_soc=None,
            # Valid night guidance already checks forecast and measured PV.
            # The broader legacy daylight window must not undo explicit sleep.
            inverter_on=(guidance.get("inverter_on") is True)
                if valid and guidance.get("night_active") is True else
                (inputs.in_production_hours or soc > policy.night_rest_soc),
            reason=str(guidance.get("reason", "Savas vartojimas ir PV kaupimas")),
            priority="horizon_night" if valid and guidance.get("night_active") is True else
                "horizon_self_use" if valid else "forecast_fallback",
        )

    if not inputs.in_production_hours:
        night_limit = max(target, policy.night_rest_soc)
        if soc <= night_limit:
            return EnergyPlan(
                mode="self_use", target_soc=target, slot_active=False,
                slot_cutoff_soc=None, inverter_on=False,
                reason=(
                    f"Ne gamybos laikas, SOC {soc:.0f}% <= "
                    f"nakties ribos {night_limit:.0f}%"
                ),
                priority="night_idle",
            )
        return EnergyPlan(
            mode="night_export", target_soc=target, slot_active=True,
            slot_cutoff_soc=target, inverter_on=True,
            reason=(
                f"Ne gamybos laikas, SOC {soc:.0f}% > "
                f"tikslo {target:.0f}% — trumpas eksportas iki tikslo"
            ),
            priority="night_export",
        )

    if soc < export_floor + policy.low_soc_recovery_margin:
        return EnergyPlan(
            mode="self_use", target_soc=target, slot_active=False,
            slot_cutoff_soc=None, inverter_on=True,
            reason=(
                f"Krovimo prioritetas: SOC {soc:.0f}% < "
                f"{export_floor + policy.low_soc_recovery_margin:.0f}%"
            ),
            priority="day_recovery",
        )

    if soc >= policy.ceiling:
        return EnergyPlan(
            mode="feed_in", target_soc=target, slot_active=True,
            slot_cutoff_soc=policy.ceiling, inverter_on=True,
            reason=(
                f"SOC {soc:.0f}% >= sveikatos juostos viršaus "
                f"{policy.ceiling:.0f}%"
            ),
            priority="day_ceiling",
        )

    threshold = (
        policy.low_soc_shortfall_kwh
        if soc < policy.low_soc_shortfall_boundary
        else policy.normal_shortfall_kwh
    )
    if shortfall > threshold:
        return EnergyPlan(
            mode="feed_in", target_soc=target, slot_active=True,
            slot_cutoff_soc=export_floor, inverter_on=True,
            reason=(
                f"Vietos trūkumas {shortfall:.1f} kWh > {threshold:.1f} kWh "
                f"— eksportas iki {export_floor:.0f}%"
            ),
            priority="day_room",
        )

    if soc >= target:
        return EnergyPlan(
            mode="feed_in", target_soc=target, slot_active=False,
            slot_cutoff_soc=None, inverter_on=True,
            reason=(
                f"SOC {soc:.0f}% >= tikslo {target:.0f}% — "
                "likusi saulė tiesiai į tinklą"
            ),
            priority="day_export",
        )

    return EnergyPlan(
        mode="self_use", target_soc=target, slot_active=False,
        slot_cutoff_soc=None, inverter_on=True,
        reason=f"SOC {soc:.0f}% < tikslo {target:.0f}% — kaupiamas buferis",
        priority="day_buffer",
    )
