#!/usr/bin/env bash
# Genereer self-signed cert voor HTTPS op de LXC/Pi IP.
# Browser toont een waarschuwing (self-signed) — Advanced → Proceed.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$ROOT/data/certs}"
DAYS="${2:-825}"
CN="${SSL_CN:-idrac-cool.local}"

# Optioneel: expliciet IP. Anders auto-detect.
IP="${3:-${SSL_IP:-}}"
if [[ -z "$IP" ]]; then
  IP="$(hostname -I 2>/dev/null | awk '{print $1}')" || true
fi
if [[ -z "$IP" ]]; then
  IP="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')" || true
fi

mkdir -p "$OUT"
chmod 700 "$OUT"

SAN="DNS:${CN},DNS:localhost,IP:127.0.0.1"
if [[ -n "$IP" ]]; then
  SAN="${SAN},IP:${IP}"
fi

# openssl config (werkt op Debian/Ubuntu OpenSSL 3)
TMPCONF="$(mktemp)"
trap 'rm -f "$TMPCONF"' EXIT
cat >"$TMPCONF" <<EOF
[req]
distinguished_name = req_dn
x509_extensions = v3_req
prompt = no

[req_dn]
CN = ${CN}

[v3_req]
subjectAltName = ${SAN}
basicConstraints = CA:FALSE
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
EOF

openssl req -x509 -newkey rsa:2048 -sha256 -nodes \
  -keyout "$OUT/key.pem" \
  -out "$OUT/cert.pem" \
  -days "$DAYS" \
  -config "$TMPCONF"

chmod 600 "$OUT/key.pem" "$OUT/cert.pem"

echo "Cert: $OUT/cert.pem"
echo "Key:  $OUT/key.pem"
echo "SAN:  $SAN"
echo
if [[ -n "$IP" ]]; then
  echo "Open: https://${IP}:8787"
else
  echo "Open: https://<host-ip>:8787"
fi
echo "(self-signed → browser-waarschuwing is normaal)"
