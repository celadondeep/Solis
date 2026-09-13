"""HA input adapter for the shared forward energy model; no actuator writes."""
from datetime import datetime, timedelta
from time import monotonic
from zoneinfo import ZoneInfo
from dataclasses import replace
from energy_system.dawn import plan_dawn,day_dispatch
from energy_system.soc_buffer import preferred_policy, daytime_buffer, grid_present

from energy_system.horizon import (
    VERSION, HorizonPolicy, clamp, finite, forecast_slots, plan_horizon, stamp, ha_attributes,
)


def build_horizon_mixin(profile):
    sensors, output = profile["SENSOR"], profile["OUTPUT"]
    site, label = profile["KEY"], profile["SITE_LABEL"]
    config = profile.get("HORIZON", {})
    buffer = profile["SOC_BUFFER"]
    policy = HorizonPolicy(
        hard_floor=profile["PLAN_HARD_FLOOR"],
        kwh_per_soc=profile["KWH_PER_SOC"],
        export_kw=profile["ESO_EXPORT_LIMIT_KW"],
        **config,
    )

    class HorizonMixin:
        def _consumed_today(self, now):
            entity = sensors["consumption_today"]
            current = self.get_optional_sensor_float(entity)
            if not profile.get("CONSUMPTION_TODAY_IS_TOTAL", False):
                return current
            # Eimo uses a lifetime integration meter, not a daily counter.
            # Recorder supplies the value at midnight, once per day/restart.
            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
            cache = getattr(self, "_horizon_daily_base", None)
            if not cache or cache[0] != now.date():
                self._horizon_daily_base = (now.date(), None)
                try:
                    history = self.get_history(entity_id=entity, start_time=midnight,
                                               end_time=midnight+timedelta(seconds=1))
                    records = history[0] if history and isinstance(history[0], list) else []
                    for record in records:
                        at = stamp(record.get("last_changed") or record.get("last_updated"))
                        value = finite(record.get("state"))
                        if at <= stamp(midnight)+timedelta(seconds=1) and value is not None:
                            self._horizon_daily_base = (now.date(), value)
                            break
                except Exception as exc:
                    self.log(f"[{site}] Nepavyko nuskaityti paros skaitiklio bazės: {exc}", level="WARNING")
            base = self._horizon_daily_base[1]
            return current-base if current is not None and base is not None and current >= base else None

        def horizon_guidance(self, soc, force=False):
            now = datetime.now(ZoneInfo(profile.get("TIMEZONE", "Europe/Vilnius")))
            previous = getattr(self, "_horizon_result", None)
            power = self._read_state_record(sensors["power_state"]).get("state")
            boundary = previous.get("wake_at") if previous else None
            crossed = bool(boundary and stamp(now) >= stamp(boundary))
            soc_changed = (finite(soc) is None or not previous or
                abs(float(soc)-float(previous.get("measured_soc", -100))) >= 0.5)
            if (not force and previous is not None
                    and not soc_changed and not crossed
                    and power == previous.get("observed_power")
                    and monotonic() - getattr(self, "_horizon_at", 0) < 55):
                return previous
            try:
                result = self._calculate_horizon(now, soc)
            except Exception as exc:
                result = dict(version=VERSION, valid=False, export_now=False,
                              night_active=False, inverter_on=True,
                              solar_export_priority=False, soc_buffer_active=False,
                              predictive_buffer_due=False, buffer_required_kwh=0,
                              buffer_first_pressure_at=None, buffer_protected_soc=None,
                              reserve_soc=100, cutoff_soc=100, target_soc=profile["PLAN_TARGET_BAND_CEILING"],
                              wake_at=None, discharge_start_at=None, discharge_deadline=None,
                              required_preexport_kwh=0, required_headroom_kwh=0,
                              standby_saved_kwh=0, dawn=None,
                              reason=f"Prognozė nepatikima; savas vartojimas be priverstinio eksporto ({str(exc)[:100]})")
                if not previous or previous.get("valid"):
                    self.log(f"[{site}] {result['reason']}", level="WARNING")
            result.update(site=site, calculated_at=now.isoformat(),
                          measured_soc=finite(soc, -100), observed_power=power,
                          expires_at=(now + timedelta(minutes=7)).isoformat(),
                          forecast_source=profile["FORECAST_SOURCE_LABEL"],
                          reserve_scope="Ryto talpos tikslas ir apsauga nuo priverstinio eksporto; nakties miegas saugo likusį įkrovimą",
                          grid_charging="Naujų įkrovimo iš tinklo komandų nėra; kaupiama PV")
            self._horizon_result = result
            self._horizon_at = monotonic()
            self.last_target_soc = 100 if self.is_storm_mode() else int(result.get("target_soc", result["reserve_soc"]))
            self.set_state(output["target_soc"], state=str(self.last_target_soc), attributes={
                "friendly_name": f"{label}: tikslinė SOC riba",
                "unit_of_measurement": "%", "device_class": "battery",
                "model_version": VERSION, "reason": result["reason"],
            })
            self.set_state(output["horizon"], state=(
                "export" if result["export_now"] else "sleep" if result.get("night_active")
                and not result.get("inverter_on",True) else "self_use" if result["valid"] else "fallback"
            ), attributes=ha_attributes({"friendly_name": f"{label}: energijos perspektyva", **result}))
            if not previous or (previous.get("export_now"), previous.get("valid"),previous.get("inverter_on")) != (
                    result["export_now"], result["valid"],result.get("inverter_on")):
                self.log(f"[{site}] Horizon: {result['reason']}; rezervas {result['reserve_soc']}%")
            return result

        def _calculate_horizon(self, now, soc):
            actual_export = self.get_optional_sensor_float(sensors.get("export_limit"))
            active_policy = replace(policy,export_kw=min(policy.export_kw,max(0,actual_export/1000))) \
                if actual_export is not None else policy
            active_policy = preferred_policy(active_policy,
                self.get_optional_sensor_float(buffer["min"]),
                self.get_optional_sensor_float(buffer["max"]),
                extra_headroom_soc=profile.get("EXTRA_HEADROOM_SOC", 0))
            connected = grid_present(now, self._read_state_record(buffer["heartbeat"]).get("state"),
                [self.get_optional_sensor_float(e) for e in buffer["voltages"]],
                self.get_optional_sensor_float(buffer["frequency"]), buffer["max_age"])
            # Read-only BMS acceptance. No guessed Eimo taper curve or register writes.
            current = self.get_optional_sensor_float(buffer.get("charge_current"))
            voltage = self.get_optional_sensor_float(buffer.get("battery_voltage"))
            acceptance = (current * voltage / 1000 if connected and current is not None
                          and current >= 0 and voltage is not None and 40 <= voltage <= 65 else None)
            records = []
            for key, expected in (("solcast_today_total", now.date()),
                                  ("solcast_tomorrow", (now + timedelta(days=1)).date())):
                record = self._read_state_record(sensors[key])
                attrs = record.get("attributes") or {}
                updated = stamp(record.get("last_updated") or record.get("last_reported"))
                age = (stamp(now) - updated).total_seconds()
                if not -300 <= age <= 30 * 3600 or attrs.get("dataCorrect") is False:
                    raise ValueError("stale/invalid Solcast")
                rows = attrs.get("detailedForecast")
                if not isinstance(rows, list) or not rows:
                    raise ValueError("missing detailedForecast")
                if any(stamp(r["period_start"]).astimezone(now.tzinfo).date() != expected for r in rows):
                    raise ValueError("forecast date mismatch")
                records.extend(rows)
            consumption_record = self._read_state_record(sensors["consumption_profile"])
            attrs = consumption_record.get("attributes") or {}
            hourly = attrs.get("hourly_kwh")
            if not isinstance(hourly, list) or len(hourly) != 24:
                raise ValueError("missing hourly household profile")
            hourly = [finite(v) for v in hourly]
            if any(v is None or v < 0 for v in hourly) or sum(hourly) < 1:
                raise ValueError("invalid household profile")
            if (stamp(now) - stamp(consumption_record.get("last_updated"))).total_seconds() > 48 * 3600:
                raise ValueError("stale household profile")
            tomorrow = self.get_sensor_float(profile["CONSUMPTION_TOMORROW_SENSOR"], default=-1)
            if finite(tomorrow) is None or tomorrow <= 0:
                raise ValueError("missing tomorrow consumption")
            today_total = sum(hourly)

            def load_at(local):
                scale = 1.0 if local.date() == now.date() else tomorrow / today_total
                return hourly[local.hour] * scale

            end = (now + timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)
            initial, expected_pv, expected_load = forecast_slots(
                records, now, end, load_at, self.hourly_factor,
                self_kw=profile["INVERTER_SELF_KW"],
            )
            pv_actual = self.get_optional_sensor_float(sensors["pv_today"])
            consumption_actual = self._consumed_today(now)
            pv_ratio = load_ratio = 1.0
            if pv_actual is not None and pv_actual >= 0 and expected_pv >= 1.0:
                weight = min(0.75, expected_pv / 5.0 * 0.75)
                pv_ratio = clamp(1 + (pv_actual / expected_pv - 1) * weight, 0.55, 1.35)
                # Full battery/export ceiling can curtail production; do not learn it as cloud.
                grid = self.get_optional_float("grid_power")
                if soc is not None and soc >= 93 and grid is not None and abs(grid) >= policy.export_kw * 900:
                    pv_ratio = max(1.0, pv_ratio)
            if consumption_actual is not None and consumption_actual >= 0 and expected_load >= 2.0:
                weight = min(0.75, expected_load / 5.0 * 0.75)
                load_ratio = clamp(1 + (consumption_actual / expected_load - 1) * weight, 0.70, 1.60)
            # A single unusual day weakly influences tomorrow, never replaces learned data.
            tomorrow_ratio = clamp(1 + (load_ratio - 1) * 0.25, 0.9, 1.15)
            slots, _, _ = forecast_slots(
                records, now, end, load_at, self.hourly_factor,
                today_ratio=pv_ratio, load_ratio=load_ratio,
                tomorrow_load_ratio=tomorrow_ratio, self_kw=profile["INVERTER_SELF_KW"],
            )
            # Short-lived measured demand correction; do not project a kettle
            # spike over the whole night or count sleeping grid loads as DC drain.
            load_now = self.get_optional_float("house_load")
            if load_now is not None and load_now >= 0:
                adjusted = []
                for s in slots:
                    lead = (stamp(s.start)-stamp(now)).total_seconds()/3600
                    weight = 0.5*max(0,1-lead)
                    live_load = load_now/1000 + profile["INVERTER_SELF_KW"]
                    adjusted.append(replace(s,load=s.load*(1-weight)+live_load*weight))
                slots = adjusted
            if acceptance is not None:
                slots[0] = replace(slots[0], charge_limit_kw=acceptance)
            # Cumulative daily bias reacts too slowly after a cloudy morning.
            # Blend fresh measured PV for one hour only; retain the P10 downside.
            pv_now = self.get_optional_float("pv_power")
            if connected and pv_now is not None and pv_now >= 0:
                adjusted = []
                for s in slots:
                    lead = (stamp(s.start)-stamp(now)).total_seconds()/3600
                    weight = 0.5*max(0, 1-lead)
                    pv = s.pv*(1-weight) + pv_now/1000*weight
                    adjusted.append(replace(s, pv=pv, low_pv=min(s.low_pv, pv)))
                slots = adjusted
            result = plan_horizon(slots, soc, active_policy)
            pv_now = self.get_optional_float("pv_power")
            production = (slots[0].pv >= profile["MORNING_PV_THRESHOLD_KW"] or
                (pv_now is not None and pv_now/1000 >= profile["MORNING_PV_THRESHOLD_KW"]))
            night = plan_dawn(slots,now,soc,active_policy,production_on=production,
                power_control=profile["INVERTER_CONTROL_AVAILABLE"],
                power_on=self._read_state_record(sensors["power_state"]).get("state") != "off",
                already_exporting=self._read_state_record(profile["ACTUATOR"]["slot"]).get("state") == "on",
                export_floor_soc=self.get_optional_sensor_float(sensors["daytime_export_floor"]),
                idle_kw=profile["INVERTER_IDLE_W"]/1000,
                off_kw=profile["INVERTER_OFF_W"]/1000,
                pv_threshold_kw=profile["MORNING_PV_THRESHOLD_KW"],
                wake_margin_minutes=profile["MORNING_ON_MARGIN_MIN"])
            result.update(night_active=False,inverter_on=True,dawn=night,
                          solar_export_priority=False,
                          wake_at=None,discharge_start_at=None,discharge_deadline=None,
                          required_headroom_kwh=0,standby_saved_kwh=0)
            if night:
                result.update(night)
                result["useful_headroom_kwh"] = night["required_headroom_kwh"]
            else:
                result.update(day_dispatch(slots,now,soc,active_policy))
                previous = getattr(self, "_horizon_result", {})
                result = daytime_buffer(result, slots, now, soc, active_policy,
                    connected=connected,
                    export_floor=self.get_optional_sensor_float(sensors["daytime_export_floor"]),
                    already_buffering=(previous.get("valid") is True and
                        previous.get("soc_buffer_active") is True),
                    previous_cutoff=previous.get("cutoff_soc"),
                    execution_minutes=profile.get("HEADROOM_EXECUTION_MINUTES", 5),
                    extra_headroom_soc=profile.get("EXTRA_HEADROOM_SOC", 0),
                    charge_acceptance_kw=acceptance)
                if result["solar_export_priority"] and not result["export_now"]:
                    result["reason"] = "Dienos PV: namai → leistinas eksportas → baterija; vietos rezervas saugomas"
            result.update(preferred_soc_min=active_policy.comfort_soc,
                          preferred_soc_max=active_policy.storage_ceiling,
                          grid_connected=connected, charge_acceptance_kw=acceptance)
            if not connected:
                result.update(export_now=False, solar_export_priority=False,
                              reason="Tinklo buvimas nepatvirtintas; priverstinis eksportas išjungtas")
            result.update(model_export_limit_kw=active_policy.export_kw,
                          model_charge_kw=active_policy.charge_kw,
                          model_discharge_kw=active_policy.discharge_kw,
                          pv_adjustment=round(pv_ratio, 3),
                          consumption_adjustment=round(load_ratio, 3),
                          consumption_tomorrow_adjustment=round(tomorrow_ratio, 3),
                          expected_pv_so_far_kwh=round(expected_pv, 2),
                          actual_pv_so_far_kwh=pv_actual,
                          expected_load_so_far_kwh=round(expected_load, 2),
                          actual_load_so_far_kwh=consumption_actual)
            return result

        def publish_night_economics(self):
            """Expose the same actionable schedule consumed by Planner."""
            result = self.horizon_guidance(self.get_optional_float("soc"))
            night = result.get("dawn") or {}
            attrs = {"friendly_name": f"{label}: nakties planas",
                     "icon":"mdi:power-sleep", "model_version":VERSION,
                     "idle_w":profile["INVERTER_IDLE_W"],
                     "isjungto_w":profile["INVERTER_OFF_W"],
                     "sutaupyta_savivarta_kwh":night.get("standby_saved_kwh",0),
                     "namai_is_tinklo_kwh":night.get("sleep_grid_import_kwh",0),
                     "power_control_available":profile["INVERTER_CONTROL_AVAILABLE"],
                     "kaina_ijungtas_eur_h":None,"kaina_isjungtas_eur_h":None,
                     "skirtumas_eur_h":None,"sutaupymas_nakti_eur":None,
                     "baterijos_kwh_verte":None,
                     **night}
            self.set_state(output["night_economics"],state=result["reason"][:254],
                           attributes=ha_attributes(attrs))
            sleeping = result.get("night_active") and not result.get("inverter_on",True)
            self.set_state(output["shutdown_eta"],state="dabar" if sleeping else "—",
                attributes=ha_attributes({"friendly_name":f"{label}: inverterio išjungimas",
                    "reason":result["reason"],"tikslinis_soc":self.last_target_soc}))
            if night.get("wake_at"):
                self.set_state(output["morning_on_time"],state=night["wake_at"],
                    attributes={"friendly_name":f"{label}: inverterio įjungimas",
                        "device_class":"timestamp","pv_start_at":night["pv_start_at"],
                        "reason":night["reason"]})
            self.set_state(output["cons_until_production"],
                state=str(round(night.get("natural_discharge_if_on_kwh",0)*policy.discharge_eff,3)),
                attributes={"friendly_name":f"{label}: vartojimas iki gamybos įjungus inverterį",
                    "unit_of_measurement":"kWh","state_class":"measurement",
                    "reason":"Miegant namai maitinami iš tinklo; iki 30 W likutinės sąnaudos įtrauktos baterijai"})
            self.set_state(output["correction_factor"],
                state=str(round(self.correction.get("factor",1),3)),
                attributes=ha_attributes({"friendly_name":f"{label}: Solcast korekcija",
                    "dienos":self.correction.get("days",0),
                    "atnaujinta":self.correction.get("updated"),
                    "valandiniai":self.correction.get("hourly_factors",{}),
                    "valandiniu_dienos":self.correction.get("hourly_days",0)}))

        def publish_realization(self):
            result = self.horizon_guidance(self.get_optional_float("soc"))
            value = result.get("useful_headroom_kwh", 0.0)
            self.set_state(output["room_shortfall"], state=str(value), attributes={
                "friendly_name": f"{label}: vietos poreikis pagal perspektyvą",
                "unit_of_measurement": "kWh", "state_class": "measurement",
                "model_version": VERSION, "valid": "on" if result["valid"] else "off",
                "reason": result["reason"], "horizon_hours": result.get("horizon_hours", 0),
            })

    return HorizonMixin
