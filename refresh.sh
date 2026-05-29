#!/bin/bash
# Incremental WTA refresh: re-scrape recent trip reports, re-summarize, re-render.
# Unlike run.sh it skips the slow regional top-N scrape and drive-time routing.
#
# Usage:
#   ./refresh.sh              full refresh (re-summarize every trail with a new report)
#   ./refresh.sh --non-open   only re-summarize non-open trails (skips the LLM for Open ones)
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

# Load secrets. Project .env first, then ~/dotEnv (canonical OPENAI_API_KEY; wins on overlap).
if [ -f .env ]; then
  set -a; source .env; set +a
fi
if [ -f "$HOME/dotEnv" ]; then
  set -a; source "$HOME/dotEnv"; set +a
fi

# --non-open -> pass --only-non-open through to the summarizer.
SUMMARIZE_FLAG=""
MODE="full"
if [ "${1:-}" = "--non-open" ]; then
  SUMMARIZE_FLAG="--only-non-open"
  MODE="non-open"
fi

LOG_DIR="$PROJECT_DIR/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/refresh-$(date +%Y%m%d-%H%M%S).log"

{
  echo "=== WTA refresh ($MODE) @ $(date) ==="
  uv run python src/scrape_reports.py
  uv run python src/summarize.py $SUMMARIZE_FLAG
  uv run python src/render.py
  echo "=== done @ $(date) ==="
} 2>&1 | tee "$LOG"

# Keep the 12 most recent refresh logs.
ls -1t "$LOG_DIR"/refresh-*.log 2>/dev/null | tail -n +13 | xargs -r rm -f
