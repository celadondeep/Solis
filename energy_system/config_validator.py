"""Perplantinių profilių statinė validacija prieš gyvą paleidimą."""

from __future__ import annotations

from collections import Counter
from typing import Iterable, Mapping


_REQUIRED_PROFILE_KEYS = {
    "KEY",
    "SITE_LABEL",
    "SENSOR",
    "ACTUATOR",
    "OUTPUT",
    "EXECUTOR_ENTITY",
    "PLAN_HARD_FLOOR",
    "NIGHT_REST_SOC",
    "TELEMETRY_MAX_AGE_SECONDS",
}
_REQUIRED_ACTUATOR_KEYS = {
    "mode",
    "mode_by_plan",
    "slot",
    "slot_cutoff",
    "exclusive_off",
    "power",
}
_FORBIDDEN_ENTITY_TEXT = ("over-discharge", "overdischarge")


def _entity_values(profile: Mapping) -> list[str]:
    values = []
    for section in ("SENSOR", "ACTUATOR", "OUTPUT"):
        raw = profile.get(section, {})
        if isinstance(raw, Mapping):
            for value in raw.values():
                if isinstance(value, str):
                    values.append(value)
                elif isinstance(value, (tuple, list)):
                    values.extend(item for item in value if isinstance(item, str))
    values.append(str(profile.get("EXECUTOR_ENTITY", "")))
    return [value for value in values if value]


def validate_profile(profile: Mapping) -> tuple[str, ...]:
    issues = []
    missing = sorted(_REQUIRED_PROFILE_KEYS - set(profile))
    issues.extend(f"missing_profile:{key}" for key in missing)
    missing_actuator = sorted(
        _REQUIRED_ACTUATOR_KEYS - set(profile.get("ACTUATOR", {}))
    )
    issues.extend(f"missing_actuator:{key}" for key in missing_actuator)
    if profile.get("PLAN_HARD_FLOOR", 0) > profile.get("NIGHT_REST_SOC", 0):
        issues.append("hard_floor_above_night_rest")
    if profile.get("INVERTER_CONTROL_AVAILABLE") is None:
        issues.append("inverter_capability_unspecified")
    enabled = profile.get("MODULES_ENABLED", ())
    unknown_modules = sorted(
        name for name in enabled
        if name not in {
            "telemetry", "forecast", "consumption", "eso",
            "battery_health", "safety", "planner",
        }
    )
    issues.extend(f"unknown_module:{name}" for name in unknown_modules)
    entity_values = _entity_values(profile)
    for forbidden in _FORBIDDEN_ENTITY_TEXT:
        if any(forbidden in value.lower() for value in entity_values):
            issues.append(f"forbidden_entity_text:{forbidden}")
    # Sensor + to paties fizinio aktuatoriaus readback yra leidžiamas;
    # dublikatus tikriname atskiroje vardų skiltyje.
    for section in ("SENSOR", "ACTUATOR", "OUTPUT"):
        raw = profile.get(section, {})
        values = []
        if isinstance(raw, Mapping):
            for value in raw.values():
                if isinstance(value, str):
                    values.append(value)
                elif isinstance(value, (tuple, list)):
                    values.extend(
                        item for item in value if isinstance(item, str)
                    )
        duplicates = [
            value for value, count in Counter(values).items() if count > 1
        ]
        issues.extend(
            f"duplicate_{section.lower()}:{value}"
            for value in sorted(duplicates)
        )
    return tuple(issues)


def validate_profiles(profiles: Iterable[Mapping]) -> tuple[str, ...]:
    """Tikrina ir vidinę profilio struktūrą, ir elektrinių izoliaciją."""
    profiles = tuple(profiles)
    issues = []
    keys = [str(profile.get("KEY", "")) for profile in profiles]
    for key in keys:
        if not key:
            issues.append("empty_site_key")
    if len(keys) != len(set(keys)):
        issues.append("duplicate_site_key")
    for profile in profiles:
        issues.extend(
            f"{profile.get('KEY', 'unknown')}:{issue}"
            for issue in validate_profile(profile)
        )
    # Read-only telemetry can be shared. Writable ownership cannot.
    seen = {}
    for profile in profiles:
        site = profile.get("KEY", "unknown")
        owned = {k: profile.get(k, {}) for k in ("ACTUATOR", "OUTPUT")}
        owned["EXECUTOR_ENTITY"] = profile.get("EXECUTOR_ENTITY", "")
        for entity in _entity_values(owned):
            owner = seen.get(entity)
            if owner and owner != site:
                issues.append(f"cross_site_entity:{entity}:{owner}!={site}")
            else:
                seen[entity] = site
    return tuple(dict.fromkeys(issues))
