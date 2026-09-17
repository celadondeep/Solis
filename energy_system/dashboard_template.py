"""Parametrinis vienos elektrinės Home Assistant dashboardo šablonas.

Keturi tie patys puslapiai generuojami kiekvienai elektrinei. Bendras kodas
aprašo išdėstymą ir korteles, o PLANTS žemėlapis pateikia tik konkrečios
elektrinės entity ID bei fizines galimybes. Trūkstami fiziniai duomenys nėra
spėjami: vietoje klaidingo grafiko pateikiamas aiškus paaiškinimas.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Iterable


from energy_system.site_registry import APPS_DIR, dashboard_profiles

BASE = APPS_DIR.parent.parent
LIVE_DIR = BASE / "lovelace"
PREVIEW_DIR = BASE / ".codex-preview" / "energy_dashboards"
PLANTS = dashboard_profiles()


HOURLY_PROFILE_JS = """const arr = entity.attributes.hourly_kwh || [];
const pts = arr.map((v, i) => {
  const d = new Date(); d.setHours(i, 0, 0, 0);
  return [d.getTime(), v];
});
if (arr.length) {
  const d = new Date(); d.setHours(24, 0, 0, 0);
  pts.push([d.getTime(), arr[0]]);
}
return pts;"""


WEEKDAY_PROFILE_JS = """const now = new Date();
const dow = (now.getDay() + 6) % 7;
const monday = new Date(now); monday.setDate(now.getDate() - dow - 7); monday.setHours(0, 0, 0, 0);
return (entity.attributes.weekday_kwh || []).map((v, i) => {
  const d = new Date(monday); d.setDate(monday.getDate() + i); d.setHours(0, 0, 0, 0);
  return [d.getTime(), v];
});"""


WEEKDAY_AVG_JS = """const arr = entity.attributes.weekday_kwh || [];
const avg = entity.attributes.daily_avg ?? null;
const now = new Date();
const dow = (now.getDay() + 6) % 7;
const monday = new Date(now); monday.setDate(now.getDate() - dow - 7); monday.setHours(0, 0, 0, 0);
return arr.map((v, i) => {
  const d = new Date(monday); d.setDate(monday.getDate() + i); d.setHours(0, 0, 0, 0);
  return [d.getTime(), avg];
});"""


def payback_generator(finance, savings=False):
    """Same model for any portfolio membership; all dates and shares are data."""
    return "const cfg = " + json.dumps(finance, ensure_ascii=False) + ";\n" + r"""
