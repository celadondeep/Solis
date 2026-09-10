"""Gryni kaupiklio sveikatos skaičiavimai be HA/AppDaemon priklausomybių."""

from __future__ import annotations

from math import isfinite


EFFICIENCY_MAX_VALID = 105.0


def battery_efficiency_percent(charged, discharged, inverter_self, soc_delta_kwh):
    """Grąžina dienos energijos balanso efektyvumą procentais."""
    try:
        charged = float(charged)
        output = float(discharged) + float(inverter_self) + float(soc_delta_kwh)
    except (TypeError, ValueError):
        return None
    if not isfinite(charged) or charged <= 0 or not isfinite(output):
        return None
    return output / charged * 100.0


def is_plausible_efficiency(value, maximum=EFFICIENCY_MAX_VALID):
    """Atmeta nefizinius ar sugadintus matavimo rezultatus."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return False
    return isfinite(value) and 0 < value <= float(maximum)


def plausible_efficiency_rows(rows, maximum=EFFICIENCY_MAX_VALID):
    """Filtruoja trendams tinkamus įrašus, išsaugodama žalią istoriją."""
    usable = []
    for row in rows:
        if row.get("valid") is False:
            continue
        if is_plausible_efficiency(row.get("efficiency"), maximum):
            usable.append(row)
    return usable
