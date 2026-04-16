#!/usr/bin/env bash
set -euo pipefail

if command -v qdrant >/dev/null 2>&1; then
  exec qdrant
fi

if [[ -x "${HOME}/qdrant" ]]; then
  exec "${HOME}/qdrant"
fi

echo "Qdrant binary not found."
echo "Install qdrant so it is on your PATH, or place the binary at \$HOME/qdrant."
exit 1
