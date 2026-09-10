"""Single configuration source for the read-only power-quality adapters."""

from __future__ import annotations

from pathlib import Path

APPS_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = APPS_DIR / "data"


HOME_POWER_QUALITY = {
    "key": "home",
    "label": "Namai",
    "sample_interval_seconds": 60,
    "publish_interval_seconds": 300,
    "max_source_age_seconds": 180,
    "history_days": 14,
    "history_file": str(DATA_DIR / "power_quality_home.json"),
    "heartbeat": "sensor.solis_s6_eh3p_last_modbus_success",
    "heartbeat_kind": "iso_datetime",
    "append_only_on_new_heartbeat": False,
    "entities": {
        "pcc_voltage_v": [
            "sensor.solis_s6_eh3p_meter_ac_voltage_a",
            "sensor.solis_s6_eh3p_meter_ac_voltage_b",
            "sensor.solis_s6_eh3p_meter_ac_voltage_c",
        ],
        "inverter_voltage_v": [
            "sensor.solis_s6_eh3p_a_phase_voltage",
            "sensor.solis_s6_eh3p_b_phase_voltage",
            "sensor.solis_s6_eh3p_c_phase_voltage",
        ],
        "pcc_active_w": [
            "sensor.solis_s6_eh3p_meter_active_power_a",
            "sensor.solis_s6_eh3p_meter_active_power_b",
            "sensor.solis_s6_eh3p_meter_active_power_c",
        ],
        "pcc_reactive_var": [
            "sensor.solis_s6_eh3p_meter_reactive_power_a",
            "sensor.solis_s6_eh3p_meter_reactive_power_b",
            "sensor.solis_s6_eh3p_meter_reactive_power_c",
        ],
        "pcc_apparent_va": [
            "sensor.solis_s6_eh3p_meter_apparent_power_a",
            "sensor.solis_s6_eh3p_meter_apparent_power_b",
            "sensor.solis_s6_eh3p_meter_apparent_power_c",
        ],
        "inverter_current_a": [
            "sensor.solis_s6_eh3p_a_phase_current",
            "sensor.solis_s6_eh3p_b_phase_current",
            "sensor.solis_s6_eh3p_c_phase_current",
        ],
        "frequency_hz": "sensor.solis_s6_eh3p_grid_frequency",
    },
    "outputs": {
        "state": "sensor.solis_power_quality_state",
        "voltage_min": "sensor.solis_power_quality_voltage_min",
        "voltage_max": "sensor.solis_power_quality_voltage_max",
        "voltage_spread": "sensor.solis_power_quality_voltage_spread",
        "active_power": "sensor.solis_power_quality_active_power",
        "reactive_power": "sensor.solis_power_quality_reactive_power",
        "power_factor": "sensor.solis_power_quality_power_factor",
        "frequency": "sensor.solis_power_quality_frequency",
        "voltage_rise": "sensor.solis_power_quality_voltage_rise",
        "active_imbalance": "sensor.solis_power_quality_active_imbalance",
        "current_imbalance": "sensor.solis_power_quality_current_imbalance",
    },
}


EIMO_POWER_QUALITY = {
    "key": "eimo",
    "label": "Eimo",
    "sample_interval_seconds": 60,
    "publish_interval_seconds": 300,
    "max_source_age_seconds": 1200,
    "history_days": 14,
    "history_file": str(DATA_DIR / "power_quality_eimo.json"),
    "heartbeat": "sensor.solis_inverter_1033300254190112_solis_timestamp_measurements_received",
    "heartbeat_kind": "unix_seconds",
    "append_only_on_new_heartbeat": True,
    "entities": {
        "pcc_voltage_v": [
            "sensor.solis_inverter_1033300254190112_solis_meter_item_a_volt",
            "sensor.solis_inverter_1033300254190112_solis_meter_item_b_volt",
            "sensor.solis_inverter_1033300254190112_solis_meter_item_c_volt",
        ],
        "inverter_voltage_v": [
            "sensor.solis_inverter_1033300254190112_solis_ac_voltage_r",
            "sensor.solis_inverter_1033300254190112_solis_ac_voltage_s",
            "sensor.solis_inverter_1033300254190112_solis_ac_voltage_t",
        ],
        "pcc_active_w": [
            "sensor.solis_inverter_1033300254190112_solis_grid_phase1_power",
            "sensor.solis_inverter_1033300254190112_solis_grid_phase2_power",
            "sensor.solis_inverter_1033300254190112_solis_grid_phase3_power",
        ],
        "pcc_reactive_var": [
            "sensor.solis_inverter_1033300254190112_solis_grid_phase1_reactive_power",
            "sensor.solis_inverter_1033300254190112_solis_grid_phase2_reactive_power",
            "sensor.solis_inverter_1033300254190112_solis_grid_phase3_reactive_power",
        ],
        "pcc_apparent_va": [
            "sensor.solis_inverter_1033300254190112_solis_grid_phase1_apparent_power",
            "sensor.solis_inverter_1033300254190112_solis_grid_phase2_apparent_power",
            "sensor.solis_inverter_1033300254190112_solis_grid_phase3_apparent_power",
        ],
        "inverter_current_a": [
            "sensor.solis_inverter_1033300254190112_solis_ac_current_r",
            "sensor.solis_inverter_1033300254190112_solis_ac_current_s",
            "sensor.solis_inverter_1033300254190112_solis_ac_current_t",
        ],
        "frequency_hz": "sensor.solis_inverter_1033300254190112_solis_ac_frequency",
    },
    "outputs": {
        "state": "sensor.eimo_power_quality_state",
        "voltage_min": "sensor.eimo_power_quality_voltage_min",
        "voltage_max": "sensor.eimo_power_quality_voltage_max",
        "voltage_spread": "sensor.eimo_power_quality_voltage_spread",
        "active_power": "sensor.eimo_power_quality_active_power",
        "reactive_power": "sensor.eimo_power_quality_reactive_power",
        "power_factor": "sensor.eimo_power_quality_power_factor",
        "frequency": "sensor.eimo_power_quality_frequency",
        "voltage_rise": "sensor.eimo_power_quality_voltage_rise",
        "active_imbalance": "sensor.eimo_power_quality_active_imbalance",
        "current_imbalance": "sensor.eimo_power_quality_current_imbalance",
    },
}


POWER_QUALITY_PROFILES = {
    "home": HOME_POWER_QUALITY,
    "eimo": EIMO_POWER_QUALITY,
}
