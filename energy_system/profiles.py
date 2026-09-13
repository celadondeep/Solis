"""Vienintelė abiejų elektrinių konfigūracijos vieta."""

from __future__ import annotations

from pathlib import Path


APPS_DIR = Path(__file__).resolve().parent.parent

_COMMON_ENERGY = {
    "BOILER_POWER_KW": 2.2,
    "ESO_EXPORT_LIMIT_KW": 1.0,
    "CONS_BASE_KW": 0.3,
    "PLAN_MARGIN": 1.25,
    "PLAN_TARGET_BAND_FLOOR": 20,
    "PLAN_TARGET_BAND_CEILING": 85,
    "INVERTER_SELF_KW": 0.13,
    "INVERTER_IDLE_W": 130.0,
    "INVERTER_OFF_W": 30.0,
    "PRICE_BUY_DEFAULT": 0.18,
    "PRICE_SELL_DEFAULT": 0.08,
    "CORRECTION_ALPHA": 0.2,
    "CORRECTION_MIN": 0.7,
    "CORRECTION_MAX": 1.3,
    "HOURLY_MIN_DAYS": 7,
    "HOURLY_FC_MIN_KWH": 0.05,
    "HOURLY_RATIO_MIN": 0.3,
    "HOURLY_RATIO_MAX": 2.0,
    "HOURLY_FACTOR_MIN": 0.4,
    "HOURLY_FACTOR_MAX": 1.6,
    "MORNING_PV_THRESHOLD_KW": 0.1,
    "MORNING_ON_MARGIN_MIN": 30,
    "BOILER_TEMP_MAX": 75.0,
    "BOILER_TEMP_WINTER": 70.0,
    "BOILER_TEMP_SUMMER": 55.0,
    "BALANCE_MIN_KWH": 0.0,
    "SOC_HIGH_THRESHOLD": 90.0,
    "SOC_TARGET_CHARGE": 95.0,
    "SOC_DROP_RATE_MAX": 2.0,
    "SURPLUS_MIN_KW": 0.3,
    "DEFAULT_DAILY_CONSUMPTION": 13.0,
    # Forward model settings. Per-site overrides can replace this mapping.
    "HORIZON": {"comfort_soc": 20.0, "reserve_margin_kwh": 0.5,
                "storage_ceiling": 95.0, "charge_kw": 3.0,
                "discharge_kw": 3.0, "burst_kwh": 0.75},
}


def _energy_profile(**overrides):
    profile = dict(_COMMON_ENERGY)
    profile.update(overrides)
    return profile


