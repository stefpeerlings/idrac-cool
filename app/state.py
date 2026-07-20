"""Persist desired fan mode/percent per host (survives app restart)."""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from .config import MODES, Mode

ModeType = Mode


@dataclass
class DesiredState:
    mode: ModeType = "smart_auto"
    percent: int = 20


class StateStore:
    def __init__(
        self,
        path: Path,
        default_mode: ModeType = "smart_auto",
        default_percent: int = 20,
        host_ids: list[str] | None = None,
    ) -> None:
        self.path = path
        self.default_mode = default_mode if default_mode in MODES else "smart_auto"
        self.default_percent = default_percent
        self._lock = threading.RLock()
        self._data: dict[str, DesiredState] = {}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._load()
        if host_ids:
            for hid in host_ids:
                self.ensure(hid)

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict):
            return
        for hid, val in raw.items():
            if not isinstance(val, dict):
                continue
            mode = val.get("mode", self.default_mode)
            if mode not in MODES:
                mode = self.default_mode
            percent = int(val.get("percent", self.default_percent))
            self._data[str(hid)] = DesiredState(mode=mode, percent=percent)  # type: ignore[arg-type]

    def _save(self) -> None:
        payload = {hid: asdict(st) for hid, st in self._data.items()}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    def ensure(self, host_id: str) -> DesiredState:
        with self._lock:
            if host_id not in self._data:
                self._data[host_id] = DesiredState(
                    mode=self.default_mode,
                    percent=self.default_percent,
                )
                self._save()
            return self._data[host_id]

    def get(self, host_id: str) -> DesiredState:
        with self._lock:
            return self.ensure(host_id)

    def set_mode(self, host_id: str, mode: ModeType) -> DesiredState:
        if mode not in MODES:
            raise ValueError(f"invalid mode: {mode}")
        with self._lock:
            st = self.ensure(host_id)
            st.mode = mode
            self._save()
            return DesiredState(mode=st.mode, percent=st.percent)

    def set_percent(self, host_id: str, percent: int) -> DesiredState:
        with self._lock:
            st = self.ensure(host_id)
            st.percent = percent
            self._save()
            return DesiredState(mode=st.mode, percent=st.percent)

    def set_manual_percent(self, host_id: str, percent: int) -> DesiredState:
        with self._lock:
            st = self.ensure(host_id)
            st.mode = "manual"
            st.percent = percent
            self._save()
            return DesiredState(mode=st.mode, percent=st.percent)

    def all(self) -> dict[str, DesiredState]:
        with self._lock:
            return {k: DesiredState(mode=v.mode, percent=v.percent) for k, v in self._data.items()}
