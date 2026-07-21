"""iDRAC Fan & Temperature Dashboard — API + background loops + SQLite."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .auth import (
    AuthUser,
    default_admin_credentials,
    hash_password,
    new_session_token,
    verify_password,
)
from .config import AppConfig, MODES, Mode, load_config
from .db import Database, HostRow
from .ipmi import IpmiClient, IpmiError, SensorBundle, TempReading
from .smart_auto import SmartAutoEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("idrac")

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"

cfg: AppConfig
db: Database
smart_engine: SmartAutoEngine
runtime: dict[str, dict[str, Any]] = {}
runtime_lock = threading.RLock()
clients: dict[str, IpmiClient] = {}
stop_event = threading.Event()
_bg_threads: list[threading.Thread] = []


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _format_cpu_list(cpus: list[TempReading]) -> list[dict[str, Any]]:
    """Normalize dual-socket labels: Temp / Temp 2 → CPU 1 / CPU 2."""
    out: list[dict[str, Any]] = []
    for i, c in enumerate(cpus):
        raw = (c.label or "").strip()
        label = raw
        m = re.match(r"^temp\s*(\d*)$", raw, re.IGNORECASE)
        if m:
            n = m.group(1)
            if n:
                label = f"CPU {n}"
            elif len(cpus) > 1:
                label = f"CPU {i + 1}"
            else:
                label = "CPU"
        elif re.match(r"^cpu\s*\d+", raw, re.IGNORECASE):
            label = re.sub(r"\s+", " ", raw)
        elif len(cpus) > 1 and not re.search(r"cpu", raw, re.IGNORECASE):
            label = f"CPU {i + 1}"
        out.append({"label": label, "celsius": c.celsius, "sensor": raw})
    return out


def _slug(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", name.strip().lower()).strip("-")
    return s or "idrac"


def _creds(h: HostRow) -> tuple[str, str]:
    user = h.username or cfg.credentials_username
    password = h.password or cfg.credentials_password
    return user, password


def _client_for(h: HostRow) -> IpmiClient:
    user, password = _creds(h)
    with runtime_lock:
        c = clients.get(h.id)
        if (
            c is None
            or c.host != h.host
            or c.username != user
            or c.password != password
        ):
            c = IpmiClient(
                host=h.host,
                username=user,
                password=password,
                timeout=cfg.ipmi_timeout_seconds,
                mock=cfg.mock_ipmi,
                host_id=h.id,
            )
            clients[h.id] = c
        return c


def _empty_status(h: HostRow) -> dict[str, Any]:
    desired = db.get_fan_state(h.id, cfg.default_mode, cfg.default_percent)
    detect = db.get_detect(h.id)
    return {
        "id": h.id,
        "name": h.name,
        "host": h.host,
        "model": h.model,
        "product": h.product,
        "serial": h.serial,
        "source": h.source,
        "online": False,
        "mode": desired.mode,
        "desired_percent": desired.percent,
        "applied_percent": desired.percent,
        "fan_ui": desired.fan_ui,
        "temps": {
            "inlet": None,
            "outlet": None,
            "cpu": [],
            "cpu_max": None,
            "raw": [],
        },
        "fans": [],
        "detect": detect,
        "last_error": None,
        "last_reapply_at": None,
        "updated_at": None,
        "removable": h.source != "config",
        "fan_presets": cfg.fan_presets,
    }


def _apply_mode(h: HostRow, mode: Mode, percent: int) -> None:
    client = _client_for(h)
    if mode == "dell_auto":
        client.set_dell_auto()
    elif mode in ("manual", "smart_auto"):
        client.set_fan_percent(percent)
        if mode == "smart_auto":
            smart_engine.note_applied(h.id, percent)
    else:
        raise ValueError(f"unknown mode {mode}")


def _poll_one(h: HostRow) -> None:
    status = _empty_status(h)
    desired = db.get_fan_state(h.id, cfg.default_mode, cfg.default_percent)
    status["mode"] = desired.mode
    status["desired_percent"] = desired.percent

    with runtime_lock:
        prev = runtime.get(h.id, {})
        was_online = bool(prev.get("online"))
        status["last_reapply_at"] = prev.get("last_reapply_at")
        status["applied_percent"] = prev.get("applied_percent", desired.percent)
        if prev.get("detect"):
            status["detect"] = prev.get("detect")

    client = _client_for(h)
    sensors = client.read_sensors()
    status["online"] = sensors.online
    status["last_error"] = sensors.error
    status["temps"] = {
        "inlet": sensors.inlet,
        "outlet": sensors.outlet,
        "cpu": _format_cpu_list(sensors.cpu),
        "cpu_max": sensors.cpu_max,
        "raw": [{"label": t.label, "celsius": t.celsius} for t in sensors.raw_temps],
    }
    status["fans"] = [{"label": f.label, "rpm": f.rpm} for f in sensors.fans]
    status["updated_at"] = _now()

    if cfg.reapply_on_host_online and sensors.online and not was_online and prev:
        try:
            _apply_mode(h, desired.mode, desired.percent)
            status["last_reapply_at"] = _now()
            status["applied_percent"] = desired.percent
            log.info("re-apply on online: %s mode=%s %%=%s", h.id, desired.mode, desired.percent)
        except IpmiError as e:
            status["last_error"] = f"reapply: {e}"
            log.warning("re-apply failed %s: %s", h.id, e)

    if sensors.online and desired.mode == "smart_auto":
        result = smart_engine.compute(h.id, sensors)
        if result.percent != status.get("applied_percent"):
            try:
                client.set_fan_percent(result.percent)
                db.set_fan_percent(h.id, result.percent)
                status["desired_percent"] = result.percent
                status["applied_percent"] = result.percent
                smart_engine.note_applied(h.id, result.percent)
                log.info(
                    "smart_auto %s → %s%% (metric=%s reason=%s)",
                    h.id,
                    result.percent,
                    result.metric_value,
                    result.reason,
                )
            except IpmiError as e:
                status["last_error"] = str(e)
        else:
            status["applied_percent"] = result.percent

    with runtime_lock:
        runtime[h.id] = status


def _reapply_all() -> None:
    for h in db.list_hosts():
        desired = db.get_fan_state(h.id, cfg.default_mode, cfg.default_percent)
        try:
            _apply_mode(h, desired.mode, desired.percent)
            with runtime_lock:
                st = runtime.get(h.id) or _empty_status(h)
                st["last_reapply_at"] = _now()
                st["applied_percent"] = desired.percent
                st["mode"] = desired.mode
                runtime[h.id] = st
            log.info("reapply %s mode=%s %%=%s", h.id, desired.mode, desired.percent)
        except IpmiError as e:
            log.warning("reapply %s failed: %s", h.id, e)
            with runtime_lock:
                st = runtime.get(h.id) or _empty_status(h)
                st["last_error"] = f"reapply: {e}"
                runtime[h.id] = st


def _poll_loop() -> None:
    log.info("poll loop started (interval=%ss)", cfg.poll_interval_seconds)
    while not stop_event.is_set():
        for h in db.list_hosts():
            if stop_event.is_set():
                break
            try:
                _poll_one(h)
            except Exception:
                log.exception("poll error %s", h.id)
        stop_event.wait(cfg.poll_interval_seconds)


def _reapply_loop() -> None:
    interval = max(10.0, cfg.reapply_interval_seconds)
    log.info("reapply loop started (interval=%ss)", interval)
    stop_event.wait(3)
    if not stop_event.is_set():
        _reapply_all()
    while not stop_event.is_set():
        stop_event.wait(interval)
        if stop_event.is_set():
            break
        _reapply_all()


def _public_status(h: HostRow) -> dict[str, Any]:
    with runtime_lock:
        st = runtime.get(h.id)
        if st is None:
            st = _empty_status(h)
        else:
            st = dict(st)
    desired = db.get_fan_state(h.id, cfg.default_mode, cfg.default_percent)
    st["mode"] = desired.mode
    st["desired_percent"] = desired.percent
    # applied_percent blijft de live runtime-waarde als die er is
    if st.get("applied_percent") is None:
        st["applied_percent"] = desired.percent
    st["fan_ui"] = desired.fan_ui
    st["name"] = h.name
    st["host"] = h.host
    st["model"] = h.model or st.get("model")
    st["product"] = h.product or st.get("product")
    st["serial"] = h.serial or st.get("serial")
    st["source"] = h.source
    st["removable"] = h.source != "config"
    st["fan_presets"] = cfg.fan_presets
    if not st.get("detect"):
        st["detect"] = db.get_detect(h.id)
    return st


def _seed_admin_user() -> None:
    if db.user_count() > 0:
        return
    user, password = default_admin_credentials()
    db.create_user(user, hash_password(password))
    log.warning(
        "Default dashboard account created: user=%s password=%s  — change this!",
        user,
        password if password == "admin123" else "(from DASHBOARD_PASSWORD)",
    )


def _user_from_token(token: str | None) -> AuthUser | None:
    if not token:
        return None
    db.purge_expired_sessions()
    row = db.get_session_user(token)
    if not row:
        return None
    full = db.get_user_by_id(int(row["id"]))
    if not full or not full.get("active", True):
        db.delete_session(token)
        return None
    return AuthUser(id=int(row["id"]), username=str(row["username"]))


def _extract_token(
    authorization: str | None = None,
    x_session_token: str | None = None,
) -> str | None:
    if x_session_token:
        return x_session_token.strip()
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


async def require_user(
    authorization: str | None = Header(default=None),
    x_session_token: str | None = Header(default=None, alias="X-Session-Token"),
) -> AuthUser:
    token = _extract_token(authorization, x_session_token)
    user = _user_from_token(token)
    if not user:
        raise HTTPException(401, "Niet ingelogd")
    return user


@asynccontextmanager
async def lifespan(app: FastAPI):
    global cfg, db, smart_engine
    cfg = load_config()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    db_path = cfg.data_dir / "idrac.db"
    db = Database(db_path)
    db.seed_config_hosts(cfg.hosts, cfg.default_mode, cfg.default_percent)
    _seed_admin_user()
    _load_persisted_settings()
    # Smart Auto stays capped by its curve bands (typically 30%); manual may go higher.
    smart_auto_cap = max((b.percent for b in cfg.smart_auto.bands), default=30)
    smart_auto_cap = min(smart_auto_cap, cfg.fan_max_percent)
    smart_engine = SmartAutoEngine(cfg.smart_auto, fan_max=smart_auto_cap)
    stop_event.clear()
    t1 = threading.Thread(target=_poll_loop, name="poll", daemon=True)
    t2 = threading.Thread(target=_reapply_loop, name="reapply", daemon=True)
    t1.start()
    t2.start()
    _bg_threads[:] = [t1, t2]
    log.info(
        "idrac-dashboard ready — db=%s hosts=%s mock=%s",
        db_path,
        len(db.list_hosts()),
        cfg.mock_ipmi,
    )
    yield
    stop_event.set()
    for t in _bg_threads:
        t.join(timeout=2)


app = FastAPI(title="iDRAC Fan Dashboard", version="1.0.0", lifespan=lifespan)

if STATIC.is_dir():
    app.mount("/assets", StaticFiles(directory=STATIC), name="assets")


class ModeBody(BaseModel):
    mode: Literal["smart_auto", "manual", "dell_auto"] | None = None
    # UI: Auto toggle → smart_auto; checkbox dell_auto True/False
    auto: bool | None = None
    dell_auto: bool | None = None


class FanBody(BaseModel):
    percent: int = Field(..., ge=1, le=100)
    # preset = knop; custom = handmatige invoer (geen knop-highlight)
    source: Literal["preset", "custom"] = "preset"


class AddHostBody(BaseModel):
    host: str
    name: str | None = None
    username: str | None = None
    password: str | None = None
    detect: bool = True


class EditHostBody(BaseModel):
    name: str | None = None
    host: str | None = None
    username: str | None = None
    # leeg = ongewijzigd; "__clear__" = wis opgeslagen wachtwoord
    password: str | None = None


class LoginBody(BaseModel):
    username: str
    password: str


class PasswordBody(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=6)


class UsernameBody(BaseModel):
    current_password: str
    new_username: str = Field(..., min_length=3, max_length=32)


class CreateUserBody(BaseModel):
    username: str = Field(..., min_length=3, max_length=32)
    password: str = Field(..., min_length=6)


SESSION_DAYS = 14
SETTINGS_KEY = "app_settings"


def _settings_dict() -> dict[str, Any]:
    return {
        "poll_interval_seconds": cfg.poll_interval_seconds,
        "reapply_interval_seconds": cfg.reapply_interval_seconds,
        "fan_min_percent": cfg.fan_min_percent,
        "fan_max_percent": cfg.fan_max_percent,
        "fan_presets": list(cfg.fan_presets),
        "default_mode": cfg.default_mode,
        "default_percent": cfg.default_percent,
        "reapply_on_host_online": cfg.reapply_on_host_online,
        "mock": cfg.mock_ipmi,
        "database": str(cfg.data_dir / "idrac.db"),
        "host_count": len(db.list_hosts()) if db else 0,
    }


def _apply_runtime_settings(data: dict[str, Any]) -> None:
    """Update live cfg from settings payload (validated)."""
    if "poll_interval_seconds" in data:
        v = float(data["poll_interval_seconds"])
        cfg.poll_interval_seconds = max(2.0, min(60.0, v))
    if "reapply_interval_seconds" in data:
        v = float(data["reapply_interval_seconds"])
        cfg.reapply_interval_seconds = max(10.0, min(600.0, v))
    if "fan_min_percent" in data:
        cfg.fan_min_percent = max(1, min(100, int(data["fan_min_percent"])))
    if "fan_max_percent" in data:
        cfg.fan_max_percent = max(1, min(100, int(data["fan_max_percent"])))
    if cfg.fan_min_percent > cfg.fan_max_percent:
        cfg.fan_min_percent, cfg.fan_max_percent = cfg.fan_max_percent, cfg.fan_min_percent
    if "fan_presets" in data and isinstance(data["fan_presets"], list):
        presets = sorted({int(p) for p in data["fan_presets"] if 1 <= int(p) <= 100})
        presets = [p for p in presets if cfg.fan_min_percent <= p <= cfg.fan_max_percent]
        if presets:
            cfg.fan_presets = presets
    if "default_mode" in data and data["default_mode"] in MODES:
        cfg.default_mode = data["default_mode"]  # type: ignore[assignment]
    if "default_percent" in data:
        p = int(data["default_percent"])
        if cfg.fan_min_percent <= p <= cfg.fan_max_percent:
            cfg.default_percent = p
        elif cfg.fan_presets:
            cfg.default_percent = cfg.fan_presets[0]
    if "reapply_on_host_online" in data:
        cfg.reapply_on_host_online = bool(data["reapply_on_host_online"])


def _load_persisted_settings() -> None:
    raw = db.get_meta(SETTINGS_KEY)
    if not raw:
        return
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            _apply_runtime_settings(data)
            log.info("Loaded settings from database")
    except json.JSONDecodeError:
        log.warning("Invalid settings JSON in meta, ignoring")


def _save_persisted_settings() -> None:
    payload = {
        "poll_interval_seconds": cfg.poll_interval_seconds,
        "reapply_interval_seconds": cfg.reapply_interval_seconds,
        "fan_min_percent": cfg.fan_min_percent,
        "fan_max_percent": cfg.fan_max_percent,
        "fan_presets": list(cfg.fan_presets),
        "default_mode": cfg.default_mode,
        "default_percent": cfg.default_percent,
        "reapply_on_host_online": cfg.reapply_on_host_online,
    }
    db.set_meta(SETTINGS_KEY, json.dumps(payload))


@app.get("/")
async def index():
    index_path = STATIC / "index.html"
    if not index_path.is_file():
        raise HTTPException(404, "static/index.html missing")
    return FileResponse(index_path)


@app.get("/api/meta")
async def meta():
    return {
        "fan_presets": cfg.fan_presets,
        "fan_min_percent": cfg.fan_min_percent,
        "fan_max_percent": cfg.fan_max_percent,
        "default_mode": cfg.default_mode,
        "default_percent": cfg.default_percent,
        "mock": cfg.mock_ipmi,
        "poll_interval_seconds": cfg.poll_interval_seconds,
        "reapply_interval_seconds": cfg.reapply_interval_seconds,
        "reapply_on_host_online": cfg.reapply_on_host_online,
        "modes": list(MODES),
        "database": str(cfg.data_dir / "idrac.db"),
        "auth_required": True,
    }


@app.get("/api/settings")
async def get_settings(user: AuthUser = Depends(require_user)):
    return _settings_dict()


class SettingsBody(BaseModel):
    poll_interval_seconds: float | None = None
    reapply_interval_seconds: float | None = None
    fan_min_percent: int | None = None
    fan_max_percent: int | None = None
    fan_presets: list[int] | None = None
    default_mode: str | None = None
    default_percent: int | None = None
    reapply_on_host_online: bool | None = None


@app.put("/api/settings")
async def put_settings(body: SettingsBody, user: AuthUser = Depends(require_user)):
    data = body.model_dump(exclude_none=True)
    if not data:
        raise HTTPException(400, "geen instellingen opgegeven")
    _apply_runtime_settings(data)
    _save_persisted_settings()
    # keep smart auto cap in sync
    global smart_engine
    smart_auto_cap = max((b.percent for b in cfg.smart_auto.bands), default=30)
    smart_auto_cap = min(smart_auto_cap, cfg.fan_max_percent)
    smart_engine = SmartAutoEngine(cfg.smart_auto, fan_max=smart_auto_cap)
    return _settings_dict()


@app.post("/api/auth/login")
async def login(body: LoginBody):
    username = body.username.strip()
    row = db.get_user_by_username(username)
    if not row or not verify_password(body.password, row["password_hash"]):
        raise HTTPException(401, "Ongeldige gebruikersnaam of wachtwoord")
    if not row.get("active", True):
        raise HTTPException(403, "Dit account is gedeactiveerd")
    token = new_session_token()
    # expires via SQLite for easy purge
    with db._db() as conn:
        conn.execute(
            """
            INSERT INTO sessions (token, user_id, expires_at)
            VALUES (?, ?, datetime('now', ?))
            """,
            (token, row["id"], f"+{SESSION_DAYS} days"),
        )
    return {
        "token": token,
        "username": row["username"],
        "expires_days": SESSION_DAYS,
    }


@app.get("/api/auth/me")
async def auth_me(user: AuthUser = Depends(require_user)):
    return {"username": user.username, "id": user.id}


@app.post("/api/auth/logout")
async def logout(
    authorization: str | None = Header(default=None),
    x_session_token: str | None = Header(default=None, alias="X-Session-Token"),
):
    token = _extract_token(authorization, x_session_token)
    if token:
        db.delete_session(token)
    return {"ok": True}


@app.post("/api/auth/password")
async def change_password(body: PasswordBody, user: AuthUser = Depends(require_user)):
    row = db.get_user_by_id(user.id)
    if not row or not verify_password(body.current_password, row["password_hash"]):
        raise HTTPException(400, "Huidig wachtwoord is onjuist")
    db.set_user_password(user.id, hash_password(body.new_password))
    # invalidate other sessions
    db.delete_user_sessions(user.id)
    # new session for current client
    token = new_session_token()
    with db._db() as conn:
        conn.execute(
            """
            INSERT INTO sessions (token, user_id, expires_at)
            VALUES (?, ?, datetime('now', ?))
            """,
            (token, user.id, f"+{SESSION_DAYS} days"),
        )
    return {"ok": True, "token": token, "username": user.username}


@app.post("/api/auth/username")
async def change_username(body: UsernameBody, user: AuthUser = Depends(require_user)):
    row = db.get_user_by_id(user.id)
    if not row or not verify_password(body.current_password, row["password_hash"]):
        raise HTTPException(400, "Huidig wachtwoord is onjuist")
    new_name = body.new_username.strip()
    if not re.fullmatch(r"[a-zA-Z0-9_]{3,32}", new_name):
        raise HTTPException(400, "username: 3–32 tekens, letters/cijfers/_")
    try:
        db.set_username(user.id, new_name)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True, "username": new_name}


@app.get("/api/auth/users")
async def list_users(user: AuthUser = Depends(require_user)):
    return {
        "users": db.list_users(),
        "online": db.list_active_sessions(),
        "me": {"id": user.id, "username": user.username},
    }


@app.post("/api/auth/users")
async def create_user(body: CreateUserBody, user: AuthUser = Depends(require_user)):
    name = body.username.strip()
    if not re.fullmatch(r"[a-zA-Z0-9_]{3,32}", name):
        raise HTTPException(400, "username: 3–32 tekens, letters/cijfers/_")
    if db.get_user_by_username(name):
        raise HTTPException(400, "gebruikersnaam bestaat al")
    uid = db.create_user(name, hash_password(body.password))
    return {"ok": True, "id": uid, "username": name}


@app.delete("/api/auth/users/{user_id}")
async def delete_user(user_id: int, user: AuthUser = Depends(require_user)):
    if user_id == user.id:
        raise HTTPException(400, "je kan jezelf niet verwijderen")
    try:
        ok = db.delete_user(user_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if not ok:
        raise HTTPException(404, "user not found")
    return {"ok": True}


class ActiveBody(BaseModel):
    active: bool


@app.post("/api/auth/users/{user_id}/active")
async def set_user_active(
    user_id: int, body: ActiveBody, user: AuthUser = Depends(require_user)
):
    """Deactivate or reactivate an account (e.g. default admin)."""
    if user_id == user.id and not body.active:
        raise HTTPException(
            400,
            "je kan jezelf niet deactiveren — log in met een ander account",
        )
    try:
        db.set_user_active(user_id, body.active)
    except KeyError:
        raise HTTPException(404, "user not found") from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True, "id": user_id, "active": body.active}


@app.get("/api/hosts")
async def list_hosts(user: AuthUser = Depends(require_user)):
    return {"hosts": [_public_status(h) for h in db.list_hosts()]}


@app.get("/api/hosts/{host_id}")
async def get_host(host_id: str, user: AuthUser = Depends(require_user)):
    h = db.get_host(host_id)
    if not h:
        raise HTTPException(404, "host not found")
    return _public_status(h)


@app.post("/api/hosts")
async def add_host(body: AddHostBody, user: AuthUser = Depends(require_user)):
    """Add iDRAC node to internal DB; auto-detect model & sensors."""
    address = body.host.strip()
    if not address:
        raise HTTPException(400, "host (IP/hostname) is required")
    if db.get_host_by_address(address):
        raise HTTPException(400, f"host {address} already exists")

    detect_info = None
    display_name = body.name
    model = product = serial = None
    det_dict = None

    if body.detect:
        user = body.username or cfg.credentials_username
        password = body.password or cfg.credentials_password
        if not password and not cfg.mock_ipmi:
            raise HTTPException(
                400,
                "password required (body.password or IDRAC_PASSWORD env)",
            )
        probe = IpmiClient(
            host=address,
            username=user or "root",
            password=password or "mock",
            timeout=cfg.ipmi_timeout_seconds,
            mock=cfg.mock_ipmi,
            host_id=f"probe-{address}",
        )
        detect_info = probe.detect()
        if not detect_info.online:
            raise HTTPException(
                400,
                f"iDRAC niet bereikbaar: {detect_info.error or 'unknown error'}",
            )
        model = detect_info.model
        product = detect_info.product
        serial = detect_info.serial
        if not display_name and model:
            display_name = f"{model}"
        det_dict = {
            "manufacturer": detect_info.manufacturer,
            "product": detect_info.product,
            "model": detect_info.model,
            "serial": detect_info.serial,
            "firmware": detect_info.firmware,
            "ipmi_version": detect_info.ipmi_version,
            "temp_sensors": detect_info.temp_sensors,
            "fan_sensors": detect_info.fan_sensors,
        }

    if not display_name:
        display_name = address

    base = _slug(display_name)
    hid = base
    n = 2
    while db.get_host(hid):
        hid = f"{base}-{n}"
        n += 1

    h = db.upsert_host(
        id=hid,
        name=display_name,
        host=address,
        username=body.username,
        password=body.password,
        model=model,
        product=product,
        serial=serial,
        source="ui",
        default_mode=cfg.default_mode,
        default_percent=cfg.default_percent,
    )
    db.set_fan_mode(h.id, cfg.default_mode)

    if det_dict:
        db.save_detect(h.id, det_dict)

    try:
        desired = db.get_fan_state(h.id, cfg.default_mode, cfg.default_percent)
        _apply_mode(h, desired.mode, desired.percent)
    except IpmiError as e:
        log.warning("initial apply after add failed: %s", e)

    try:
        _poll_one(h)
    except Exception:
        log.exception("poll after add")

    out = _public_status(h)
    if det_dict:
        out["detect"] = det_dict
    return out


@app.post("/api/hosts/{host_id}/detect")
async def detect_host(host_id: str, user: AuthUser = Depends(require_user)):
    h = db.get_host(host_id)
    if not h:
        raise HTTPException(404, "host not found")
    client = _client_for(h)
    # Run blocking IPMI in a thread so we don't block the event loop / poll
    info = await asyncio.to_thread(client.detect)
    if not info.online:
        raise HTTPException(400, info.error or "detect failed")
    det = {
        "manufacturer": info.manufacturer,
        "product": info.product,
        "model": info.model,
        "serial": info.serial,
        "firmware": info.firmware,
        "ipmi_version": info.ipmi_version,
        "temp_sensors": info.temp_sensors,
        "fan_sensors": info.fan_sensors,
    }
    if info.error:
        det["warning"] = info.error
    db.save_detect(h.id, det)
    # optional rename if still generic IP
    new_name = None
    if info.model and (h.name == h.host or not h.name):
        new_name = info.model
    db.update_host_meta(
        h.id,
        model=info.model,
        product=info.product,
        serial=info.serial,
        name=new_name,
    )
    h2 = db.get_host(host_id) or h
    with runtime_lock:
        st = runtime.get(h.id) or _empty_status(h2)
        st["detect"] = det
        st["model"] = info.model or st.get("model")
        st["product"] = info.product or st.get("product")
        st["serial"] = info.serial or st.get("serial")
        runtime[h.id] = st
    out = _public_status(h2)
    out["detect"] = det
    return out


async def _edit_host_impl(host_id: str, body: EditHostBody):
    """Rename / reconfigure a panel (e.g. label as rack-01)."""
    h = db.get_host(host_id)
    if not h:
        raise HTTPException(404, "host not found")
    try:
        clear_pw = body.password == "__clear__"
        pw = None if clear_pw else body.password
        updated = db.update_host(
            host_id,
            name=body.name,
            host=body.host,
            username=body.username,
            password=pw,
            clear_password=clear_pw,
        )
    except KeyError:
        raise HTTPException(404, "host not found") from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    # Drop cached IPMI client so new host/creds are used
    with runtime_lock:
        clients.pop(host_id, None)
        st = runtime.get(host_id)
        if st:
            st["name"] = updated.name
            st["host"] = updated.host
            runtime[host_id] = st

    # Quick poll with new settings
    try:
        await asyncio.to_thread(_poll_one, updated)
    except Exception:
        log.exception("poll after edit")

    return _public_status(updated)


@app.patch("/api/hosts/{host_id}")
async def edit_host_patch(host_id: str, body: EditHostBody, user: AuthUser = Depends(require_user)):
    return await _edit_host_impl(host_id, body)


@app.put("/api/hosts/{host_id}")
async def edit_host_put(host_id: str, body: EditHostBody, user: AuthUser = Depends(require_user)):
    return await _edit_host_impl(host_id, body)


@app.post("/api/hosts/{host_id}/edit")
async def edit_host_post(host_id: str, body: EditHostBody, user: AuthUser = Depends(require_user)):
    return await _edit_host_impl(host_id, body)


@app.delete("/api/hosts/{host_id}")
async def delete_host(host_id: str, user: AuthUser = Depends(require_user)):
    try:
        ok = db.delete_host(host_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if not ok:
        raise HTTPException(404, "host not found")
    with runtime_lock:
        runtime.pop(host_id, None)
        clients.pop(host_id, None)
    return {"ok": True}


@app.post("/api/hosts/{host_id}/mode")
async def set_mode(host_id: str, body: ModeBody, user: AuthUser = Depends(require_user)):
    h = db.get_host(host_id)
    if not h:
        raise HTTPException(404, "host not found")

    # Resolve mode from UI fields
    mode: Mode
    if body.mode and body.mode in MODES:
        mode = body.mode  # type: ignore[assignment]
    elif body.auto is False:
        mode = "manual"
    elif body.dell_auto is True:
        mode = "dell_auto"
    elif body.auto is True or body.dell_auto is False:
        mode = "smart_auto"
    else:
        raise HTTPException(400, "provide mode or auto/dell_auto")

    if body.dell_auto is True:
        mode = "dell_auto"
    elif body.dell_auto is False and mode == "dell_auto":
        mode = "smart_auto"

    desired = db.get_fan_state(host_id, cfg.default_mode, cfg.default_percent)
    percent = desired.percent
    if percent < cfg.fan_min_percent or percent > cfg.fan_max_percent:
        percent = cfg.default_percent

    try:
        if mode == "smart_auto":
            with runtime_lock:
                st = runtime.get(host_id)
            if st and st.get("temps"):
                t = st["temps"]
                sensors = SensorBundle(
                    inlet=t.get("inlet"),
                    outlet=t.get("outlet"),
                    cpu=[
                        TempReading(c["label"], c["celsius"])
                        for c in (t.get("cpu") or [])
                    ],
                    cpu_max=t.get("cpu_max"),
                    online=True,
                )
                percent = smart_engine.compute(host_id, sensors).percent
            _apply_mode(h, "smart_auto", percent)
        else:
            _apply_mode(h, mode, percent)
    except IpmiError as e:
        raise HTTPException(502, str(e)) from e

    db.set_fan_mode(host_id, mode)
    db.set_fan_percent(host_id, percent)
    with runtime_lock:
        st = runtime.get(host_id) or _empty_status(h)
        st["mode"] = mode
        st["desired_percent"] = percent
        st["applied_percent"] = percent if mode != "dell_auto" else None
        st["last_error"] = None
        runtime[host_id] = st
    return _public_status(h)


@app.post("/api/hosts/{host_id}/fan")
async def set_fan(host_id: str, body: FanBody, user: AuthUser = Depends(require_user)):
    h = db.get_host(host_id)
    if not h:
        raise HTTPException(404, "host not found")
    percent = int(body.percent)
    if percent < cfg.fan_min_percent or percent > cfg.fan_max_percent:
        raise HTTPException(
            400,
            f"percent must be between {cfg.fan_min_percent} and {cfg.fan_max_percent}",
        )

    fan_ui = body.source if body.source in ("preset", "custom") else "preset"
    try:
        _apply_mode(h, "manual", percent)
    except IpmiError as e:
        raise HTTPException(502, str(e)) from e

    db.set_manual_percent(host_id, percent, fan_ui=fan_ui)
    smart_engine.note_applied(host_id, percent)
    with runtime_lock:
        st = runtime.get(host_id) or _empty_status(h)
        st["mode"] = "manual"
        st["desired_percent"] = percent
        st["applied_percent"] = percent
        st["fan_ui"] = fan_ui
        st["last_error"] = None
        runtime[host_id] = st
    return _public_status(h)


def run():
    import uvicorn

    c = load_config()
    uvicorn.run(
        "app.main:app",
        host=c.bind_host,
        port=c.bind_port,
        reload=False,
    )


if __name__ == "__main__":
    run()
