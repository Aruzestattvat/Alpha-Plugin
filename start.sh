#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
SECRETS="${WV_SECRETS_PATH:-$HOME/.config/wvbar/secrets.env}"
if [ ! -f "$SECRETS" ]; then
  echo "缺少 $SECRETS，请先运行 ./setup.sh"
  exit 1
fi
set -a
. "$SECRETS"
set +a
PY="python3"
[ -x .venv/bin/python ] && PY=".venv/bin/python"

if ! pgrep -f "[w]vcore2.py" >/dev/null; then
  nohup "$PY" -u wvcore2.py >> app-launch.log 2>&1 &
  echo "后端已启动"
else
  echo "后端已在运行"
fi
sleep 2

if [ -x ./wvbar ] && ! pgrep -f "[w]vbar .*state.json" >/dev/null; then
  nohup ./wvbar "$PWD/state.json" >/dev/null 2>&1 &
  echo "菜单栏已启动"
fi

PORT="${WV_HTTP_PORT:-8765}"
open "http://127.0.0.1:$PORT" 2>/dev/null || true
echo "仪表盘：http://127.0.0.1:$PORT"
