"""Atnaujina tik dashboardų peržiūros kopijas, kol nepatvirtintas dizainas."""

from __future__ import annotations

import appdaemon.plugins.hass.hassapi as hass

from energy_system.dashboard_template import render_preview


class DashboardTemplateSync(hass.Hass):
    """Tik failų peržiūros periferija; gyvų dashboardų ir valdymo entity neliečia."""

    def initialize(self):
        self.log("DashboardTemplateSync paleistas saugiu peržiūros režimu")
        self.run_in(self.sync, 10)
        self.run_every(self.sync, "now+120", 300)

    def sync(self, kwargs=None):
        try:
            changed = render_preview()
            if any(changed.values()):
                self.log(f"Dashboard peržiūros šablonas atnaujintas: {changed}")
        except Exception as exc:
            self.log(f"Dashboard peržiūros šablono klaida: {exc}", level="ERROR")
