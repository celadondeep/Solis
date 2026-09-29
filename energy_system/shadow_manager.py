"""HA šešėlinio branduolio adapteris — tik skaitymas ir diagnostikos sensoriai."""

from __future__ import annotations

from datetime import datetime

import appdaemon.plugins.hass.hassapi as hass

from energy_system.planner import PlannerInput, PlannerPolicy, decide_plan
from energy_system.shadow import compare_plan, preview_execution
from energy_system.telemetry import assess_freshness, sample_timestamp
from energy_system.horizon import read_guidance


def build_shadow_manager(profile):
    sensor = profile["SENSOR"]
    output = profile["OUTPUT"]
    shadow_output = profile["SHADOW_OUTPUT"]
    actuator = profile["ACTUATOR"]
    site = profile["KEY"]
    label = profile["SITE_LABEL"]
    max_soc_age = profile.get("TELEMETRY_MAX_AGE_SECONDS", {}).get("soc", 900)
    heartbeat = profile.get("TELEMETRY_HEARTBEAT", {})
    require_heartbeat = profile.get("TELEMETRY_REQUIRE_HEARTBEAT", False)
    allow_unknown = profile.get("TELEMETRY_ALLOW_UNKNOWN_TIMESTAMP", True)
    threshold_kw = profile.get("MORNING_PV_THRESHOLD_KW", 0.1)

    class ShadowManager(hass.Hass):
        """Naujo plano šešėlinė projekcija; fiziniai rašymai sąmoningai neegzistuoja."""

        def initialize(self):
            self.log(f"[{label}] ShadowManager paleidžiamas (read-only).")
            self.run_every(self.evaluate, "now+30", 5 * 60)
            for key in ("soc", "pv_power", "storm_mode", "manual_override"):
                entity = sensor.get(key)
                if entity:
                    self.listen_state(self._changed, entity)
            if output.get("plan"):
                self.listen_state(self._changed, output["plan"])
            for entity in set(heartbeat.values()) if require_heartbeat else ():
                self.listen_state(self._changed, entity)
            self.run_in(self.evaluate, 3)

        def _changed(self, entity, attribute, old, new, kwargs):
            self.evaluate()

        def _record(self, entity_id):
            try:
                value = self.get_state(entity_id, attribute="all")
                return value if isinstance(value, dict) else {"state": value}
            except Exception:
                return {"state": None}

        def _float(self, entity_id, default=None):
            if not entity_id:
                return default
            raw = self._record(entity_id).get("state")
            try:
                return float(raw)
            except (TypeError, ValueError):
                return default

        def _production(self):
            pv = self._float(sensor.get("pv_power"), 0.0)
            if pv is not None and pv / 1000 >= threshold_kw:
                return True
            # Match the live Planner's forecast-based warm-up window.
            now = datetime.now().astimezone()
            rows = self._record(sensor.get("solcast_today_total")).get("attributes",{}).get("detailedForecast",[])
            starts = []
            for row in rows:
                try:
                    at = datetime.fromisoformat(str(row["period_start"])).astimezone()
                    if at.date() == now.date() and float(row.get("pv_estimate",0)) >= threshold_kw:
                        starts.append(at)
                except (TypeError,ValueError,KeyError):
                    continue
            if starts:
                from datetime import timedelta
                margin = timedelta(minutes=profile["MORNING_ON_MARGIN_MIN"])
                return min(starts)-margin <= now <= max(starts)+timedelta(minutes=30)+margin
            return self.get_state(sensor.get("sun")) == "above_horizon"

        def _observed(self):
            return {
                "executor": self.get_state(profile["EXECUTOR_ENTITY"]),
                "mode": self.get_state(actuator["mode"]),
                "slot": self.get_state(actuator["slot"]),
                "slot_cutoff_soc": self.get_state(actuator["slot_cutoff"]),
                "exclusive_slots": {
                    entity_id: self.get_state(entity_id)
                    for entity_id in actuator.get("exclusive_off", ())
                },
                "power": self.get_state(actuator["power"]),
            }

        def evaluate(self, kwargs=None):
            soc_record = self._record(sensor.get("soc"))
            soc_raw = soc_record.get("state")
            heartbeat_entity = heartbeat.get("soc")
            heartbeat_record = (
                self._record(heartbeat_entity)
                if heartbeat_entity else {}
            )
            reported_at = sample_timestamp(
                soc_record, heartbeat_record,
                require_heartbeat=require_heartbeat,
            )
            freshness = assess_freshness(
                soc_raw,
                reported_at,
                max_age_seconds=max_soc_age,
                now=datetime.now().astimezone(),
                allow_unknown_timestamp=allow_unknown,
            )
            try:
                soc = float(soc_raw) if freshness.usable else None
            except (TypeError, ValueError):
                soc = None
            manual_entity = sensor.get("manual_override")
            target = self._float(output.get("target_soc"), 100.0)
            export_floor = self._float(
                sensor.get("daytime_export_floor"),
                profile["PLAN_HARD_FLOOR"],
            )
            shortfall = self._float(output.get("room_shortfall"), 0.0)
            horizon = read_guidance(
                self._record(output.get("horizon")).get("attributes"),
                datetime.now().astimezone(),
            )
            plan = decide_plan(
                PlannerInput(
                    storm=self.get_state(sensor.get("storm_mode")) == "on",
                    manual=bool(manual_entity)
                    and self.get_state(manual_entity) == "on",
                    soc=soc,
                    target_soc=target,
                    in_production_hours=self._production(),
                    export_floor=export_floor,
                    room_shortfall_kwh=shortfall,
                    horizon=horizon,
                ),
                PlannerPolicy(
                    hard_floor=profile["PLAN_HARD_FLOOR"],
                    night_rest_soc=profile["NIGHT_REST_SOC"],
                    preferred_floor=profile["PLAN_TARGET_BAND_FLOOR"],
                    ceiling=profile["PLAN_TARGET_BAND_CEILING"],
                ),
            )
            active_record = self._record(output.get("plan"))
            active_attrs = active_record.get("attributes") or {}
            comparison = compare_plan(
                plan,
                site=site,
                active_mode=active_record.get("state"),
                active_attributes=active_attrs,
                slot_cutoff_tolerance=float(actuator.get("slot_cutoff_tolerance", 0.5)),
            )
            preview = preview_execution(
                plan,
                self._observed(),
                actuator,
                site=site,
                inverter_control_available=profile["INVERTER_CONTROL_AVAILABLE"],
            )
            status = comparison.status
            if status == "match" and preview.status == "shadow_only":
                status = "drift"
            self.set_state(
                shadow_output["plan"],
                state=plan.mode,
                attributes={
                    "friendly_name": f"{label}: šešėlinis kandidatinis planas",
                    "site": site,
                    "reason": plan.reason,
                    "priority": plan.priority,
                    "actionable": "on" if plan.actionable else "off",
                    "inputs_valid": "on" if plan.inputs_valid else "off",
                    "telemetry_quality": freshness.quality,
                    "telemetry_age_seconds": freshness.age_seconds,
                    "writes_disabled": True,
                },
            )
            self.set_state(
                shadow_output["check"],
                state=status,
                attributes={
                    "friendly_name": f"{label}: šešėlinio plano patikra",
                    "site": site,
                    "candidate_mode": comparison.candidate_mode,
                    "active_mode": comparison.active_mode,
                    "differences": list(comparison.differences),
                    "preview_status": preview.status,
                    "preview_commands": [
                        {
                            "actuator": command.actuator,
                            "value": command.value,
                            "reason": command.reason,
                        }
                        for command in preview.commands
                    ],
                    "writes_disabled": True,
                    "last_checked": datetime.now().astimezone().isoformat(),
                },
            )
            self.set_state(
                shadow_output["commands"],
                state=str(len(preview.commands)),
                attributes={
                    "friendly_name": f"{label}: šešėlinės komandos (nevykdomos)",
                    "site": site,
                    "commands": [command.actuator for command in preview.commands],
                    "writes_disabled": True,
                },
            )

    return ShadowManager
