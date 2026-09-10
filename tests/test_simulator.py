from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from energy_system.planner import PlannerPolicy
from energy_system.profiles import EIMO_ENERGY, HOME_ENERGY
from energy_system.simulator.assertions import (
    assert_executor_converges,
    assert_no_physical_commands_when_paused,
    assert_single_writer,
    assert_site_isolation,
)
from energy_system.simulator.model import SimObservation, SimPlant, ScenarioEvent
from energy_system.simulator.replay import events_from_rows


BASE = datetime(2026, 9, 5, 0, 0, tzinfo=timezone.utc)


class SimulatorTests(unittest.TestCase):
    def test_duplicate_commit_signal_does_not_repeat_commands(self):
        plant = SimPlant(
            "home",
            PlannerPolicy(hard_floor=12, night_rest_soc=16),
            actuator=HOME_ENERGY["ACTUATOR"],
            initial=SimObservation(
                at=BASE, soc=90, in_production_hours=False,
                mode="Self-Use", slot="off", slot_cutoff_soc=None,
            ),
        )
        results = plant.run([
            ScenarioEvent(BASE, label="commit-1"),
            ScenarioEvent(BASE, label="commit-duplicate"),
        ])
        self.assertGreaterEqual(len(results[0].commands.commands), 2)
        self.assertEqual(len(results[1].commands.commands), 0)
        assert_single_writer(results)
        assert_executor_converges(results)

    def test_stale_soc_holds_without_physical_commands(self):
        initial = SimObservation(
            at=BASE,
            soc=55,
            soc_reported_at=BASE,
            in_production_hours=True,
            mode="Self-Use",
            slot="off",
        )
        plant = SimPlant(
            "home",
            PlannerPolicy(hard_floor=12),
            actuator=HOME_ENERGY["ACTUATOR"],
            initial=initial,
            max_soc_age_seconds=60,
        )
        result = plant.step(BASE + timedelta(minutes=2), label="stale-soc")
        self.assertEqual(result.plan.mode, "hold")
        self.assertFalse(result.plan.actionable)
        self.assertEqual(result.telemetry_quality, "stale")
        self.assertEqual(result.commands.commands, ())
        assert_no_physical_commands_when_paused([result])

    def test_delayed_executor_converges_after_second_cycle(self):
        plant = SimPlant(
            "home",
            PlannerPolicy(hard_floor=12, night_rest_soc=16),
            actuator=HOME_ENERGY["ACTUATOR"],
            initial=SimObservation(
                at=BASE, soc=70, in_production_hours=False,
                mode="Self-Use", slot="off",
            ),
            command_delay_steps=1,
        )
        first = plant.step(BASE, label="delayed-1")
        self.assertNotEqual(first.execution_after.state, "ok")
        second = plant.step(BASE + timedelta(minutes=1), label="delayed-2")
        self.assertEqual(second.execution_after.state, "ok")
        assert_executor_converges([first, second])

    def test_failed_command_stays_degraded(self):
        plant = SimPlant(
            "home",
            PlannerPolicy(hard_floor=12, night_rest_soc=16),
            actuator=HOME_ENERGY["ACTUATOR"],
            initial=SimObservation(
                at=BASE, soc=90, in_production_hours=False,
                mode="Self-Use", slot="off",
            ),
            command_failures={"slot"},
        )
        result = plant.step(BASE, label="write-failure")
        self.assertEqual(result.commands.status, "error")
        self.assertEqual(result.execution_after.state, "degraded")
        self.assertIn("slot", result.commands.errors[0])

    def test_storm_does_not_write_when_plan_paused(self):
        plant = SimPlant(
            "eimo",
            PlannerPolicy(hard_floor=6, night_rest_soc=6),
            actuator=EIMO_ENERGY["ACTUATOR"],
            inverter_control_available=False,
            initial=SimObservation(
                at=BASE, soc=70, storm=True,
                in_production_hours=True, mode="Self-Use", slot="off",
            ),
        )
        result = plant.step(BASE, label="storm")
        self.assertEqual(result.plan.mode, "storm")
        self.assertFalse(result.plan.actionable)
        self.assertEqual(result.commands.commands, ())
        assert_no_physical_commands_when_paused([result])

    def test_two_plants_remain_isolated(self):
        home = SimPlant(
            "home",
            PlannerPolicy(hard_floor=12),
            actuator=HOME_ENERGY["ACTUATOR"],
            initial=SimObservation(at=BASE, soc=75),
        )
        eimo = SimPlant(
            "eimo",
            PlannerPolicy(hard_floor=6),
            actuator=EIMO_ENERGY["ACTUATOR"],
            inverter_control_available=False,
            initial=SimObservation(at=BASE, soc=75),
        )
        home_result = home.step(BASE, {"storm": True}, label="home-storm")
        eimo_result = eimo.step(BASE, label="eimo-normal")
        self.assertEqual(home_result.plan.mode, "storm")
        self.assertNotEqual(eimo_result.plan.mode, "storm")
        assert_site_isolation({"home": home.history, "eimo": eimo.history})

    def test_historical_rows_are_replayable(self):
        events = events_from_rows([
            {"at": "2026-09-05T00:00:00Z", "soc": 70, "in_production_hours": False},
            {"at": "2026-09-05T00:10:00Z", "soc": 65, "in_production_hours": False},
        ])
        plant = SimPlant(
            "home",
            PlannerPolicy(hard_floor=12, night_rest_soc=16),
            initial=SimObservation(at=BASE, soc=70, in_production_hours=False),
        )
        results = plant.run(events)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[-1].state.soc, 65)


if __name__ == "__main__":
    unittest.main()
