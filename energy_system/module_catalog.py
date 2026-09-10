"""Universalių periferinių modulių katalogas.

Katalogas aprašo atsakomybę, bet neleidžia periferijai tapti antru
Valdymo koordinatoriumi ar tiesioginiu fizinio aktuatoriaus savininku.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Tuple


@dataclass(frozen=True)
class ModuleDefinition:
    name: str
    role: str
    can_propose: bool
    can_write_actuators: bool
    outputs: Tuple[str, ...] = ()


MODULE_CATALOG = {
    "telemetry": ModuleDefinition(
        "telemetry", "normalizuoja ir tikrina įėjimus", True, False
    ),
    "forecast": ModuleDefinition(
        "forecast", "prognozuoja PV ir korekcijas", True, False
    ),
    "consumption": ModuleDefinition(
        "consumption", "mokosi vartojimo profilio", True, False
    ),
    "eso": ModuleDefinition(
        "eso", "vertina tinklo / eksporto ribas", True, False
    ),
    "battery_health": ModuleDefinition(
        "battery_health", "stebi baterijos būklę", True, False
    ),
    "safety": ModuleDefinition(
        "safety", "saugo rezervą ir draudžia nesaugius veiksmus", True, False
    ),
    "planner": ModuleDefinition(
        "planner", "apskaičiuoja vieną kandidatinį planą", True, False
    ),
    "coordinator": ModuleDefinition(
        "coordinator", "parenka vieną galutinį planą", True, False
    ),
    "executor": ModuleDefinition(
        "executor", "vienintelis fizinių komandų adapteris", False, True
    ),
    "diagnostics": ModuleDefinition(
        "diagnostics", "rodo noras-faktas-ryšys-klaidos būseną", False, False
    ),
    "dashboard": ModuleDefinition(
        "dashboard", "tik vizualizuoja profilio duomenis", False, False
    ),
}


def module_definitions(profile: Mapping) -> tuple[ModuleDefinition, ...]:
    names = profile.get(
        "MODULES_ENABLED",
        tuple(name for name in MODULE_CATALOG if name != "coordinator"),
    )
    definitions = []
    for name in names:
        if name not in MODULE_CATALOG:
            continue
        definitions.append(MODULE_CATALOG[name])
    # Koordinatorius ir vykdyklė visada egzistuoja kaip branduolio ribos.
    definitions.extend(
        [
            MODULE_CATALOG["coordinator"],
            MODULE_CATALOG["executor"],
            MODULE_CATALOG["diagnostics"],
        ]
    )
    return tuple(definitions)
