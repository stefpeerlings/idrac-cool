#!/usr/bin/env bash
# Genereer self-signed cert voor lokale HTTPS (browser toont waarschuwing).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$ROOT/data/certs}"
DAYS="${2:-825}"
CN="${SSL_CN:-idrac-cool.local}"

mkdir -p "$OUT"
chmod 700 "$OUT"

openssl req -x509 -newkey rsa:2048 -sha256 -nodes \
  -keyout "$OUT/key.pem" \
  -out "$OUT/cert.pem" \
  -days "$DAYS" \
  -subj "/CN=${CN}" \
  -addext "subjectAltName=DNS:${CN},DNS:localhost,IP:127.0.0.1"

chmod 600 "$OUT/key.pem" "$OUT/cert.pem"
echo "Cert: $OUT/cert.pem"
echo "Key:  $OUT/key.pem"
echo
echo "Start met:"
echo "  export SSL_CERTFILE=$OUT/cert.pem"
echo "  export SSL_KEYFILE=$OUT/key.pem"
echo "  ./run-live.sh"
echo
echo "Open: https://<host-ip>:8787  (self-signed → browser-waarschuwing)"
