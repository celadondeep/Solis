"""AppDaemon classes for every registered site; no per-inverter Python wrappers."""
from energy_system.site_registry import SITES, get_site
from energy_system.manager import build_energy_manager
from energy_system.consumption import build_consumption_model
from energy_system.shadow_manager import build_shadow_manager

for _key in SITES:
    _site = get_site(_key)
    for _name, _factory, _section in (
        ("EnergyManager", build_energy_manager, "energy"),
        ("ConsumptionModel", build_consumption_model, "consumption"),
        ("ShadowManager", build_shadow_manager, "energy"),
    ):
        _class = _factory(_site[_section])
        _class.__name__ = f"{_name}_{_key}"
        _class.__module__ = __name__
        globals()[_class.__name__] = _class
del _key, _site, _name, _factory, _section, _class
