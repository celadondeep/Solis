"""Uždaro ciklo vykdymo priežiūra abiem elektrinėms."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from time import monotonic


_UNAVAILABLE = {None, "", "unknown", "unavailable"}


@dataclass(frozen=True)
class ExecutionCheck:
    state: str
    mismatches: tuple[str, ...]
    desired: dict
    observed: dict


def _same_number(actual, expected, tolerance=0.5):
    try:
        return isfinite(float(actual)) and abs(float(actual) - float(expected)) <= tolerance
    except (TypeError, ValueError):
        return False


def _same_mode(actual, expected, actuator):
    if actual == expected:
        return True
    aliases = actuator.get("mode_aliases", {})
    return actual in aliases.get(expected, ())


def evaluate_execution(
    plan,
    observed,
    actuator,
    inverter_control_available,
    slot_cutoff_tolerance=0.5,
):
    """Palygina atominį planą su realiomis pavarų būsenomis."""
    if plan is None:
        return ExecutionCheck("starting", (), {}, dict(observed))

    desired = {
        "mode": actuator["mode_by_plan"].get(plan.mode),
        "slot": None if plan.slot_active is None else (
            "on" if plan.slot_active else "off"
        ),
        "slot_cutoff_soc": plan.slot_cutoff_soc,
        "exclusive_slots": {
            entity_id: "off" for entity_id in actuator.get("exclusive_off", ())
        },
        "power": None if plan.inverter_on is None else (
            "on" if plan.inverter_on else "off"
        ),
    }

    if not plan.actionable:
        return ExecutionCheck("paused", (), desired, dict(observed))

    mismatches = []
    if observed.get("executor") != "on":
        mismatches.append("executor_off")

    expected_mode = desired["mode"]
    actual_mode = observed.get("mode")
    if expected_mode is not None and not _same_mode(actual_mode, expected_mode, actuator):
        mismatches.append(
            "mode_unavailable" if actual_mode in _UNAVAILABLE else "mode"
        )

    expected_slot = desired["slot"]
    actual_slot = observed.get("slot")
    if expected_slot is not None and actual_slot != expected_slot:
        mismatches.append(
            "slot_unavailable" if actual_slot in _UNAVAILABLE else "slot"
        )

    if plan.slot_active and plan.slot_cutoff_soc is not None:
        actual_cutoff = observed.get("slot_cutoff_soc")
        if not _same_number(
            actual_cutoff,
            plan.slot_cutoff_soc,
            tolerance=slot_cutoff_tolerance,
        ):
            mismatches.append(
                "slot_cutoff_unavailable"
                if actual_cutoff in _UNAVAILABLE else "slot_cutoff_soc"
            )

    exclusive_states = observed.get("exclusive_slots", {})
    for entity_id in actuator.get("exclusive_off", ()):
        actual = exclusive_states.get(entity_id)
        if actual != "off":
            prefix = "shadow_slot_unavailable" if actual in _UNAVAILABLE else "shadow_slot_on"
            mismatches.append(f"{prefix}:{entity_id}")

    if inverter_control_available and desired["power"] is not None:
        actual_power = observed.get("power")
        if actual_power != desired["power"]:
            mismatches.append(
                "power_unavailable" if actual_power in _UNAVAILABLE else "power"
            )

    return ExecutionCheck(
        "ok" if not mismatches else "degraded",
        tuple(mismatches),
        desired,
        dict(observed),
    )


def build_supervisor_mixin(profile):
    """Sukuria vienos elektrinės uždaro ciklo HA adapterį."""
    actuator = profile["ACTUATOR"]
    output = profile["OUTPUT"]
    site_key = profile["KEY"]
    site_label = profile["SITE_LABEL"]
    executor_entity = profile["EXECUTOR_ENTITY"]
    settle_seconds = float(profile["EXECUTOR_SETTLE_SECONDS"])
    # SOC reikšmės yra sveiki procentai; kai kurie inverteriai grąžina
    # gretimą kvantuotą reikšmę po Modbus rašymo (pvz. 12 -> 13).
    slot_cutoff_tolerance = float(actuator.get("slot_cutoff_tolerance", 0.5))
    inverter_control_available = bool(profile["INVERTER_CONTROL_AVAILABLE"])
    forecast_source = profile["FORECAST_SOURCE_LABEL"]

    class SupervisorMixin:

        def publish_executor_health(self, kwargs=None):
            observed = {
                "executor": self.get_state(executor_entity),
                "mode": self.get_state(actuator["mode"]),
                "slot": self.get_state(actuator["slot"]),
                "slot_cutoff_soc": self.get_state(actuator["slot_cutoff"]),
                "exclusive_slots": {
                    entity_id: self.get_state(entity_id)
                    for entity_id in actuator.get("exclusive_off", ())
                },
                "power": self.get_state(actuator["power"]),
            }
            check = evaluate_execution(
                self.current_plan,
                observed,
                actuator,
                inverter_control_available,
                slot_cutoff_tolerance,
            )
            state = check.state
            age = monotonic() - self.last_plan_committed_monotonic
            command_entity = profile.get("COMMAND_STATUS_ENTITY")
            command_status = self.get_state(command_entity) if command_entity else None
            observed["command_status"] = command_status
            if command_entity:
                if command_status in {"sending", "settling", "cooldown", "ready"} and state == "degraded":
                    state = "applying"
                elif command_status in {"not_confirmed", "awaiting_confirmation", "waiting_for_cloud", "cloud_backoff", "storage_error", "unsupported_profile", "unknown", "unavailable", None} and state != "paused":
                    state = "degraded"
            elif state == "degraded" and age < settle_seconds:
                state = "applying"

            limitations = []
            if not inverter_control_available:
                limitations.append(
                    "Inverterio on/off tik stebimas: SolisCloud paskyra neturi rašymo teisės"
                )

            self.set_state(
                output["executor_health"],
                state=state,
                attributes={
                    "friendly_name": f"{site_label}: Executor sveikata",
                    "icon": "mdi:shield-check" if state == "ok" else "mdi:shield-alert",
                    "site": site_key,
                    "last_checked": datetime.now().astimezone().isoformat(),
                    "mismatches": list(check.mismatches),
                    "desired": check.desired,
                    "observed": observed,
                    "settle_seconds": settle_seconds,
                    "slot_cutoff_tolerance": slot_cutoff_tolerance,
                    "mode_aliases": actuator.get("mode_aliases", {}),
                    "limitations": limitations,
                    "forecast_source": forecast_source,
                },
            )

    return SupervisorMixin
