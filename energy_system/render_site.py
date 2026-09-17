"""Generate reviewable HA bindings for an additional registered plant.

python -m energy_system.render_site --site SITE --output /tmp/site-preview
The destination is explicit; this command does not reload HA or send commands.
"""
import argparse
import json
from pathlib import Path
from energy_system.site_registry import get_site, dashboard_profiles
from energy_system.dashboard_template import build_dashboard


def render(site, output):
    config = get_site(site)
    execution = config["execution"]
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    items = {
        "executor.json": {"id": execution["automation_id"], "alias": config["energy"]["SITE_LABEL"] + " - Atominio energijos plano vykdymas",
            "use_blueprint": {"path": "energy/" + execution["adapter"] + "_executor.yaml", "input": execution["inputs"]}},
        "apps.json": {f"{name}_{site}": {"module": "plant_apps", "class": f"{cls}_{site}"}
            for name, cls in (("energy_manager","EnergyManager"), ("consumption_model","ConsumptionModel"), ("energy_shadow","ShadowManager"))},
        "dashboard.json": build_dashboard(dashboard_profiles()[site]),
    }
    for name, value in items.items():
        (output/name).write_text(json.dumps(value, ensure_ascii=False, indent=2)+"\n")
    return list(items)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(render(args.site, args.output))
