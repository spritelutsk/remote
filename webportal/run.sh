#!/usr/bin/env bash
# Launch the Remote-Access Portal.
# Set a stable SECRET_KEY so sessions survive restarts.
set -e
cd "$(dirname "$0")"

export PORT="${PORT:-8000}"
export HOST="${HOST:-0.0.0.0}"
# Persist a secret key on first run so logins survive restarts.
if [ -z "$SECRET_KEY" ]; then
  if [ ! -f .secret ]; then python3 -c "import secrets;print(secrets.token_hex(32))" > .secret; fi
  export SECRET_KEY="$(cat .secret)"
fi
export CORTENDESK_URL="${CORTENDESK_URL:-http://localhost:8080}"

exec python3 app.py