HOME_ENERGY = _energy_profile(
    KEY="home",
    SITE_LABEL="Namai",
    SOC_BUFFER={
        "min": "number.solis_s6_eh3p_overdischarge_soc",
        "max": "number.solis_s6_eh3p_max_charge_soc",
        "heartbeat": "sensor.solis_s6_eh3p_last_modbus_success",
        "max_age": 180,
        "voltages": ["sensor.solis_s6_eh3p_meter_ac_voltage_" + p for p in "abc"],
        "frequency": "sensor.solis_s6_eh3p_grid_frequency",
        "charge_current": "sensor.solis_s6_eh3p_battery_charge_current_limitation_bms",
        "battery_voltage": "sensor.solis_s6_eh3p_battery_voltage",
    },
    # Read-only capacity model: 160 A BMS at >=40 V; no device register writes.
    HORIZON={**_COMMON_ENERGY["HORIZON"], "charge_kw":6.4},
    BATTERY_USABLE_KWH=14.4,
    KWH_PER_SOC=14.4 / 90.0,
    # Gyvai patvirtintas mažiausias stabilus TOU cut-off; apsaugos registro neliečia.
    MIN_SOC=13,
    PLAN_HARD_FLOOR=13,
    NIGHT_REST_SOC=16,
    FORECAST_SOURCE_LABEL="Solcast seskiniu_2 (kalibruota Namų elektrinei)",
    CORRECTION_FILE=str(APPS_DIR / "forecast_correction.json"),
    TARGET_SOC_FILE=str(APPS_DIR / "target_soc.json"),
    TACTICAL_INTERVAL=10,
    CORE_VERSION="4.2-soc-buffer",
    CORE_NAME="Valdymo koordinatorius",
    TELEMETRY_MAX_AGE_SECONDS={"soc": 600, "pv_power": 900},
    # Modbus heartbeat atnaujina net nekintant SOC reikšmei.
    TELEMETRY_HEARTBEAT={"soc": "sensor.solis_s6_eh3p_last_modbus_success"},
    TELEMETRY_REQUIRED=("soc",),
    TELEMETRY_ALLOW_UNKNOWN_TIMESTAMP=True,
    MODULES_ENABLED=("planner", "forecast", "consumption", "battery_health", "safety"),
    DASHBOARD_VISIBILITY_DEFAULT={"overview": True, "forecast": True, "diagnostics": True, "boiler": True},
    BOILER_ENABLED=True,
    BATTERY_DISCHARGE_SIGN=1.0,
    INVERTER_CONTROL_AVAILABLE=True,
    SEASON_SOC_MIN={"žiema": 80, "pavasaris": 60, "vasara": 10, "ruduo": 60},
    CONSUMPTION_TOMORROW_SENSOR="sensor.consumption_forecast_tomorrow",
    CONSUMPTION_REMAINING_SENSOR="sensor.consumption_remaining_today",
    SENSOR={
        "solcast_today": "sensor.solcast_pv_forecast_forecast_remaining_today",
        "solcast_tomorrow": "sensor.solcast_pv_forecast_forecast_tomorrow",
        "soc": "sensor.solis_s6_eh3p_battery_soc",
        "pv_power": "sensor.solis_s6_eh3p_total_pv_power",
        "house_load": "sensor.solis_s6_eh3p_household_load_power",
        "grid_power": "sensor.solis_s6_eh3p_grid_power_net",
        "export_limit": "number.solis_s6_eh3p_backflow_power",
        "boiler_temp": "sensor.boiler_temperature",
        "boiler_switch": "switch.boiler_switch",
        "season": "input_select.energy_season",
        "storm_mode": "input_boolean.storm_mode",
        "manual_override": "input_boolean.manual_tou_override",
        "inverter_temp": "sensor.solis_s6_eh3p_temperature",
        "pv_today": "sensor.solis_s6_eh3p_pv_today_energy_generation",
        "consumption_today": "sensor.solis_s6_eh3p_today_energy_consumption",
        "solcast_today_total": "sensor.solcast_pv_forecast_forecast_today",
        "battery_power": "sensor.solis_s6_eh3p_battery_power_net",
        "power_state": "switch.solis_s6_eh3p_power_state",
        "price_buy": "input_number.electricity_price_buy",
        "price_sell": "input_number.electricity_price_sell",
        "daytime_export_floor": "input_number.daytime_export_floor",
        "consumption_profile": "sensor.consumption_profile",
        "intraday_ratio": "sensor.solcast_intraday_ratio",
        "sun": "sun.sun",
    },
    ACTUATOR={
        "mode": "select.solis_s6_eh3p_work_mode",
        "mode_by_plan": {
            "self_use": "Self-Use",
            "feed_in": "Feed-in Priority",
            "night_export": "Feed-in Priority",
        },
        # S6-EH su aktyviu TOU slotu readback'e prideda „+ TOU“.
        "mode_aliases": {
            "Self-Use": ("Self-Use + TOU",),
            "Feed-in Priority": ("Feed-in Priority + TOU",),
        },
        "slot": "switch.solis_s6_eh3p_grid_time_of_use_discharge_period_1",
        "slot_cutoff": (
            "number.solis_s6_eh3p_grid_time_of_use_discharge_cut_off_soc_slot_1"
        ),
        # S6-EH sveiko procento readback kvantavimo paklaida; >1 % vis dar klaida.
        "slot_cutoff_tolerance": 1.0,
        # Tik 1-as slotas priklauso branduoliui; likę privalo būti išjungti.
        "exclusive_off": tuple(
            f"switch.solis_s6_eh3p_grid_time_of_use_discharge_period_{slot}"
            for slot in range(2, 7)
        ),
        "power": "switch.solis_s6_eh3p_power_state",
    },
    EXECUTOR_ENTITY="automation.solis_planas_bazinio_rezimo_vykdymas",
    EXECUTOR_SETTLE_SECONDS=45,
    OUTPUT={
        "decision": "sensor.energy_manager_decision",
        "balance": "sensor.energy_manager_balance",
        "surplus_now": "sensor.energy_manager_surplus_now",
        "target_soc": "sensor.energy_manager_target_soc",
        "inverter_temp": "sensor.energy_manager_inverter_temp",
        "boiler_status": "sensor.energy_manager_boiler_status",
        "room_shortfall": "sensor.energy_manager_room_shortfall",
        "night_economics": "sensor.inverter_night_economics",
        "shutdown_eta": "sensor.inverter_shutdown_eta",
        "morning_on_time": "sensor.inverter_morning_on_time",
        "cons_until_production": "sensor.energy_manager_cons_until_production",
        "correction_factor": "sensor.solcast_correction_factor",
        "plan_mode": "sensor.solis_plan_mode",
        "plan_target_soc": "sensor.solis_plan_target_soc",
        "plan_slot_active": "sensor.solis_plan_slot_active",
        "plan_slot_cutoff_soc": "sensor.solis_plan_slot_cutoff_soc",
        "plan_inverter_on": "sensor.solis_plan_inverter_on",
        "plan": "sensor.solis_plan",
        "horizon": "sensor.solis_energy_horizon",
        "planner_health": "sensor.solis_planner_health",
        "executor_health": "sensor.solis_executor_health",
        "core_state": "sensor.solis_core_state",
        "telemetry_health": "sensor.solis_telemetry_health",
        "coordinator": "sensor.solis_control_coordinator",
    },
    SHADOW_OUTPUT={
        "plan": "sensor.solis_shadow_plan",
        "check": "sensor.solis_shadow_check",
        "commands": "sensor.solis_shadow_commands",
    },
)

