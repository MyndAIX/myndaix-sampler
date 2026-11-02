#!/bin/bash
# One-time setup: a Python environment with the stem splitter (demucs), the audio analysis
# libraries and yt-dlp. Needs Python 3.10+ and ffmpeg. Downloads about 1-2 GB (PyTorch).
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
. "$(dirname "$0")/config.sh"

if ! command -v ffmpeg > /dev/null; then
  echo "ffmpeg is missing. On a Mac: brew install ffmpeg   (Homebrew: https://brew.sh)" >&2
  exit 1
fi

PY=""
for c in python3 python3.14 python3.13 python3.12 python3.11 python3.10; do
  if command -v "$c" > /dev/null && "$c" -c 'import sys; sys.exit(sys.version_info[:2] < (3, 10))'; then
    PY="$(command -v "$c")"; break
  fi
done
[ -n "$PY" ] || { echo "Need Python 3.10 or newer. On a Mac: brew install python" >&2; exit 1; }

if [ ! -x "$VS_VENV/bin/python" ]; then
  echo "Creating the Python environment at $VS_VENV ($("$PY" --version))"
  "$PY" -m venv "$VS_VENV"
fi
"$VS_VENV/bin/python" -m pip install --upgrade pip
"$VS_VENV/bin/python" -m pip install -r "$REPO/requirements.txt"
echo
echo "Setup done. Start the sampler with ./run.sh"
