#!/bin/bash
# Arc'teryx Outlet stock watcher - run script

# Activate virtualenv
source venv/bin/activate

# Load environment variables (optional): if .env exists, export it
if [ -f .env ]; then
  set -a
  source .env
  set +a
fi

# Run the watcher (shoe restock monitoring)
CONFIG_FILE="stock_watch_config.json"
HAS_CONFIG_ARG=false
for arg in "$@"; do
  case "$arg" in
    --config|--config=*) HAS_CONFIG_ARG=true ;;
  esac
done

if [ "$HAS_CONFIG_ARG" = true ]; then
  python3 watch_stock.py "$@"
elif [ -f "$CONFIG_FILE" ]; then
  python3 watch_stock.py --config "$CONFIG_FILE" "$@"
else
  python3 watch_stock.py "$@"
fi
