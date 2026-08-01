#!/bin/bash
# Start C2 side (CLI mode by default; pass "web" for Web mode)
MODE="${1:-cli}"
if [[ "$MODE" != "cli" && "$MODE" != "web" ]]; then
    echo "Usage: $0 [cli|web]"
    exit 1
fi
exec python -m src.main --mode "$MODE" --config config_c2.json