"""Komandinės eilutės įrankis istoriniam scenarijui atkurti."""

from __future__ import annotations

# Leidžia scenarijų paleisti tiesiogiai iš /homeassistant kelio.
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import argparse
import json
from pathlib import Path

from energy_system.planner import PlannerPolicy
from energy_system.profiles import EIMO_ENERGY, HOME_ENERGY
from energy_system.simulator.assertions import (
    assert_executor_converges,
    assert_no_physical_commands_when_paused,
    assert_single_writer,
)
from energy_system.simulator.model import SimObservation, SimPlant
from energy_system.simulator.replay import events_from_rows, load_rows


def _profile(site: str):
    return HOME_ENERGY if site == "home" else EIMO_ENERGY


def run_file(path: str, site: str) -> dict:
    profile = _profile(site)
    rows = load_rows(path)
    events = events_from_rows(rows)
    policy = PlannerPolicy(
        hard_floor=profile["PLAN_HARD_FLOOR"],
        night_rest_soc=profile["NIGHT_REST_SOC"],
        preferred_floor=profile["PLAN_TARGET_BAND_FLOOR"],
        ceiling=profile["PLAN_TARGET_BAND_CEILING"],
    )
    first_at = events[0].at if events else None
    initial = SimObservation(
        at=first_at or __import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        soc=rows[0].get("soc", 50.0) if rows else 50.0,
        in_production_hours=False,
    )
    plant = SimPlant(
        site,
        policy,
        actuator=profile["ACTUATOR"],
        inverter_control_available=profile["INVERTER_CONTROL_AVAILABLE"],
        max_soc_age_seconds=profile.get("TELEMETRY_MAX_AGE_SECONDS", {}).get(
            "soc", 900
        ),
        initial=initial,
    )
    results = plant.run(events)
    # Invariantai tikrina, kad atkūrimas neįveda fizinių komandų į pause ir
    # nepalieka kelių komandų tam pačiam aktuatoriui.
    assert_no_physical_commands_when_paused(results)
    assert_single_writer(results)
    if results and results[-1].plan.actionable:
        assert_executor_converges(results)
    modes = {}
    for result in results:
        modes[result.plan.mode] = modes.get(result.plan.mode, 0) + 1
    return {
        "site": site,
        "file": str(path),
        "cycles": len(results),
        "commands": sum(len(item.commands.commands) for item in results),
        "errors": sum(len(item.commands.errors) for item in results),
        "paused_cycles": sum(not item.plan.actionable for item in results),
        "degraded_cycles": sum(
            item.execution_after.state == "degraded" for item in results
        ),
        "modes": modes,
        "first_at": results[0].at.isoformat() if results else None,
        "last_at": results[-1].at.isoformat() if results else None,
        "last_plan": results[-1].plan.mode if results else None,
        "last_executor": results[-1].execution_after.state if results else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", choices=("home", "eimo"), required=True)
    parser.add_argument("--file", required=True)
    args = parser.parse_args()
    print(json.dumps(run_file(args.file, args.site), ensure_ascii=False))


if __name__ == "__main__":
    main()