EIMO_ENERGY = _energy_profile(
    KEY="eimo",
    CONSUMPTION_TODAY_IS_TOTAL=True,
    SITE_LABEL="Eimo",
    SOC_BUFFER={
        "min": "number.solis_inverter_1033300254190112_battery_over_discharge_soc",
        "max": "number.solis_inverter_1033300254190112_battery_max_charge_soc",
        "heartbeat": "sensor.solis_inverter_1033300254190112_solis_timestamp_measurements_received",
        "max_age": 900,
        "voltages": ["sensor.solis_inverter_1033300254190112_solis_meter_item_" + p + "_volt" for p in "abc"],
        "frequency": "sensor.solis_inverter_1033300254190112_solis_ac_frequency",
    },
    BATTERY_USABLE_KWH=13.619,
    KWH_PER_SOC=13.619 / 95.0,
    MIN_SOC=5,
    PLAN_HARD_FLOOR=6,
    NIGHT_REST_SOC=6,
    FORECAST_SOURCE_LABEL=(
        "Solcast seskiniu_2 orų profilis + atskira Eimo realizacijos korekcija"
    ),
    CORRECTION_FILE=str(APPS_DIR / "forecast_correction_eimo.json"),
    TARGET_SOC_FILE=str(APPS_DIR / "target_soc_eimo.json"),
    TACTICAL_INTERVAL=60,
    CORE_VERSION="4.2-soc-buffer",
    CORE_NAME="Valdymo koordinatorius",
    TELEMETRY_MAX_AGE_SECONDS={"soc": 900, "pv_power": 900},
    # SOC gali nekisti valandomis. Šviežumą įrodo debesijos matavimo laikas,
    # ne HA last_updated ir ne sėkminga seno debesijos atsakymo apklausa.
    TELEMETRY_HEARTBEAT={
        key: "sensor.solis_inverter_1033300254190112_solis_timestamp_measurements_received"
        for key in ("soc", "pv_power")
    },
    TELEMETRY_REQUIRE_HEARTBEAT=True,
    TELEMETRY_REQUIRED=("soc",),
    TELEMETRY_ALLOW_UNKNOWN_TIMESTAMP=False,
    MODULES_ENABLED=("planner", "forecast", "consumption", "battery_health", "safety"),
    DASHBOARD_VISIBILITY_DEFAULT={"overview": True, "forecast": True, "diagnostics": True, "boiler": False},
    BOILER_ENABLED=False,
    BATTERY_DISCHARGE_SIGN=-1.0,
    INVERTER_CONTROL_AVAILABLE=True,  # CID 5162 OFF/ON readbacks verified 2026-09-12.
    COMMAND_STATUS_ENTITY="sensor.solis_inverter_1033300254190112_cloud_command_status",
    SEASON_SOC_MIN={"žiema": 80, "pavasaris": 60, "vasara": 5, "ruduo": 60},
    CONSUMPTION_TOMORROW_SENSOR="sensor.consumption_forecast_tomorrow_eimo",
    CONSUMPTION_REMAINING_SENSOR="sensor.consumption_remaining_today_eimo",
    SENSOR={
        "solcast_today": "sensor.solcast_pv_forecast_forecast_remaining_today",
        "solcast_tomorrow": "sensor.solcast_pv_forecast_forecast_tomorrow",
        "soc": "sensor.solis_inverter_1033300254190112_solis_remaining_battery_capacity",
        "pv_power": "sensor.eimo_pv_power",
        "house_load": "sensor.solis_inverter_1033300254190112_solis_total_consumption_power",
        "grid_power": "sensor.solis_inverter_1033300254190112_solis_power_grid_total_power",
        "boiler_temp": "sensor.boiler_temperature_eimo",
        "boiler_switch": "switch.boiler_switch_eimo",
        "season": "input_select.energy_season_eimo",
        "storm_mode": "input_boolean.storm_mode_eimo",
        "manual_override": None,
        "inverter_temp": "sensor.solis_inverter_1033300254190112_solis_temperature",
        "pv_today": "sensor.solis_inverter_1033300254190112_solis_energy_today",
        "consumption_today": "sensor.eimo_house_energy",
        "solcast_today_total": "sensor.solcast_pv_forecast_forecast_today",
        "battery_power": "sensor.solis_inverter_1033300254190112_solis_battery_power",
        "power_state": "switch.solis_inverter_1033300254190112_inverter_on_off",
        "price_buy": "input_number.electricity_price_buy_eimo",
        "price_sell": "input_number.electricity_price_sell_eimo",
        "daytime_export_floor": "input_number.daytime_export_floor_eimo",
        "consumption_profile": "sensor.consumption_profile_eimo",
        "intraday_ratio": "sensor.solcast_intraday_ratio_eimo",
        "sun": "sun.sun",
    },
    ACTUATOR={
        "mode": "select.solis_inverter_1033300254190112_storage_mode",
        "mode_by_plan": {
            "self_use": "Self-Use",
            "feed_in": "Feed-In Priority",
            "night_export": "Feed-In Priority",
        },
        "slot": "switch.solis_inverter_1033300254190112_slot1_discharge",
        "slot_cutoff": (
            "number.solis_inverter_1033300254190112_slot1_discharge_soc"
        ),
        # Eimo taip pat naudoja sveikų procentų SOC nustatymą.
        "slot_cutoff_tolerance": 1.0,
        # Integracijos eilė tikrina visų paslėptų 2–6 įkrovimo ir iškrovimo slotų OFF.
        "exclusive_off": (),
        "power": "switch.solis_inverter_1033300254190112_inverter_on_off",
    },
    EXECUTOR_ENTITY="automation.eimo_planas_bazinio_rezimo_vykdymas",
    EXECUTOR_SETTLE_SECONDS=420,
    OUTPUT={
        "decision": "sensor.energy_manager_eimo_decision",
        "balance": "sensor.energy_manager_eimo_balance",
        "surplus_now": "sensor.energy_manager_eimo_surplus_now",
        "target_soc": "sensor.energy_manager_eimo_target_soc",
        "inverter_temp": "sensor.energy_manager_eimo_inverter_temp",
        "boiler_status": "sensor.energy_manager_eimo_boiler_status",
        "room_shortfall": "sensor.energy_manager_eimo_room_shortfall",
        "night_economics": "sensor.inverter_night_economics_eimo",
        "shutdown_eta": "sensor.inverter_shutdown_eta_eimo",
        "morning_on_time": "sensor.inverter_morning_on_time_eimo",
        "cons_until_production": "sensor.energy_manager_eimo_cons_until_production",
        "correction_factor": "sensor.solcast_correction_factor_eimo",
        "plan_mode": "sensor.eimo_plan_mode",
        "plan_target_soc": "sensor.eimo_plan_target_soc",
        "plan_slot_active": "sensor.eimo_plan_slot_active",
        "plan_slot_cutoff_soc": "sensor.eimo_plan_slot_cutoff_soc",
        "plan_inverter_on": "sensor.eimo_plan_inverter_on",
        "plan": "sensor.eimo_plan",
        "horizon": "sensor.eimo_energy_horizon",
        "planner_health": "sensor.eimo_planner_health",
        "executor_health": "sensor.eimo_executor_health",
        "core_state": "sensor.eimo_core_state",
        "telemetry_health": "sensor.eimo_telemetry_health",
        "coordinator": "sensor.eimo_control_coordinator",
    },
    SHADOW_OUTPUT={
        "plan": "sensor.eimo_shadow_plan",
        "check": "sensor.eimo_shadow_check",
        "commands": "sensor.eimo_shadow_commands",
    },
)


