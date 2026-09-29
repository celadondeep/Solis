"""Namų vartojimo modelio adapteris į bendrą modulį."""

from energy_system.consumption import build_consumption_model
from energy_system.profiles import HOME_CONSUMPTION

ConsumptionModel = build_consumption_model(HOME_CONSUMPTION)
