"""Telemetrijos normalizavimas ir šviežumo patikra.

HA skaitinė būsena nėra automatiškai šviežia: reikšmė gali likti paskutinė,
kai Modbus arba debesija jau nutrūko. Šis modulis leidžia branduoliui atskirti
"gavau skaičių" nuo "skaičius dar tinkamas saugiam valdymui".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from typing import Any, Optional

from energy_system.contracts import TelemetrySample


_UNAVAILABLE = {None, "", "unknown", "unavailable", "none", "null"}


@dataclass(frozen=True)
class Freshness:
    """Vieno lauko kokybės įvertis."""

    quality: str
    usable: bool
    age_seconds: Optional[float]
    reason: str


def as_utc(value: Any) -> Optional[datetime]:
    """Normalizuoja HA ISO datą arba Unix sekundes (SolisCloud) į UTC."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float, str)):
        try:
            stamp = float(value)
        except (TypeError, ValueError):
            stamp = None
        if stamp is not None:
            try:
                return datetime.fromtimestamp(stamp, timezone.utc) if isfinite(stamp) else None
            except (ValueError, OverflowError, OSError):
                return None
        if not isinstance(value, str):
            return None
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        # HA paprastai siunčia offsetą; senas adapteris gali grąžinti naive
        # reikšmę. Jai suteikiame UTC, kad negautume TypeError ir nesukurtume
        # klaidingo "šviežio" sprendimo.
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def sample_timestamp(record, heartbeat=None, *, require_heartbeat=False):
    """Debesijos matavimo laikas turi pirmenybę prieš HA įrašo laiką."""
    heartbeat_at = as_utc((heartbeat or {}).get("state"))
    if require_heartbeat or heartbeat_at is not None:
        return heartbeat_at
    return as_utc(
        record.get("last_reported") or record.get("last_updated")
        or record.get("last_changed")
    )


def _now_utc(now: Optional[datetime]) -> datetime:
    parsed = as_utc(now)
    return parsed if parsed is not None else datetime.now(timezone.utc)


def age_seconds(reported_at: Any, now: Optional[datetime] = None) -> Optional[float]:
    """Grąžina amžių sekundėmis; neparsiduodanti data grąžina None."""
    reported = as_utc(reported_at)
    if reported is None:
        return None
    delta = (_now_utc(now) - reported).total_seconds()
    return max(0.0, delta)


def assess_freshness(
    value: Any,
    reported_at: Any,
    *,
    max_age_seconds: float,
    now: Optional[datetime] = None,
    allow_unknown_timestamp: bool = True,
    max_future_skew_seconds: float = 30.0,
) -> Freshness:
    """Įvertina reikšmę, jos datą ir leistiną amžių.

    "unknown" leidžiama tik suderinamumui su adapteriais, kurie nepateikia
    last_reported. Tokia reikšmė nėra laikoma įrodyta šviežia; ją vis tiek
    galima naudoti, kol adapteris pradės pateikti datą. Neteisingi arba seni
    kritiniai laukai visada tampa netinkami planui.
    """
    if value is None or (
        isinstance(value, str) and value.strip().lower() in _UNAVAILABLE
    ):
        return Freshness("missing", False, None, "reikšmė nepasiekiama")
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return Freshness("invalid", False, None, "reikšmė nėra skaičius")
    if not isfinite(numeric):
        return Freshness("invalid", False, None, "reikšmė nėra baigtinė")

    reported = as_utc(reported_at)
    if reported is None:
        return Freshness(
            "unknown",
            bool(allow_unknown_timestamp),
            None,
            "adapteris nepateikė last_reported",
        )

    current = _now_utc(now)
    raw_age = (current - reported).total_seconds()
    if raw_age < -abs(float(max_future_skew_seconds)):
        return Freshness(
            "future",
            False,
            raw_age,
            "last_reported yra per daug ateityje",
        )
    age = max(0.0, raw_age)
    max_age = max(0.0, float(max_age_seconds))
    if age > max_age:
        return Freshness(
            "stale",
            False,
            age,
            f"telemetrija {age:.0f}s sena, riba {max_age:.0f}s",
        )
    return Freshness("fresh", True, age, f"telemetrija šviežia ({age:.0f}s)")


def numeric_value(
    value: Any,
    reported_at: Any,
    *,
    max_age_seconds: float,
    now: Optional[datetime] = None,
    allow_unknown_timestamp: bool = True,
) -> tuple[Optional[float], Freshness]:
    """Grąžina skaičių tik jei jis tinkamas saugiam naudojimui."""
    freshness = assess_freshness(
        value,
        reported_at,
        max_age_seconds=max_age_seconds,
        now=now,
        allow_unknown_timestamp=allow_unknown_timestamp,
    )
    if not freshness.usable:
        return None, freshness
    try:
        return float(value), freshness
    except (TypeError, ValueError):
        return None, Freshness("invalid", False, None, "nepavyko konvertuoti")


def make_sample(
    key: str,
    value: Any,
    *,
    reported_at: Any,
    received_at: Any = None,
    max_age_seconds: float,
    source: str = "ha",
    allow_unknown_timestamp: bool = True,
) -> TelemetrySample:
    """Sukuria bendrą kontrakto objektą iš HA arba emuliatoriaus lauko."""
    received = as_utc(received_at) or datetime.now(timezone.utc)
    parsed_reported = as_utc(reported_at)
    normalized, freshness = numeric_value(
        value,
        parsed_reported,
        max_age_seconds=max_age_seconds,
        now=received,
        allow_unknown_timestamp=allow_unknown_timestamp,
    )
    final_value = normalized if normalized is not None else value
    return TelemetrySample(
        key=key,
        value=final_value,
        reported_at=parsed_reported,
        received_at=received,
        quality=freshness.quality,
        age_seconds=freshness.age_seconds,
        source=source,
    )
