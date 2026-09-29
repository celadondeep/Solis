"""Šešėlinio režimo palyginimas ir komandų peržiūra.

Šiame modulyje nėra jokio HA serviso kvietimo. Jis gali suformuoti tik
hipotetines komandas, kad jas būtų galima patikrinti emuliatoriuje arba
dashboarde.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any, Mapping, Optional

from energy_system.contracts import CommandIntent, ExecutionResult
from energy_system.supervisor import evaluate_execution


@dataclass(frozen=True)
class ShadowComparison:
    site: str
    status: str
    differences: tuple[str, ...]
    candidate_mode: str
    active_mode: Optional[str]
    candidate_revision: Optional[int] = None


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value)
        return number if isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _bool_state(value: Any) -> Optional[bool]:
    if value in (True, "on", "true", "1", 1):
        return True
    if value in (False, "off", "false", "0", 0):
        return False
    return None


def compare_plan(
    plan,
    *,
    site: str,
    active_mode: Any,
    active_attributes: Optional[Mapping[str, Any]] = None,
    slot_cutoff_tolerance: float = 0.5,
) -> ShadowComparison:
    """Palygina kandidatinį planą su paskutiniu HA commit sensoriumi."""
    attributes = dict(active_attributes or {})
    active = None if active_mode in (None, "", "unknown", "unavailable") else str(active_mode)
    if active is None:
        return ShadowComparison(
            site=site,
            status="pending",
            differences=("active_plan_missing",),
            candidate_mode=plan.mode,
            active_mode=None,
        )

    if not plan.actionable:
        return ShadowComparison(
            site=site,
            status="paused",
            differences=(),
            candidate_mode=plan.mode,
            active_mode=active,
            candidate_revision=attributes.get("revision"),
        )

    differences = []
    if active != plan.mode:
        differences.append(f"mode:{active}!={plan.mode}")

    expected_slot = None if plan.slot_active is None else (
        "on" if plan.slot_active else "off"
    )
    actual_slot = attributes.get("slot_active")
    if expected_slot is not None and actual_slot not in (None, "unknown"):
        if _bool_state(actual_slot) != _bool_state(expected_slot):
            differences.append(f"slot:{actual_slot}!={expected_slot}")

    if plan.slot_cutoff_soc is not None:
        actual_cutoff = _number(attributes.get("slot_cutoff_soc"))
        if (
            actual_cutoff is not None
            and abs(actual_cutoff - plan.slot_cutoff_soc) > slot_cutoff_tolerance
        ):
            differences.append(
                f"cutoff:{actual_cutoff:g}!={plan.slot_cutoff_soc:g}"
            )

    expected_power = None if plan.inverter_on is None else (
        "on" if plan.inverter_on else "off"
    )
    actual_power = attributes.get("inverter_on")
    if expected_power is not None and actual_power not in (None, "unknown"):
        if _bool_state(actual_power) != _bool_state(expected_power):
            differences.append(f"power:{actual_power}!={expected_power}")

    return ShadowComparison(
        site=site,
        status="match" if not differences else "drift",
        differences=tuple(differences),
        candidate_mode=plan.mode,
        active_mode=active,
        candidate_revision=attributes.get("revision"),
    )


def preview_execution(
    plan,
    observed: Mapping[str, Any],
    actuator: Mapping[str, Any],
    *,
    site: str,
    inverter_control_available: bool,
) -> ExecutionResult:
    """Sukuria komandų peržiūrą; nieko nevykdo."""
    check = evaluate_execution(
        plan,
        dict(observed),
        actuator,
        inverter_control_available,
        slot_cutoff_tolerance=float(actuator.get("slot_cutoff_tolerance", 0.5)),
    )
    if not plan.actionable:
        return ExecutionResult(site=site, status="paused")
    if not check.mismatches:
        return ExecutionResult(site=site, status="no_change")

    commands = []
    desired = check.desired
    current = check.observed

    def add(actuator_name: str, value: Any, mismatch: str) -> None:
        commands.append(
            CommandIntent(
                site=site,
                actuator=actuator_name,
                value=value,
                reason=f"šešėlinė peržiūra: {mismatch}",
                idempotency_key=f"{site}:{actuator_name}:{value!r}",
            )
        )

    if desired.get("mode") is not None and current.get("mode") != desired["mode"]:
        add("mode", desired["mode"], "mode")
    if desired.get("slot") is not None and current.get("slot") != desired["slot"]:
        add("slot", desired["slot"], "slot")
    if (
        desired.get("slot_cutoff_soc") is not None
        and current.get("slot_cutoff_soc") != desired["slot_cutoff_soc"]
    ):
        add("slot_cutoff_soc", desired["slot_cutoff_soc"], "cutoff")
    for entity_id, expected in desired.get("exclusive_slots", {}).items():
        if current.get("exclusive_slots", {}).get(entity_id) != expected:
            add(f"exclusive:{entity_id}", expected, "shadow_slot")
    if (
        inverter_control_available
        and desired.get("power") is not None
        and current.get("power") != desired["power"]
    ):
        add("power", desired["power"], "power")

    return ExecutionResult(
        site=site,
        status="shadow_only",
        commands=tuple(commands),
    )
