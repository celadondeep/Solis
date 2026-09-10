"""Idempotentinė vieno rašytojo komandų eilė.

Vykdymo adapteris gauna tik šios eilės rezultatus. Dublikatai ir dvi
prieštaraujančios komandos tam pačiam aktuatoriui viename cikle atmetamos.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from energy_system.contracts import CommandIntent, ExecutionResult


@dataclass(frozen=True)
class QueueDecision:
    accepted: tuple[CommandIntent, ...]
    suppressed: tuple[CommandIntent, ...]
    errors: tuple[str, ...]


class SingleWriterQueue:
    """Vienas komandų savininkas vienai elektrinei."""

    def __init__(self, site: str):
        if not site:
            raise ValueError("site privalomas")
        self.site = str(site)

    def prepare(self, commands: Iterable[CommandIntent]) -> QueueDecision:
        accepted = []
        suppressed = []
        errors = []
        by_actuator = {}
        by_key = {}
        for command in commands:
            if command.site != self.site:
                errors.append(f"cross_site:{command.site}")
                continue
            if command.idempotency_key in by_key:
                suppressed.append(command)
                continue
            previous = by_actuator.get(command.actuator)
            if previous is not None and previous.value != command.value:
                errors.append(
                    f"conflict:{command.actuator}:{previous.value!r}!={command.value!r}"
                )
                suppressed.append(command)
                continue
            by_actuator[command.actuator] = command
            by_key[command.idempotency_key] = command
            accepted.append(command)
        return QueueDecision(tuple(accepted), tuple(suppressed), tuple(errors))

    def result(self, commands: Iterable[CommandIntent]) -> ExecutionResult:
        decision = self.prepare(commands)
        status = "error" if decision.errors else (
            "suppressed" if decision.suppressed and not decision.accepted else "queued"
        )
        return ExecutionResult(
            site=self.site,
            status=status,
            commands=decision.accepted,
            suppressed=decision.suppressed,
            errors=decision.errors,
        )
