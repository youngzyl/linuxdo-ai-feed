#!/usr/bin/env bash
# Local preview: serve the app and (optionally) expose it through a cloudflared quick
# tunnel so the page can be opened on a phone.
#
#   scripts/preview.sh            # local only, http://127.0.0.1:8791
#   scripts/preview.sh --tunnel   # + public https://<random>.trycloudflare.com
set -u
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs data

HOST=${LINUXDO_AI_HOST:-127.0.0.1}
PORT=${LINUXDO_AI_PORT:-8791}

if ! curl -sf -m 3 "http://$HOST:$PORT/health" >/dev/null 2>&1; then
  echo "[preview] starting server…"
  scripts/serve.sh >> logs/preview.log 2>&1
  for _ in $(seq 1 20); do
    curl -sf -m 2 "http://$HOST:$PORT/health" >/dev/null 2>&1 && break
    sleep 1
  done
fi

if ! curl -sf -m 3 "http://$HOST:$PORT/health" >/dev/null 2>&1; then
  echo "[preview] server did not come up; see logs/serve.log" >&2
  exit 1
fi
echo "[preview] local: http://$HOST:$PORT"

if [ "${1:-}" = "--tunnel" ]; then
  if ! command -v cloudflared >/dev/null 2>&1; then
    echo "[preview] cloudflared not installed" >&2
    exit 1
  fi
  echo "[preview] opening quick tunnel (temporary URL, dies with this process)…"
  exec cloudflared tunnel --url "http://$HOST:$PORT" --no-autoupdate
fi
