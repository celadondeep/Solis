from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from energy_system.command_queue import SingleWriterQueue
from energy_system.contracts import CommandIntent, ModuleProposal
from energy_system.decision_center import ValdymoKoordinatorius
from energy_system.state_machine import PlantState, PlantStateMachine, StateInputs


class CoordinatorTests(unittest.TestCase):
    def test_safety_proposal_wins_and_other_is_recorded(self):
        coordinator = ValdymoKoordinatorius("home")
        normal = ModuleProposal(
            site="home", module="planner", priority=500,
            kind="planner", reason="normal", payload={"mode": "self_use"},
        )
        safety = ModuleProposal(
            site="home", module="telemetry", priority=950,
            kind="telemetry_hold", reason="stale", payload={"mode": "hold"},
        )
        result = coordinator.choose([normal, safety])
        self.assertEqual(result.selected, safety)
        self.assertEqual(result.status, "selected")
        self.assertEqual(result.rejected, (normal,))

    def test_cross_site_proposal_cannot_win(self):
        result = ValdymoKoordinatorius("eimo").choose([
            ModuleProposal(
                site="home", module="planner", priority=1000,
                kind="safety", reason="wrong site", payload={"x": 1},
            )
        ])
        self.assertIsNone(result.selected)
        self.assertEqual(result.status, "hold")
        self.assertEqual(result.rejected[0].site, "home")


class StateMachineTests(unittest.TestCase):
    def test_offline_then_requires_recovery_cycles(self):
        machine = PlantStateMachine(recovery_cycles=2)
        self.assertEqual(
            machine.step(StateInputs(adapter_online=False)).current,
            PlantState.OFFLINE,
        )
        self.assertEqual(
            machine.step(StateInputs()).current,
            PlantState.RECOVERY,
        )
        self.assertEqual(
            machine.step(StateInputs()).current,
            PlantState.NORMAL,
        )

    def test_storm_and_manual_have_explicit_safety_states(self):
        machine = PlantStateMachine()
        self.assertEqual(
            machine.step(StateInputs(storm=True)).current,
            PlantState.STORM,
        )
        self.assertEqual(
            machine.step(StateInputs(manual=True)).current,
            PlantState.MANUAL,
        )


class CommandQueueTests(unittest.TestCase):
    def setUp(self):
        self.queue = SingleWriterQueue("home")

    def _command(self, actuator, value, key, site="home"):
        return CommandIntent(site, actuator, value, "test", key)

    def test_duplicate_is_suppressed(self):
        result = self.queue.result([
            self._command("slot", "on", "a"),
            self._command("slot", "on", "a"),
        ])
        self.assertEqual(result.status, "queued")
        self.assertEqual(len(result.commands), 1)
        self.assertEqual(len(result.suppressed), 1)

    def test_conflict_and_cross_site_are_rejected(self):
        result = self.queue.result([
            self._command("slot", "on", "a"),
            self._command("slot", "off", "b"),
            self._command("mode", "Self-Use", "c", site="eimo"),
        ])
        self.assertEqual(result.status, "error")
        self.assertTrue(any(item.startswith("conflict:slot") for item in result.errors))
        self.assertIn("cross_site:eimo", result.errors)


if __name__ == "__main__":
    unittest.main()
