#!/bin/bash
# Start the sampler and open it in the browser. Ctrl-C stops it.
#   ./run.sh [port]      (default 8765)
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
. "$(dirname "$0")/config.sh"
PORT="${1:-8765}"
python3 -I "$REPO/serve.py" "$PORT" &
pid=$!
trap 'kill "$pid" 2>/dev/null' EXIT INT TERM
sleep 0.5
url="http://127.0.0.1:$PORT/"
if command -v open > /dev/null; then open "$url"; elif command -v xdg-open > /dev/null; then xdg-open "$url"; else echo "Open $url"; fi
wait "$pid"
