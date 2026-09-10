from __future__ import annotations

import unittest
from datetime import datetime, timezone

from energy_system.core import PlantCore
from energy_system.planner import PlannerInput, PlannerPolicy
from energy_system.state_machine import PlantState


class PlantCoreTests(unittest.TestCase):
    def setUp(self):
        self.core = PlantCore(
            "home",
            PlannerPolicy(hard_floor=12, night_rest_soc=16),
        )
        self.inputs = PlannerInput(
            storm=False,
            manual=False,
            soc=90,
            target_soc=80,
            in_production_hours=False,
            export_floor=12,
            room_shortfall_kwh=0,
        )

    def test_core_returns_one_plan_and_one_coordinated_decision(self):
        cycle = self.core.run(
            self.inputs,
            adapter_online=True,
            executor_online=True,
            now=datetime(2026, 9, 5, tzinfo=timezone.utc),
        )
        self.assertEqual(cycle.plan.mode, "night_export")
        self.assertEqual(cycle.decision.site, "home")
        self.assertEqual(cycle.decision.status, "selected")
        self.assertEqual(cycle.transition.current, PlantState.NORMAL)

    def test_core_does_not_cross_site(self):
        with self.assertRaises(ValueError):
            PlantCore("", PlannerPolicy(hard_floor=12))


if __name__ == "__main__":
    unittest.main()
