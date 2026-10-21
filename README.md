# 知间 · 聊天关系（Alpha 插件）

macOS 菜单栏应用 + 本地 Web 仪表盘：从 Alpha（微信本地数据读取器）的 HTTP API 拉取指定会话的完整聊天记录，做分片分析、关系判断与趋势追踪，并用菜单栏和卡片展示。

> 仓库内**不包含**任何真实聊天数据库、头像、日志、`state.json` 或密钥；这些只存在于本机运行时目录。

## 功能

- **按会话全量分析**：消息同步 → 本地语音转写 → 分片摘要 → 汇总评分
- **双指数**：亲近度（−100 ~ +100）与心动指数（0 ~ 100）
- **趋势**：最近 300 条消息分窗口采样，输出每 100 条消息的变化量（斜率箭头）
- **关系判定**：朋友/同学/同事/暧昧/恋人/家人；暧昧与恋人变化必须通过原文核验 + 稳定性观察
- **实时 / 省额度两种模式**：eco 模式用分片 gist + 滚动摘要 + 批次合并，降低 token 消耗
- **手动控制**：实时分析开关、模式切换；每人每日 token 用量（W = 万）
- **菜单栏**：置顶会话分数、趋势箭头、快捷开关

## 目录结构

| 路径 | 说明 |
|---|---|
| `wvcore2.py` | 主后端：Alpha 客户端、同步、ASR、分析、HTTP/SSE、状态输出 |
| `context_trend.py` | 按消息上下文的趋势计算 |
| `score_trend.py` | 旧版按日期趋势（保留参考） |
| `relationship_*.py` | 关系稳定性、时间特征、边界证据核验 |
| `dashboard2.html` | 仪表盘 |
| `menubar.swift` / `wvbar` | 菜单栏程序与编译产物 |
| `build.sh` / `build_app.py` | 编译菜单栏 / 打包 `知间.app` |
| `tests/` | 单元测试（79 个） |
| `assets/` | Logo、封面与图标槽位说明 |
| `state.example.json` | 状态文件格式示例 |

## 环境要求

- macOS（Apple Silicon 测试通过）
- Python 3.9+
- [Alpha](https://github.com/)（本机微信数据读取器，需其 HTTP API）
- DeepSeek API Key
- 可选：`sherpa-onnx` + Qwen3-ASR 0.6B ONNX 模型（语音转写）

## 快速开始

```bash
# 1. 密钥（不要提交到仓库）
mkdir -p ~/.config/wvbar
cat > ~/.config/wvbar/secrets.env <<'EOF'
ALPHA_TOKEN=你的_alpha_token
DEEPSEEK_API_KEY=你的_deepseek_key
EOF
chmod 600 ~/.config/wvbar/secrets.env

# 2. 依赖
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# 3. 启动后端（默认 http://127.0.0.1:8765）
set -a; . ~/.config/wvbar/secrets.env; set +a
python3 -u wvcore2.py

# 4. 编译菜单栏并运行
bash build.sh
./wvbar "$PWD/state.json"
```

仪表盘：<http://127.0.0.1:8765>  
菜单栏程序读取同目录 `state.json`；`watched.json` 保存已选会话。

## 配置（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `WV_REALTIME_PROFILE` | `accurate` | `eco`=省额度；`accurate`=每条全量 |
| `WV_ECO_MODEL` | `deepseek-v4-pro` | 增量模型 |
| `WV_ECO_BATCH_MESSAGES` / `WV_ECO_BATCH_SECONDS` | 5 / 90 | 实时批次合并 |
| `WV_SCORE_GAIN` | 1.15 | 分数拉伸系数 |
| `WV_RELATION_STRICT` | 1 | 严格原文核验 |
| `WV_TREND_MESSAGES` / `WV_TREND_WINDOWS` | 300 / 5 | 趋势采样窗口 |
| `WV_TREND_MIN_NEW` | 30 | 新增多少条后重采样 |
| `WV_TREND_SAMPLING` | 1 | 关闭趋势采样设为 0 |
| `WV_HTTP_PORT` / `WV_DB_PATH` / `WV_STATE_PATH` / `WV_WATCHED_PATH` | — | 端口与运行时路径（便于隔离测试） |

## 测试

```bash
python3 -m unittest discover -s tests -p "test_*.py"
```

## 隐私与安全

- 仓库默认**不提交** `state.json`、`watched.json`、`*.sqlite3*`、`avatars/`、`logs/`、`shots/`、密钥文件。
- 运行时数据只保存在本机；公开仓库前请再次确认 `.gitignore` 生效。
- 本项目只读取本机 Alpha API，不向除 DeepSeek 之外的服务发送聊天内容（DeepSeek 用于分析）。
- `assets/ICON_SLOT.md` 说明应用图标槽位，可替换 `assets/zhijian-logo.png` 后重新打包。

## 许可

未指定 License。上传到 GitHub 前请补充合适的许可证（如 MIT / 私有仓库）。
