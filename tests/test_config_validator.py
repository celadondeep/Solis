from __future__ import annotations

import unittest

from energy_system.config_validator import validate_profile, validate_profiles
from energy_system.module_catalog import module_definitions
from energy_system.profiles import EIMO_ENERGY, HOME_ENERGY


class ConfigValidatorTests(unittest.TestCase):
    def test_both_real_profiles_are_valid(self):
        self.assertEqual(validate_profile(HOME_ENERGY), ())
        self.assertEqual(validate_profile(EIMO_ENERGY), ())
        self.assertEqual(validate_profiles([HOME_ENERGY, EIMO_ENERGY]), ())

    def test_cross_site_actuator_is_rejected(self):
        clone = dict(EIMO_ENERGY)
        clone["KEY"] = "other"
        clone["SENSOR"] = dict(EIMO_ENERGY["SENSOR"])
        clone["ACTUATOR"] = dict(EIMO_ENERGY["ACTUATOR"])
        clone["OUTPUT"] = dict(EIMO_ENERGY["OUTPUT"])
        clone["OUTPUT"]["plan"] = HOME_ENERGY["OUTPUT"]["plan"]
        issues = validate_profiles([HOME_ENERGY, clone])
        self.assertTrue(any(item.startswith("cross_site_entity:") for item in issues))

    def test_catalog_has_single_physical_writer(self):
        definitions = {item.name: item for item in module_definitions(HOME_ENERGY)}
        self.assertTrue(definitions["executor"].can_write_actuators)
        self.assertFalse(definitions["planner"].can_write_actuators)
        self.assertFalse(definitions["coordinator"].can_write_actuators)
        self.assertEqual(
            sum(item.can_write_actuators for item in definitions.values()),
            1,
        )


if __name__ == "__main__":
    unittest.main()
