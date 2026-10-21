#!/bin/bash
# 以省额度模式启动知间后端（accu 模式用 WV_REALTIME_PROFILE=accurate）
cd "$(dirname "$0")"
set -a; . "$HOME/.config/wvbar/secrets.env"; set +a
export WV_REALTIME_PROFILE="${WV_REALTIME_PROFILE:-eco}"
exec python3 -u wvcore2.py >> app-launch.log 2>&1
