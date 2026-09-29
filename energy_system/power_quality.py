"""Pure power-quality calculations shared by every independent plant.

The module intentionally has no Home Assistant or AppDaemon dependency and
never writes to an inverter.  It evaluates observations only.  Voltage
``magnitude_spread_pct`` is a spread of phase-to-neutral RMS magnitudes; it is
not the formal negative-sequence voltage-unbalance factor, which would require
phase angles.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from math import hypot, isfinite, sqrt
from statistics import fmean
from typing import Iterable, Mapping, Sequence


SEVERITY_ORDER = {
    "insufficient": -1,
    "ok": 0,
    "watch": 1,
    "warning": 2,
    "critical": 3,
    "stale": 4,
}


def as_float(value: object) -> float | None:
    """Return a finite float or ``None`` for HA unknown/unavailable values."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str) and value.strip().lower() in {
        "", "none", "null", "unknown", "unavailable", "nan", "inf", "-inf",
    }:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _numbers(values: Iterable[object]) -> tuple[float, ...]:
    parsed = tuple(as_float(value) for value in values)
    return tuple(value for value in parsed if value is not None)


def magnitude_spread_pct(values: Sequence[object]) -> float | None:
    """Range of three RMS magnitudes as a percentage of their mean."""
    phases = _numbers(values)
    if len(phases) != 3:
        return None
    mean = fmean(phases)
    if abs(mean) < 1e-9:
        return None
    return (max(phases) - min(phases)) / abs(mean) * 100.0


def phase_deviation_pct(values: Sequence[object], minimum_mean: float = 0.0) -> float | None:
    """Largest phase deviation from the signed mean, normalized by mean magnitude."""
    phases = _numbers(values)
    if len(phases) != 3:
        return None
    scale = fmean(abs(value) for value in phases)
    if scale < minimum_mean or scale < 1e-9:
        return None
    mean = fmean(phases)
    return max(abs(value - mean) for value in phases) / scale * 100.0


def derived_power_factor(active_w: float | None, reactive_var: float | None,
                         minimum_apparent_va: float = 300.0) -> float | None:
    if active_w is None or reactive_var is None:
        return None
    apparent = hypot(active_w, reactive_var)
    if apparent < minimum_apparent_va:
        return None
    return min(1.0, abs(active_w) / apparent)


def triangle_residual_pct(active: Sequence[object], reactive: Sequence[object],
                          apparent: Sequence[object], minimum_va: float = 100.0) -> float | None:
    """Worst P/Q/S identity residual across aligned phases."""
    p = _numbers(active)
    q = _numbers(reactive)
    s = _numbers(apparent)
    if not (len(p) == len(q) == len(s) == 3):
        return None
    residuals = []
    for p_i, q_i, s_i in zip(p, q, s):
        scale = max(abs(s_i), hypot(p_i, q_i))
        if scale >= minimum_va:
            residuals.append(abs(abs(s_i) - hypot(p_i, q_i)) / scale * 100.0)
    return max(residuals) if residuals else None


@dataclass(frozen=True)
class PowerQualityThresholds:
    voltage_watch_low: float = 212.0
    voltage_warning_low: float = 210.0
    voltage_critical_low: float = 207.0
    voltage_watch_high: float = 248.0
    voltage_warning_high: float = 250.0
    voltage_critical_high: float = 253.0
    spread_watch_pct: float = 3.0
    spread_warning_pct: float = 5.0
    spread_critical_pct: float = 10.0
    frequency_warning_low: float = 49.8
    frequency_critical_low: float = 49.5
    frequency_warning_high: float = 50.2
    frequency_critical_high: float = 50.5
    active_imbalance_watch_pct: float = 25.0
    active_imbalance_warning_pct: float = 50.0
    current_imbalance_watch_pct: float = 35.0
    current_imbalance_warning_pct: float = 60.0
    voltage_rise_watch_v: float = 4.0
    voltage_rise_warning_v: float = 6.0
    voltage_rise_critical_v: float = 10.0
    pf_watch: float = 0.95
    pf_warning: float = 0.90
    reactive_ratio_watch_pct: float = 33.0
    reactive_ratio_warning_pct: float = 50.0
    minimum_phase_power_w: float = 150.0
    minimum_current_a: float = 1.0
    minimum_apparent_va: float = 300.0
    coherence_warning_pct: float = 15.0


