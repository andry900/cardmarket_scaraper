#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_PYTHON="$PROJECT_ROOT/.venv/bin/python"
SCRAPER="$SCRIPT_DIR/scraper.py"

if [[ ! -x "$VENV_PYTHON" ]]; then
  echo "[ERRORE] Python virtualenv non trovato: $VENV_PYTHON"
  echo "Crea la venv in $PROJECT_ROOT/.venv oppure aggiorna questo script."
  exit 1
fi

DEFAULT_CONFIG="$SCRIPT_DIR/products.json"
DEFAULT_OUTPUT="$SCRIPT_DIR/results.json"
DEFAULT_PROFILE="$HOME/.cm-debug-profile"
DEFAULT_BRAVE="/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
DEFAULT_CDP_URL="http://127.0.0.1:9222"

if [[ ! -x "$DEFAULT_BRAVE" ]]; then
  echo "[ERRORE] Brave non trovato: $DEFAULT_BRAVE"
  exit 1
fi

if ! lsof -nP -iTCP:9222 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "[INFO] Avvio Brave in debug mode su porta 9222..."
  open -na "Brave Browser" --args \
    --remote-debugging-port=9222 \
    --user-data-dir="$DEFAULT_PROFILE"

  ready=0
  for _ in {1..20}; do
    if lsof -nP -iTCP:9222 -sTCP:LISTEN >/dev/null 2>&1; then
      ready=1
      break
    fi
    sleep 0.5
  done

  if [[ "$ready" -ne 1 ]]; then
    echo "[ERRORE] Porta CDP 9222 non disponibile."
    exit 1
  fi
fi

exec "$VENV_PYTHON" "$SCRAPER" \
  --config "$DEFAULT_CONFIG" \
  --output-json "$DEFAULT_OUTPUT" \
  --browser-mode connect-cdp \
  --cdp-url "$DEFAULT_CDP_URL" \
  --use-existing-page \
  --browser-executable-path "$DEFAULT_BRAVE" \
  --user-data-dir "$DEFAULT_PROFILE" \
  --manual-challenge \
  --manual-challenge-timeout-seconds 180 \
  --request-delay-seconds 4 \
  --request-delay-max-seconds 5 \
  "$@"
