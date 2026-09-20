#!/usr/bin/env bash
# Supervisor: keep `python3 run.py serve` alive. Logs to logs/serve.log.
# Use: nohup scripts/serve.sh &   (or terminal(background=true, ...))
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs data

HOST=${LINUXDO_AI_HOST:-127.0.0.1}
PORT=${LINUXDO_AI_PORT:-8791}

if curl -sf -m 3 "http://$HOST:$PORT/health" >/dev/null 2>&1; then
  echo "[serve] already healthy on $HOST:$PORT - not starting a second instance"
  exit 0
fi

if [ -f logs/serve.pid ] && kill -0 "$(cat logs/serve.pid)" 2>/dev/null; then
  echo "[serve] supervisor pid $(cat logs/serve.pid) already running"
  exit 0
fi

echo $$ > logs/serve.pid
trap 'rm -f logs/serve.pid; exit 0' TERM INT

while true; do
  printf '[serve] start %s\n' "$(date -u +%FT%TZ)" >> logs/serve.log
  python3 run.py serve >> logs/serve.log 2>&1
  code=$?
  printf '[serve] exit code=%s %s\n' "$code" "$(date -u +%FT%TZ)" >> logs/serve.log
  if [ "$code" = "0" ] && [ "${LINUXDO_AI_ONESHOT:-0}" = "1" ]; then
    break
  fi
  sleep 5
done
