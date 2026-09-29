"""Namų elektrinės adapteris į bendrą modulinį valdiklį."""

from energy_system.manager import build_energy_manager
from energy_system.profiles import HOME_ENERGY

EnergyManager = build_energy_manager(HOME_ENERGY)
