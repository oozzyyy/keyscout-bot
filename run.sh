#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
ROOT="$(pwd)"
BOT_BIN="$ROOT/bot/keyscout-bot"

[ -d analyzer/.venv ] || python3 -m venv analyzer/.venv
analyzer/.venv/bin/pip install -q -r analyzer/requirements.txt 2>&1 | grep -v -i notice || true

OLD_BOT=$(pgrep -f "^$BOT_BIN" || true)
for pid in $(pgrep -f "go-build.*/exe/bot$" || true); do
  if [ "$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p')" = "$ROOT/bot" ]; then
    OLD_BOT="$OLD_BOT $pid"
  fi
done
OLD_BOT=$(echo $OLD_BOT)
if [ -n "$OLD_BOT" ]; then
  echo "Останавливаю уже запущенного бота (pid $OLD_BOT)"
  kill $OLD_BOT; sleep 1
fi

OLD=$(lsof -nP -tiTCP:8000 -sTCP:LISTEN 2>/dev/null || true)
if [ -n "$OLD" ]; then
  if ps -o command= -p "$OLD" | grep -q "uvicorn main:app"; then
    echo "Останавливаю старый анализатор (pid $OLD)"
    kill "$OLD"; sleep 1
  else
    echo "Порт 8000 занят другой программой: $(ps -o command= -p "$OLD")" >&2
    exit 1
  fi
fi

TUN=""
if [ "$1" = "--tunnel" ]; then
  command -v cloudflared >/dev/null || { echo "Для --tunnel нужен cloudflared: brew install cloudflared" >&2; exit 1; }
  TUNNEL_LOG=$(mktemp -t keyscout-tunnel)
  cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8080 >"$TUNNEL_LOG" 2>&1 &
  TUN=$!
  WEBAPP_URL=""
  for _ in $(seq 1 40); do
    WEBAPP_URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$TUNNEL_LOG" | head -1 || true)
    [ -n "$WEBAPP_URL" ] && break
    sleep 1
  done
  if [ -z "$WEBAPP_URL" ]; then
    echo "Туннель не поднялся, подробности: $TUNNEL_LOG" >&2
    kill $TUN 2>/dev/null; exit 1
  fi
  export WEBAPP_URL
  echo "Веб-приложение доступно по $WEBAPP_URL — кнопка «Обзор» в боте ведёт туда"
fi

(cd analyzer && exec .venv/bin/uvicorn main:app --port 8000 --log-level warning) &
PY=$!
trap 'kill $PY $TUN 2>/dev/null' EXIT INT TERM

(cd bot && go build -o "$BOT_BIN" .)
sleep 1
cd bot && "$BOT_BIN"
