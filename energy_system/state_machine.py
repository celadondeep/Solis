"""Vienos elektrinės darbo būsenų mašina.

Būsenos apibrėžia elgesį dingus internetui, dingus telemetrijai ar atsiradus
rankiniam valdymui. Tai ne HA automacija ir neatlieka fizinių rašymų.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


class PlantState:
    INIT = "init"
    NORMAL = "normal"
    DEGRADED = "degraded"
    RESERVE = "reserve"
    STORM = "storm"
    MANUAL = "manual"
    HOLD = "hold"
    OFFLINE = "offline"
    RECOVERY = "recovery"


@dataclass(frozen=True)
class StateInputs:
    telemetry_ok: bool = True
    storm: bool = False
    manual: bool = False
    adapter_online: bool = True
    executor_online: bool = True
    reserve_low: bool = False


@dataclass(frozen=True)
class StateTransition:
    previous: str
    current: str
    reason: str
    healthy_streak: int


class PlantStateMachine:
    """Histerezę turinti, deterministinė vienos elektrinės būsena."""

    def __init__(self, *, recovery_cycles: int = 2):
        self.state = PlantState.INIT
        self.recovery_cycles = max(1, int(recovery_cycles))
        self.healthy_streak = 0

    def step(self, inputs: StateInputs) -> StateTransition:
        previous = self.state

        # Saugos prioritetas: ryšio nebuvimas reiškia, kad jokių naujų
        # debesijos komandų negalima planuoti. Storm išlieka aukščiau manual,
        # nes jis yra fizinės atsargos režimas.
        if not inputs.adapter_online:
            desired = PlantState.OFFLINE
            reason = "vietinis adapteris / ryšys nepasiekiamas"
            self.healthy_streak = 0
        elif inputs.storm:
            desired = PlantState.STORM
            reason = "aktyvus audros / rezervo režimas"
            self.healthy_streak = 0
        elif inputs.manual:
            desired = PlantState.MANUAL
            reason = "aktyvus rankinis valdymas"
            self.healthy_streak = 0
        elif not inputs.telemetry_ok:
            desired = PlantState.HOLD
            reason = "kritinė telemetrija sena arba nepasiekiama"
            self.healthy_streak = 0
        elif inputs.reserve_low:
            desired = PlantState.RESERVE
            reason = "pasiekta rezervinė SOC juosta"
            self.healthy_streak = 0
        elif not inputs.executor_online:
            desired = PlantState.DEGRADED
            reason = "vykdyklė nepatvirtina fizinės būsenos"
            self.healthy_streak = 0
        else:
            self.healthy_streak += 1
            if previous in {
                PlantState.OFFLINE,
                PlantState.HOLD,
                PlantState.DEGRADED,
                PlantState.RECOVERY,
            } and self.healthy_streak < self.recovery_cycles:
                desired = PlantState.RECOVERY
                reason = (
                    f"ryšys atstatytas, laukiama {self.recovery_cycles} "
                    f"stabilių ciklų ({self.healthy_streak})"
                )
            else:
                desired = PlantState.NORMAL
                reason = "visi būtini įėjimai ir vykdyklė sveiki"

        self.state = desired
        return StateTransition(
            previous=previous,
            current=desired,
            reason=reason,
            healthy_streak=self.healthy_streak,
        )
