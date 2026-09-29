"""Compatibility exports; configuration lives in sites/*.json."""
from energy_system.site_registry import APPS_DIR, profiles
ENERGY_PROFILES = profiles("energy")
CONSUMPTION_PROFILES = profiles("consumption")
# Historical scripts and simulations retain their existing imports.
HOME_ENERGY = ENERGY_PROFILES["home"]
EIMO_ENERGY = ENERGY_PROFILES["eimo"]
HOME_CONSUMPTION = CONSUMPTION_PROFILES["home"]
EIMO_CONSUMPTION = CONSUMPTION_PROFILES["eimo"]
