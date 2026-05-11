#!/bin/bash
# Weekly WTA trail status pipeline. Designed to be invoked by launchd.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

# Pull OPENAI_API_KEY from .env if not in env. zshrc isn't sourced by launchd.
if [ -z "${OPENAI_API_KEY:-}" ] && [ -f .env ]; then
  set -a; source .env; set +a
fi

LOG_DIR="$PROJECT_DIR/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/run-$(date +%Y%m%d-%H%M%S).log"

{
  echo "=== WTA status run @ $(date) ==="
  uv run python src/scrape_trails.py
  uv run python src/compute_drive.py
  uv run python src/scrape_reports.py
  uv run python src/summarize.py
  uv run python src/render.py
  echo "=== done @ $(date) ==="
} 2>&1 | tee "$LOG"

# Keep the 12 most recent logs.
ls -1t "$LOG_DIR"/run-*.log 2>/dev/null | tail -n +13 | xargs -r rm -f
