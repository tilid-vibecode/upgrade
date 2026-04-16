#!/bin/sh
set -eu

API_BASE_URL="${API_BASE_URL:-}"

escape_js() {
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

ESCAPED_API_BASE_URL="$(escape_js "$API_BASE_URL")"

cat > /usr/share/nginx/html/app-config.js <<EOF
window.__APP_CONFIG__ = Object.assign({}, window.__APP_CONFIG__, {
  API_BASE_URL: "${ESCAPED_API_BASE_URL}"
})
EOF

exec nginx -g 'daemon off;'
