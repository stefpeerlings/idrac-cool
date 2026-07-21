#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -d .venv ]]; then
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
fi
# shellcheck disable=SC1091
source .venv/bin/activate

HOST="${BIND_HOST:-0.0.0.0}"
PORT="${BIND_PORT:-8787}"
UV_ARGS=(--host "$HOST" --port "$PORT")

if [[ -n "${SSL_CERTFILE:-}" && -n "${SSL_KEYFILE:-}" ]]; then
  UV_ARGS+=(--ssl-certfile "$SSL_CERTFILE" --ssl-keyfile "$SSL_KEYFILE")
elif [[ -n "${SSL_CERTFILE:-}${SSL_KEYFILE:-}" ]]; then
  echo "Zet SSL_CERTFILE én SSL_KEYFILE (beide), of geen van beide."
  exit 1
fi

exec python -m uvicorn app.main:app "${UV_ARGS[@]}"
