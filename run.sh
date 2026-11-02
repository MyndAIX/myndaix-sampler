#!/bin/bash
# Start the virtual sampler and open it in the browser. Ctrl-C stops it.
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
HERE="$(cd "$(dirname "$0")" && pwd)"
PORT="${1:-8765}"
python3 -I "$HERE/serve.py" "$PORT" &
pid=$!
trap 'kill "$pid" 2>/dev/null' EXIT INT TERM
sleep 0.5
open "http://127.0.0.1:$PORT/"
wait "$pid"
