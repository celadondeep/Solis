"""Valdymo koordinatorius: vienintelis sprendimo taškas vienai elektrinei.

Periferijos gali teikti pasiūlymus, bet nė viena periferija negali pati
valdyti inverterio. Koordinatorius filtruoja neteisingą plant raktą, galiojimo
laiką ir deterministiškai pasirenka vieną laimėtoją.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Optional

from energy_system.contracts import (
    CoordinatorDecision,
    ModuleProposal,
)


# Didesnis skaičius laimi. Saugos režimai visada aukščiau optimizavimo.
PRIORITY = {
    "safety": 1000,
    "telemetry_hold": 950,
    "storm": 900,
    "manual": 850,
    "reserve": 800,
    "recovery": 700,
    "planner": 500,
    "forecast": 300,
    "consumption": 200,
    "display": 100,
}


def priority_for(kind: str, fallback: int = 0) -> int:
    return PRIORITY.get(str(kind), int(fallback))


def _utc(value: Optional[datetime]) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class ValdymoKoordinatorius:
    """Vienos elektrinės pasiūlymų surinkimo ir sprendimo sluoksnis."""

    def __init__(self, site: str):
        if not site or not str(site).strip():
            raise ValueError("site privalomas")
        self.site = str(site)
        self.last_decision: Optional[CoordinatorDecision] = None

    def choose(
        self,
        proposals: Iterable[ModuleProposal],
        *,
        now: Optional[datetime] = None,
    ) -> CoordinatorDecision:
        current = _utc(now)
        accepted = []
        rejected = []
        for proposal in proposals:
            if proposal.site != self.site:
                rejected.append(proposal)
                continue
            if proposal.expires_at is not None and _utc(proposal.expires_at) < current:
                rejected.append(proposal)
                continue
            accepted.append(proposal)

        if not accepted:
            decision = CoordinatorDecision(
                site=self.site,
                selected=None,
                rejected=tuple(rejected),
                status="hold",
                reason="Nėra galiojančių šios elektrinės pasiūlymų",
            )
            self.last_decision = decision
            return decision

        # Stabilus prioritetų tvarkymas: vienodas prioritetas nesukuria
        # atsitiktinio konfliktų sprendimo pagal įterpimo eilę.
        winner = sorted(
            accepted,
            key=lambda item: (
                -int(item.priority),
                str(item.module),
                str(item.kind),
                item.reason,
            ),
        )[0]
        rejected.extend(item for item in accepted if item is not winner)
        status = "selected" if winner.payload else "hold"
        reason = winner.reason if winner.payload else "Pasiūlymas neturi sprendimo duomenų"
        decision = CoordinatorDecision(
            site=self.site,
            selected=winner if winner.payload else None,
            rejected=tuple(rejected),
            status=status,
            reason=reason,
        )
        self.last_decision = decision
        return decision
