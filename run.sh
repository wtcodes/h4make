#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python_bin="${PYTHON_BIN:-python3}"

usage() {
  cat <<'EOF'
Usage:
  ./run.sh CONFIG [--mode dry-run|testnet|mainnet] [--output PREFIX] [--cycles N]
                  [--kalshi-credentials FILE]

Environment:
  PYTHON_BIN              Python executable to use (default: python3)
  HL_CREDENTIALS_FILE     JSON private-key credentials; derives the account

Examples:
  ./run.sh config/my_market.json --mode dry-run --output run

  # Writes run_orders.jsonl and run_basis.jsonl.

  ./run.sh config/my_market.json --mode dry-run \
    --kalshi-credentials /path/to/kalshi.credentials.json

  HL_CREDENTIALS_FILE=/path/to/credentials.json \
    ./run.sh config/my_market.json --mode testnet --output run
EOF
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

if ! command -v "$python_bin" >/dev/null 2>&1; then
  printf 'ERROR Python executable not found: %s\n' "$python_bin" >&2
  exit 2
fi

export PYTHONPATH="$project_dir/src${PYTHONPATH:+:$PYTHONPATH}"

exec "$python_bin" -m hip4maker run "$config_file" "$@"