const day = value => { const [y,m,d]=value.split('-').map(Number); return new Date(y,m-1,d); };
const numeric = (value,fallback) => value!==null && value!==undefined && String(value).trim()!=='' && Number.isFinite(Number(value)) ? Number(value) : fallback;
const a=entity.attributes, W=cfg.monthly_weights, defaults=cfg.defaults;
const annual=numeric(a.metine_gamyba,defaults.annual), price=numeric(a.kwh_verte,defaults.price);
const ratio=numeric(a.naudingumo_koef,defaults.ratio), fee=numeric(a.men_mokestis,defaults.fee);
let d=day(cfg.start), cum=0; const end=day(cfg.end), pts=[];
while(d<=end) {
  pts.push([d.getTime(),cum]);
  const active=cfg.members.filter(m=>d>=day(m.start));
  const share=active.reduce((sum,m)=>sum+m.production_share,0);
  const fees=active.reduce((sum,m)=>sum+m.fee_share,0);
  cum += annual*W[d.getMonth()]*share*MULTIPLIER - FEES;
  d=new Date(d.getFullYear(),d.getMonth()+1,1);
}
const now=Date.now(); let modelNow=null;
for(let i=1;i<pts.length;i++) {
  if(pts[i][0]>=now) { const [t0,v0]=pts[i-1], [t1,v1]=pts[i]; modelNow=v0+(v1-v0)*(now-t0)/(t1-t0); break; }
}
const actual=Number(entity.state), k=modelNow>0 && actual>0 ? actual/modelNow : 1;
return pts.map(([t,v])=>[t,Math.round(v*k)]);
""".replace("MULTIPLIER", "price*ratio" if savings else "1").replace("FEES", "fee*fees" if savings else "0")


def _row(entity: str | None, name: str | None = None, icon: str | None = None) -> Any:
    if not entity:
        return None
    if not name and not icon:
        return entity
    row: Dict[str, Any] = {"entity": entity}
    if name:
        row["name"] = name
    if icon:
        row["icon"] = icon
    return row


def _rows(*items: Any) -> list[Any]:
    return [item for item in items if item is not None]


def _named_rows(items: Iterable[tuple[str, str]]) -> list[Dict[str, str]]:
    return [{"entity": entity, "name": name} for entity, name in items]


def _conditional(toggle: str, card: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "type": "conditional",
        "conditions": [{"condition": "state", "entity": toggle, "state": "on"}],
        "card": card,
    }


def _glance(title: str, entities: list[Any], columns: int | None = None) -> Dict[str, Any]:
    card: Dict[str, Any] = {
        "type": "glance",
        "title": title,
        "entities": entities,
        "show_state": True,
    }
    if columns:
        card["columns"] = columns
    return card


def _notice(title: str, content: str) -> Dict[str, Any]:
    return {
        "type": "markdown",
        "title": title,
        "content": f"ℹ️ {content}",
    }


def _plant_energy(p: Dict[str, Any]) -> Dict[str, Any]:
    t = p["toggles"]
    status_rows = _rows(
        _row(p["core"], "Branduolio būsena"),
        _row(p["coordinator"], "Valdymo koordinatorius"),
        _row(p["telemetry"], "Telemetrijos sveikata"),
        _row(p["planner"], "Planner sveikata"),
        _row(p["executor"], "Executor sveikata"),
        _row(p["shadow"], "Šešėlinio plano patikra"),
        _row(p["plan"], "Atominis planas"),
    )
    metrics = _rows(
        _row(p["soc"], "Baterijos SOC"),
        _row(p["pv"], "PV galia"),
        _row(p["load"], "Namų apkrova"),
        _row(p["grid"], "Tinklo galia"),
        _row(p["battery"], "Baterijos galia"),
        _row(p["target"], "Plano SOC tikslas"),
    )
    toggles = [
        _row(t["forecast"], "Rodyti prognozes"),
        _row(t["diagnostics"], "Rodyti diagnostiką"),
        _row(t["control"], "Rodyti valdymą"),
        _row(t["graphs"], "Rodyti grafikus"),
        _row(t["boiler"], "Rodyti boilerį"),
    ]
    plan_rows = _rows(
        _row(p["plan_mode"], "Plano režimas"),
        _row(p["decision"], "Sprendimo paaiškinimas"),
        _row(p["balance"], "Energijos balansas"),
        _row(p["surplus"], "Saulės perteklius dabar"),
        _row(p["room"], "Vietos trūkumas saulei"),
        _row(p["shadow_plan"], "Šešėlinis kandidatas"),
        _row(p["shadow_cmd"], "Šešėlinės komandos"),
    )
    forecast = {
        "type": "entities",
        "title": "Prognozė ir mokymasis",
        "show_header_toggle": False,
        "entities": _rows(
            *[_row(entity) for entity in p["forecast_entities"]],
            _row(p["cons_remaining"], "Likęs suvartojimas šiandien"),
            _row(p["cons_tomorrow"], "Rytojaus suvartojimo prognozė"),
            _row(p["correction"], "Solcast korekcija"),
        ),
    }
    control = {
        "type": "entities",
        "title": "Šios elektrinės valdymas",
        "show_header_toggle": False,
        "state_color": True,
        "entities": _rows(
            _row(p["storm"], "Audros / ESO režimas"),
            _row(p["manual"], "Rankinis iškrovimo valdymas"),
            _row(p["manual_soc"], "Rankinis SOC bypass"),
            _row(p["plan"], "Atominis planas"),
            _row(p["plan_mode"], "Plano režimas"),
            _row(p["target"], "Tikslinis SOC"),
        ),
    }
    diagnostics = {
        "type": "entities",
        "title": "Diagnostika",
        "show_header_toggle": False,
        "entities": [
            p["core"], p["telemetry"], p["planner"], p["executor"],
            p["shadow"], p["shadow_plan"], p["shadow_cmd"],
        ],
    }
    return {
        "title": "Energija",
        "path": "energija",
        "icon": "mdi:lightning-bolt",
        "type": "sections",
        "max_columns": 2,
        "sections": [
            {"type": "grid", "cards": [
                {"type": "heading", "heading": f"{p['site_label']}: branduolys"},
                {"type": "entities", "entities": status_rows, "show_header_toggle": False},
            ]},
            {"type": "grid", "cards": [
                {"type": "heading", "heading": "Gyva telemetrija"},
                _glance("Dabartinė būsena", metrics, 3),
            ]},
            {"type": "grid", "cards": [
                {"type": "heading", "heading": "Rodymo parinktys"},
                {"type": "entities", "entities": toggles, "show_header_toggle": False},
                _conditional(t["forecast"], forecast),
            ]},
            {"type": "grid", "cards": [
                {"type": "heading", "heading": "Plano faktai"},
                {"type": "entities", "entities": plan_rows, "show_header_toggle": False},
                _conditional(t["diagnostics"], diagnostics),
                _conditional(t["control"], control),
                _conditional(t["boiler"], {
                    "type": "entities",
                    "title": "Boilerio modulis",
                    "show_header_toggle": False,
                    "entities": [p["boiler"]],
                }),
            ]},
        ],
    }


def _phase_card(a: Dict[str, Any]) -> Dict[str, Any]:
    rows: list[Any] = []
    for i, entity in enumerate(a["phases"]["voltage"], 1):
        rows.append(_row(entity, f"Įtampa L{i}"))
    for i, entity in enumerate(a["phases"]["grid"], 1):
        rows.append(_row(entity, f"Tinklas L{i}", "mdi:transmission-tower"))
    for i, entity in enumerate(a["phases"]["inverter"], 1):
        rows.append(_row(entity, f"Inverteris L{i}"))
    return _glance("Fazės L1 / L2 / L3", _rows(*rows), 3)


def _daily_balance_card(a: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "type": "custom:apexcharts-card",
        "header": {"show": True, "title": "Dienos balansas (14 d. · kWh)"},
        "graph_span": "14d",
        "update_interval": "5min",
        "span": {"end": "day"},
        "apex_config": {"chart": {"height": 280, "animations": {"enabled": False}}},
        "yaxis": [{"min": 0, "decimals": 0}],
        "series": [
            {
                "entity": entity,
                "name": name,
                "type": "column",
                "color": color,
                "group_by": {"func": "max", "duration": "1d"},
            }
            for entity, name, color in a["daily_balance"]
        ],
    }


def _temperature_card(entity: str) -> Dict[str, Any]:
    return {
        "type": "custom:apexcharts-card",
        "header": {"show": True, "title": "Inverterio temperatūra (48 val.)"},
        "graph_span": "48h",
        "update_interval": "5min",
        "apex_config": {"chart": {"height": 220, "animations": {"enabled": False}}},
        "yaxis": [{"min": "~20", "decimals": 0}],
        "series": [{
            "entity": entity,
            "name": "Temperatūra °C",
            "color": "#ff7043",
            "stroke_width": 2,
            "group_by": {"func": "avg", "duration": "15min"},
        }],
    }


def _battery_efficiency_card(entity: str) -> Dict[str, Any]:
    return {
        "type": "custom:apexcharts-card",
        "header": {"show": True, "title": "Baterijos round-trip efektyvumas (30 d.)"},
        "graph_span": "30d",
        "update_interval": "10min",
        "span": {"end": "day"},
        "apex_config": {"chart": {"height": 220, "animations": {"enabled": False}}},
        "yaxis": [{"min": 88, "max": 100, "decimals": 1}],
        "series": [{
            "entity": entity,
            "name": "Round-trip %",
            "color": "#7e57c2",
            "stroke_width": 2,
            "group_by": {"func": "last", "duration": "1d"},
        }],
    }


def _forecast_accuracy_card(entity: str) -> Dict[str, Any]:
    return {
        "type": "custom:apexcharts-card",
        "header": {"show": True, "title": "Prognozės tikslumas (30 d.)"},
        "graph_span": "30d",
        "update_interval": "10min",
        "span": {"end": "day"},
        "apex_config": {"chart": {"animations": {"enabled": False}}},
        "yaxis": [{"min": 0, "decimals": 0}],
        "series": [{
            "entity": entity,
            "name": "Tikslumas %",
            "type": "column",
            "group_by": {"func": "last", "duration": "1d"},
        }],
    }


def _power_quality_glance(q: Dict[str, str]) -> Dict[str, Any]:
    return _glance("Tinklo ir inverterio kokybė", _rows(
        _row(q["state"], "Būsena", "mdi:shield-check"),
        _row(q["voltage_min"], "PCC įtampa min", "mdi:arrow-collapse-down"),
        _row(q["voltage_max"], "PCC įtampa max", "mdi:arrow-collapse-up"),
        _row(q["spread"], "Fazių dydžių išsiskyrimas"),
        _row(q["active"], "PCC P"),
        _row(q["reactive"], "PCC Q"),
        _row(q["pf"], "Galios faktorius"),
        _row(q["frequency"], "Dažnis"),
        _row(q["rise"], "Inverterio–PCC skirtumas"),
        _row(q["active_imbalance"], "PCC P disbalansas"),
        _row(q["current_imbalance"], "Inverterio srovių netolygumas"),
    ), 3)


def _power_quality_voltage_card(q: Dict[str, str]) -> Dict[str, Any]:
    return {
        "type": "custom:apexcharts-card",
        "header": {"show": True, "title": "PCC fazinė įtampa (7 d.)"},
        "graph_span": "7d",
        "update_interval": "5min",
        "apex_config": {"chart": {"height": 250, "animations": {"enabled": False}}},
        "yaxis": [{"min": 190, "max": 260, "decimals": 0}],
        "series": [
            {"entity": q["voltage_min"], "name": "Min", "color": "#42a5f5", "stroke_width": 2},
            {"entity": q["voltage_max"], "name": "Max", "color": "#ef5350", "stroke_width": 2},
        ],
    }


def _power_quality_power_card(q: Dict[str, str]) -> Dict[str, Any]:
    return {
        "type": "custom:apexcharts-card",
        "header": {"show": True, "title": "PCC aktyvioji ir reaktyvioji galia (7 d.)"},
        "graph_span": "7d",
        "update_interval": "5min",
        "apex_config": {"chart": {"height": 250, "animations": {"enabled": False}}},
        "yaxis": [
            {"id": "p", "decimals": 0},
            {"id": "q", "opposite": True, "decimals": 0},
        ],
        "series": [
            {"entity": q["active"], "name": "P (W)", "yaxis_id": "p", "color": "#ffb300", "stroke_width": 2},
            {"entity": q["reactive"], "name": "Q (var)", "yaxis_id": "q", "color": "#7e57c2", "stroke_width": 2},
        ],
    }


def _power_quality_health_card(q: Dict[str, str]) -> Dict[str, Any]:
    return {
        "type": "custom:apexcharts-card",
        "header": {"show": True, "title": "Fazių išsiskyrimas ir įtampos skirtumas (7 d.)"},
        "graph_span": "7d",
        "update_interval": "5min",
        "apex_config": {"chart": {"height": 250, "animations": {"enabled": False}}},
        "yaxis": [
            {"id": "spread", "min": 0, "decimals": 1},
            {"id": "rise", "opposite": True, "min": 0, "decimals": 1},
        ],
        "series": [
            {"entity": q["spread"], "name": "Fazių dydžių išsiskyrimas (%)", "yaxis_id": "spread", "color": "#26a69a", "stroke_width": 2},
            {"entity": q["rise"], "name": "Inverterio–PCC skirtumas (V)", "yaxis_id": "rise", "color": "#ec407a", "stroke_width": 2},
        ],
    }


def _plant_analysis(p: Dict[str, Any]) -> Dict[str, Any]:
    a = p["analysis"]
    t = p["toggles"]
    today = a["today"]
    model = a["consumption_model"]
    battery = a["battery_health"]
    inverter = a["inverter"]
    quality = a["quality"]

    # Naudotojo pažymėjimas: „Savarankiškumas“ sąmoningai neįtrauktas.
    today_card = _glance("Šiandienos rezultatas", [
        _row(today["savings"], "Sutaupyta", "mdi:piggy-bank"),
        _row(today["consumption"], "Suvartota"),
        _row(today["import"], "Pirkta"),
        _row(today["export"], "Parduota"),
    ])
    model_card = _glance("Vartojimo modelis (AppDaemon)", [
        _row(model["daily_avg"], "Paros vidurkis", "mdi:counter"),
        _row(model["remaining"], "Liko šiandien", "mdi:timer-sand"),
        _row(model["tomorrow"], "Rytoj", "mdi:calendar-arrow-right"),
    ])
    battery_card = _glance("Baterijos sveikata", [
        _row(battery["soh"], "SOH", "mdi:battery-heart-variant"),
        _row(battery["cycles"], "Ciklai"),
        _row(battery["charged_today"], "Įkrauta šiandien", "mdi:battery-plus"),
        _row(battery["discharged_today"], "Iškrauta šiandien", "mdi:battery-minus"),
    ])
    inverter_card = _glance("Inverteris", [
        _row(inverter["temperature"], "Temperatūra"),
        _row(inverter["status"], "Būsena"),
        _row(inverter["frequency"], "Tinklo dažnis"),
    ])
    efficiency_card = _glance(
        "Efektyvumas ir nuostoliai (nuo įrengimo)",
        _named_rows(a["efficiency"]),
    )
    if a.get("cable"):
        cable_card = _glance(
            a["cable"]["title"],
            _named_rows(a["cable"]["entities"]),
        )
    else:
        cable_card = _notice("Kabelio nuostoliai", a["cable_note"])

    graphs = [
        _conditional(t["graphs"], _daily_balance_card(a)),
        _conditional(t["graphs"], _temperature_card(a["temperature"])),
        _conditional(t["graphs"], _battery_efficiency_card(a["battery_efficiency"])),
        _conditional(t["diagnostics"], _conditional(t["graphs"], _power_quality_voltage_card(quality))),
        _conditional(t["diagnostics"], _conditional(t["graphs"], _power_quality_power_card(quality))),
        _conditional(t["diagnostics"], _conditional(t["graphs"], _power_quality_health_card(quality))),
    ]
    if a.get("forecast_accuracy"):
        graphs.append(_conditional(t["graphs"], _forecast_accuracy_card(a["forecast_accuracy"])))
    else:
        graphs.append(_conditional(
            t["graphs"],
            _notice("Prognozės tikslumas (30 d.)", a["forecast_note"]),
        ))

    return {
        "title": "Analizė",
        "path": "analize",
        "icon": "mdi:chart-box",
        "type": "sections",
        "max_columns": 2,
        "sections": [
            {"type": "grid", "cards": [
                today_card,
                _conditional(t["forecast"], model_card),
                _conditional(t["diagnostics"], _phase_card(a)),
                _conditional(t["diagnostics"], _power_quality_glance(quality)),
            ]},
            {"type": "grid", "cards": [
                battery_card,
                inverter_card,
                _conditional(t["diagnostics"], efficiency_card),
                _conditional(t["diagnostics"], cable_card),
            ]},
            {"type": "grid", "column_span": 2, "cards": graphs},
        ],
    }


def _payback_view(p) -> Dict[str, Any]:
    finance = p["finance"]
    entities = finance["entities"]
    result_entities = [
        *[_row(member["entity"], member["label"], "mdi:solar-panel") for member in finance["members"]],
        _row(entities["savings"], "Sutaupyta"),
        _row(entities["net"], "Investicija–parama"),
        _row(entities["percent"], "Atsipirko"),
        _row(entities["remaining"], "Liko"),
        _row(entities["date"], "Kada atsipirks"),
    ]
    graph = {
        "type": "custom:apexcharts-card",
        "header": {"show": True, "title": "Atsipirkimas — sutaupyta € ir pagaminta kWh vs investicija"},
        "graph_span": "4y",
        "span": {"start": "year"},
        "update_interval": "1h",
        "now": {"show": True, "label": "šiandien"},
        "apex_config": {
            "chart": {"height": 380, "animations": {"enabled": False}},
            "stroke": {"dashArray": [0, 8, 0]},
            "legend": {"show": True},
        },
        "yaxis": [
            {"id": "eur", "min": 0, "decimals": 0, "apex_config": {"title": {"text": "€"}}},
            {"id": "kwh", "opposite": True, "min": 0, "decimals": 0, "apex_config": {"title": {"text": "kWh"}}},
        ],
        "series": [
            {
                "entity": entities["savings"],
                "name": "Sutaupyta €",
                "yaxis_id": "eur",
                "color": "#4caf50",
                "stroke_width": 3,
                "extend_to": False,
                "show": {"extremas": False},
                "data_generator": payback_generator(finance, savings=True),
            },
            {
                "entity": entities["net"],
                "name": "Investicija (be paramos)",
                "yaxis_id": "eur",
                "color": "#f44336",
                "stroke_width": 2,
                "extend_to": False,
                "show": {"extremas": False},
                "data_generator": (
                    "const v = Number(entity.state) || 0;\n"
                    f"return [[new Date({json.dumps(finance['start'])}).getTime(), v], "
                    f"[new Date({json.dumps(finance['end'])}).getTime(), v]];"
                ),
            },
            {
                "entity": entities["production"],
                "name": "Pagaminta kWh",
                "yaxis_id": "kwh",
                "color": "#fbc02d",
                "stroke_width": 2,
                "opacity": 0.7,
                "extend_to": False,
                "show": {"extremas": False},
                "data_generator": payback_generator(finance),
            },
        ],
    }
    return {
        "title": "Atsipirkimas",
        "path": "atsipirkimas",
        "icon": "mdi:cash-clock",
        "type": "sections",
        "max_columns": 2,
        "sections": [
            {"type": "grid", "cards": [
                {
                    "type": "entities",
                    "show_header_toggle": False,
                    "title": "Įvestis (pildyk ranka)",
                    "entities": [
                        _row(entities["investment"], "Portfelio investicija"),
                        _row(entities["grant"], "Parama (APVA)"),
                        _row(entities["price"], "kWh vertė"),
                        _row(entities["fee"], "Pasaugojimo mokestis /mėn"),
                        _row(entities["annual"], "Metinė gamyba (prognozė)"),
                    ],
                },
                _glance("Rezultatas", result_entities, 3),
            ]},
            {"type": "grid", "column_span": 2, "cards": [graph]},
        ],
    }


def _profile_markdown(p: Dict[str, Any]) -> str:
    entity = p["consumption_profile"]
    label = p["site_label"]
    return (
        f"### {label} vartojimo profilis\n\n"
        f"Slenkantis **{{{{ state_attr('{entity}','window_days') }}}} parų** langas: "
        f"{{{{ state_attr('{entity}','window_start') }}}} – {{{{ state_attr('{entity}','window_end') }}}}.\n\n"
        f"Paros vidurkis **{{{{ state_attr('{entity}','daily_avg') | round(2, default=0) }}}} kWh** "
        f"· parų **{{{{ state_attr('{entity}','daily_sample_days') }}}}** "
        f"· pilnų valandinių parų **{{{{ state_attr('{entity}','hourly_sample_days') }}}}** "
        f"· atskirtų šuolių **{{{{ state_attr('{entity}','anomaly_count') | default(0, true) }}}}**. "
        "Nepilna šiandiena ir trūkstamos valandos į vidurkį neįtraukiamos."
    )



def _hourly_consumption_card(profile: str) -> Dict[str, Any]:
    return {
        "type": "custom:apexcharts-card",
        "header": {"show": True, "title": "Vidutinis vartojimas pagal valandą (kWh)"},
        "graph_span": "24h",
        "span": {"start": "day"},
        "apex_config": {
            "chart": {"height": 260, "animations": {"enabled": False}},
            "xaxis": {"labels": {"format": "HH:00"}},
            "tooltip": {"x": {"format": "HH:00"}},
        },
        "yaxis": [{"min": 0, "decimals": 1}],
        "series": [{
            "entity": profile,
            "name": "kWh",
            "type": "column",
            "color": "#42a5f5",
            "data_generator": HOURLY_PROFILE_JS,
        }],
    }


def _weekday_consumption_card(profile: str) -> Dict[str, Any]:
    return {
        "type": "custom:apexcharts-card",
        "header": {"show": True, "title": "Vidutinis vartojimas pagal savaitės dieną (kWh)"},
        "graph_span": "167h",
        "span": {"start": "isoWeek", "offset": "-7d"},
        "apex_config": {
            "chart": {
                "height": 240,
                "animations": {"enabled": False},
                "locales": [{
                    "name": "en",
                    "options": {
                        "days": ["Sekmadienis", "Pirmadienis", "Antradienis", "Trečiadienis", "Ketvirtadienis", "Penktadienis", "Šeštadienis"],
                        "shortDays": ["Sk", "Pr", "An", "Tr", "Kt", "Pn", "Št"],
                        "months": ["Sausis", "Vasaris", "Kovas", "Balandis", "Gegužė", "Birželis", "Liepa", "Rugpjūtis", "Rugsėjis", "Spalis", "Lapkritis", "Gruodis"],
                        "shortMonths": ["Sau", "Vas", "Kov", "Bal", "Geg", "Bir", "Lie", "Rgp", "Rgs", "Spa", "Lap", "Gru"],
                    },
                }],
            },
            "xaxis": {"type": "datetime", "tickAmount": 6, "labels": {"format": "ddd", "datetimeUTC": False}},
            "tooltip": {"x": {"format": "dddd"}},
            "stroke": {"curve": "straight"},
            "markers": {"size": 0, "hover": {"size": 0}},
        },
        "yaxis": [{"min": 0, "decimals": 1}],
        "series": [
            {
                "entity": profile,
                "name": "kWh",
                "type": "column",
                "color": "#66bb6a",
                "data_generator": WEEKDAY_PROFILE_JS,
            },
            {
                "entity": profile,
                "name": "Vidurkis",
                "type": "line",
                "color": "#78909c",
                "stroke_width": 1,
                "data_generator": WEEKDAY_AVG_JS,
            },
        ],
    }


def _plant_consumption(p: Dict[str, Any]) -> Dict[str, Any]:
    profile = p["consumption_profile"]
    graphs = p["toggles"]["graphs"]
    eso = {
        "type": "statistics-graph",
        "title": "ESO tinklo srautas (valandinis)",
        "chart_type": "bar",
        "period": "hour",
        "days_to_show": 7,
        "stat_types": ["change"],
        "entities": [
            _row(p["eso_import"], "Iš tinklo (pirkta)"),
            _row(p["eso_export"], "Į tinklą (parduota)"),
        ],
    }
    # Masonry išdėstymas čia paliktas tyčia: jis atkuria vartotojo pažymėtą
    # dviejų stulpelių kortelių elgesį plačiame ekrane ir vieną stulpelį telefone.
    return {
        "title": "Vartojimas",
        "path": "vartojimas",
        "icon": "mdi:chart-bar",
        "cards": [
            {"type": "markdown", "content": _profile_markdown(p)},
            _conditional(graphs, _hourly_consumption_card(profile)),
            _conditional(graphs, _weekday_consumption_card(profile)),
            _conditional(graphs, eso),
        ],
    }


def build_dashboard(p: Dict[str, Any]) -> Dict[str, Any]:
    from energy_system.dashboard_design import build_views, button_templates

    # Preserve the selected chart definitions and their plant-specific sources.
    old_views = [_plant_analysis(p), _plant_consumption(p)]
    if p.get("finance"):
        old_views.append(_payback_view(p))
    by_title = {}

    def collect(value):
        if isinstance(value, dict):
            title = value.get("title") or value.get("header", {}).get("title")
            if isinstance(title, str) and "type" in value:
                by_title[title] = value
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    collect(old_views)
    titles = {
        "today": "Šiandienos rezultatas", "daily": "Dienos balansas (14 d. · kWh)",
        "battery": "Baterijos sveikata", "inverter": "Inverteris",
        "battery_efficiency": "Baterijos round-trip efektyvumas (30 d.)",
        "temperature": "Inverterio temperatūra (48 val.)", "accuracy": "Prognozės tikslumas (30 d.)",
        "efficiency": "Efektyvumas ir nuostoliai (nuo įrengimo)", "phase": "Fazės L1 / L2 / L3",
        "quality": "Tinklo ir inverterio kokybė", "voltage": "PCC fazinė įtampa (7 d.)",
        "reactive": "PCC aktyvioji ir reaktyvioji galia (7 d.)",
        "spread": "Fazių išsiskyrimas ir įtampos skirtumas (7 d.)",
        "payback_graph": "Atsipirkimas — sutaupyta € ir pagaminta kWh vs investicija",
        "payback_inputs": "Įvestis (pildyk ranka)", "consumption_model": "Vartojimo modelis (AppDaemon)",
        "hourly": "Vidutinis vartojimas pagal valandą (kWh)", "weekday": "Vidutinis vartojimas pagal savaitės dieną (kWh)",
    }
    legacy = {key: by_title[title] for key, title in titles.items() if title in by_title}
    legacy["cable"] = next(card for title, card in by_title.items() if title.startswith("Kabelio nuostoliai"))
    return {
        "title": p["title"],
        "button_card_templates": button_templates(),
        "views": build_views(p, legacy),
    }


def render_dashboard(site: str, output_dir: Path = PREVIEW_DIR) -> bool:
    if site not in PLANTS:
        raise KeyError(f"Nežinoma elektrinė: {site}")
    p = PLANTS[site]
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / p["file"]
    text = "# GENERATED by energy_system.dashboard_template.py\n" + json.dumps(
        build_dashboard(p), ensure_ascii=False, indent=2
    ) + "\n"
    old = path.read_text(encoding="utf-8") if path.exists() else None
    if old == text:
        return False
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)
    return True


def render_all(output_dir: Path = PREVIEW_DIR) -> Dict[str, bool]:
    return {site: render_dashboard(site, output_dir) for site in PLANTS}


def render_preview() -> Dict[str, bool]:
    return render_all(PREVIEW_DIR)


def render_live() -> Dict[str, bool]:
    """Aiškiai įvardytas vienintelis kelias rašyti į gyvus dashboardus."""
    return render_all(LIVE_DIR)


def _main() -> None:
    parser = argparse.ArgumentParser(description="Generuoti elektrinių dashboardus")
    parser.add_argument(
        "--live",
        action="store_true",
        help="rašyti į gyvus /homeassistant/lovelace failus (be šios žymos kuriama tik peržiūra)",
    )
    args = parser.parse_args()
    target = LIVE_DIR if args.live else PREVIEW_DIR
    print(json.dumps(render_all(target), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    _main()
