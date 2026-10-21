#!/bin/bash
set -uo pipefail
cd "$(dirname "$0")"
SECRETS="${WV_SECRETS_PATH:-$HOME/.config/wvbar/secrets.env}"
if [ -f "$SECRETS" ]; then
  set -a; . "$SECRETS"; set +a
fi
ALPHA_BASE_VALUE="${ALPHA_BASE:-http://127.0.0.1:5031}"
echo "== 知间自检 =="

echo "[1/5] Alpha 健康检查"
if curl -fsS --max-time 3 "$ALPHA_BASE_VALUE/health" >/dev/null 2>&1; then
  echo "  ✓ $ALPHA_BASE_VALUE/health"
else
  echo "  ✗ 无法访问 $ALPHA_BASE_VALUE/health；请确认 Alpha 已启动"
fi

echo "[2/5] Alpha API Token"
if [ -n "${ALPHA_TOKEN:-}" ]; then
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
    -H "Authorization: Bearer $ALPHA_TOKEN" "$ALPHA_BASE_VALUE/api/v1/sessions?limit=1" || true)
  if [ "$code" = "200" ]; then echo "  ✓ token 可用"; else echo "  ✗ HTTP $code"; fi
else
  echo "  ✗ 未配置 ALPHA_TOKEN"
fi

echo "[3/5] DeepSeek API Key"
if [ -n "${DEEPSEEK_API_KEY:-}" ]; then
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 \
    -H "Authorization: Bearer $DEEPSEEK_API_KEY" "${DEEPSEEK_BASE:-https://api.deepseek.com}/user/balance" || true)
  if [ "$code" = "200" ]; then echo "  ✓ key 可用"; else echo "  ✗ HTTP $code"; fi
else
  echo "  ✗ 未配置 DEEPSEEK_API_KEY"
fi

echo "[4/5] 运行时"
command -v python3 >/dev/null && echo "  ✓ python3 $(python3 -V 2>&1 | awk '{print $2}')" || echo "  ✗ 缺少 python3"
[ -x ./wvbar ] && echo "  ✓ 菜单栏二进制" || echo "  ✗ 缺少 ./wvbar，请运行 bash build.sh"
if pgrep -f "[w]vcore2.py" >/dev/null; then echo "  ✓ 后端运行中"; else echo "  ⚠ 后端未运行"; fi

echo "[5/5] ASR 模型（可选）"
MODEL="${WV_ASR_MODEL_DIR:-$HOME/Library/Application Support/Alpha/voice/models/qwen3-asr-0.6b}"
[ -d "$MODEL" ] && echo "  ✓ $MODEL" || echo "  ⚠ 未找到 ASR 模型，语音将跳过"
