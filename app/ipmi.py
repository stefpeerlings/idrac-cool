"""ipmitool wrapper for Dell iDRAC fan control and SDR sensors."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("idrac.ipmi")

# Mock state for MOCK_IPMI=1
_mock_lock = threading.Lock()
_mock: dict[str, dict[str, Any]] = {}


@dataclass
class TempReading:
    label: str
    celsius: float


@dataclass
class FanReading:
    label: str
    rpm: float | None


@dataclass
class SensorBundle:
    inlet: float | None = None
    outlet: float | None = None
    cpu: list[TempReading] = field(default_factory=list)
    cpu_max: float | None = None
    raw_temps: list[TempReading] = field(default_factory=list)
    fans: list[FanReading] = field(default_factory=list)
    online: bool = False
    error: str | None = None


@dataclass
class DetectInfo:
    """Auto-detected identity + inventory from iDRAC/IPMI."""

    online: bool = False
    error: str | None = None
    manufacturer: str | None = None
    product: str | None = None
    model: str | None = None
    serial: str | None = None
    firmware: str | None = None
    ipmi_version: str | None = None
    temp_sensors: list[str] = field(default_factory=list)
    fan_sensors: list[str] = field(default_factory=list)


class IpmiError(Exception):
    pass


class IpmiClient:
    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        timeout: float = 8.0,
        mock: bool = False,
        host_id: str = "",
    ) -> None:
        self.host = host
        self.username = username
        self.password = password
        self.timeout = timeout
        self.mock = mock or os.environ.get("MOCK_IPMI", "").lower() in ("1", "true", "yes")
        self.host_id = host_id or host
        self._lock = threading.RLock()

    def _base_cmd(self) -> list[str]:
        return [
            "ipmitool",
            "-I",
            "lanplus",
            "-H",
            self.host,
            "-U",
            self.username,
            "-P",
            self.password,
        ]

    def _run(self, args: list[str], *, timeout: float | None = None) -> str:
        """Run ipmitool. Uses host lock so poll/detect/fan never run in parallel."""
        with self._lock:
            return self._run_unlocked(args, timeout=timeout)

    def _run_unlocked(self, args: list[str], *, timeout: float | None = None) -> str:
        if self.mock:
            return self._mock_run(args)
        cmd = self._base_cmd() + args
        to = self.timeout if timeout is None else timeout
        # Never log password
        safe = ["ipmitool", "-I", "lanplus", "-H", self.host, "-U", self.username, "-P", "***"] + args
        log.debug("ipmi: %s", " ".join(safe))
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=to,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            raise IpmiError(f"ipmitool timeout after {to}s") from e
        except FileNotFoundError as e:
            raise IpmiError("ipmitool not installed") from e
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "ipmitool failed").strip()
            # Redact accidental password echo
            err = err.replace(self.password, "***") if self.password else err
            raise IpmiError(err[:300])
        return proc.stdout

    def _mock_ensure(self) -> dict[str, Any]:
        with _mock_lock:
            if self.host_id not in _mock:
                # Vary demo hosts slightly
                seed = sum(ord(c) for c in self.host_id) % 10
                _mock[self.host_id] = {
                    "mode": "smart_auto",
                    "percent": 20,
                    "cpu": 42.0 + seed,
                    "inlet": 22.0 + seed * 0.2,
                    "outlet": 35.0 + seed * 0.5,
                    "fan_rpm": 4000 + seed * 50,
                }
            return _mock[self.host_id]

    def _mock_run(self, args: list[str]) -> str:
        st = self._mock_ensure()
        # raw 0x30 0x30 0x01 0x00/0x01
        if args[:1] == ["raw"] and len(args) >= 5 and args[1:4] == ["0x30", "0x30", "0x01"]:
            if args[4] == "0x01":
                st["mode"] = "dell_auto"
            else:
                st["mode"] = "manual"
            return ""
        # raw set percent
        if args[:1] == ["raw"] and len(args) >= 6 and args[1:4] == ["0x30", "0x30", "0x02"]:
            try:
                pct = int(args[5], 16)
            except ValueError:
                pct = 20
            st["percent"] = pct
            st["mode"] = "manual"
            st["fan_rpm"] = 2500 + pct * 80
            return ""
        if args[:3] == ["sdr", "type", "temperature"]:
            cpu = st["cpu"]
            # mild drift for demo
            cpu = max(30.0, min(80.0, cpu + (time.time() % 7 - 3) * 0.05))
            st["cpu"] = cpu
            return (
                f"Inlet Temp       | {st['inlet']:.0f} degrees C      | ok\n"
                f"Exhaust Temp     | {st['outlet']:.0f} degrees C      | ok\n"
                f"Temp             | {cpu:.0f} degrees C      | ok\n"
                f"Temp 2           | {cpu - 2:.0f} degrees C      | ok\n"
            )
        if args[:3] == ["sdr", "type", "fan"]:
            rpm = st["fan_rpm"]
            return (
                f"Fan1A RPM        | {rpm:.0f} RPM            | ok\n"
                f"Fan1B RPM        | {rpm - 50:.0f} RPM            | ok\n"
                f"Fan2A RPM        | {rpm + 20:.0f} RPM            | ok\n"
            )
        if args[:2] == ["fru", "print"] or args[:1] == ["fru"]:
            return (
                " Product Manufacturer  : Dell Inc.\n"
                " Product Name          : PowerEdge R630\n"
                " Product Part Number   : 0XXXXX\n"
                " Product Serial        : MOCKSERIAL01\n"
            )
        if args[:2] == ["mc", "info"]:
            return (
                "Device ID                 : 32\n"
                "Device Revision           : 1\n"
                "Firmware Revision         : 2.75\n"
                "IPMI Version              : 2.0\n"
                "Manufacturer ID           : 674\n"
                "Manufacturer Name         : DELL Inc\n"
                "Product ID                : 256 (0x0100)\n"
                "Product Name              : iDRAC7\n"
            )
        return ""

    def set_dell_auto(self) -> None:
        self._run(["raw", "0x30", "0x30", "0x01", "0x01"])

    def set_manual_mode(self) -> None:
        self._run(["raw", "0x30", "0x30", "0x01", "0x00"])

    def set_fan_percent(self, percent: int) -> None:
        percent = max(1, min(100, int(percent)))
        hex_pct = f"0x{percent:02x}"
        with self._lock:
            # Ensure manual first, then set speed (one lock for both)
            self._run_unlocked(["raw", "0x30", "0x30", "0x01", "0x00"])
            self._run_unlocked(["raw", "0x30", "0x30", "0x02", "0xff", hex_pct])

    def read_sensors(self) -> SensorBundle:
        bundle = SensorBundle()
        try:
            with self._lock:
                temp_out = self._run_unlocked(["sdr", "type", "temperature"])
                try:
                    fan_out = self._run_unlocked(["sdr", "type", "fan"])
                except IpmiError:
                    fan_out = ""
        except IpmiError as e:
            bundle.online = False
            bundle.error = str(e)
            return bundle

        temps = _parse_temp_sdr(temp_out)
        fans = _parse_fan_sdr(fan_out)
        bundle.raw_temps = temps
        bundle.fans = fans
        bundle.online = True

        for t in temps:
            name = t.label.lower()
            if "inlet" in name:
                bundle.inlet = t.celsius
            elif "exhaust" in name or "outlet" in name:
                bundle.outlet = t.celsius
            elif any(k in name for k in ("cpu", "proc", "temp")):
                # "Temp" / "Temp 2" on R630 are often CPU package sensors
                if "inlet" not in name and "exhaust" not in name and "outlet" not in name:
                    if "dimm" in name or "ambient" in name or "planar" in name:
                        continue
                    bundle.cpu.append(t)

        # If nothing classified as CPU, use sensors named exactly like Temp / CPU
        if not bundle.cpu:
            for t in temps:
                n = t.label.lower().strip()
                if n.startswith("temp") or "cpu" in n:
                    if "inlet" not in n and "exhaust" not in n:
                        bundle.cpu.append(t)

        if bundle.cpu:
            bundle.cpu_max = max(c.celsius for c in bundle.cpu)
        return bundle

    def detect(self) -> DetectInfo:
        """Probe FRU + MC info + sensor names. Partial success is OK (no hard fail on one cmd)."""
        info = DetectInfo()
        errors: list[str] = []
        fru = mc = temps = fans = ""

        # Hold host lock for whole detect so poll cannot interleave and cause timeouts
        with self._lock:
            for label, args, bucket in (
                ("fru", ["fru", "print", "0"], "fru"),
                ("mc", ["mc", "info"], "mc"),
                ("temps", ["sdr", "type", "temperature"], "temps"),
                ("fans", ["sdr", "type", "fan"], "fans"),
            ):
                try:
                    out = self._run_unlocked(args, timeout=max(self.timeout, 15))
                    if bucket == "fru":
                        fru = out
                    elif bucket == "mc":
                        mc = out
                    elif bucket == "temps":
                        temps = out
                    else:
                        fans = out
                except IpmiError as e:
                    errors.append(f"{label}: {e}")
                    # FRU fallback without id
                    if bucket == "fru":
                        try:
                            fru = self._run_unlocked(["fru"], timeout=max(self.timeout, 15))
                        except IpmiError as e2:
                            errors.append(f"fru: {e2}")

        # online if we got anything useful
        if fru or mc or temps or fans:
            info.online = True
        else:
            info.online = False
            info.error = "; ".join(errors) if errors else "detect failed"
            return info

        if errors:
            log.warning("detect partial errors host=%s: %s", self.host_id, "; ".join(errors))

        info.manufacturer = _fru_field(fru, "Product Manufacturer") or _mc_field(mc, "Manufacturer Name")
        info.product = _fru_field(fru, "Product Name")
        # Prefer chassis product name over iDRAC product name from mc
        info.model = info.product or _fru_field(fru, "Board Product")
        if not info.model:
            mc_prod = _mc_field(mc, "Product Name")
            # "iDRAC7" is not the server model
            if mc_prod and "idrac" not in mc_prod.lower():
                info.model = mc_prod
        # Op Dell PowerEdge is Product Serial de service tag
        info.serial = (
            _fru_field(fru, "Product Serial")
            or _fru_field(fru, "Product Asset Tag")
            or _fru_field(fru, "Chassis Serial")
        )
        info.firmware = _mc_field(mc, "Firmware Revision")
        info.ipmi_version = _mc_field(mc, "IPMI Version")
        info.temp_sensors = [t.label for t in _parse_temp_sdr(temps)]
        info.fan_sensors = [f.label for f in _parse_fan_sdr(fans)]
        if errors and not (info.model or info.serial or info.firmware):
            info.error = "; ".join(errors)
        return info

    def probe(self) -> DetectInfo:
        """Alias: connectivity + inventory check (used when adding a node)."""
        return self.detect()


def _fru_field(text: str, key: str) -> str | None:
    if not text:
        return None
    # " Product Name : PowerEdge R630"
    pat = re.compile(rf"^\s*{re.escape(key)}\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
    m = pat.search(text)
    if m:
        val = m.group(1).strip()
        return val or None
    return None


def _mc_field(text: str, key: str) -> str | None:
    return _fru_field(text, key)


# Supports both short form and full ipmitool form:
#   Inlet Temp | 27 degrees C | ok
#   Inlet Temp | 04h | ok | 7.1 | 27 degrees C
_TEMP_VAL = re.compile(r"([-+]?\d+(?:\.\d+)?)\s*degrees?\s*C", re.IGNORECASE)
_FAN_VAL = re.compile(r"(\d+(?:\.\d+)?)\s*RPM", re.IGNORECASE)


def _parse_temp_sdr(text: str) -> list[TempReading]:
    out: list[TempReading] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        low = line.lower()
        if "disabled" in low or "no reading" in low:
            continue
        m = _TEMP_VAL.search(line)
        if not m:
            continue
        label = line.split("|", 1)[0].strip()
        if not label:
            continue
        out.append(TempReading(label=label, celsius=float(m.group(1))))
    return out


def _parse_fan_sdr(text: str) -> list[FanReading]:
    out: list[FanReading] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        low = line.lower()
        if "disabled" in low or "no reading" in low:
            continue
        label = line.split("|", 1)[0].strip()
        m = _FAN_VAL.search(line)
        if m:
            out.append(FanReading(label=label, rpm=float(m.group(1))))
        elif label.lower().startswith("fan"):
            out.append(FanReading(label=label, rpm=None))
    return out
