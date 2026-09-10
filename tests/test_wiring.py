from __future__ import annotations

import importlib
import sys
import types
import unittest


class WiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        appdaemon = types.ModuleType("appdaemon")
        plugins = types.ModuleType("appdaemon.plugins")
        hass_pkg = types.ModuleType("appdaemon.plugins.hass")
        hassapi = types.ModuleType("appdaemon.plugins.hass.hassapi")

        class Hass:
            pass

        hassapi.Hass = Hass
        sys.modules.setdefault("appdaemon", appdaemon)
        sys.modules.setdefault("appdaemon.plugins", plugins)
        sys.modules.setdefault("appdaemon.plugins.hass", hass_pkg)
        sys.modules.setdefault("appdaemon.plugins.hass.hassapi", hassapi)

        cls.home = importlib.import_module("energy_manager").EnergyManager
        cls.eimo = importlib.import_module("energy_manager_eimo").EnergyManager
        cls.home_consumption = importlib.import_module(
            "consumption_model"
        ).ConsumptionModel
        cls.eimo_consumption = importlib.import_module(
            "consumption_model_eimo"
        ).ConsumptionModel

    def test_site_adapters_build_distinct_classes(self):
        self.assertIsNot(self.home, self.eimo)
        self.assertIsNot(self.home_consumption, self.eimo_consumption)

    def test_energy_manager_has_explicit_peripheral_layers(self):
        expected = [
            "EnergyManager",
            "ForecastMixin",
            "NightPlanningMixin",
            "BoilerMixin",
            "SupervisorMixin",
            "Hass",
        ]
        self.assertEqual(
            [item.__name__ for item in self.home.__mro__[:6]],
            expected,
        )

    def test_core_and_peripheral_responsibilities_are_wired(self):
        self.assertIn("compute_plan", self.home.__dict__)
        self.assertTrue(callable(getattr(self.home, "battery_room_needed")))
        self.assertTrue(callable(getattr(self.home, "publish_night_economics")))
        self.assertTrue(callable(getattr(self.home, "strategic_cycle")))
        self.assertTrue(callable(getattr(self.home, "publish_executor_health")))


if __name__ == "__main__":
    unittest.main()