@dataclass(frozen=True)
class PowerQualityInput:
    pcc_voltage_v: Sequence[object]
    inverter_voltage_v: Sequence[object] = field(default_factory=tuple)
    pcc_active_w: Sequence[object] = field(default_factory=tuple)
    pcc_reactive_var: Sequence[object] = field(default_factory=tuple)
    pcc_apparent_va: Sequence[object] = field(default_factory=tuple)
    inverter_current_a: Sequence[object] = field(default_factory=tuple)
    frequency_hz: object = None
    stale: bool = False
    source_age_seconds: float | None = None
    source_skew_seconds: float | None = None


@dataclass(frozen=True)
class PowerQualitySnapshot:
    state: str
    issues: tuple[str, ...]
    data_warnings: tuple[str, ...]
    pcc_voltage_min_v: float | None
    pcc_voltage_mean_v: float | None
    pcc_voltage_max_v: float | None
    pcc_voltage_spread_pct: float | None
    pcc_active_total_w: float | None
    pcc_reactive_total_var: float | None
    pcc_apparent_total_va: float | None
    derived_power_factor: float | None
    reactive_ratio_pct: float | None
    pcc_active_imbalance_pct: float | None
    inverter_current_imbalance_pct: float | None
    voltage_rise_v: tuple[float, ...]
    max_abs_voltage_rise_v: float | None
    frequency_hz: float | None
    triangle_residual_pct: float | None
    source_age_seconds: float | None
    source_skew_seconds: float | None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _sum_three(values: Sequence[object]) -> float | None:
    parsed = _numbers(values)
    return sum(parsed) if len(parsed) == 3 else None


def _raise(severity: str, candidate: str) -> str:
    return candidate if SEVERITY_ORDER[candidate] > SEVERITY_ORDER[severity] else severity


