#!/bin/bash
# Always-on local HTTP server: serves dist/ and exposes the WTA fetch API.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

# Load OPENAI_API_KEY so /api/add can summarize.
if [ -z "${OPENAI_API_KEY:-}" ] && [ -f .env ]; then
  set -a; source .env; set +a
fi

exec uv run python src/server.py
