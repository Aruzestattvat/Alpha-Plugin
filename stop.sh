#!/bin/bash
set -euo pipefail
pkill -f "[w]vcore2.py" 2>/dev/null || true
pkill -f "[w]vbar .*state.json" 2>/dev/null || true
echo "已停止知间后端与菜单栏"
