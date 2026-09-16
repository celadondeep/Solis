"""Bendras abiejų elektrinių HA I/O ir planavimo tvarkaraščio adapteris.

Sprendimai yra grynajame planner branduolyje. Prognozės, nakties
planavimas ir boileris įjungiami kaip atskiros periferijos.
"""

from datetime import datetime, time, timedelta
from math import isfinite
from time import monotonic

import appdaemon.plugins.hass.hassapi as hass

from energy_system.planner import (
    PlannerInput,
    PlannerPolicy,
    decide_plan,
)
from energy_system.boiler import build_boiler_mixin
from energy_system.forecast import build_forecast_mixin
from energy_system.night import build_night_mixin
from energy_system.supervisor import build_supervisor_mixin
from energy_system.telemetry import assess_freshness, as_utc, sample_timestamp
from energy_system.core import PlantCore
from energy_system.config_validator import validate_profile
from energy_system.horizon_adapter import build_horizon_mixin


def build_energy_manager(profile):
    """Sukuria izoliuotą AppDaemon klasę iš vieno elektrinės profilio."""
    PLAN_TARGET_BAND_FLOOR = profile["PLAN_TARGET_BAND_FLOOR"]
    PLAN_TARGET_BAND_CEILING = profile["PLAN_TARGET_BAND_CEILING"]
    PLAN_HARD_FLOOR = profile["PLAN_HARD_FLOOR"]
    INVERTER_SELF_KW = profile["INVERTER_SELF_KW"]
    SEASON_SOC_MIN = profile["SEASON_SOC_MIN"]
    BOILER_TEMP_WINTER = profile["BOILER_TEMP_WINTER"]
    BOILER_TEMP_SUMMER = profile["BOILER_TEMP_SUMMER"]
    DEFAULT_DAILY_CONSUMPTION = profile["DEFAULT_DAILY_CONSUMPTION"]
    CONSUMPTION_TOMORROW_SENSOR = profile["CONSUMPTION_TOMORROW_SENSOR"]
    CONSUMPTION_REMAINING_SENSOR = profile["CONSUMPTION_REMAINING_SENSOR"]
    TACTICAL_INTERVAL = profile["TACTICAL_INTERVAL"]
    BOILER_ENABLED = profile["BOILER_ENABLED"]
    INVERTER_CONTROL_AVAILABLE = profile["INVERTER_CONTROL_AVAILABLE"]
    SENSOR = profile["SENSOR"]
    OUTPUT = profile["OUTPUT"]
    TELEMETRY_MAX_AGE_SECONDS = profile.get("TELEMETRY_MAX_AGE_SECONDS", {})
    TELEMETRY_ALLOW_UNKNOWN_TIMESTAMP = profile.get("TELEMETRY_ALLOW_UNKNOWN_TIMESTAMP", True)
    TELEMETRY_REQUIRED = profile.get("TELEMETRY_REQUIRED", ("soc",))
    TELEMETRY_HEARTBEAT = profile.get("TELEMETRY_HEARTBEAT", {})
    REQUIRE_HEARTBEAT = profile.get("TELEMETRY_REQUIRE_HEARTBEAT", False)
    EXECUTOR_ENTITY = profile["EXECUTOR_ENTITY"]
    SITE_KEY = profile["KEY"]
    SITE_LABEL = profile["SITE_LABEL"]

    HorizonMixin = build_horizon_mixin(profile)
    ForecastMixin = build_forecast_mixin(profile)
    NightPlanningMixin = build_night_mixin(profile)
    BoilerMixin = build_boiler_mixin(profile)
    SupervisorMixin = build_supervisor_mixin(profile)

    class EnergyManager(
        HorizonMixin,
        ForecastMixin,
        NightPlanningMixin,
        BoilerMixin,
        SupervisorMixin,
        hass.Hass,
    ):

        def initialize(self):
            self.log(f"[{SITE_LABEL}] EnergyManager v2 paleidžiamas...")

            self.boiler_allowed = False
            self.soc_history = []
            self.last_strategic_run = None
            self.last_decision = (
                "Paleidžiama..." if BOILER_ENABLED else "Boilerio šiame objekte nėra"
            )
            self.last_balance = 0.0
            self.last_surplus = 0.0
            self.last_plan_mode = None
            self.last_plan_signature = None
            self.current_plan = None
            self.last_plan_committed_monotonic = monotonic()
            self.plan_revision = 0
            self.telemetry_status = {}
            self.telemetry_checked_at = None
            self.valdymo_koordinatorius = None
            self.state_machine = None
            self.last_coordinator_decision = None
            self.core_state = "init"
            self.config_issues = validate_profile(profile)
            if self.config_issues:
                self.log(
                    f"[{SITE_KEY}] Profilio klaidos: {self.config_issues}",
                    level="ERROR",
                )
            self.last_target_soc = self.load_target_soc()
            self.correction = self.load_correction()
            self.plan_policy = PlannerPolicy(
                hard_floor=PLAN_HARD_FLOOR,
                night_rest_soc=profile["NIGHT_REST_SOC"],
                preferred_floor=PLAN_TARGET_BAND_FLOOR,
                ceiling=PLAN_TARGET_BAND_CEILING,
            )
            # Kiekvienas wrapperis gauna atskirą PlantCore instanciją.
            self.plant_core = PlantCore(SITE_KEY, self.plan_policy)
            self.valdymo_koordinatorius = self.plant_core.coordinator
            self.state_machine = self.plant_core.state_machine

            self.run_daily(self.update_forecast_correction, time(23, 50))
            self.run_daily(self.snapshot_today_forecast, time(4, 40))

            if BOILER_ENABLED:
                self.run_every(self.strategic_cycle, "now+5", 30 * 60)
                if self.entity_exists(SENSOR["boiler_switch"]):
                    self.run_every(
                        self.tactical_cycle, "now+15", TACTICAL_INTERVAL
                    )
                else:
                    self.log(
                        f"[{SITE_LABEL}] Boilerio relė nerasta — taktinė kilpa "
                        "nepaleista.",
                        level="INFO",
                    )

            self.run_every(self.refresh_publish, "now+20", 5 * 60)
            self.run_every(self.compute_plan, "now+10", 60)
            self.run_every(self.publish_executor_health, "now+45", 60)

            # Horizon owns the target continuously; legacy evening/morning writers are unscheduled.

            for key in (
                "storm_mode", "manual_override", "soc",
                "pv_power", "daytime_export_floor",
            ):
                entity = SENSOR.get(key)
                if entity:
                    self.listen_state(self._plan_input_changed, entity)

            for entity in set(TELEMETRY_HEARTBEAT.values()) if REQUIRE_HEARTBEAT else ():
                self.listen_state(self._plan_input_changed, entity)

            self.set_state(
                OUTPUT["surplus_now"], state="0.0",
                attributes={
                    "friendly_name": f"{SITE_LABEL}: saulės perteklius (dabar)",
                    "unit_of_measurement": "kW",
                    "icon": "mdi:solar-panel",
                },
            )
            self.set_state(
                OUTPUT["target_soc"], state=str(self.last_target_soc),
                attributes={
                    "friendly_name": f"{SITE_LABEL}: tikslinė SOC riba",
                    "unit_of_measurement": "%",
                    "icon": "mdi:battery-charging-80",
                    "device_class": "battery",
                },
            )
            self.set_state(
                OUTPUT["inverter_temp"], state="0",
                attributes={
                    "friendly_name": f"{SITE_LABEL}: inverterio temperatūra",
                    "unit_of_measurement": "°C",
                    "icon": "mdi:thermometer",
                    "device_class": "temperature",
                },
            )
            self.set_state(
                OUTPUT["correction_factor"],
                state=str(round(self.correction.get("factor", 1.0), 3)),
                attributes={
                    "friendly_name": f"{SITE_LABEL}: Solcast korekcija",
                    "icon": "mdi:tune-variant",
                },
            )
            self.set_state(
                OUTPUT["plan_mode"], state="hold",
                attributes={
                    "friendly_name": f"{SITE_LABEL}: plano režimas",
                    "priezastis": "Valdiklis paleidžiamas",
                    "icon": "mdi:brain",
                },
            )
            self.set_state(
                OUTPUT["plan_target_soc"], state=str(self.last_target_soc),
                attributes={
                    "friendly_name": f"{SITE_LABEL}: plano tikslinis SOC",
                    "unit_of_measurement": "%",
                    "device_class": "battery",
                },
            )
            self.set_state(
                OUTPUT["plan_slot_active"], state="unknown",
                attributes={"friendly_name": f"{SITE_LABEL}: plano slotas"},
            )
            self.set_state(
                OUTPUT["plan_slot_cutoff_soc"], state="unknown",
                attributes={
                    "friendly_name": f"{SITE_LABEL}: plano sloto cut-off",
                    "unit_of_measurement": "%",
                },
            )
            self.set_state(
                OUTPUT["plan_inverter_on"], state="unknown",
                attributes={
                    "friendly_name": f"{SITE_LABEL}: inverterio pageidaujama būsena"
                },
            )
            self.set_state(
                OUTPUT["plan"], state="hold",
                attributes={
                    "friendly_name": f"{SITE_LABEL}: atominis energijos planas",
                    "actionable": "off",
                    "inputs_valid": "off",
                    "reason": "Valdiklis paleidžiamas",
                    "revision": 0,
                },
            )
            self.set_state(
                OUTPUT["planner_health"], state="starting",
                attributes={
                    "friendly_name": f"{SITE_LABEL}: Planner sveikata",
                    "site": SITE_KEY,
                },
            )
            self.set_state(
                OUTPUT["executor_health"], state="starting",
                attributes={
                    "friendly_name": f"{SITE_LABEL}: Executor sveikata",
                    "site": SITE_KEY,
                },
            )

            self.run_in(self.compute_plan, 2)
            self.log(f"[{SITE_LABEL}] EnergyManager v2 paleistas.")


        # ============================================================
        #  BŪSENOS PUBLIKAVIMAS Į HA SENSORIUS
        # ============================================================

        def refresh_publish(self, kwargs):
            """Periodinis (kas 5 min) būsenos publikavimas — kad temperatūros ir
            kitų sensorių grafikai turėtų reguliarius taškus net kai taktinis
            ciklas išeina anksčiau. compute_plan() visada kviečiamas PASKUTINIS,
            kad naudotų šviežiai publikuotą room_shortfall."""
            self.publish_status()
            self.publish_night_economics()
            self.publish_realization()
            self.compute_plan()

        def publish_status(self):
            """Eksportuoja vidinę automacijos būseną kaip HA sensorius."""
            # SVARBU: visi skaitiniai state perduodami kaip str(). AppDaemon 4.5.13
            # clean_http_kwargs() filtruoja reikšmes per `v not in (None, False)`,
            # o Python'e 0.0 == False, tad skaitinis 0 tyliai dingsta iš POST ir
            # HA grąžina 400 "No state specified". String "0.0" pereina saugiai.
            soc           = self.get_float("soc")
            boiler_temp   = self.get_float("boiler_temp", default=20.0)
            inverter_temp = self.get_float("inverter_temp", default=0.0)
            pv_kw         = self.get_float("pv_power") / 1000
            house_kw      = self.get_float("house_load") / 1000
            season        = self.get_season()
            soc_min       = self.get_season_soc_min()
            storm         = self.is_storm_mode()
            boiler_state  = self.get_state(SENSOR["boiler_switch"])
            solcast_t     = self.corrected_remaining_today()
            solcast_tm    = self.corrected_tomorrow()

            # PASTABA: sensor.energy_manager_status priklauso template sensoriui
            # (configuration.yaml — rodo inverterio režimą). Sprendimo tekstas
            # publikuojamas į ATSKIRĄ entity, kad du šaltiniai nesipjautų.
            self.set_state(
                OUTPUT["decision"],
                # Būsena = paskutinis priimtas sprendimas (HA riboja iki 255 simb.)
                state=self.last_decision[:254],
                attributes={
                    "friendly_name": f"{SITE_LABEL}: energijos valdymo sprendimas",
                    "icon": "mdi:solar-power-variant",
                    "automacija": "aktyvus",
                    "strateginis_leidimas": "TAIP" if self.boiler_allowed else "NE",
                    "audros_rezimas": "AKTYVUS" if storm else "neaktyvus",
                    "sezonas": season,
                    "soc_minimumas": f"{soc_min}%",
                    "inverterio_temperatura": f"{inverter_temp:.1f}°C",
                }
            )

            self.set_state(
                OUTPUT["balance"],
                state=str(round(self.last_balance, 2)),
                attributes={
                    "friendly_name": f"{SITE_LABEL}: energijos balansas",
                    "unit_of_measurement": "kWh",
                    "icon": "mdi:scale-balance",
                    # state_class reikalingas, kad recorder kauptų ilgalaikę
                    # statistiką; device_class: energy čia negalimas (HA leidžia
                    # tik su total/total_increasing, o balansas svyruoja).
                    "state_class": "measurement",
                    "solcast_siandiena": f"{solcast_t:.1f} kWh",
                    "solcast_rytoj": f"{solcast_tm:.1f} kWh",
                }
            )

            # Momentinis perteklius (kW) — ATSKIRAS entity nuo template
            # sensor.energy_manager_surplus (likusios dienos perteklius kWh).
            self.set_state(
                OUTPUT["surplus_now"],
                state=str(round(self.last_surplus, 2)),
                attributes={
                    "friendly_name": f"{SITE_LABEL}: saulės perteklius (dabar)",
                    "unit_of_measurement": "kW",
                    "icon": "mdi:solar-panel",
                    "pv_galia": f"{pv_kw:.2f} kW",
                    "namu_apkrova": f"{house_kw:.2f} kW",
                }
            )

            self.set_state(
                OUTPUT["target_soc"],
                state=str(self.last_target_soc),
                attributes={
                    "friendly_name": f"{SITE_LABEL}: tikslinė SOC riba",
                    "unit_of_measurement": "%",
                    "icon": "mdi:battery-charging-80",
                    "device_class": "battery",
                }
            )

            self.set_state(
                OUTPUT["inverter_temp"],
                state=str(round(inverter_temp, 1)),
                attributes={
                    "friendly_name": f"{SITE_LABEL}: inverterio temperatūra",
                    "unit_of_measurement": "°C",
                    "icon": "mdi:thermometer",
                    "device_class": "temperature",
                }
            )

            self.set_state(
                OUTPUT["boiler_status"],
                state=boiler_state if boiler_state else "unknown",
                attributes={
                    "friendly_name": f"{SITE_LABEL}: boilerio valdymo būsena",
                    "icon": "mdi:water-boiler",
                    "temperatura": f"{boiler_temp:.1f}°C",
                    "tikslas": f"{self.get_boiler_temp_target():.0f}°C",
                    "strateginis_leidimas": "TAIP" if self.boiler_allowed else "NE",
                }
            )

        # ============================================================
        #  PAGALBINĖS FUNKCIJOS
        # ============================================================

        def get_float(self, sensor_name, default=0.0):
            """Gauna sensoriaus reikšmę kaip float, grąžina default jei klaida."""
            try:
                val = self.get_state(SENSOR[sensor_name])
                if val in (None, "unavailable", "unknown"):
                    return default
                return float(val)
            except (ValueError, TypeError):
                self.log(f"Klaida skaitant {sensor_name}, naudojamas default {default}")
                return default

        def get_sensor_float(self, entity_id, default=0.0):
            """Kaip get_float, bet pagal pilną entity_id."""
            value = self.get_optional_sensor_float(entity_id)
            return default if value is None else value

        def get_optional_float(self, sensor_name):
            """Grąžina None, jei kritinė telemetrija sena, nežinoma ar nebaigtinė."""
            return self.get_optional_sensor_float(
                SENSOR.get(sensor_name),
                sensor_name=sensor_name,
            )

        def _read_state_record(self, entity_id):
            """Perskaito būseną kartu su last_reported, jei AppDaemon ją pateikia."""
            if not entity_id:
                return {"state": None}
            try:
                record = self.get_state(entity_id, attribute="all")
                if isinstance(record, dict) and "state" in record:
                    return record
            except (TypeError, AttributeError):
                # Senesnė AppDaemon versija gali nepalaikyti attribute="all".
                pass
            try:
                return {"state": self.get_state(entity_id)}
            except Exception:
                return {"state": None}

        def get_optional_sensor_float(self, entity_id, sensor_name=None):
            if not entity_id:
                return None
            try:
                record = self._read_state_record(entity_id)
                raw = record.get("state")
                if raw in (None, "unavailable", "unknown", ""):
                    return None
                value = float(raw)
                if not isfinite(value):
                    return None

                # Kritiniams laukams (dabar SOC, ateityje PV/ryšio matas)
                # neleidžiame planuoti pagal seną paskutinę reikšmę.
                key = sensor_name
                if key is None:
                    key = next(
                        (
                            name for name, configured in SENSOR.items()
                            if configured == entity_id
                        ),
                        None,
                    )
                max_age = TELEMETRY_MAX_AGE_SECONDS.get(key)
                if max_age is not None:
                    # Kai reikšmė nekinta, vien last_reported gali atrodyti sena.
                    # Profilis gali nurodyti atskirą ryšio/Modbus heartbeat entity;
                    # jos timestamp naudojamas tik šviežumui, o ne kaip matavimo
                    # reikšmė. Debesijai privalomas galiojantis matavimo laikas;
                    # vietiniams profiliams išlieka entity timestamp alternatyva.
                    heartbeat_entity = TELEMETRY_HEARTBEAT.get(key)
                    heartbeat_record = (
                        self._read_state_record(heartbeat_entity)
                        if heartbeat_entity else {}
                    )
                    heartbeat_at = as_utc(heartbeat_record.get("state"))
                    reported_at = sample_timestamp(
                        record, heartbeat_record,
                        require_heartbeat=REQUIRE_HEARTBEAT,
                    )
                    freshness = assess_freshness(
                        value,
                        reported_at,
                        max_age_seconds=max_age,
                        now=datetime.now().astimezone(),
                        allow_unknown_timestamp=TELEMETRY_ALLOW_UNKNOWN_TIMESTAMP,
                    )
                    reported = as_utc(reported_at)
                    self.telemetry_status[key or entity_id] = {
                        "entity_id": entity_id,
                        "quality": freshness.quality,
                        "usable": freshness.usable,
                        "age_seconds": freshness.age_seconds,
                        "max_age_seconds": max_age,
                        "reported_at": (
                            reported.isoformat() if reported is not None else None
                        ),
                        "reported_source": (
                            "heartbeat" if heartbeat_at is not None
                            and reported_at == heartbeat_at else
                            "missing_heartbeat" if REQUIRE_HEARTBEAT else "entity"
                        ),
                        "heartbeat_entity": heartbeat_entity,
                        "reason": freshness.reason,
                    }
                    if not freshness.usable:
                        return None
                return value
            except (ValueError, TypeError):
                if sensor_name:
                    self.telemetry_status[sensor_name] = {
                        "entity_id": entity_id,
                        "quality": "invalid",
                        "usable": False,
                        "age_seconds": None,
                        "max_age_seconds": TELEMETRY_MAX_AGE_SECONDS.get(sensor_name),
                        "reported_at": None,
                        "reason": "nepavyko perskaityti skaitinės reikšmės",
                    }
                return None

        def _plan_input_changed(self, entity, attribute, old, new, kwargs):
            """Eventinis perskaičiavimas; plano deduplikacija saugo nuo churn."""
            self.compute_plan()

        def get_season(self):
            """Grąžina dabartinį sezoną."""
            season = self.get_state(SENSOR["season"])
            if season not in SEASON_SOC_MIN:
                # Auto sezonas pagal mėnesį jei input_select nepasirinktas
                month = datetime.now().month
                if month in (12, 1, 2):
                    return "žiema"
                elif month in (3, 4, 5):
                    return "pavasaris"
                elif month in (6, 7, 8):
                    return "vasara"
                else:
                    return "ruduo"
            return season

        def get_season_soc_min(self):
            """Grąžina sezono SOC minimumą %."""
            return SEASON_SOC_MIN.get(self.get_season(), 60)

        def is_storm_mode(self):
            """Tikrina ar įjungtas audros/ESO režimas."""
            return self.get_state(SENSOR["storm_mode"]) == "on"

        def get_boiler_temp_target(self):
            """Grąžina boilerio temperatūros tikslą pagal sezoną."""
            season = self.get_season()
            if season in ("žiema", "ruduo"):
                return BOILER_TEMP_WINTER
            return BOILER_TEMP_SUMMER

        def get_daily_consumption(self):
            """
            Grąžina prognozuojamą rytojaus suvartojimą kWh: namų poreikis iš
            vartojimo modelio (consumption_model.py, mokosi iš realios Solis
            istorijos) + inverterio savivartojimas (INVERTER_SELF_KW × 24 h),
            kurio Solis vartojimo sensoriai nemato.
            Jei modelio sensorius dar neprieinamas — naudoja DEFAULT_DAILY_CONSUMPTION.
            """
            base = self.get_sensor_float(
                CONSUMPTION_TOMORROW_SENSOR,
                default=DEFAULT_DAILY_CONSUMPTION,
            )
            return base + INVERTER_SELF_KW * 24

        def get_consumption_remaining_today(self):
            """
            Likęs suvartojimas šiandien kWh: namų poreikis iš vartojimo modelio
            (valandinis profilis) + inverterio savivartojimas likusioms valandoms.
            Atsarginis variantas: tolygus įvertis pagal likusias valandas.
            """
            now = datetime.now()
            hours_left = 24 - now.hour - (now.minute / 60)
            base_daily = self.get_sensor_float(
                CONSUMPTION_TOMORROW_SENSOR,
                default=DEFAULT_DAILY_CONSUMPTION,
            )
            fallback = base_daily / 24 * hours_left
            base = self.get_sensor_float(
                CONSUMPTION_REMAINING_SENSOR,
                default=round(fallback, 2),
            )
            return base + INVERTER_SELF_KW * hours_left

        # ============================================================
        #  ADAPTYVI SOLCAST KOREKCIJA
        # ============================================================

        def get_price(self, key, default):
            """Elektros kaina iš input_number. 0 — TEISĖTA reikšmė (ESO pasaugojimo
            schema: buy=0.0/sell=0.25), todėl fallback į konstantą taikomas tik kai
            entity nepasiekiamas (iki 2026-07-17 sentinel >0.001 versdavo sąmoningą
            0.0 į 0.18 ir ekonomika skaičiuota ne ta kaina)."""
            val = self.get_sensor_float(SENSOR[key], default=-1.0)
            return val if val >= 0 else default

        def compute_plan(self, kwargs=None):
            """Surinkti HA įėjimus ir perduoti juos PlantCore."""
            soc = self.get_optional_float("soc")
            manual_entity = SENSOR.get("manual_override")
            self.telemetry_checked_at = datetime.now().astimezone()

            try:
                adapter_online = self.get_state(SENSOR["soc"]) not in (
                    None, "unknown", "unavailable", ""
                )
            except Exception:
                adapter_online = False
            try:
                executor_online = self.get_state(EXECUTOR_ENTITY) == "on"
            except Exception:
                executor_online = False

            guidance = self.horizon_guidance(soc)
            planner_inputs = PlannerInput(
                storm=self.is_storm_mode(),
                manual=(
                    bool(manual_entity)
                    and self.get_state(manual_entity) == "on"
                ),
                soc=soc,
                target_soc=self.last_target_soc,
                horizon=guidance,
                in_production_hours=self.is_production_time(),
                export_floor=self.get_sensor_float(
                    SENSOR["daytime_export_floor"],
                    default=PLAN_HARD_FLOOR,
                ),
                room_shortfall_kwh=self.get_sensor_float(
                    OUTPUT["room_shortfall"],
                    default=0.0,
                ),
            )
            cycle = self.plant_core.run(
                planner_inputs,
                adapter_online=adapter_online,
                executor_online=executor_online,
                now=self.telemetry_checked_at,
            )
            self.last_coordinator_decision = cycle.decision
            self.core_state = cycle.transition.current
            self._publish_plan(cycle.plan)

        def _publish_plan(self, plan):
            """Publikuoja legacy sensorius ir vieną atominį commit sensorių."""
            self.current_plan = plan
            mode_changed = plan.mode != self.last_plan_mode
            self.last_plan_mode = plan.mode

            self.set_state(
                OUTPUT["plan_mode"], state=plan.mode,
                attributes={
                    "friendly_name": f"{SITE_LABEL}: plano režimas",
                    "priezastis": plan.reason,
                    "priority": plan.priority,
                    "actionable": ("on" if plan.actionable else "off"),
                    "icon": "mdi:brain",
                },
            )
            self.set_state(
                OUTPUT["plan_target_soc"], state=str(round(plan.target_soc, 0)),
                attributes={
                    "friendly_name": f"{SITE_LABEL}: plano tikslinis SOC",
                    "unit_of_measurement": "%",
                    "device_class": "battery",
                },
            )
            self.set_state(
                OUTPUT["plan_slot_active"],
                state=(
                    "unknown" if plan.slot_active is None
                    else ("on" if plan.slot_active else "off")
                ),
                attributes={"friendly_name": f"{SITE_LABEL}: plano slotas"},
            )
            self.set_state(
                OUTPUT["plan_slot_cutoff_soc"],
                state=(
                    str(round(plan.slot_cutoff_soc, 0))
                    if plan.slot_cutoff_soc is not None
                    else "unknown"
                ),
                attributes={
                    "friendly_name": f"{SITE_LABEL}: plano sloto cut-off",
                    "unit_of_measurement": "%",
                },
            )
            self.set_state(
                OUTPUT["plan_inverter_on"],
                state=(
                    "unknown" if plan.inverter_on is None
                    else ("on" if plan.inverter_on else "off")
                ),
                attributes={
                    "friendly_name": f"{SITE_LABEL}: inverterio pageidaujama būsena"
                },
            )

            signature = (
                plan.mode,
                round(plan.target_soc, 1),
                plan.slot_active,
                None if plan.slot_cutoff_soc is None else round(
                    plan.slot_cutoff_soc, 1
                ),
                plan.inverter_on,
                plan.actionable,
                plan.inputs_valid,
            )
            signature_changed = signature != self.last_plan_signature
            if signature_changed:
                self.plan_revision += 1
                self.last_plan_signature = signature
                self.last_plan_committed_monotonic = monotonic()
                self.plan_committed_at = datetime.now().astimezone().isoformat()

            # Sveikata publikuojama PRIEŠ atominį commit. Taip Executor,
            # gavęs plan state event po AppDaemon restarto, jau mato "ok"
            # ir neprivalo laukti 10 min. reconcile ciklo.
            health_state = (
                "ok" if plan.actionable and plan.inputs_valid
                else plan.priority
            )
            self.set_state(
                OUTPUT["planner_health"], state=health_state,
                attributes={
                    "friendly_name": f"{SITE_LABEL}: Planner sveikata",
                    "site": SITE_KEY,
                    "last_checked": datetime.now().astimezone().isoformat(),
                    "inputs_valid": ("on" if plan.inputs_valid else "off"),
                    "actionable": ("on" if plan.actionable else "off"),
                    "revision": self.plan_revision,
                    "reason": plan.reason,
                    "telemetry": dict(self.telemetry_status),
                    "core_state": self.core_state,
                    "coordinator": "Valdymo koordinatorius",
                    "rejected_proposals": len(
                        self.last_coordinator_decision.rejected
                    ) if self.last_coordinator_decision else 0,
                    "config_issues": list(self.config_issues),
                },
            )

            # Naujo branduolio diagnostikos išėjimai. Jie yra papildomi ir
            # nieko nerašo į inverterį; dashboardas gali rodyti šias būsenas
            # kiekvienai elektrinei atskirai.
            core_state_entity = OUTPUT.get("core_state")
            if core_state_entity:
                self.set_state(
                    core_state_entity,
                    state=self.core_state,
                    attributes={
                        "friendly_name": f"{SITE_LABEL}: branduolio būsena",
                        "site": SITE_KEY,
                        "coordinator": "Valdymo koordinatorius",
                        "reason": plan.reason,
                        "last_checked": datetime.now().astimezone().isoformat(),
                    },
                )
            telemetry_entity = OUTPUT.get("telemetry_health")
            if telemetry_entity:
                required = [
                    self.telemetry_status.get(key)
                    for key in TELEMETRY_REQUIRED
                ]
                telemetry_state = (
                    "ok"
                    if required and all(item and item.get("usable") for item in required)
                    else "degraded"
                )
                self.set_state(
                    telemetry_entity,
                    state=telemetry_state,
                    attributes={
                        "friendly_name": f"{SITE_LABEL}: telemetrijos sveikata",
                        "site": SITE_KEY,
                        "required": list(TELEMETRY_REQUIRED),
                        "fields": dict(self.telemetry_status),
                        "last_checked": datetime.now().astimezone().isoformat(),
                    },
                )
            coordinator_entity = OUTPUT.get("coordinator")
            if coordinator_entity:
                selected = (
                    self.last_coordinator_decision.selected.module
                    if self.last_coordinator_decision
                    and self.last_coordinator_decision.selected is not None
                    else "hold"
                )
                self.set_state(
                    coordinator_entity,
                    state=selected,
                    attributes={
                        "friendly_name": f"{SITE_LABEL}: Valdymo koordinatorius",
                        "site": SITE_KEY,
                        "status": (
                            self.last_coordinator_decision.status
                            if self.last_coordinator_decision
                            else "starting"
                        ),
                        "rejected_proposals": len(
                            self.last_coordinator_decision.rejected
                        ) if self.last_coordinator_decision else 0,
                        "last_checked": datetime.now().astimezone().isoformat(),
                    },
                )

            # Publish the current explanation and a finite source lease even
            # when actuator values did not change. Revision changes only for a
            # different physical target; this is not an inverter command.
            self.set_state(
                OUTPUT["plan"], state=plan.mode,
                attributes={
                    "friendly_name": f"{SITE_LABEL}: atominis energijos planas",
                    "site": SITE_KEY,
                    "revision": self.plan_revision,
                    "target_soc": round(plan.target_soc, 1),
                    "slot_active": (
                        "unknown" if plan.slot_active is None
                        else ("on" if plan.slot_active else "off")
                    ),
                    "slot_cutoff_soc": (
                        "unknown" if plan.slot_cutoff_soc is None
                        else round(plan.slot_cutoff_soc, 1)
                    ),
                    "inverter_on": (
                        "unknown" if plan.inverter_on is None
                        else ("on" if plan.inverter_on else "off")
                    ),
                    "inverter_control_available": (
                        "on" if INVERTER_CONTROL_AVAILABLE else "off"
                    ),
                    "actionable": ("on" if plan.actionable else "off"),
                    "inputs_valid": ("on" if plan.inputs_valid else "off"),
                    "priority": plan.priority,
                    "reason": plan.reason,
                    "telemetry": dict(self.telemetry_status),
                    "core_state": self.core_state,
                    "coordinator": "Valdymo koordinatorius",
                    "config_issues": list(self.config_issues),
                    "committed_at": self.plan_committed_at,
                    "evaluated_at": datetime.now().astimezone().isoformat(),
                    "valid_until": (datetime.now().astimezone() + timedelta(seconds=180)).isoformat(),
                },
            )
            if signature_changed:
                self.log(
                    f"[PLAN:{SITE_KEY}] rev={self.plan_revision} "
                    f"{plan.mode}: {plan.reason}"
                )
            elif mode_changed:
                self.log(f"[PLAN:{SITE_KEY}] {plan.mode}: {plan.reason}")

        def send_notification(self, message, title="Energijos valdymas"):
            """
            Siunčia pranešimą į HA persistent notifications (notify.telegram
            nesukonfigūruotas — anksčiau visi pranešimai krisdavo į exception).
            Atsiradus Telegram/mobile_app — pakeisti servisą čia.
            """
            try:
                self.call_service(
                    "notify/persistent_notification",
                    title=title,
                    message=message
                )
            except Exception as e:
                self.log(f"Pranešimo klaida: {e}", level="WARNING")

        def notify_power_outage(self):
            """Pranešimas kai dingsta elektra."""
            self.send_notification(
                "⚡ DINGO ELEKTRA! Sistema veikia iš kaupiklio.",
                title="Elektros gedimas"
            )

        def notify_storm_mode_activated(self):
            """Pranešimas kai aktyvuojamas audros režimas."""
            soc = self.get_float("soc")
            self.send_notification(
                f"🌩️ AUDROS režimas įjungtas. Kaupiklio lygis: {soc:.0f}%\n"
                f"Boileris išjungtas. Kraunama iki 100%.",
                title="Audros režimas"
            )

    return EnergyManager
