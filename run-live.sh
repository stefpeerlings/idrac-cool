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

echo "Starting LIVE on http://0.0.0.0:8787  (mock uit)"
exec .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8787
