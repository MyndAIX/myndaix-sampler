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

# 3.10-3.14 only: on 3.15 (checked 2026-11-01) demucs's dependency sphn has no prebuilt wheel
# and fails to build, so a newer default python3 is skipped in favor of an installed 3.14 etc.
PY_OK='import sys; sys.exit(not ((3, 10) <= sys.version_info[:2] <= (3, 14)))'
PY=""
for c in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" > /dev/null && "$c" -c "$PY_OK"; then
    PY="$(command -v "$c")"; break
  fi
done
[ -n "$PY" ] || { echo "Need Python 3.10-3.14 (the stem splitter doesn't install on newer yet). On a Mac: brew install python@3.14" >&2; exit 1; }

if [ -x "$VS_VENV/bin/python" ] && ! "$VS_VENV/bin/python" -c "$PY_OK"; then
  echo "$VS_VENV uses an unsupported Python; move it aside (e.g. mv .venv .venv.old) and run ./setup.sh again" >&2
  exit 1
fi
if [ ! -x "$VS_VENV/bin/python" ]; then
  echo "Creating the Python environment at $VS_VENV ($("$PY" --version))"
  "$PY" -m venv "$VS_VENV"
fi
"$VS_VENV/bin/python" -m pip install --upgrade pip
"$VS_VENV/bin/python" -m pip install -r "$REPO/requirements.txt"
echo
echo "Setup done. Start the sampler with ./run.sh"
