# shellcheck shell=bash
# Shared settings, sourced by the scripts. An optional local.env (never committed) holds this
# machine's overrides, one KEY=value per line, no quotes:
#   SAMPLER_HOME=/path/to/library   where inbox/, stems/ and packs/ live (default: library/ here)
#   VS_VENV=/path/to/venv           the Python environment setup.sh made (default: .venv/ here)
# The file is parsed, never sourced, so it can't run code. A variable already set in the
# environment wins over the file.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$REPO/local.env" ]; then
  while IFS='=' read -r k v || [ -n "$k" ]; do
    case "$k" in
      SAMPLER_HOME|VS_VENV) [ -z "${!k:-}" ] && export "$k=$v" ;;
    esac
  done < "$REPO/local.env"
fi
export SAMPLER_HOME="${SAMPLER_HOME:-$REPO/library}"
export VS_VENV="${VS_VENV:-$REPO/.venv}"
