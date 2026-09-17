"""Independent plant configuration; shared code never selects a site by name."""
from __future__ import annotations
from copy import deepcopy
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent
APPS_DIR = ROOT.parent


def merge(base, override):
    result = deepcopy(base)
    for key, value in override.items():
        result[key] = merge(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else deepcopy(value)
    return result


def resolve(document, apps_dir=APPS_DIR):
    """References are local to one plant and return independent copies."""
    def visit(value, chain=()):
        if isinstance(value, dict):
            if "$ref" in value:
                if set(value) != {"$ref"}:
                    raise ValueError("$ref cannot have sibling fields")
                path = value["$ref"]
                if path in chain:
                    raise ValueError(f"Cyclic site reference: {path}")
                target = document
                try:
                    for part in path.split("."):
                        target = target[int(part)] if isinstance(target, list) else target[part]
                except (KeyError, IndexError, ValueError, TypeError) as exc:
                    raise ValueError(f"Unknown site reference: {path}") from exc
                return visit(target, chain+(path,))
            return {k:visit(v, chain) for k,v in value.items()}
        if isinstance(value, list):
            return [visit(v, chain) for v in value]
        return value.replace("{apps_dir}", str(apps_dir)) if isinstance(value, str) else value
    return visit(document)


def load_sites(directory=None, defaults=None, apps_dir=APPS_DIR):
    directory = Path(directory) if directory else ROOT / "sites"
    defaults = defaults if defaults is not None else json.loads((ROOT / "site_defaults.json").read_text())
    sites = {}
    for path in sorted(directory.glob("*.json")):
        raw = json.loads(path.read_text())
        if raw.get("schema_version") != 1:
            raise ValueError(f"Unsupported site schema: {path.name}")
        site = resolve(merge(defaults, raw), apps_dir)
        key = site["energy"]["KEY"]
        if not re.fullmatch(r"[a-z][a-z0-9_]*", key) or key in sites or path.stem != key:
            raise ValueError(f"Invalid or duplicate site key: {key}")
        if site["execution"]["adapter"] not in {"modbus", "solis_cloud"}:
            raise ValueError(f"Unknown transport for {key}")
        if site["energy"]["TRANSPORT"] != site["execution"]["adapter"]:
            raise ValueError(f"Transport mismatch for {key}")
        energy, bindings = site["energy"], site["execution"]["inputs"]
        expected = {"plan_entity": energy["OUTPUT"]["plan"],
                    "planner_entity": energy["OUTPUT"]["planner_health"],
                    "storm_entity": energy["SENSOR"]["storm_mode"],
                    "hard_floor": energy["PLAN_HARD_FLOOR"]}
        if any(bindings.get(k) != v for k,v in expected.items()):
            raise ValueError(f"Executor differs from the planner profile: {key}")
        if Path(site["dashboard"]["file"]).name != site["dashboard"]["file"]:
            raise ValueError(f"Dashboard filename must be local: {key}")
        sites[key] = site
    if not sites:
        raise ValueError("No plant profiles configured")
    from energy_system.config_validator import validate_profiles
    issues = validate_profiles(s["energy"] for s in sites.values())
    for label, getter in (
        ("cloud_target", lambda s: (s["execution"]["inputs"].get("entry_id"), s["execution"]["inputs"].get("inverter_sn")) if s["execution"]["adapter"] == "solis_cloud" else None),
        ("dashboard_file", lambda s: s["dashboard"]["file"]),
        ("dashboard_url", lambda s: s["dashboard"]["url"]),
        ("automation_id", lambda s: s["execution"]["automation_id"]),
    ):
        seen = set()
        for key, site in sites.items():
            value = getter(site)
            if value is not None and value in seen:
                issues += (f"duplicate_{label}:{key}",)
            seen.add(value)
    outputs, data_files = {}, {}
    for key, site in sites.items():
        for mapping in (site["energy"].get("SHADOW_OUTPUT", {}), site["energy"]["OUTPUT"],
                        site["consumption"]["OUTPUT"], site["power_quality"]["outputs"]):
            for entity in mapping.values():
                if entity in outputs and outputs[entity] != key:
                    issues += (f"cross_site_output:{entity}",)
                outputs[entity] = key
        for section, fields in (("energy", ("CORRECTION_FILE", "TARGET_SOC_FILE")),
                ("consumption", ("MODEL_FILE",)), ("power_quality", ("history_file",)),
                ("battery_health", ("HISTORY_FILE",)), ("weekly_report", ("REPORT_FILE",))):
            for field in fields:
                filename = site.get(section, {}).get(field)
                if filename and filename in data_files:
                    issues += (f"shared_state_file:{key}:{field}",)
                if filename:
                    data_files[filename] = key
    if issues:
        raise ValueError("Invalid plant profiles: " + "; ".join(issues))
    return sites


SITES = load_sites()


def get_site(key):
    return deepcopy(SITES[key])


def profiles(section):
    return {key:deepcopy(site[section]) for key,site in SITES.items() if section in site}


def dashboard_profiles():
    result = profiles("dashboard")
    portfolios = json.loads((ROOT / "portfolios.json").read_text())
    for key, dashboard in result.items():
        dashboard["navigation"] = [
            {"title": other["title"], "url": other["url"], "overview_path": other.get("overview_path", "energija")}
            for name, other in result.items() if name != key
        ]
        dashboard["finance"] = deepcopy(portfolios.get(dashboard.get("portfolio")))
    return result
