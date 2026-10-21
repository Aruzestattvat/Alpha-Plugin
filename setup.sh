#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "本项目仅支持 macOS。"
  exit 1
fi

ALPHA_BASE_VALUE="${ALPHA_BASE:-http://127.0.0.1:5031}"
SECRETS="${WV_SECRETS_PATH:-$HOME/.config/wvbar/secrets.env}"
echo "== 知间 / Alpha 插件 安装 =="
echo "Alpha API: $ALPHA_BASE_VALUE"

if curl -fsS --max-time 3 "$ALPHA_BASE_VALUE/health" >/dev/null 2>&1; then
  echo "✓ Alpha 本地 API 可访问"
else
  echo "⚠ 未检测到 Alpha（$ALPHA_BASE_VALUE/health）。"
  echo "  请先启动 Alpha 并在 Alpha 内配置微信加密 key；安装可以继续。"
fi

mkdir -p "$(dirname "$SECRETS")"
if [ -f "$SECRETS" ]; then
  echo "✓ 已存在密钥文件：$SECRETS（不覆盖）"
else
  alpha="${ALPHA_TOKEN:-}"
  deepseek="${DEEPSEEK_API_KEY:-}"
  if [ -z "$alpha" ]; then
    if [ -t 0 ]; then read -r -p "Alpha API Token: " alpha; else echo "缺少 ALPHA_TOKEN"; exit 1; fi
  fi
  if [ -z "$deepseek" ]; then
    if [ -t 0 ]; then read -r -s -p "DeepSeek API Key: " deepseek; echo; else echo "缺少 DEEPSEEK_API_KEY"; exit 1; fi
  fi
  umask 077
  cat > "$SECRETS" <<EOF
ALPHA_BASE=$ALPHA_BASE_VALUE
ALPHA_TOKEN=$alpha
DEEPSEEK_API_KEY=$deepseek
EOF
  chmod 600 "$SECRETS"
  echo "✓ 已写入密钥文件：$SECRETS"
fi

echo "== 安装 Python 依赖 =="
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -q --upgrade pip
if ! pip install -q -r requirements.txt; then
  echo "⚠ 依赖安装失败；语音转写需要 sherpa-onnx，其他功能仍可运行。"
fi

echo "== 编译菜单栏程序 =="
bash build.sh

echo
echo "安装完成。"
echo "启动：  ./start.sh"
echo "停止：  ./stop.sh"
echo "自检：  ./doctor.sh"
echo "仪表盘：http://127.0.0.1:${WV_HTTP_PORT:-8765}"
