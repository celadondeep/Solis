"""Namų elektrinės šešėlinio branduolio adapteris."""

from energy_system.profiles import HOME_ENERGY
from energy_system.shadow_manager import build_shadow_manager

ShadowManager = build_shadow_manager(HOME_ENERGY)
