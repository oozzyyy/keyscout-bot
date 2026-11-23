#!/usr/bin/env bash
cd "$(dirname "$0")"
alive() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }
stop_pidfile() {
  alive "$1" || return 0
  kill "$(cat "$1")"
  for _ in $(seq 1 20); do alive "$1" || break; sleep 0.5; done
  rm -f "$1"
}

stop_pidfile start.pid
case "$1" in
  stop) stop_pidfile tunnel.pid; echo "KeyScout остановлен"; exit 0 ;;
  tunnel) stop_pidfile tunnel.pid ;;
esac
nohup setsid ./start.sh >> app.log 2>&1 < /dev/null &
if alive tunnel.pid; then echo "KeyScout перезапущен, адрес приложения прежний. Лог: $(pwd)/app.log"
else echo "KeyScout запущен с новым туннелем. Лог: $(pwd)/app.log"; fi
