"""Grynas vienos elektrinės scenarijų vykdymo modelis."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

from energy_system.command_queue import SingleWriterQueue
from energy_system.contracts import (
    CommandIntent,
    CoordinatorDecision,
    ExecutionResult,
    ModuleProposal,
)
from energy_system.decision_center import ValdymoKoordinatorius, priority_for
from energy_system.planner import (
    EnergyPlan,
    PlannerInput,
    PlannerPolicy,
    decide_plan,
)
from energy_system.supervisor import evaluate_execution
from energy_system.telemetry import as_utc, assess_freshness


def _utc(value: datetime) -> datetime:
    parsed = as_utc(value)
    return parsed or datetime.now(timezone.utc)


def _default_actuator() -> dict:
    return {
        "mode_by_plan": {
            "self_use": "Self-Use",
            "feed_in": "Feed-in Priority",
            "night_export": "Feed-in Priority",
        },
        "mode": "mode",
        "slot": "slot",
        "slot_cutoff": "slot_cutoff_soc",
        "exclusive_off": tuple(f"shadow_slot_{slot}" for slot in range(2, 7)),
        "power": "power",
    }


@dataclass(frozen=True)
class ScenarioEvent:
    at: datetime
    patch: Mapping[str, Any] = field(default_factory=dict)
    label: str = ""


@dataclass
class SimObservation:
    """Fizinės ir telemetrijos būsenos projekcija."""

    at: datetime
    soc: Any = 50.0
    soc_reported_at: Optional[datetime] = None
    pv_power: Any = 0.0
    pv_reported_at: Optional[datetime] = None
    house_load: Any = 0.0
    storm: bool = False
    manual: bool = False
    export_floor: float = 12.0
    room_shortfall_kwh: float = 0.0
    in_production_hours: bool = True
    adapter_online: bool = True
    executor: str = "on"
    mode: str = "Self-Use"
    slot: str = "off"
    slot_cutoff_soc: Any = None
    exclusive_slots: dict[str, str] = field(default_factory=dict)
    power: str = "on"


@dataclass(frozen=True)
class CycleResult:
    at: datetime
    label: str
    plan: EnergyPlan
    coordinator: CoordinatorDecision
    telemetry_quality: str
    telemetry_age_seconds: Optional[float]
    execution_before: Any
    execution_after: Any
    commands: ExecutionResult
    state: SimObservation


class SimPlant:
    """Emuliuoja vienos elektrinės planavimo ir vykdymo ciklą.

    Vienas SimPlant objektas yra viena izoliuota elektrinė. Keli objektai
    gali būti paleisti greta, tačiau jie niekada negauna vienas kito būsenos.
    """

    def __init__(
        self,
        site: str,
        policy: PlannerPolicy,
        *,
        actuator: Optional[Mapping[str, Any]] = None,
        inverter_control_available: bool = True,
        max_soc_age_seconds: float = 600.0,
        initial: Optional[SimObservation] = None,
        command_delay_steps: int = 0,
        command_failures: Optional[Iterable[str]] = None,
    ):
        self.site = str(site)
        self.policy = policy
        self.actuator = dict(actuator or _default_actuator())
        self.inverter_control_available = bool(inverter_control_available)
        self.max_soc_age_seconds = float(max_soc_age_seconds)
        self.observation = initial or SimObservation(
            at=datetime(2026, 1, 1, tzinfo=timezone.utc)
        )
        self.observation.at = _utc(self.observation.at)
        if self.observation.soc_reported_at is None and self.observation.soc is not None:
            self.observation.soc_reported_at = self.observation.at
        if self.observation.pv_reported_at is None and self.observation.pv_power is not None:
            self.observation.pv_reported_at = self.observation.at
        self.command_delay_steps = max(0, int(command_delay_steps))
        self.command_failures = set(command_failures or ())
        self.pending: list[tuple[int, CommandIntent]] = []
        self.coordinator = ValdymoKoordinatorius(self.site)
        self.queue = SingleWriterQueue(self.site)
        self.history: list[CycleResult] = []

    def _patch_observation(self, at: datetime, patch: Mapping[str, Any]) -> None:
        values = dict(patch)
        if "soc" in values and "soc_reported_at" not in values:
            values["soc_reported_at"] = at
        if "pv_power" in values and "pv_reported_at" not in values:
            values["pv_reported_at"] = at
        if "exclusive_slots" in values:
            merged = dict(self.observation.exclusive_slots)
            merged.update(values.pop("exclusive_slots") or {})
            values["exclusive_slots"] = merged
        for key, value in values.items():
            if not hasattr(self.observation, key):
                raise ValueError(f"Nežinomas emuliatoriaus laukas: {key}")
            if key in {"at", "soc_reported_at", "pv_reported_at"}:
                value = _utc(value) if value is not None else None
            setattr(self.observation, key, value)
        self.observation.at = at

    def _observed(self) -> dict:
        exclusive = dict(self.observation.exclusive_slots)
        for entity_id in self.actuator.get("exclusive_off", ()):
            exclusive.setdefault(entity_id, "off")
        return {
            "executor": self.observation.executor,
            "mode": self.observation.mode,
            "slot": self.observation.slot,
            "slot_cutoff_soc": self.observation.slot_cutoff_soc,
            "exclusive_slots": exclusive,
            "power": self.observation.power,
        }

    def _apply_intent(self, command: CommandIntent) -> Optional[str]:
        if command.actuator in self.command_failures:
            return f"command_failed:{command.actuator}"
        actuator = command.actuator
        value = command.value
        if actuator == "mode":
            self.observation.mode = str(value)
        elif actuator == "slot":
            self.observation.slot = str(value)
        elif actuator == "slot_cutoff_soc":
            self.observation.slot_cutoff_soc = value
        elif actuator.startswith("exclusive:"):
            self.observation.exclusive_slots[actuator.split(":", 1)[1]] = str(value)
        elif actuator == "power":
            self.observation.power = str(value)
        return None

    def _drain_pending(self) -> list[str]:
        remaining: list[tuple[int, CommandIntent]] = []
        errors = []
        for steps, command in self.pending:
            if steps <= 0:
                error = self._apply_intent(command)
                if error:
                    errors.append(error)
            else:
                remaining.append((steps - 1, command))
        self.pending = remaining
        return errors

    def _intents_for(self, check) -> list[CommandIntent]:
        desired = check.desired
        observed = check.observed
        intents: list[CommandIntent] = []

        def add(logical: str, value: Any) -> None:
            intents.append(
                CommandIntent(
                    site=self.site,
                    actuator=logical,
                    value=value,
                    reason="emuliatoriaus vykdymas",
                    idempotency_key=(
                        f"{self.site}:{logical}:{value!r}"
                    ),
                )
            )

        if desired.get("mode") is not None and observed.get("mode") != desired["mode"]:
            add("mode", desired["mode"])
        if desired.get("slot") is not None and observed.get("slot") != desired["slot"]:
            add("slot", desired["slot"])
        if (
            desired.get("slot_cutoff_soc") is not None
            and observed.get("slot_cutoff_soc") != desired["slot_cutoff_soc"]
        ):
            add("slot_cutoff_soc", desired["slot_cutoff_soc"])
        for entity_id, expected in desired.get("exclusive_slots", {}).items():
            if observed.get("exclusive_slots", {}).get(entity_id) != expected:
                add(f"exclusive:{entity_id}", expected)
        if (
            self.inverter_control_available
            and desired.get("power") is not None
            and observed.get("power") != desired["power"]
        ):
            add("power", desired["power"])
        return intents

    def _schedule_or_apply(
        self,
        commands: ExecutionResult,
    ) -> ExecutionResult:
        errors = list(commands.errors)
        if not commands.commands:
            return commands
        for command in commands.commands:
            if self.command_delay_steps:
                self.pending.append((self.command_delay_steps - 1, command))
            else:
                error = self._apply_intent(command)
                if error:
                    errors.append(error)
        status = "error" if errors else commands.status
        return replace(commands, status=status, errors=tuple(errors))

    def step(
        self,
        at: datetime,
        patch: Optional[Mapping[str, Any]] = None,
        *,
        label: str = "",
    ) -> CycleResult:
        """Atlieka vieną deterministinį planavimo ciklą."""
        moment = _utc(at)
        self._patch_observation(moment, patch or {})
        drain_errors = self._drain_pending()

        freshness = assess_freshness(
            self.observation.soc,
            self.observation.soc_reported_at,
            max_age_seconds=self.max_soc_age_seconds,
            now=moment,
        )
        soc = self.observation.soc if freshness.usable else None
        plan = decide_plan(
            PlannerInput(
                storm=bool(self.observation.storm),
                manual=bool(self.observation.manual),
                soc=soc,
                target_soc=80.0,
                in_production_hours=bool(self.observation.in_production_hours),
                export_floor=self.observation.export_floor,
                room_shortfall_kwh=self.observation.room_shortfall_kwh,
            ),
            self.policy,
        )
        proposal = ModuleProposal(
            site=self.site,
            module="planner",
            priority=priority_for(plan.priority, 500),
            kind=plan.priority,
            reason=plan.reason,
            payload={"plan": plan},
            created_at=moment,
        )
        coordinator = self.coordinator.choose([proposal], now=moment)
        selected_plan = (
            coordinator.selected.payload["plan"]
            if coordinator.selected is not None
            else plan
        )
        before = evaluate_execution(
            selected_plan,
            self._observed(),
            self.actuator,
            self.inverter_control_available,
        )
        command_result = self.queue.result(
            self._intents_for(before) if selected_plan.actionable and self.observation.adapter_online else ()
        )
        command_result = self._schedule_or_apply(command_result)
        if drain_errors:
            command_result = replace(
                command_result,
                status="error",
                errors=tuple(list(command_result.errors) + drain_errors),
            )
        after = evaluate_execution(
            selected_plan,
            self._observed(),
            self.actuator,
            self.inverter_control_available,
        )
        result = CycleResult(
            at=moment,
            label=label,
            plan=selected_plan,
            coordinator=coordinator,
            telemetry_quality=freshness.quality,
            telemetry_age_seconds=freshness.age_seconds,
            execution_before=before,
            execution_after=after,
            commands=command_result,
            state=replace(
                self.observation,
                exclusive_slots=dict(self.observation.exclusive_slots),
            ),
        )
        self.history.append(result)
        return result

    def run(self, events: Sequence[ScenarioEvent]) -> list[CycleResult]:
        """Vykdo įvykius chronologine tvarka."""
        ordered = sorted(events, key=lambda item: _utc(item.at))
        return [
            self.step(item.at, item.patch, label=item.label)
            for item in ordered
        ]

    def latest(self) -> Optional[CycleResult]:
        return self.history[-1] if self.history else None
