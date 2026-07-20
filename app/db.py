"""Internal SQLite database for hosts, fan state, and detect cache."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Literal

Mode = Literal["smart_auto", "manual", "dell_auto"]
MODES = ("smart_auto", "manual", "dell_auto")

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS hosts (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    host        TEXT NOT NULL UNIQUE,
    username    TEXT,
    password    TEXT,
    model       TEXT,
    product     TEXT,
    serial      TEXT,
    source      TEXT NOT NULL DEFAULT 'ui',  -- ui | config
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS fan_state (
    host_id     TEXT PRIMARY KEY REFERENCES hosts(id) ON DELETE CASCADE,
    mode        TEXT NOT NULL DEFAULT 'smart_auto',
    percent     INTEGER NOT NULL DEFAULT 20,
    fan_ui      TEXT NOT NULL DEFAULT 'preset',  -- preset | custom (manual highlight)
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS detect_cache (
    host_id         TEXT PRIMARY KEY REFERENCES hosts(id) ON DELETE CASCADE,
    manufacturer    TEXT,
    product         TEXT,
    model           TEXT,
    serial          TEXT,
    firmware        TEXT,
    ipmi_version    TEXT,
    temp_sensors    TEXT,  -- JSON array
    fan_sensors     TEXT,  -- JSON array
    raw_json        TEXT,
    detected_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
    token       TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at  TEXT NOT NULL
);
"""


@dataclass
class HostRow:
    id: str
    name: str
    host: str
    username: str | None = None
    password: str | None = None
    model: str | None = None
    product: str | None = None
    serial: str | None = None
    source: str = "ui"


