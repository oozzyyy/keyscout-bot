#!/usr/bin/env bash
cd "$(dirname "$0")"
umask 077
ROOT=$(pwd)
export PATH="$ROOT/bin:$PATH"
export ANALYZER_URL=http://127.0.0.1:8001
export WEBAPP_ADDR=0.0.0.0:8000
export TELEGRAM_API_IP=${TELEGRAM_API_IP:-149.154.167.220}

log() { echo "$(date '+%F %T') [start] $*"; }
alive() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }

ensure_tunnel() {
  if ! alive "$ROOT/tunnel.pid"; then
    : > "$ROOT/tunnel.log"
    nohup cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8000 > "$ROOT/tunnel.log" 2>&1 < /dev/null &
    echo $! > "$ROOT/tunnel.pid"
    log "туннель запущен заново — адрес приложения сменится"
  fi
  WEBAPP_URL=""
  for _ in $(seq 1 60); do
    WEBAPP_URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$ROOT/tunnel.log" | grep -v '//api\.' | head -1)
    [ -n "$WEBAPP_URL" ] && break
    sleep 1
  done
  export WEBAPP_URL
  log "веб-приложение: ${WEBAPP_URL:-туннель не поднялся — бот работает без кнопки приложения}"
}

PIDS=()
stop_all() { [ ${#PIDS[@]} -gt 0 ] && { kill "${PIDS[@]}" 2>/dev/null; wait "${PIDS[@]}" 2>/dev/null; }; PIDS=(); }
trap 'log "остановка (туннель остаётся)"; stop_all; rm -f "$ROOT/start.pid"; exit 0' INT TERM
echo $$ > "$ROOT/start.pid"

pause=5
while true; do
  started=$(date +%s)
  ensure_tunnel
  (cd analyzer && exec .venv/bin/uvicorn main:app --host 127.0.0.1 --port 8001 --log-level warning) &
  PIDS+=($!)
  (cd bot && exec ./keyscout-bot) &
  PIDS+=($!)

  reason=""
  while [ -z "$reason" ]; do
    sleep 10 & wait $!
    for p in "${PIDS[@]}"; do kill -0 "$p" 2>/dev/null || reason="процесс $p завершился"; done
    alive "$ROOT/tunnel.pid" || reason="туннель завершился"
  done
  stop_all
  if [ $(( $(date +%s) - started )) -lt 120 ]; then pause=$(( pause * 2 > 300 ? 300 : pause * 2 )); else pause=5; fi
  log "$reason — перезапуск через ${pause} с"
  sleep "$pause"
done