_COMMON_CONSUMPTION = {
    "HISTORY_MAX_DAYS": 120,
    "HISTORY_MIN_DAYS": 5,
    "MEDIAN_WINDOW_DAYS": 14,
    "PROFILE_ALPHA": 0.10,
    "PROFILE_MIN_TOTAL": 1.0,
    "PROFILE_MIN_HOURS": 20,
    "DEFAULT_DAILY_KWH": 15.0,
    "DEFAULT_WEEKDAY_FACTORS": {
        "0": 1.15, "1": 1.10, "2": 1.10, "3": 1.10,
        "4": 1.05, "5": 0.85, "6": 0.90,
    },
    "DEFAULT_SEASON_FACTORS": {
        "žiema": 1.35, "pavasaris": 0.95, "vasara": 0.80, "ruduo": 1.05,
    },
}


def _weather_url(latitude, longitude):
    return (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={latitude}&longitude={longitude}"
        "&daily=temperature_2m_mean,temperature_2m_max"
        "&past_days=92&forecast_days=2&timezone=Europe%2FVilnius"
    )


def _consumption_profile(**overrides):
    profile = dict(_COMMON_CONSUMPTION)
    profile.update(overrides)
    return profile


HOME_CONSUMPTION = _consumption_profile(
    KEY="home",
    SITE_LABEL="Namai",
    SOC_BUFFER={
        "min": "number.solis_s6_eh3p_overdischarge_soc",
        "max": "number.solis_s6_eh3p_max_charge_soc",
        "heartbeat": "sensor.solis_s6_eh3p_last_modbus_success",
        "max_age": 180,
        "voltages": ["sensor.solis_s6_eh3p_meter_ac_voltage_" + p for p in "abc"],
        "frequency": "sensor.solis_s6_eh3p_grid_frequency",
        "charge_current": "sensor.solis_s6_eh3p_battery_charge_current_limitation_bms",
        "battery_voltage": "sensor.solis_s6_eh3p_battery_voltage",
    },
    WEATHER_URL=_weather_url(54.45612, 23.02449),
    ESO_CSV_FILE=str(APPS_DIR / "eso_data.csv"),
    MODEL_FILE=str(APPS_DIR / "consumption_model.json"),
    MANUAL_EVENT="CONSUMPTION_MODEL_RUN",
    SENSOR={
        "house_load": "sensor.solis_s6_eh3p_household_load_power",
        "daily_consumption": "sensor.solis_s6_eh3p_yesterday_energy_consumption",
        "today_consumption": "sensor.solis_s6_eh3p_today_energy_consumption",
        "season": "input_select.energy_season",
    },
    OUTPUT={
        "remaining": "sensor.consumption_remaining_today",
        "tomorrow": "sensor.consumption_forecast_tomorrow",
        "daily_avg": "sensor.consumption_daily_avg",
        "profile": "sensor.consumption_profile",
    },
)

