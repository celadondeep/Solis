from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from energy_system.dashboard_template import PLANTS, build_dashboard, render_all


EXPECTED_VIEWS = [
    ("Energija", "energija"),
    ("Analizė", "analize"),
    ("Atsipirkimas", "atsipirkimas"),
    ("Vartojimas", "vartojimas"),
]


def _walk(value):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _all_entity_ids(value):
    found = []
    for item in _walk(value):
        if isinstance(item, dict) and isinstance(item.get("entity"), str):
            found.append(item["entity"])
        elif isinstance(item, str) and "." in item and not item.startswith("mdi:"):
            if item.startswith(("sensor.", "input_", "select.", "switch.", "binary_sensor.", "eso:")):
                found.append(item)
    return found


def _title(item):
    if not isinstance(item, dict):
        return None
    if isinstance(item.get("title"), str):
        return item["title"]
    header = item.get("header")
    return header.get("title") if isinstance(header, dict) else None


def _card(dashboard, title):
    for item in _walk(dashboard):
        if _title(item) == title:
            return item
    raise AssertionError(f"Nerasta kortelė: {title}")


class DashboardTemplateTests(unittest.TestCase):
    def test_both_dashboards_have_four_selected_views(self):
        for plant in PLANTS.values():
            dashboard = build_dashboard(plant)
            self.assertEqual(
                [(view["title"], view["path"]) for view in dashboard["views"]],
                EXPECTED_VIEWS,
            )

    def test_both_profiles_use_same_display_toggles(self):
        for plant in PLANTS.values():
            self.assertEqual(
                set(plant["toggles"]),
                {"forecast", "diagnostics", "control", "graphs", "boiler"},
            )

    def test_marked_out_metrics_are_removed_only_from_glance_cards(self):
        for plant in PLANTS.values():
            dashboard = build_dashboard(plant)
            today = _card(dashboard, "Šiandienos rezultatas")
            self.assertNotIn("Savarankiškumas", [row.get("name") for row in today["entities"]])

            metric_cards = [item for item in _walk(dashboard)
                            if isinstance(item, dict) and item.get("template") == "se_metric"]
            self.assertNotIn("sensor.pv_pagaminta_viso", [card.get("entity") for card in metric_cards])

            payback = _card(dashboard, "Modeliuojamas atsipirkimas · abi elektrinės")
            self.assertIn("sensor.pv_pagaminta_viso", [series["entity"] for series in payback["series"]])

    def test_selected_cards_exist(self):
        required = {
            "Šiandienos rezultatas",
            "Vartojimo modelis (AppDaemon)",
            "Fazės L1 / L2 / L3",
            "Baterijos sveikata",
            "Inverteris",
            "Efektyvumas ir nuostoliai (nuo įrengimo)",
            "Dienos balansas (14 d. · kWh)",
            "Inverterio temperatūra (48 val.)",
            "Baterijos round-trip efektyvumas (30 d.)",
            "Prognozės tikslumas (30 d.)",
            "Įvestis (pildyk ranka)",
            "Modeliuojamas atsipirkimas · abi elektrinės",
            "Vartojimas pagal valandą (kWh)",
            "Vartojimas pagal savaitės dieną (kWh)",
            "ESO tinklo srautas (valandinis)",
            "Tinklo ir inverterio kokybė",
            "PCC fazinė įtampa (7 d.)",
            "PCC aktyvioji ir reaktyvioji galia (7 d.)",
            "Fazių išsiskyrimas ir įtampos skirtumas (7 d.)",
        }
        for plant in PLANTS.values():
            dashboard = build_dashboard(plant)
            # Nested chart headers also have titles, but are not cards.
            for item in _walk(dashboard["views"]):
                if isinstance(item, dict) and "cards" in item:
                    self.assertTrue(all(isinstance(card, dict) and "type" in card
                                        for card in item["cards"]))
            titles = {_title(item) for item in _walk(dashboard)}
            self.assertTrue(required <= titles)
            self.assertTrue(any(
                isinstance(title, str) and title.startswith("Kabelio nuostoliai")
                for title in titles
            ))

    def test_plant_specific_views_do_not_cross_raw_integrations(self):
        home = build_dashboard(PLANTS["home"])
        eimo = build_dashboard(PLANTS["eimo"])
        # Atsipirkimas yra sąmoningai bendra finansinė periferija; ją atskiriame.
        home_specific = {"views": [home["views"][0], home["views"][1], home["views"][3]]}
        eimo_specific = {"views": [eimo["views"][0], eimo["views"][1], eimo["views"][3]]}
        home_entities = _all_entity_ids(home_specific)
        eimo_entities = _all_entity_ids(eimo_specific)
        self.assertFalse(any("inverter_1033300254190112" in item for item in home_entities))
        self.assertFalse(any("solis_s6_eh3p" in item for item in eimo_entities))
        self.assertIn("sensor.solis_plan", home_entities)
        self.assertIn("sensor.eimo_plan", eimo_entities)

    def test_preview_generation_never_needs_live_dashboard_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            changed = render_all(output_dir)
            self.assertEqual(changed, {"home": True, "eimo": True})
            for plant in PLANTS.values():
                path = output_dir / plant["file"]
                text = path.read_text(encoding="utf-8")
                self.assertTrue(text.startswith("# GENERATED"))
                parsed = json.loads(text.split("\n", 1)[1])
                self.assertEqual(parsed["title"], plant["title"])
                self.assertEqual(len(parsed["views"]), 4)


if __name__ == "__main__":
    unittest.main()