def evaluate_power_quality(
    observation: PowerQualityInput,
    thresholds: PowerQualityThresholds | None = None,
) -> PowerQualitySnapshot:
    """Evaluate one time-aligned observation without inferring causality."""
    limits = thresholds or PowerQualityThresholds()
    voltage = _numbers(observation.pcc_voltage_v)
    frequency = as_float(observation.frequency_hz)
    active_total = _sum_three(observation.pcc_active_w)
    reactive_total = _sum_three(observation.pcc_reactive_var)
    apparent_total = _sum_three(observation.pcc_apparent_va)

    voltage_min = min(voltage) if len(voltage) == 3 else None
    voltage_max = max(voltage) if len(voltage) == 3 else None
    voltage_mean = fmean(voltage) if len(voltage) == 3 else None
    spread = magnitude_spread_pct(observation.pcc_voltage_v)
    active_imbalance = phase_deviation_pct(
        observation.pcc_active_w, limits.minimum_phase_power_w
    )
    current_imbalance = phase_deviation_pct(
        observation.inverter_current_a, limits.minimum_current_a
    )

    inverter_voltage = _numbers(observation.inverter_voltage_v)
    rise = (
        tuple(inv - grid for inv, grid in zip(inverter_voltage, voltage))
        if len(inverter_voltage) == len(voltage) == 3 else tuple()
    )
    max_rise = max((abs(value) for value in rise), default=None)

    pf = derived_power_factor(active_total, reactive_total, limits.minimum_apparent_va)
    reactive_ratio = None
    if active_total is not None and reactive_total is not None:
        if hypot(active_total, reactive_total) >= limits.minimum_apparent_va:
            reactive_ratio = abs(reactive_total) / max(abs(active_total), 1.0) * 100.0
    coherence = triangle_residual_pct(
        observation.pcc_active_w,
        observation.pcc_reactive_var,
        observation.pcc_apparent_va,
    )

    issues: list[str] = []
    data_warnings: list[str] = []
    if observation.stale:
        state = "stale"
        issues.append("telemetry_stale")
    elif len(voltage) != 3:
        state = "insufficient"
        issues.append("missing_phase_voltage")
    else:
        state = "ok"
        if voltage_min <= limits.voltage_critical_low:
            state = _raise(state, "critical"); issues.append("voltage_critical_low")
        elif voltage_min <= limits.voltage_warning_low:
            state = _raise(state, "warning"); issues.append("voltage_warning_low")
        elif voltage_min <= limits.voltage_watch_low:
            state = _raise(state, "watch"); issues.append("voltage_watch_low")

        if voltage_max >= limits.voltage_critical_high:
            state = _raise(state, "critical"); issues.append("voltage_critical_high")
        elif voltage_max >= limits.voltage_warning_high:
            state = _raise(state, "warning"); issues.append("voltage_warning_high")
        elif voltage_max >= limits.voltage_watch_high:
            state = _raise(state, "watch"); issues.append("voltage_watch_high")

        if spread is not None:
            if spread >= limits.spread_critical_pct:
                state = _raise(state, "critical"); issues.append("phase_magnitude_spread_critical")
            elif spread >= limits.spread_warning_pct:
                state = _raise(state, "warning"); issues.append("phase_magnitude_spread_warning")
            elif spread >= limits.spread_watch_pct:
                state = _raise(state, "watch"); issues.append("phase_magnitude_spread_watch")

        if frequency is not None:
            if not limits.frequency_critical_low <= frequency <= limits.frequency_critical_high:
                state = _raise(state, "critical"); issues.append("frequency_critical")
            elif not limits.frequency_warning_low <= frequency <= limits.frequency_warning_high:
                state = _raise(state, "warning"); issues.append("frequency_warning")

        if max_rise is not None:
            if max_rise >= limits.voltage_rise_critical_v:
                state = _raise(state, "critical"); issues.append("voltage_rise_critical")
            elif max_rise >= limits.voltage_rise_warning_v:
                state = _raise(state, "warning"); issues.append("voltage_rise_warning")
            elif max_rise >= limits.voltage_rise_watch_v:
                state = _raise(state, "watch"); issues.append("voltage_rise_watch")

        if active_imbalance is not None:
            if active_imbalance >= limits.active_imbalance_warning_pct:
                state = _raise(state, "warning"); issues.append("pcc_active_imbalance_warning")
            elif active_imbalance >= limits.active_imbalance_watch_pct:
                state = _raise(state, "watch"); issues.append("pcc_active_imbalance_watch")

        # Inverter output-current imbalance may be intentional load compensation;
        # keep it observational and never elevate beyond warning.
        if current_imbalance is not None:
            if current_imbalance >= limits.current_imbalance_warning_pct:
                state = _raise(state, "warning"); issues.append("inverter_current_imbalance_warning")
            elif current_imbalance >= limits.current_imbalance_watch_pct:
                state = _raise(state, "watch"); issues.append("inverter_current_imbalance_watch")

        if pf is not None:
            if pf < limits.pf_warning:
                state = _raise(state, "warning"); issues.append("power_factor_warning")
            elif pf < limits.pf_watch:
                state = _raise(state, "watch"); issues.append("power_factor_watch")

        if reactive_ratio is not None:
            if reactive_ratio >= limits.reactive_ratio_warning_pct:
                state = _raise(state, "warning"); issues.append("reactive_ratio_warning")
            elif reactive_ratio >= limits.reactive_ratio_watch_pct:
                state = _raise(state, "watch"); issues.append("reactive_ratio_watch")

    if coherence is not None and coherence >= limits.coherence_warning_pct:
        data_warnings.append("power_triangle_incoherent")
    if observation.source_skew_seconds is not None and observation.source_skew_seconds > 120:
        data_warnings.append("source_timestamps_not_aligned")

    return PowerQualitySnapshot(
        state=state,
        issues=tuple(dict.fromkeys(issues)),
        data_warnings=tuple(dict.fromkeys(data_warnings)),
        pcc_voltage_min_v=voltage_min,
        pcc_voltage_mean_v=voltage_mean,
        pcc_voltage_max_v=voltage_max,
        pcc_voltage_spread_pct=spread,
        pcc_active_total_w=active_total,
        pcc_reactive_total_var=reactive_total,
        pcc_apparent_total_va=apparent_total,
        derived_power_factor=pf,
        reactive_ratio_pct=reactive_ratio,
        pcc_active_imbalance_pct=active_imbalance,
        inverter_current_imbalance_pct=current_imbalance,
        voltage_rise_v=rise,
        max_abs_voltage_rise_v=max_rise,
        frequency_hz=frequency,
        triangle_residual_pct=coherence,
        source_age_seconds=observation.source_age_seconds,
        source_skew_seconds=observation.source_skew_seconds,
    )


