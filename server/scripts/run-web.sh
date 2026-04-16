#!/usr/bin/env bash
# =============================================================================
# run-web.sh — Start the local dev web server (HTTPS if certs exist)
# Called by Procfile via honcho.
# =============================================================================

CERT_DIR="$(cd "$(dirname "$0")/../.." && pwd)/certs"
KEY_FILE="$CERT_DIR/backend-key.pem"
CERT_FILE="$CERT_DIR/backend-cert.pem"

if [ -f "$KEY_FILE" ] && [ -f "$CERT_FILE" ]; then
  echo "🔒 Starting Uvicorn with HTTPS (certs found in certs/)"
  exec python -m uvicorn server.asgi:application \
    --host 0.0.0.0 \
    --port 8000 \
    --reload \
    --ssl-keyfile="$KEY_FILE" \
    --ssl-certfile="$CERT_FILE"
else
  echo "⚠️  No certs found — starting Uvicorn in plain HTTP mode"
  echo "   Run: bash scripts/generate-certs.sh"
  exec python -m uvicorn server.asgi:application \
    --host 0.0.0.0 \
    --port 8000 \
    --reload
fi