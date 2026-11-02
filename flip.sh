#!/bin/bash
# A song -> a pack of loops and chops the sampler can play (in $SAMPLER_HOME/packs/<slug>/).
#   ./flip.sh "https://www.youtube.com/watch?v=..."   [--slug name] [--force]
#   ./flip.sh path/to/song.mp3
# Usually you don't call this yourself: ./vs.py run <id> calls it for a song requested in the app.
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
. "$(dirname "$0")/config.sh"
[ -x "$VS_VENV/bin/python" ] || { echo "No Python environment at $VS_VENV: run ./setup.sh first" >&2; exit 1; }
# -I: isolated mode, so nothing in the current folder shadows a library import
exec "$VS_VENV/bin/python" -I "$REPO/flippack/flippack.py" "$@"
