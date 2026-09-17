"""Read-only AppDaemon adapter for the shared power-quality module."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from time import monotonic

import appdaemon.plugins.hass.hassapi as hass

from energy_system.power_quality import (
    PowerQualityInput,
    analyze_history,
    evaluate_power_quality,
)
from energy_system.power_quality_profiles import POWER_QUALITY_PROFILES


ISSUE_TEXT = {
    "telemetry_stale": "telemetrija pasenusi",
    "missing_phase_voltage": "trūksta fazių įtampų",
    "voltage_critical_low": "kritiškai žema įtampa",
    "voltage_warning_low": "per žema įtampa",
    "voltage_watch_low": "artėjama prie žemos įtampos ribos",
    "voltage_critical_high": "kritiškai aukšta įtampa",
    "voltage_warning_high": "per aukšta įtampa",
    "voltage_watch_high": "artėjama prie aukštos įtampos ribos",
    "phase_magnitude_spread_critical": "kritinis fazinių įtampų dydžių išsiskyrimas",
    "phase_magnitude_spread_warning": "didelis fazinių įtampų dydžių išsiskyrimas",
    "phase_magnitude_spread_watch": "padidėjęs fazinių įtampų dydžių išsiskyrimas",
    "frequency_critical": "kritinis dažnio nuokrypis",
    "frequency_warning": "dažnio nuokrypis",
    "voltage_rise_critical": "kritinis inverterio–PCC įtampos skirtumas",
    "voltage_rise_warning": "didelis inverterio–PCC įtampos skirtumas",
    "voltage_rise_watch": "padidėjęs inverterio–PCC įtampos skirtumas",
    "pcc_active_imbalance_warning": "didelis PCC aktyviosios galios disbalansas",
    "pcc_active_imbalance_watch": "padidėjęs PCC aktyviosios galios disbalansas",
    "inverter_current_imbalance_warning": "didelis inverterio srovių netolygumas",
    "inverter_current_imbalance_watch": "padidėjęs inverterio srovių netolygumas",
    "power_factor_warning": "žemas apskaičiuotas galios faktorius",
    "power_factor_watch": "stebėtinas apskaičiuotas galios faktorius",
    "reactive_ratio_warning": "didelė reaktyviosios galios dalis",
    "reactive_ratio_watch": "padidėjusi reaktyviosios galios dalis",
}


def _parse_time(value):
    if value in (None, "", "unknown", "unavailable"):
        return None
    try:
        number = float(value)
        if number > 1_000_000_000:
            return datetime.fromtimestamp(number, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _state_string(value, digits=2):
    if value is None:
        return "unknown"
    return str(round(float(value), digits))


class PowerQualityManager(hass.Hass):
    """Observes one plant and publishes compact diagnostics; never controls it."""

    def initialize(self):
        site = str(self.args.get("site", "")).strip().lower()
        if site not in POWER_QUALITY_PROFILES:
            self.log(f"Nežinomas power-quality profilis: {site!r}", level="ERROR")
            return
        self.profile = POWER_QUALITY_PROFILES[site]
        self.site = site
        self.label = self.profile["label"]
        self.history_path = Path(self.profile["history_file"])
        self.history = self._load_history()
        self.last_heartbeat = None
        self.last_snapshot = None
        self.last_publish_at = 0.0
        self.samples_since_save = 0
        self.run_every(
            self.sample,
            "now+5",
            int(self.profile["sample_interval_seconds"]),
        )
        self.log(
            f"[{self.label}] Tinklo kokybės stebėsena paleista "
            "(tik skaitymas, inverterio valdymas išjungtas)."
        )

    def terminate(self):
        self._save_history()

    def _load_history(self):
        try:
            payload = json.loads(self.history_path.read_text(encoding="utf-8"))
            samples = payload.get("samples", []) if isinstance(payload, dict) else []
            return [sample for sample in samples if isinstance(sample, dict)]
        except FileNotFoundError:
            return []
        except Exception as err:
            self.log(f"[{self.label}] Nepavyko įkelti kokybės istorijos: {err}", level="WARNING")
            return []

    def _save_history(self):
        if not hasattr(self, "history_path"):
            return
        try:
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.history_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps({"version": 1, "samples": self.history}, separators=(",", ":")),
                encoding="utf-8",
            )
            temporary.replace(self.history_path)
            self.samples_since_save = 0
        except Exception as err:
            self.log(f"[{self.label}] Nepavyko išsaugoti kokybės istorijos: {err}", level="ERROR")

    def _heartbeat(self):
        value = self.get_state(self.profile["heartbeat"])
        stamp = _parse_time(value)
        now = datetime.now(timezone.utc)
        age = max(0.0, (now - stamp).total_seconds()) if stamp else None
        return value, age

    def _values(self, key):
        entities = self.profile["entities"].get(key, [])
        if isinstance(entities, str):
            return self.get_state(entities)
        return [self.get_state(entity) for entity in entities]

    def sample(self, kwargs=None):
        heartbeat, age = self._heartbeat()
        stale = age is None or age > float(self.profile["max_source_age_seconds"])
        observation = PowerQualityInput(
            pcc_voltage_v=self._values("pcc_voltage_v"),
            inverter_voltage_v=self._values("inverter_voltage_v"),
            pcc_active_w=self._values("pcc_active_w"),
            pcc_reactive_var=self._values("pcc_reactive_var"),
            pcc_apparent_va=self._values("pcc_apparent_va"),
            inverter_current_a=self._values("inverter_current_a"),
            frequency_hz=self._values("frequency_hz"),
            stale=stale,
            source_age_seconds=age,
            # State-level last_updated is not a safe alignment proxy when a
            # sensor repeatedly reports the same value.  Keep this unknown
            # rather than manufacture a false skew alarm.
            source_skew_seconds=None,
        )
        snapshot = evaluate_power_quality(observation)

        new_source = heartbeat != self.last_heartbeat
        should_append = not self.profile.get("append_only_on_new_heartbeat") or new_source
        if should_append and snapshot.pcc_voltage_mean_v is not None and not stale:
            row = snapshot.as_dict()
            row["timestamp"] = datetime.now(timezone.utc).isoformat()
            self.history.append(row)
            maximum = int(
                self.profile["history_days"] * 86400
                / self.profile["sample_interval_seconds"]
            )
            self.history = self.history[-maximum:]
            self.samples_since_save += 1
            save_every = int(self.profile.get("save_every_samples", 60))
            if self.samples_since_save >= save_every:
                self._save_history()

        now_mono = monotonic()
        transition = self.last_snapshot is None or (
            snapshot.state != self.last_snapshot.state
            or snapshot.issues != self.last_snapshot.issues
        )
        due = now_mono - self.last_publish_at >= self.profile["publish_interval_seconds"]
        if transition or due:
            self._publish(snapshot)
            self.last_publish_at = now_mono
        if transition and snapshot.state in {"warning", "critical", "stale"}:
            level = "WARNING" if snapshot.state in {"warning", "critical", "stale"} else "INFO"
            issue_text = "; ".join(ISSUE_TEXT.get(code, code) for code in snapshot.issues) or "be nukrypimų"
            self.log(f"[{self.label}] Tinklo kokybė: {snapshot.state} — {issue_text}", level=level)
        self.last_snapshot = snapshot
        self.last_heartbeat = heartbeat

    def _publish_sensor(self, entity_id, value, name, unit=None, icon=None, device_class=None):
        attributes = {
            "friendly_name": f"{self.label}: {name}",
            "state_class": "measurement",
            "source": "power_quality_manager",
            "writes_disabled": True,
        }
        if unit:
            attributes["unit_of_measurement"] = unit
        if icon:
            attributes["icon"] = icon
        if device_class:
            attributes["device_class"] = device_class
        self.set_state(entity_id, state=_state_string(value, 3), attributes=attributes)

    def _publish(self, snapshot):
        outputs = self.profile["outputs"]
        analysis = analyze_history(self.history)
        issue_text = [ISSUE_TEXT.get(code, code) for code in snapshot.issues]
        attrs = snapshot.as_dict()
        attrs.update({
            "friendly_name": f"{self.label}: tinklo ir inverterio kokybė",
            "icon": "mdi:sine-wave",
            "site": self.site,
            "issues_lt": issue_text,
            "writes_disabled": True,
            "control_scope": "observation_only",
            "formal_voltage_unbalance_available": False,
            "voltage_metric": "phase_rms_magnitude_spread_not_vuf",
            "reactive_power_sign_calibrated": False,
            "history_analysis": analysis,
            "history_days_target": self.profile["history_days"],
            "history_samples": len(self.history),
            "last_sample_utc": datetime.now(timezone.utc).isoformat(),
            # When the source is stale, the numeric entities below become
            # unknown so a graph cannot present an old cloud value as current.
            # The last observation remains available here for diagnosis.
            "last_observation_metrics": snapshot.as_dict(),
        })
        self.set_state(outputs["state"], state=snapshot.state, attributes=attrs)
        value = lambda item: None if snapshot.state == "stale" else item
        self._publish_sensor(outputs["voltage_min"], value(snapshot.pcc_voltage_min_v), "PCC įtampa min", "V", "mdi:sine-wave", "voltage")
        self._publish_sensor(outputs["voltage_max"], value(snapshot.pcc_voltage_max_v), "PCC įtampa max", "V", "mdi:sine-wave", "voltage")
        self._publish_sensor(outputs["voltage_spread"], value(snapshot.pcc_voltage_spread_pct), "fazių įtampų dydžių išsiskyrimas", "%", "mdi:swap-vertical")
        self._publish_sensor(outputs["active_power"], value(snapshot.pcc_active_total_w), "PCC aktyvioji galia", "W", "mdi:flash", "power")
        self._publish_sensor(outputs["reactive_power"], value(snapshot.pcc_reactive_total_var), "PCC reaktyvioji galia", "var", "mdi:angle-acute", "reactive_power")
        self._publish_sensor(outputs["power_factor"], value(snapshot.derived_power_factor), "apskaičiuotas galios faktorius", None, "mdi:cosine-wave")
        self._publish_sensor(outputs["frequency"], value(snapshot.frequency_hz), "tinklo dažnis", "Hz", "mdi:sine-wave", "frequency")
        self._publish_sensor(outputs["voltage_rise"], value(snapshot.max_abs_voltage_rise_v), "inverterio–PCC įtampos skirtumas max", "V", "mdi:delta", "voltage")
        self._publish_sensor(outputs["active_imbalance"], value(snapshot.pcc_active_imbalance_pct), "PCC aktyviosios galios disbalansas", "%", "mdi:scale-unbalanced")
        self._publish_sensor(outputs["current_imbalance"], value(snapshot.inverter_current_imbalance_pct), "inverterio srovių netolygumas", "%", "mdi:current-ac")
