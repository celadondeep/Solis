"""Bendri, nuo Home Assistant nepriklausomi energijos valdymo kontraktai.

Šiame faile nėra HA I/O. Kontraktai atskiria telemetrijos skaitymą,
sprendimo priėmimą ir fizinių komandų vykdymą, todėl vienos elektrinės
modulis negali netyčia valdyti kitos.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Optional, Tuple


@dataclass(frozen=True)
class TelemetrySample:
    """Vienas normalizuotas telemetrijos laukas."""

    key: str
    value: Any
    reported_at: Optional[datetime]
    received_at: datetime
    quality: str = "unknown"
    age_seconds: Optional[float] = None
    source: str = "ha"

    @property
    def usable(self) -> bool:
        return self.quality in {"fresh", "unknown"} and self.value is not None


@dataclass(frozen=True)
class TelemetrySnapshot:
    """Vienos elektrinės telemetrija vienu planavimo momentu."""

    site: str
    at: datetime
    samples: Mapping[str, TelemetrySample] = field(default_factory=dict)

    def sample(self, key: str) -> Optional[TelemetrySample]:
        return self.samples.get(key)

    def value(self, key: str, default: Any = None) -> Any:
        sample = self.sample(key)
        return default if sample is None or not sample.usable else sample.value

    def is_usable(self, key: str) -> bool:
        sample = self.sample(key)
        return bool(sample and sample.usable)

    def quality(self, key: str) -> str:
        sample = self.sample(key)
        return sample.quality if sample else "missing"


@dataclass(frozen=True)
class ModuleProposal:
    """Periferinio modulio pasiūlymas Valdymo koordinatoriui.

    Modulis pasiūlo tik ketinimą; jis niekada tiesiogiai nerašo į HA.
    """

    site: str
    module: str
    priority: int
    kind: str
    reason: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    created_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None

    @property
    def stable_key(self) -> tuple[str, str, str]:
        return (self.site, self.module, self.kind)


@dataclass(frozen=True)
class CoordinatorDecision:
    """Vieno ciklo galutinis pasirinkimas ir atmesti pasiūlymai."""

    site: str
    selected: Optional[ModuleProposal]
    rejected: Tuple[ModuleProposal, ...] = ()
    status: str = "hold"
    reason: str = ""


@dataclass(frozen=True)
class CommandIntent:
    """Idempotentinė komanda vienam konkrečiam vykdiklio išėjimui."""

    site: str
    actuator: str
    value: Any
    reason: str
    idempotency_key: str
    created_at: Optional[datetime] = None


@dataclass(frozen=True)
class ExecutionResult:
    """Vykdymo adapterio rezultatas; tai nėra pats HA serviso kvietimas."""

    site: str
    status: str
    commands: Tuple[CommandIntent, ...] = ()
    suppressed: Tuple[CommandIntent, ...] = ()
    errors: Tuple[str, ...] = ()
