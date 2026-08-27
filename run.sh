#!/usr/bin/env bash
#
# run.sh — launch the hip4maker market maker for one configuration.
#
# Usage:
#   ./run.sh CONFIG [--mode dry-run|testnet|mainnet] [--output PREFIX]
#                   [--cycles N] [--hl-credentials FILE]
#                   [--kalshi-credentials FILE]
#
# Options:
#   CONFIG                  Path to the market JSON config (positional, required).
#   --mode MODE             Execution mode; forwarded to hip4maker (default: dry-run).
#   --output PREFIX         Output prefix -> PREFIX_orders.jsonl, PREFIX_basis.jsonl.
#   --cycles N              Run N polling cycles then stop (0 = run until interrupted).
#   --hl-credentials FILE   Hyperliquid wallet private-key JSON ({"secret_key":"0x..."}).
#                           Sets HL_CREDENTIALS_FILE for this run. Equivalent to
#                           exporting that env var yourself; the flag wins if both given.
#   --kalshi-credentials FILE   Enables the authenticated Kalshi WebSocket (forwarded).
#
# Environment (alternatives to the flags above):
#   PYTHON_BIN              Python executable to use (default: python3).
#   HL_CREDENTIALS_FILE     Hyperliquid credentials JSON; derives the account.
#
# Examples:
#   ./run.sh config/my_market.json --mode dry-run --output run
#   # -> writes run_orders.jsonl and run_basis.jsonl
#
#   ./run.sh config/my_market.json --mode testnet --output run \
#     --hl-credentials ~/.config/hip4maker/hyperliquid.json
#
#   PYTHON_BIN=/path/to/venv/bin/python \
#     ./run.sh config/my_market.json --mode dry-run

set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python_bin="${PYTHON_BIN:-python3}"

usage() {
  # Print the leading comment block (everything between the shebang and the
  # first blank line after the options) as the help text.
  sed -n '3,32p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

if [[ $# -eq 0 ]]; then
  usage >&2
  exit 2
fi

case "$1" in
  -h|--help)
    usage
    exit 0
    ;;
esac

config_file="$1"
shift

if [[ ! -f "$config_file" ]]; then
  printf 'ERROR configuration file does not exist: %s\n' "$config_file" >&2
  exit 2
fi

# Pull --hl-credentials out of the argument list into HL_CREDENTIALS_FILE (the
# Python program only reads the env var). Every other flag passes through
# unchanged to `hip4maker run`.
forward=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --hl-credentials)
      if [[ $# -lt 2 ]]; then
        printf 'ERROR --hl-credentials requires a file path\n' >&2
        exit 2
      fi
      HL_CREDENTIALS_FILE="$2"
      shift 2
      ;;
    --hl-credentials=*)
      HL_CREDENTIALS_FILE="${1#*=}"
      shift
      ;;
    *)
      forward+=("$1")
      shift
      ;;
  esac
done

# If credentials were supplied (by flag or pre-set env var), require the file to
# exist and export it so the Python child process inherits it.
if [[ -n "${HL_CREDENTIALS_FILE:-}" ]]; then
  if [[ ! -f "$HL_CREDENTIALS_FILE" ]]; then
    printf 'ERROR Hyperliquid credentials file does not exist: %s\n' "$HL_CREDENTIALS_FILE" >&2
    exit 2
  fi
  export HL_CREDENTIALS_FILE
fi

if ! command -v "$python_bin" >/dev/null 2>&1; then
  printf 'ERROR Python executable not found: %s\n' "$python_bin" >&2
  exit 2
fi

export PYTHONPATH="$project_dir/src${PYTHONPATH:+:$PYTHONPATH}"

# ${forward[@]+...} guards the empty-array case under `set -u`.
exec "$python_bin" -m hip4maker run "$config_file" ${forward[@]+"${forward[@]}"}
