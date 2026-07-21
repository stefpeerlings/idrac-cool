#!/usr/bin/env bash
# Installeer iDRAC Cool in een LXC: HTTPS-only op https://<lxc-ip>:8787
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Draai als root (pct enter / sudo)."
  exit 1
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y python3 python3-venv python3-pip ipmitool git curl openssl

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
.venv/bin/pip install -q -r requirements.txt

if [[ ! -f config.yaml ]]; then
  cp config.example.yaml config.yaml
fi

# TLS cert met LXC-IP in SAN
mkdir -p data/certs
./scripts/gen-selfsigned-cert.sh "$ROOT/data/certs"
CERT="$ROOT/data/certs/cert.pem"
KEY="$ROOT/data/certs/key.pem"

# Env (behoud bestaand wachtwoord als al gezet)
if [[ ! -f /etc/idrac-cool.env ]]; then
  cat >/etc/idrac-cool.env <<EOF
IDRAC_PASSWORD=change-me
# IDRAC_USERNAME=root
# DASHBOARD_PASSWORD=change-me
SSL_CERTFILE=${CERT}
SSL_KEYFILE=${KEY}
EOF
  chmod 600 /etc/idrac-cool.env
  echo "Zet je iDRAC-wachtwoord: nano /etc/idrac-cool.env"
else
  # Zorg dat SSL regels erin staan
  grep -q '^SSL_CERTFILE=' /etc/idrac-cool.env || echo "SSL_CERTFILE=${CERT}" >>/etc/idrac-cool.env
  grep -q '^SSL_KEYFILE=' /etc/idrac-cool.env || echo "SSL_KEYFILE=${KEY}" >>/etc/idrac-cool.env
  # Update paden als ze al bestonden
  sed -i "s|^SSL_CERTFILE=.*|SSL_CERTFILE=${CERT}|" /etc/idrac-cool.env
  sed -i "s|^SSL_KEYFILE=.*|SSL_KEYFILE=${KEY}|" /etc/idrac-cool.env
  chmod 600 /etc/idrac-cool.env
fi

cat >/etc/systemd/system/idrac-cool.service <<EOF
[Unit]
Description=iDRAC Cool Fan Dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${ROOT}
EnvironmentFile=/etc/idrac-cool.env
Environment=PATH=${ROOT}/.venv/bin:/usr/bin
ExecStart=${ROOT}/.venv/bin/python -m app.main
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now idrac-cool

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "=========================================="
echo " iDRAC Cool draait op HTTPS (geen HTTP)."
echo " Open:  https://${IP:-<lxc-ip>}:8787"
echo " Browser: Advanced → Proceed (self-signed)"
echo "=========================================="
systemctl --no-pager --full status idrac-cool || true
