#!/usr/bin/env bash
# =============================================================================
# generate-certs.sh — Local HTTPS certificates for Upgrade development
# =============================================================================
#
# Creates trusted local TLS certificates using mkcert so that both the
# frontend (Vite) and backend (Uvicorn) can run under HTTPS locally.
#
# This avoids browser security warnings, mixed-content blocks, and issues
# with APIs that require secure contexts (e.g. Clipboard, WebAuthn, OAuth).
#
# PREREQUISITES:
#   brew install mkcert nss       (macOS — nss is needed for Firefox trust)
#   mkcert -install               (one-time: creates a local CA and trusts it)
#
# USAGE:
#   cd upg
#   bash scripts/generate-certs.sh
#
# OUTPUT:
#   certs/frontend-key.pem     →  used by Vite dev server
#   certs/frontend-cert.pem
#   certs/backend-key.pem      →  used by Uvicorn / Hypercorn
#   certs/backend-cert.pem
#
# These files are gitignored. Each developer runs this script once.
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CERT_DIR="$PROJECT_ROOT/certs"

# ── Preflight checks ────────────────────────────────────────────────────────

if ! command -v mkcert &>/dev/null; then
  echo "❌ mkcert is not installed."
  echo ""
  echo "   Install it:"
  echo "     macOS:   brew install mkcert nss"
  echo "     Ubuntu:  sudo apt install libnss3-tools && brew install mkcert"
  echo "     Arch:    sudo pacman -S mkcert nss"
  echo "     Windows: choco install mkcert"
  echo ""
  echo "   Then run:  mkcert -install"
  exit 1
fi

# Check if the local CA is installed
CA_ROOT="$(mkcert -CAROOT 2>/dev/null || echo '')"
if [ -z "$CA_ROOT" ] || [ ! -f "$CA_ROOT/rootCA.pem" ]; then
  echo "⚠️  Local CA not yet installed. Running: mkcert -install"
  echo "   (you may be prompted for your password)"
  echo ""
  mkcert -install
fi

# ── Generate certificates ───────────────────────────────────────────────────

mkdir -p "$CERT_DIR"

echo ""
echo "🔐 Generating frontend certificate..."
mkcert \
  -key-file "$CERT_DIR/frontend-key.pem" \
  -cert-file "$CERT_DIR/frontend-cert.pem" \
  localhost 127.0.0.1 ::1

echo ""
echo "🔐 Generating backend certificate..."
mkcert \
  -key-file "$CERT_DIR/backend-key.pem" \
  -cert-file "$CERT_DIR/backend-cert.pem" \
  localhost 127.0.0.1 ::1

echo ""
echo "✅ Certificates created in: $CERT_DIR/"
echo ""
ls -la "$CERT_DIR"/*.pem
echo ""
echo "────────────────────────────────────────────────"
echo "  Frontend:  https://localhost:3000"
echo "  Backend:   https://localhost:8000"
echo "────────────────────────────────────────────────"
echo ""
echo "Start developing:"
echo "  cd server && honcho start"
echo "  cd client && npm run dev"
