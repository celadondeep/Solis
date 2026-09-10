"""Bendri emuliatoriaus saugos invariantai."""

from __future__ import annotations

from typing import Iterable

from .model import CycleResult


def assert_no_physical_commands_when_paused(results: Iterable[CycleResult]) -> None:
    for result in results:
        if not result.plan.actionable:
            if result.commands.commands:
                raise AssertionError(
                    f"{result.at}: paused planas sukūrė fizines komandas"
                )


def assert_executor_converges(results: Iterable[CycleResult]) -> None:
    if not results:
        raise AssertionError("Nėra emuliatoriaus ciklų")
    last = list(results)[-1]
    if last.plan.actionable and last.execution_after.state not in {"ok", "applying"}:
        raise AssertionError(
            f"{last.at}: vykdyklė nesusiderino: {last.execution_after.mismatches}"
        )


def assert_single_writer(results: Iterable[CycleResult]) -> None:
    for result in results:
        actuators = [command.actuator for command in result.commands.commands]
        if len(actuators) != len(set(actuators)):
            raise AssertionError(
                f"{result.at}: viename cikle yra dublikatinis aktuatorius"
            )


def assert_site_isolation(results_by_site) -> None:
    sites = set()
    for site, results in results_by_site.items():
        if site in sites:
            raise AssertionError(f"pasikartojantis site raktas: {site}")
        sites.add(site)
        for result in results:
            if result.coordinator.site != site:
                raise AssertionError(
                    f"{site}: koordinatoriaus site {result.coordinator.site}"
                )