def pearson_correlation(x_values: Sequence[float], y_values: Sequence[float]) -> float | None:
    if len(x_values) != len(y_values) or len(x_values) < 3:
        return None
    x_mean = fmean(x_values)
    y_mean = fmean(y_values)
    xx = sum((x - x_mean) ** 2 for x in x_values)
    yy = sum((y - y_mean) ** 2 for y in y_values)
    if xx <= 1e-12 or yy <= 1e-12:
        return None
    return sum((x - x_mean) * (y - y_mean) for x, y in zip(x_values, y_values)) / sqrt(xx * yy)


def analyze_history(samples: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """Compact rolling evidence; correlations describe association, not cause."""
    clean = []
    spread_reactive = []
    for sample in samples:
        voltage = as_float(sample.get("pcc_voltage_mean_v"))
        voltage_max = as_float(sample.get("pcc_voltage_max_v"))
        spread = as_float(sample.get("pcc_voltage_spread_pct"))
        p = as_float(sample.get("pcc_active_total_w"))
        q = as_float(sample.get("pcc_reactive_total_var"))
        if None not in (voltage, voltage_max, p, q):
            clean.append((voltage, voltage_max, p, q))
        if spread is not None and q is not None:
            spread_reactive.append((spread, q))
    if not clean:
        return {
            "samples": 0,
            "corr_voltage_active": None,
            "corr_voltage_reactive": None,
            "corr_spread_reactive": pearson_correlation(
                [row[0] for row in spread_reactive],
                [row[1] for row in spread_reactive],
            ),
            "spread_alert_samples": sum(row[0] >= 3.0 for row in spread_reactive),
            "spread_alert_mean_q_var": (
                fmean(row[1] for row in spread_reactive if row[0] >= 3.0)
                if any(row[0] >= 3.0 for row in spread_reactive) else None
            ),
            "normal_spread_mean_q_var": (
                fmean(row[1] for row in spread_reactive if row[0] < 3.0)
                if any(row[0] < 3.0 for row in spread_reactive) else None
            ),
            "interpretation": "association_only_q_sign_not_calibrated",
        }
    voltage = [row[0] for row in clean]
    voltage_max = [row[1] for row in clean]
    active = [row[2] for row in clean]
    reactive = [row[3] for row in clean]
    high_q = [row[3] for row in clean if row[1] >= 248.0]
    normal_q = [row[3] for row in clean if 212.0 < row[1] < 248.0]
    low_q = [row[3] for row in clean if row[1] <= 212.0]
    high_spread_q = [row[1] for row in spread_reactive if row[0] >= 3.0]
    normal_spread_q = [row[1] for row in spread_reactive if row[0] < 3.0]
    return {
        "samples": len(clean),
        "voltage_min_v": min(voltage),
        "voltage_max_v": max(voltage_max),
        "corr_voltage_active": pearson_correlation(voltage, active),
        "corr_voltage_reactive": pearson_correlation(voltage, reactive),
        # This is deliberately an association metric. It does not prove that
        # Q caused the phase spread (or that Q(U)/PF settings are at fault).
        "corr_spread_reactive": pearson_correlation(
            [row[0] for row in spread_reactive],
            [row[1] for row in spread_reactive],
        ),
        "high_voltage_samples": len(high_q),
        "normal_voltage_samples": len(normal_q),
        "low_voltage_samples": len(low_q),
        "high_voltage_mean_q_var": fmean(high_q) if high_q else None,
        "normal_voltage_mean_q_var": fmean(normal_q) if normal_q else None,
        "low_voltage_mean_q_var": fmean(low_q) if low_q else None,
        "spread_alert_samples": len(high_spread_q),
        "spread_alert_mean_q_var": fmean(high_spread_q) if high_spread_q else None,
        "normal_spread_mean_q_var": fmean(normal_spread_q) if normal_spread_q else None,
        "interpretation": "association_only_q_sign_not_calibrated",
    }
