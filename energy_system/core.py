"""Perplantinis branduolys: įėjimai -> vienas planas -> viena būsena.

PlantCore yra bendras kodas, tačiau kiekvienas objektas gauna savo profilio
instanciją, savo koordinatoriaus būseną ir savo vykdymo adapterį. Tarp
instancijų nėra bendrų kintamųjų ar HA entity vardų.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from energy_system.contracts import CoordinatorDecision, ModuleProposal
from energy_system.decision_center import ValdymoKoordinatorius, priority_for
from energy_system.planner import (
    EnergyPlan,
    PlannerInput,
    PlannerPolicy,
    decide_plan,
)
from energy_system.state_machine import (
    PlantStateMachine,
    StateInputs,
    StateTransition,
)


@dataclass(frozen=True)
class CoreCycle:
    plan: EnergyPlan
    decision: CoordinatorDecision
    transition: StateTransition


class PlantCore:
    """Vienos elektrinės centrinė sprendimų mašina be HA I/O."""

    def __init__(
        self,
        site: str,
        policy: PlannerPolicy,
        *,
        recovery_cycles: int = 2,
    ):
        self.site = str(site)
        self.policy = policy
        self.coordinator = ValdymoKoordinatorius(self.site)
        self.state_machine = PlantStateMachine(
            recovery_cycles=recovery_cycles
        )

    def run(
        self,
        inputs: PlannerInput,
        *,
        adapter_online: bool = True,
        executor_online: bool = True,
        now: Optional[datetime] = None,
    ) -> CoreCycle:
        plan = decide_plan(inputs, self.policy)
        proposal = ModuleProposal(
            site=self.site,
            module="planner",
            priority=priority_for(plan.priority, 500),
            kind=plan.priority,
            reason=plan.reason,
            payload={"plan": plan},
            created_at=now,
        )
        decision = self.coordinator.choose([proposal], now=now)
        selected = (
            decision.selected.payload["plan"]
            if decision.selected is not None
            else plan
        )
        transition = self.state_machine.step(
            StateInputs(
                telemetry_ok=selected.inputs_valid,
                storm=inputs.storm,
                manual=inputs.manual,
                adapter_online=adapter_online,
                executor_online=executor_online,
                reserve_low=(
                    inputs.soc is not None
                    and inputs.soc <= self.policy.hard_floor
                ),
            )
        )
        return CoreCycle(
            plan=selected,
            decision=decision,
            transition=transition,
        )
