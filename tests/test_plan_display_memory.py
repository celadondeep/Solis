"""Recorded switch events and frozen forecast points survive plan recalculation."""
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from energy_system.horizon_adapter import (
    _observe_night_events, _night_event_view,
    _encode_night_plan_state, _decode_night_plan_state,
)


class NightEventsTests(unittest.TestCase):
    def setUp(self):
        self.tz = ZoneInfo("Europe/Vilnius")
        self.dawn = datetime(2026, 10, 1, 8, tzinfo=self.tz)
        self.memory = {
            "night_plan_date": "2026-10-01", "night_plan_target_soc": 30,
            "evening_target_soc": 60, "night_split_enabled": True,
            "evening_done": False, "discharge_phase": "evening",
            "discharge_deadline": self.dawn.isoformat(),
            "pv_start_at": self.dawn.isoformat(), "discharge_committed": False,
        }

    def record(self, state, at, *, settling=False):
        return {"state": state, "last_changed": at.isoformat(),
                "attributes": {"pending_target": False,
                               "command_status": "settling" if settling else "verified",
                               "last_read": at.isoformat()}}

    def test_split_edges_are_latched_and_idempotent(self):
        eve = self.dawn-timedelta(hours=12)
        evening = {"pv_start_at": self.dawn.isoformat(),
                   "discharge_phase": "evening", "evening_done": False}
        start = _observe_night_events(self.memory, eve, self.record("on", eve),
                                     self.record("on", eve), evening, 130, 30)
        self.assertEqual(start["evening_actual_start_at"], eve.isoformat())
        _observe_night_events(self.memory, eve+timedelta(minutes=7),
                              self.record("on", eve, settling=True),
                              self.record("on", eve), evening, 130, 30)
        self.assertEqual(self.memory["night_events"][0], int(eve.timestamp()//60))
        stopped = eve+timedelta(hours=2)
        end = _observe_night_events(self.memory, stopped, self.record("off", stopped),
                                   self.record("off", stopped), dict(evening, evening_done=True), 130, 30)
        self.assertEqual(end["evening_actual_end_at"], stopped.isoformat())
        self.assertEqual(end["sleep_saved_actual_kwh"], 0)
        wake = stopped+timedelta(hours=3)
        resumed = _observe_night_events(self.memory, wake, self.record("off", stopped),
                                        self.record("on", wake), dict(evening, evening_done=True), 130, 30)
        self.assertAlmostEqual(resumed["sleep_saved_actual_kwh"], 0.3)
        morning = dict(evening, discharge_phase="morning", evening_done=True, export_now=True)
        resumed = _observe_night_events(self.memory, wake, self.record("on", wake),
                                        self.record("on", wake), morning, 130, 30)
        self.assertEqual(resumed["morning_actual_start_at"], wake.isoformat())
        raw = _encode_night_plan_state(self.memory, wake.isoformat())
        self.assertIsNotNone(raw)
        self.assertLessEqual(len(raw), 255)
        restored = _decode_night_plan_state(raw)
        self.assertEqual(restored["night_events"], self.memory["night_events"])
        self.assertAlmostEqual(_night_event_view(restored, wake, 130, 30)["sleep_saved_actual_kwh"], 0.3)


if __name__ == "__main__":
    unittest.main()
