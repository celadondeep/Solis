"""Eimo elektrinės šešėlinio branduolio adapteris."""

from energy_system.profiles import EIMO_ENERGY
from energy_system.shadow_manager import build_shadow_manager

ShadowManager = build_shadow_manager(EIMO_ENERGY)