EIMO_CONSUMPTION = _consumption_profile(
    KEY="eimo",
    SITE_LABEL="Eimo",
    SOC_BUFFER={
        "min": "number.solis_inverter_1033300254190112_battery_over_discharge_soc",
        "max": "number.solis_inverter_1033300254190112_battery_max_charge_soc",
        "heartbeat": "sensor.solis_inverter_1033300254190112_solis_timestamp_measurements_received",
        "max_age": 900,
        "voltages": ["sensor.solis_inverter_1033300254190112_solis_meter_item_" + p + "_volt" for p in "abc"],
        "frequency": "sensor.solis_inverter_1033300254190112_solis_ac_frequency",
    },
    WEATHER_URL=_weather_url(54.46831, 22.92079),
    ESO_CSV_FILE=str(APPS_DIR / "eso_data_eimo.csv"),
    MODEL_FILE=str(APPS_DIR / "consumption_model_eimo.json"),
    MANUAL_EVENT="CONSUMPTION_MODEL_RUN_EIMO",
    SENSOR={
        "house_load": "sensor.solis_inverter_1033300254190112_solis_total_consumption_power",
        "daily_consumption": "sensor.eimo_consumption_yesterday",
        "today_consumption": "sensor.eimo_house_energy",
        "season": "input_select.energy_season_eimo",
    },
    OUTPUT={
        "remaining": "sensor.consumption_remaining_today_eimo",
        "tomorrow": "sensor.consumption_forecast_tomorrow_eimo",
        "daily_avg": "sensor.consumption_daily_avg_eimo",
        "profile": "sensor.consumption_profile_eimo",
    },
)
