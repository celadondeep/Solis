"""Recovery checks for persisted night-stage memory."""
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from energy_system.horizon_adapter import build_horizon_mixin


class HorizonMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tz = ZoneInfo("Europe/Vilnius")
        now = datetime.now(self.tz)
        dawn = (now + timedelta(hours=5)).replace(minute=0, second=0, microsecond=0)
        self.memory = {
            "night_plan_date": dawn.date().isoformat(),
            "night_plan_target_soc": 48,
            "evening_target_soc": 63,
            "night_split_enabled": True,
            "evening_done": True,
            "discharge_phase": "morning",
            "discharge_deadline": dawn.isoformat(),
            "discharge_committed": True,
            "pv_start_at": dawn.isoformat(),
            "calculated_at": now.isoformat(),
        }
        self.profile = {
            "KEY": "eimo", "SITE_LABEL": "Eimo", "TIMEZONE": "Europe/Vilnius",
            "SENSOR": {"power_state": "sensor.power"},
            "OUTPUT": {"horizon": "sensor.horizon", "target_soc": "sensor.target"},
            "SOC_BUFFER": {}, "HORIZON": {}, "PLAN_HARD_FLOOR": 13,
            "KWH_PER_SOC": .16, "ESO_EXPORT_LIMIT_KW": 1,
            "PLAN_TARGET_BAND_CEILING": 85, "FORECAST_SOURCE_LABEL": "test",
        }

    def make_app(self, saved=None, night_memory=None):
        mixin = build_horizon_mixin(self.profile)

        class App(mixin):
            def __init__(app):
                app.states = {
                    "sensor.horizon": saved or {"state": "unknown", "attributes": {}},
                    "sensor.power": {"state": "on", "attributes": {}},
                }
                app.published = {}
                app._night_plan_state = night_memory
                app._horizon_result = None

            def _read_state_record(app, entity):
                return app.states.get(entity, {"state": "unknown", "attributes": {}})

            def _calculate_horizon(app, now, soc):
                raise RuntimeError("temporary forecast failure")

            def set_state(app, entity, **kwargs):
                app.published[entity] = kwargs

            def is_storm_mode(app):
                return False

            def log(app, *args, **kwargs):
                pass

        return App()

    def test_forecast_failure_keeps_memory_but_disables_forced_export(self):
        app = self.make_app(night_memory=self.memory)
        result = app.horizon_guidance(60)
        self.assertFalse(result["valid"])
        self.assertEqual(result["export_now"], False)
        self.assertEqual(result["night_plan_memory"]["night_plan_target_soc"], 48)
        self.assertTrue(result["night_plan_memory"]["discharge_committed"])

    def test_memory_survives_restart_after_invalid_forecast(self):
        first = self.make_app(night_memory=self.memory)
        failed = first.horizon_guidance(60)
        saved = {"state": "fallback", "attributes": {
            "site": "eimo", "valid": "off", "night_active": "off",
            "night_plan_memory": failed["night_plan_memory"],
        }}
        restarted = self.make_app(saved)
        restarted.horizon_guidance(60)
        self.assertEqual(restarted._night_plan_state["night_plan_target_soc"], 48)
        self.assertTrue(restarted._night_plan_state["discharge_committed"])


if __name__ == "__main__":
    unittest.main()