@dataclass
class FanStateRow:
    host_id: str
    mode: Mode = "smart_auto"
    percent: int = 20
    fan_ui: str = "preset"  # preset | custom


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self._connect()
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def _init(self) -> None:
        with self._db() as conn:
            conn.executescript(SCHEMA)
            # migrate older DBs
            cols = {r[1] for r in conn.execute("PRAGMA table_info(fan_state)").fetchall()}
            if "fan_ui" not in cols:
                conn.execute(
                    "ALTER TABLE fan_state ADD COLUMN fan_ui TEXT NOT NULL DEFAULT 'preset'"
                )
            ucols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
            if "active" not in ucols:
                conn.execute(
                    "ALTER TABLE users ADD COLUMN active INTEGER NOT NULL DEFAULT 1"
                )

    # ── users / sessions ───────────────────────────────────

    def user_count(self) -> int:
        with self._db() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    def create_user(self, username: str, password_hash: str) -> int:
        with self._db() as conn:
            cur = conn.execute(
                "INSERT INTO users (username, password_hash) VALUES (?, ?)",
                (username, password_hash),
            )
            return int(cur.lastrowid)

    def _user_dict(self, r: sqlite3.Row) -> dict[str, Any]:
        keys = r.keys()
        active = int(r["active"]) if "active" in keys else 1
        return {
            "id": r["id"],
            "username": r["username"],
            "password_hash": r["password_hash"],
            "active": bool(active),
        }

    def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        with self._db() as conn:
            r = conn.execute(
                "SELECT * FROM users WHERE username = ?",
                (username,),
            ).fetchone()
        if not r:
            return None
        return self._user_dict(r)

    def get_user_by_id(self, user_id: int) -> dict[str, Any] | None:
        with self._db() as conn:
            r = conn.execute(
                "SELECT * FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        if not r:
            return None
        return self._user_dict(r)

    def set_user_password(self, user_id: int, password_hash: str) -> None:
        with self._db() as conn:
            conn.execute(
                """
                UPDATE users SET password_hash = ?, updated_at = datetime('now')
                WHERE id = ?
                """,
                (password_hash, user_id),
            )

    def set_username(self, user_id: int, username: str) -> None:
        username = username.strip()
        if not username:
            raise ValueError("username is required")
        with self._db() as conn:
            clash = conn.execute(
                "SELECT id FROM users WHERE username = ? AND id != ?",
                (username, user_id),
            ).fetchone()
            if clash:
                raise ValueError("gebruikersnaam bestaat al")
            conn.execute(
                """
                UPDATE users SET username = ?, updated_at = datetime('now')
                WHERE id = ?
                """,
                (username, user_id),
            )

    def list_users(self) -> list[dict[str, Any]]:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT id, username, created_at, active FROM users ORDER BY username ASC"
            ).fetchall()
        return [
            {
                "id": r["id"],
                "username": r["username"],
                "created_at": r["created_at"],
                "active": bool(r["active"]) if "active" in r.keys() else True,
            }
            for r in rows
        ]

    def list_active_sessions(self) -> list[dict[str, Any]]:
        """Who is currently logged in (non-expired sessions)."""
        with self._db() as conn:
            rows = conn.execute(
                """
                SELECT s.token, s.user_id, s.created_at, u.username
                FROM sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.expires_at > datetime('now')
                  AND COALESCE(u.active, 1) = 1
                ORDER BY s.created_at DESC
                """
            ).fetchall()
        # one row per user (most recent session)
        seen: set[int] = set()
        out: list[dict[str, Any]] = []
        for r in rows:
            uid = int(r["user_id"])
            if uid in seen:
                continue
            seen.add(uid)
            out.append({
                "user_id": uid,
                "username": r["username"],
                "since": r["created_at"],
            })
        return out

    def count_active_users(self) -> int:
        with self._db() as conn:
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM users WHERE COALESCE(active, 1) = 1"
                ).fetchone()[0]
            )

    def set_user_active(self, user_id: int, active: bool) -> None:
        with self._db() as conn:
            row = conn.execute("SELECT id, active FROM users WHERE id = ?", (user_id,)).fetchone()
            if not row:
                raise KeyError(user_id)
            if not active:
                # must leave at least one active account
                n_active = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM users WHERE COALESCE(active, 1) = 1"
                    ).fetchone()[0]
                )
                is_active = bool(row["active"]) if "active" in row.keys() else True
                if is_active and n_active <= 1:
                    raise ValueError(
                        "kan laatste actieve account niet deactiveren — maak eerst een ander account"
                    )
            conn.execute(
                """
                UPDATE users SET active = ?, updated_at = datetime('now')
                WHERE id = ?
                """,
                (1 if active else 0, user_id),
            )
            if not active:
                conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    def delete_user(self, user_id: int) -> bool:
        with self._db() as conn:
            # keep at least one user
            n = int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])
            if n <= 1:
                raise ValueError("kan laatste account niet verwijderen")
            row = conn.execute(
                "SELECT active FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            if not row:
                return False
            is_active = bool(row["active"]) if "active" in row.keys() else True
            if is_active:
                n_active = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM users WHERE COALESCE(active, 1) = 1"
                    ).fetchone()[0]
                )
                if n_active <= 1:
                    raise ValueError(
                        "kan laatste actieve account niet verwijderen — deactiveer niet, of maak eerst een ander account"
                    )
            cur = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
            return cur.rowcount > 0

    def create_session(self, user_id: int, token: str, expires_at: str) -> None:
        with self._db() as conn:
            conn.execute(
                "INSERT INTO sessions (token, user_id, expires_at) VALUES (?, ?, ?)",
                (token, user_id, expires_at),
            )

    def get_session_user(self, token: str) -> dict[str, Any] | None:
        with self._db() as conn:
            r = conn.execute(
                """
                SELECT u.id, u.username, s.expires_at
                FROM sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.token = ?
                  AND s.expires_at > datetime('now')
                """,
                (token,),
            ).fetchone()
        if not r:
            return None
        return {
            "id": r["id"],
            "username": r["username"],
            "expires_at": r["expires_at"],
        }

    def delete_session(self, token: str) -> None:
        with self._db() as conn:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))

    def delete_user_sessions(self, user_id: int) -> None:
        with self._db() as conn:
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    def purge_expired_sessions(self) -> None:
        with self._db() as conn:
            conn.execute(
                "DELETE FROM sessions WHERE expires_at < datetime('now')"
            )

    def get_meta(self, key: str) -> str | None:
        with self._db() as conn:
            r = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return r["value"] if r else None

    def set_meta(self, key: str, value: str) -> None:
        with self._db() as conn:
            conn.execute(
                """
                INSERT INTO meta (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    # ── hosts ──────────────────────────────────────────────

    def list_hosts(self) -> list[HostRow]:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM hosts ORDER BY created_at ASC, name ASC"
            ).fetchall()
        return [self._host_row(r) for r in rows]

    def get_host(self, host_id: str) -> HostRow | None:
        with self._db() as conn:
            r = conn.execute("SELECT * FROM hosts WHERE id = ?", (host_id,)).fetchone()
        return self._host_row(r) if r else None

    def get_host_by_address(self, address: str) -> HostRow | None:
        with self._db() as conn:
            r = conn.execute("SELECT * FROM hosts WHERE host = ?", (address,)).fetchone()
        return self._host_row(r) if r else None

    def upsert_host(
        self,
        *,
        id: str,
        name: str,
        host: str,
        username: str | None = None,
        password: str | None = None,
        model: str | None = None,
        product: str | None = None,
        serial: str | None = None,
        source: str = "ui",
        default_mode: Mode = "smart_auto",
        default_percent: int = 20,
    ) -> HostRow:
        with self._db() as conn:
            by_id = conn.execute("SELECT * FROM hosts WHERE id = ?", (id,)).fetchone()
            by_addr = conn.execute("SELECT * FROM hosts WHERE host = ?", (host,)).fetchone()
            if by_addr and by_addr["id"] != id:
                # Same IP already under another id — update that row instead of violating UNIQUE
                id = by_addr["id"]
                by_id = by_addr
            if by_id:
                conn.execute(
                    """
                    UPDATE hosts SET
                        name = ?, host = ?,
                        username = COALESCE(?, username),
                        password = COALESCE(?, password),
                        model = COALESCE(?, model),
                        product = COALESCE(?, product),
                        serial = COALESCE(?, serial),
                        source = ?,
                        updated_at = datetime('now')
                    WHERE id = ?
                    """,
                    (name, host, username, password, model, product, serial, source, id),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO hosts (id, name, host, username, password, model, product, serial, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (id, name, host, username, password, model, product, serial, source),
                )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO fan_state (host_id, mode, percent)
                    VALUES (?, ?, ?)
                    """,
                    (id, default_mode, default_percent),
                )
            # ensure fan_state exists when updating existing host without state
            conn.execute(
                """
                INSERT OR IGNORE INTO fan_state (host_id, mode, percent)
                VALUES (?, ?, ?)
                """,
                (id, default_mode, default_percent),
            )
            r = conn.execute("SELECT * FROM hosts WHERE id = ?", (id,)).fetchone()
        return self._host_row(r)

    def update_host_meta(
        self,
        host_id: str,
        *,
        model: str | None = None,
        product: str | None = None,
        serial: str | None = None,
        name: str | None = None,
    ) -> HostRow | None:
        with self._db() as conn:
            h = conn.execute("SELECT * FROM hosts WHERE id = ?", (host_id,)).fetchone()
            if not h:
                return None
            conn.execute(
                """
                UPDATE hosts SET
                    name = COALESCE(?, name),
                    model = COALESCE(?, model),
                    product = COALESCE(?, product),
                    serial = COALESCE(?, serial),
                    updated_at = datetime('now')
                WHERE id = ?
                """,
                (name, model, product, serial, host_id),
            )
            r = conn.execute("SELECT * FROM hosts WHERE id = ?", (host_id,)).fetchone()
        return self._host_row(r)

    def update_host(
        self,
        host_id: str,
        *,
        name: str | None = None,
        host: str | None = None,
        username: str | None = None,
        password: str | None = None,
        clear_password: bool = False,
    ) -> HostRow:
        """Edit panel fields. Empty password string keeps existing unless clear_password."""
        with self._db() as conn:
            row = conn.execute("SELECT * FROM hosts WHERE id = ?", (host_id,)).fetchone()
            if not row:
                raise KeyError(host_id)
            new_name = name.strip() if name is not None else row["name"]
            new_host = host.strip() if host is not None else row["host"]
            if not new_name:
                raise ValueError("name is required")
            if not new_host:
                raise ValueError("host (IP/hostname) is required")
            # unique host check
            clash = conn.execute(
                "SELECT id FROM hosts WHERE host = ? AND id != ?",
                (new_host, host_id),
            ).fetchone()
            if clash:
                raise ValueError(f"host {new_host} already exists as {clash['id']}")

            if username is not None:
                new_user = username.strip() or None
            else:
                new_user = row["username"]

            if clear_password:
                new_pass = None
            elif password is not None and password != "":
                new_pass = password
            else:
                new_pass = row["password"]

            conn.execute(
                """
                UPDATE hosts SET
                    name = ?, host = ?, username = ?, password = ?,
                    updated_at = datetime('now')
                WHERE id = ?
                """,
                (new_name, new_host, new_user, new_pass, host_id),
            )
            r = conn.execute("SELECT * FROM hosts WHERE id = ?", (host_id,)).fetchone()
        return self._host_row(r)

    def delete_host(self, host_id: str) -> bool:
        with self._db() as conn:
            r = conn.execute("SELECT source FROM hosts WHERE id = ?", (host_id,)).fetchone()
            if not r:
                return False
            if r["source"] == "config":
                raise ValueError("cannot remove host defined in config.yaml")
            conn.execute("DELETE FROM hosts WHERE id = ?", (host_id,))
        return True

    def is_config_host(self, host_id: str) -> bool:
        with self._db() as conn:
            r = conn.execute("SELECT source FROM hosts WHERE id = ?", (host_id,)).fetchone()
        return bool(r and r["source"] == "config")

    # ── fan state ──────────────────────────────────────────

    def _fan_row(self, r: sqlite3.Row) -> FanStateRow:
        mode = r["mode"] if r["mode"] in MODES else "smart_auto"
        keys = r.keys()
        fan_ui = r["fan_ui"] if "fan_ui" in keys and r["fan_ui"] else "preset"
        if fan_ui not in ("preset", "custom"):
            fan_ui = "preset"
        return FanStateRow(
            host_id=r["host_id"],
            mode=mode,  # type: ignore[arg-type]
            percent=int(r["percent"]),
            fan_ui=fan_ui,
        )

    def get_fan_state(self, host_id: str, default_mode: Mode = "smart_auto", default_percent: int = 20) -> FanStateRow:
        with self._db() as conn:
            r = conn.execute("SELECT * FROM fan_state WHERE host_id = ?", (host_id,)).fetchone()
            if not r:
                conn.execute(
                    "INSERT INTO fan_state (host_id, mode, percent, fan_ui) VALUES (?, ?, ?, 'preset')",
                    (host_id, default_mode, default_percent),
                )
                return FanStateRow(
                    host_id=host_id, mode=default_mode, percent=default_percent, fan_ui="preset"
                )
            return self._fan_row(r)

    def set_fan_mode(self, host_id: str, mode: Mode) -> FanStateRow:
        if mode not in MODES:
            raise ValueError(f"invalid mode: {mode}")
        with self._db() as conn:
            conn.execute(
                """
                INSERT INTO fan_state (host_id, mode, percent, fan_ui) VALUES (?, ?, 20, 'preset')
                ON CONFLICT(host_id) DO UPDATE SET mode = excluded.mode, updated_at = datetime('now')
                """,
                (host_id, mode),
            )
            r = conn.execute("SELECT * FROM fan_state WHERE host_id = ?", (host_id,)).fetchone()
        return self._fan_row(r)

    def set_fan_percent(self, host_id: str, percent: int) -> FanStateRow:
        with self._db() as conn:
            conn.execute(
                """
                INSERT INTO fan_state (host_id, mode, percent, fan_ui) VALUES (?, 'smart_auto', ?, 'preset')
                ON CONFLICT(host_id) DO UPDATE SET
                    percent = excluded.percent,
                    fan_ui = 'preset',
                    updated_at = datetime('now')
                """,
                (host_id, percent),
            )
            r = conn.execute("SELECT * FROM fan_state WHERE host_id = ?", (host_id,)).fetchone()
        return self._fan_row(r)

    def set_manual_percent(self, host_id: str, percent: int, fan_ui: str = "preset") -> FanStateRow:
        if fan_ui not in ("preset", "custom"):
            fan_ui = "preset"
        with self._db() as conn:
            conn.execute(
                """
                INSERT INTO fan_state (host_id, mode, percent, fan_ui) VALUES (?, 'manual', ?, ?)
                ON CONFLICT(host_id) DO UPDATE SET
                    mode = 'manual',
                    percent = excluded.percent,
                    fan_ui = excluded.fan_ui,
                    updated_at = datetime('now')
                """,
                (host_id, percent, fan_ui),
            )
            r = conn.execute("SELECT * FROM fan_state WHERE host_id = ?", (host_id,)).fetchone()
        return self._fan_row(r)

    # ── detect cache ───────────────────────────────────────

    def save_detect(self, host_id: str, info: dict[str, Any]) -> None:
        with self._db() as conn:
            conn.execute(
                """
                INSERT INTO detect_cache (
                    host_id, manufacturer, product, model, serial,
                    firmware, ipmi_version, temp_sensors, fan_sensors, raw_json, detected_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(host_id) DO UPDATE SET
                    manufacturer = excluded.manufacturer,
                    product = excluded.product,
                    model = excluded.model,
                    serial = excluded.serial,
                    firmware = excluded.firmware,
                    ipmi_version = excluded.ipmi_version,
                    temp_sensors = excluded.temp_sensors,
                    fan_sensors = excluded.fan_sensors,
                    raw_json = excluded.raw_json,
                    detected_at = datetime('now')
                """,
                (
                    host_id,
                    info.get("manufacturer"),
                    info.get("product"),
                    info.get("model"),
                    info.get("serial"),
                    info.get("firmware"),
                    info.get("ipmi_version"),
                    json.dumps(info.get("temp_sensors") or []),
                    json.dumps(info.get("fan_sensors") or []),
                    json.dumps(info),
                ),
            )
            # mirror identity onto hosts row
            conn.execute(
                """
                UPDATE hosts SET
                    model = COALESCE(?, model),
                    product = COALESCE(?, product),
                    serial = COALESCE(?, serial),
                    updated_at = datetime('now')
                WHERE id = ?
                """,
                (info.get("model"), info.get("product"), info.get("serial"), host_id),
            )

    def get_detect(self, host_id: str) -> dict[str, Any] | None:
        with self._db() as conn:
            r = conn.execute(
                "SELECT * FROM detect_cache WHERE host_id = ?", (host_id,)
            ).fetchone()
        if not r:
            return None
        return {
            "manufacturer": r["manufacturer"],
            "product": r["product"],
            "model": r["model"],
            "serial": r["serial"],
            "firmware": r["firmware"],
            "ipmi_version": r["ipmi_version"],
            "temp_sensors": json.loads(r["temp_sensors"] or "[]"),
            "fan_sensors": json.loads(r["fan_sensors"] or "[]"),
            "detected_at": r["detected_at"],
        }

    # ── seed from config ───────────────────────────────────

    def seed_config_hosts(
        self,
        hosts: list[Any],
        default_mode: Mode = "smart_auto",
        default_percent: int = 20,
    ) -> None:
        """Upsert hosts from config.yaml (source=config). Does not delete UI hosts."""
        for h in hosts:
            self.upsert_host(
                id=h.id,
                name=h.name,
                host=h.host,
                username=h.username,
                password=h.password,
                source="config",
                default_mode=default_mode,
                default_percent=default_percent,
            )

    @staticmethod
    def _host_row(r: sqlite3.Row) -> HostRow:
        return HostRow(
            id=r["id"],
            name=r["name"],
            host=r["host"],
            username=r["username"],
            password=r["password"],
            model=r["model"],
            product=r["product"],
            serial=r["serial"],
            source=r["source"] or "ui",
        )
