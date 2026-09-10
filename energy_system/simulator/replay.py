"""Istorinių HA eilučių pavertimas į deterministinius scenarijus."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from energy_system.telemetry import as_utc
from .model import ScenarioEvent


def events_from_rows(rows: Iterable[Mapping[str, Any]]) -> list[ScenarioEvent]:
    events = []
    for index, row in enumerate(rows):
        timestamp = row.get("at", row.get("last_changed", row.get("timestamp")))
        at = as_utc(timestamp)
        if at is None:
            raise ValueError(f"Scenarijaus eilutė {index} neturi tinkamos datos")
        patch = dict(row)
        for key in ("at", "last_changed", "timestamp", "label"):
            patch.pop(key, None)
        events.append(
            ScenarioEvent(at=at, patch=patch, label=str(row.get("label", "")))
        )
    return events


def load_rows(path: str | Path) -> list[dict[str, Any]]:
    with Path(path).open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if isinstance(data, dict):
        data = data.get("rows", [])
    if not isinstance(data, list):
        raise ValueError("Istorinis scenarijus turi būti eilučių sąrašas")
    return [dict(row) for row in data]
