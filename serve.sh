#!/bin/bash
# Always-on local HTTP server: serves dist/ and exposes the WTA fetch API.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

# Load secrets. Project .env first (DASHBOARD_PASSWORD), then ~/dotEnv
# (canonical OPENAI_API_KEY + other shared keys; wins on overlap).
if [ -f .env ]; then
  set -a; source .env; set +a
fi
if [ -f "$HOME/dotEnv" ]; then
  set -a; source "$HOME/dotEnv"; set +a
fi

exec uv run python src/server.py
