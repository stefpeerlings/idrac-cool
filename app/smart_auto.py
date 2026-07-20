"""Smart Auto fan curve: temperature → percent (hard max via config)."""

from __future__ import annotations

from dataclasses import dataclass

from .config import SmartAutoConfig
from .ipmi import SensorBundle


@dataclass
class CurveResult:
    percent: int
    metric_value: float | None
    reason: str


class SmartAutoEngine:
    """Compute target fan % with hysteresis to avoid flapping."""

    def __init__(self, cfg: SmartAutoConfig, fan_max: int = 30) -> None:
        self.cfg = cfg
        self.fan_max = fan_max
        # last applied band index / percent per host
        self._last_percent: dict[str, int] = {}

    def metric_value(self, sensors: SensorBundle) -> float | None:
        m = (self.cfg.metric or "cpu_max").lower()
        if m == "inlet":
            return sensors.inlet
        if m == "outlet":
            return sensors.outlet
        # cpu_max default
        if sensors.cpu_max is not None:
            return sensors.cpu_max
        if sensors.outlet is not None:
            return sensors.outlet
        return sensors.inlet

    def _band_for_temp(self, temp: float) -> int:
        """Return percent for temperature without hysteresis."""
        bands = sorted(self.cfg.bands, key=lambda b: b.below_c)
        if not bands:
            return min(20, self.fan_max)
        for b in bands:
            if temp < b.below_c:
                return min(b.percent, self.fan_max)
        return min(bands[-1].percent, self.fan_max)

    def compute(self, host_id: str, sensors: SensorBundle) -> CurveResult:
        temp = self.metric_value(sensors)
        if temp is None:
            last = self._last_percent.get(host_id)
            if last is not None:
                return CurveResult(percent=last, metric_value=None, reason="no_sensor_keep_last")
            return CurveResult(
                percent=min(20, self.fan_max),
                metric_value=None,
                reason="no_sensor_default",
            )

        raw = self._band_for_temp(temp)
        last = self._last_percent.get(host_id)

        if last is None:
            self._last_percent[host_id] = raw
            return CurveResult(percent=raw, metric_value=temp, reason="init")

        if raw == last:
            return CurveResult(percent=last, metric_value=temp, reason="hold")

        # Going up: apply immediately
        if raw > last:
            self._last_percent[host_id] = raw
            return CurveResult(percent=raw, metric_value=temp, reason="up")

        # Going down: require hysteresis below the threshold of the current band
        # Find the lower edge of the current (higher) band
        hyst = self.cfg.hysteresis_c
        bands = sorted(self.cfg.bands, key=lambda b: b.below_c)
        # Threshold we need to fall under to leave `last` percent downward
        # last percent corresponds to a band; find its below_c of the band above the lower one
        edge = None
        for i, b in enumerate(bands):
            if min(b.percent, self.fan_max) == last:
                # lower edge is previous band's below_c (or 0)
                edge = bands[i - 1].below_c if i > 0 else 0.0
                break
        if edge is None:
            # unknown last → accept raw
            self._last_percent[host_id] = raw
            return CurveResult(percent=raw, metric_value=temp, reason="down_unknown")

        if temp < edge - hyst:
            self._last_percent[host_id] = raw
            return CurveResult(percent=raw, metric_value=temp, reason="down")

        return CurveResult(percent=last, metric_value=temp, reason="hysteresis_hold")

    def reset(self, host_id: str) -> None:
        self._last_percent.pop(host_id, None)

    def note_applied(self, host_id: str, percent: int) -> None:
        self._last_percent[host_id] = percent
