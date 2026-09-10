from __future__ import annotations

import unittest
from pathlib import Path


class PowerQualityManagerSafetyTests(unittest.TestCase):
    @staticmethod
    def _source():
        here = Path(__file__).resolve().parent
        candidates = (here / "power_quality_manager.py", here.parent / "power_quality_manager.py")
        path = next((candidate for candidate in candidates if candidate.exists()), None)
        if path is None:
            raise AssertionError("power_quality_manager.py nerastas")
        return path.read_text(encoding="utf-8")

    def test_adapter_contains_no_actuator_calls(self):
        source = self._source()
        forbidden = ("call_service(", "turn_on(", "turn_off(", "select_option(", "set_value(")
        for token in forbidden:
            self.assertNotIn(token, source)

    def test_adapter_declares_observation_only(self):
        source = self._source()
        self.assertIn('"control_scope": "observation_only"', source)
        self.assertIn('"writes_disabled": True', source)


if __name__ == "__main__":
    unittest.main()
