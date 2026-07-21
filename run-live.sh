#!/usr/bin/env bash
# Live iDRAC dashboard (geen mock)
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v ipmitool >/dev/null 2>&1; then
  echo "ipmitool ontbreekt. Installeer met:"
  echo "  sudo apt install -y ipmitool"
  exit 1
fi

if [[ -z "${IDRAC_PASSWORD:-}" ]]; then
  echo "Zet eerst je iDRAC-wachtwoord:"
  echo "  export IDRAC_PASSWORD='jouw-wachtwoord'"
  echo "  export IDRAC_USERNAME=root   # optioneel"
  exit 1
fi

# Explicit geen mock
unset MOCK_IPMI || true
export MOCK_IPMI=0

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
fi

HOST="${BIND_HOST:-0.0.0.0}"
PORT="${BIND_PORT:-8787}"
UV_ARGS=(--host "$HOST" --port "$PORT")

if [[ -n "${SSL_CERTFILE:-}" && -n "${SSL_KEYFILE:-}" ]]; then
  UV_ARGS+=(--ssl-certfile "$SSL_CERTFILE" --ssl-keyfile "$SSL_KEYFILE")
  echo "Starting LIVE on https://${HOST}:${PORT}  (TLS, mock uit)"
elif [[ -n "${SSL_CERTFILE:-}${SSL_KEYFILE:-}" ]]; then
  echo "Zet SSL_CERTFILE én SSL_KEYFILE (beide), of geen van beide."
  exit 1
else
  echo "Starting LIVE on http://${HOST}:${PORT}  (mock uit)"
  echo "Tip: HTTPS via SSL_CERTFILE/SSL_KEYFILE of reverse proxy (NPM)."
fi

exec .venv/bin/python -m uvicorn app.main:app "${UV_ARGS[@]}"
