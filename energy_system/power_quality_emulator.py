"""Deterministic scenario emulator for power-quality rules.

It contains no Home Assistant adapter and can also evaluate exported historical
observations.  This makes threshold changes reviewable before live deployment.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path

from energy_system.power_quality import PowerQualityInput, evaluate_power_quality


@dataclass(frozen=True)
class Scenario:
    name: str
    expected_state: str
    observation: PowerQualityInput


def _base(**changes):
    data = {
        "pcc_voltage_v": [230, 230, 230],
        "inverter_voltage_v": [232, 232, 232],
        "pcc_active_w": [800, 800, 800],
        "pcc_reactive_var": [50, 50, 50],
        "pcc_apparent_va": [802, 802, 802],
        "inverter_current_a": [3.5, 3.5, 3.5],
        "frequency_hz": 50.0,
        "source_age_seconds": 10,
    }
    data.update(changes)
    return PowerQualityInput(**data)


SCENARIOS = (
    Scenario("nominal_balanced", "ok", _base()),
    Scenario(
        "approaching_high_voltage",
        "watch",
        _base(
            pcc_voltage_v=[248, 247, 247],
            inverter_voltage_v=[250, 249, 249],
        ),
    ),
    Scenario("sustained_high_voltage", "critical", _base(pcc_voltage_v=[254, 253, 252])),
    Scenario("sustained_low_voltage", "critical", _base(pcc_voltage_v=[206, 207, 208])),
    Scenario(
        "eimo_2026_09_07_0711",
        "critical",
        _base(
            pcc_voltage_v=[247.2, 232.5, 200.9],
            inverter_voltage_v=[250.3, 236.6, 200.4],
            pcc_active_w=[-28, -53, -1552],
            pcc_reactive_var=[147, -15, 92],
            pcc_apparent_va=[150, 55, 1555],
        ),
    ),
    Scenario(
        "eimo_2026_09_06_1725",
        "critical",
        _base(
            pcc_voltage_v=[250.4, 231.5, 207.2],
            inverter_voltage_v=[253.8, 236.1, 205.9],
            pcc_active_w=[167, 162, -1167],
            pcc_reactive_var=[140, 0, -639],
            pcc_apparent_va=[218, 162, 1330],
        ),
    ),
    Scenario(
        "balanced_grid_unbalanced_inverter_current",
        "warning",
        _base(inverter_current_a=[1, 10, 1]),
    ),
    Scenario(
        "large_reactive_share",
        "warning",
        _base(
            pcc_active_w=[200, 200, 200],
            pcc_reactive_var=[250, 250, 250],
            pcc_apparent_va=[320, 320, 320],
        ),
    ),
    Scenario("stale_cloud_snapshot", "stale", _base(stale=True, source_age_seconds=1800)),
    Scenario("missing_phase", "insufficient", _base(pcc_voltage_v=[230, "unknown", 230])),
)


def run_scenarios():
    report = []
    for scenario in SCENARIOS:
        result = evaluate_power_quality(scenario.observation)
        report.append({
            "scenario": scenario.name,
            "expected": scenario.expected_state,
            "actual": result.state,
            "passed": result.state == scenario.expected_state,
            "issues": list(result.issues),
            "data_warnings": list(result.data_warnings),
        })
    return report


def evaluate_file(path: Path):
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("observations", payload) if isinstance(payload, dict) else payload
    report = []
    for index, row in enumerate(rows):
        observation = PowerQualityInput(**row)
        result = evaluate_power_quality(observation)
        report.append({"index": index, "state": result.state, **result.as_dict()})
    return report


def main():
    parser = argparse.ArgumentParser(description="Emuliuoti tinklo kokybės scenarijus")
    parser.add_argument("--history-json", type=Path)
    args = parser.parse_args()
    report = evaluate_file(args.history_json) if args.history_json else run_scenarios()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not args.history_json and not all(row["passed"] for row in report):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
