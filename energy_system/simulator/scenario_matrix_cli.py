"""Paleidžia visas rankines ribinių būsenų scenarijų matricas."""

from __future__ import annotations

# Leidžia scenarijų paleisti tiesiogiai iš /homeassistant kelio.
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from energy_system.planner import PlannerPolicy
from energy_system.profiles import EIMO_ENERGY, HOME_ENERGY
from energy_system.simulator.assertions import (
    assert_no_physical_commands_when_paused,
    assert_single_writer,
)
from energy_system.simulator.model import SimObservation, SimPlant
from energy_system.simulator.replay import events_from_rows


def run_matrix(path: str) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    reports = []
    for scenario in data.get("scenarios", []):
        site = str(scenario["site"])
        profile = HOME_ENERGY if site == "home" else EIMO_ENERGY
        rows = list(scenario.get("rows", []))
        events = events_from_rows(rows)
        first_at = events[0].at if events else datetime.now(timezone.utc)
        first = rows[0] if rows else {}
        plant = SimPlant(
            site,
            PlannerPolicy(
                hard_floor=profile["PLAN_HARD_FLOOR"],
                night_rest_soc=profile["NIGHT_REST_SOC"],
            ),
            actuator=profile["ACTUATOR"],
            inverter_control_available=profile["INVERTER_CONTROL_AVAILABLE"],
            max_soc_age_seconds=profile.get(
                "TELEMETRY_MAX_AGE_SECONDS", {}
            ).get("soc", 900),
            initial=SimObservation(
                at=first_at,
                soc=first.get("soc", 50.0),
                in_production_hours=first.get(
                    "in_production_hours", True
                ),
            ),
        )
        results = plant.run(events)
        assert_no_physical_commands_when_paused(results)
        assert_single_writer(results)
        reports.append(
            {
                "name": scenario["name"],
                "site": site,
                "cycles": len(results),
                "plans": [result.plan.mode for result in results],
                "telemetry": [result.telemetry_quality for result in results],
                "commands": sum(
                    len(result.commands.commands) for result in results
                ),
                "errors": [
                    error
                    for result in results
                    for error in result.commands.errors
                ],
                "executor": (
                    results[-1].execution_after.state if results else "none"
                ),
            }
        )
    return {"schema": 1, "source": str(path), "scenarios": reports}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True)
    parser.add_argument("--output")
    args = parser.parse_args()
    report = run_matrix(args.file)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
