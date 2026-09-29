"""Recovery checks for persisted night-stage memory."""
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from energy_system.horizon_adapter import (
    _decode_night_plan_state, _encode_night_plan_state, build_horizon_mixin,
)


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
            "OUTPUT": {"horizon": "sensor.horizon", "target_soc": "sensor.target",
                       "night_state": "input_text.night_memory"},
            "SOC_BUFFER": {}, "HORIZON": {}, "PLAN_HARD_FLOOR": 13,
            "KWH_PER_SOC": .16, "ESO_EXPORT_LIMIT_KW": 1,
            "PLAN_TARGET_BAND_CEILING": 85, "FORECAST_SOURCE_LABEL": "test",
        }

    def make_app(self, saved=None, night_memory=None, valid=False):
        mixin = build_horizon_mixin(self.profile)

        class App(mixin):
            def __init__(app):
                app.states = {
                    "sensor.horizon": saved or {"state": "unknown", "attributes": {}},
                    "sensor.power": {"state": "on", "attributes": {}},
                }
                app.published = {}
                app.calls = []
                app._night_plan_state = night_memory
                app._horizon_result = None

            def _read_state_record(app, entity):
                return app.states.get(entity, {"state": "unknown", "attributes": {}})

            def _calculate_horizon(app, now, soc):
                if valid:
                    dawn = (now + timedelta(hours=5)).replace(minute=0, second=0, microsecond=0)
                    return {
                        "version": "test", "valid": True, "export_now": True,
                        "night_active": True, "inverter_on": True,
                        "solar_export_priority": False, "soc_buffer_active": False,
                        "predictive_buffer_due": False, "reserve_soc": 40,
                        "cutoff_soc": 48, "target_soc": 48, "wake_at": dawn.isoformat(),
                        "discharge_start_at": now.isoformat(), "discharge_deadline": dawn.isoformat(),
                        "required_preexport_kwh": 1, "required_headroom_kwh": 2,
                        "standby_saved_kwh": 0, "reason": "test", "night_plan_date": dawn.date().isoformat(),
                        "night_plan_target_soc": 48, "evening_target_soc": 63,
                        "night_split_enabled": True, "evening_done": True,
                        "discharge_phase": "morning", "discharge_committed": True,
                        "pv_start_at": dawn.isoformat(),
                    }
                raise RuntimeError("temporary forecast failure")

            def set_state(app, entity, **kwargs):
                app.published[entity] = kwargs

            def is_storm_mode(app):
                return False

            def log(app, *args, **kwargs):
                pass

            def call_service(app, service, **kwargs):
                app.calls.append((service, kwargs))

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

    def test_compact_helper_memory_round_trips_within_input_text_limit(self):
        raw = _encode_night_plan_state(self.memory, datetime.now(self.tz).isoformat())
        self.assertIsNotNone(raw)
        self.assertLessEqual(len(raw), 255)
        restored = _decode_night_plan_state(raw)
        self.assertEqual(restored["night_plan_target_soc"], 48)
        self.assertTrue(restored["night_split_enabled"])
        self.assertTrue(restored["discharge_committed"])

    def test_persistent_helper_updates_only_when_night_plan_changes(self):
        app = self.make_app(valid=True)
        app.horizon_guidance(60, force=True)
        app.horizon_guidance(60, force=True)
        self.assertEqual(len(app.calls), 1)
        self.assertEqual(app.calls[0][0], "input_text/set_value")
        self.assertEqual(app.calls[0][1]["entity_id"], "input_text.night_memory")

    def test_helper_events_override_stale_horizon_attributes_after_restart(self):
        current = dict(self.memory, night_events=[12345678, 12345738, 0, 0, 0, 0, 0])
        raw = _encode_night_plan_state(current, self.memory["calculated_at"])
        stale = dict(self.memory, night_events=[0] * 7)
        saved = {"state": "sleep", "attributes": {
            "site": "eimo", "valid": "on", "night_active": "on",
            "night_plan_memory": stale,
        }}
        app = self.make_app(saved=saved, valid=True)
        app.states["input_text.night_memory"] = {"state": raw, "attributes": {}}
        result = app.horizon_guidance(60)
        self.assertEqual(result["night_events"][:2], current["night_events"][:2])


if __name__ == "__main__":
    unittest.main()
