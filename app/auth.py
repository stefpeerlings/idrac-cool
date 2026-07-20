"""Password hashing and session tokens (stdlib only)."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass


def hash_password(password: str, *, iterations: int = 200_000) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), iterations
    )
    return f"pbkdf2_sha256${iterations}${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters_s, salt, digest = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        iterations = int(iters_s)
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt), iterations
        )
        return hmac.compare_digest(dk.hex(), digest)
    except (ValueError, TypeError):
        return False


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


@dataclass
class AuthUser:
    id: int
    username: str


def default_admin_credentials() -> tuple[str, str]:
    user = os.environ.get("DASHBOARD_USER", "admin").strip() or "admin"
    password = os.environ.get("DASHBOARD_PASSWORD", "admin123")
    return user, password
