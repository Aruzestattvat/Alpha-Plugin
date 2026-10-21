<p align="center">
  <img src="assets/zhijian-logo.png" width="120" alt="知间 · Alpha 插件">
</p>

<h1 align="center">知间 · Alpha 插件</h1>

<p align="center">
  <img src="https://img.shields.io/badge/platform-macOS-black" alt="macOS">
  <img src="https://img.shields.io/badge/python-3.9%2B-blue" alt="Python">
  <img src="https://img.shields.io/badge/data-Alpha%20local%20API-6f42c1" alt="Alpha">
  <img src="https://img.shields.io/badge/LLM-DeepSeek-4d6bfe" alt="DeepSeek">
  <img src="https://img.shields.io/badge/tests-100%20passing-brightgreen" alt="tests">
</p>

macOS 菜单栏应用 + 本地 Web 仪表盘：通过 Alpha 的本地 HTTP API 读取指定会话的完整聊天记录，做分片分析、关系判断与趋势追踪。

> 依赖的 Alpha 原生库 / 官方发布页：[**xunchahaha/alpha-releases**](https://github.com/xunchahaha/alpha-releases)（微信聊天记录本地读取与导出）。

> **微信加密 key 不在这里配置。** 微信数据库由 Alpha 负责解密；本项目只读取 Alpha 已经解密好的本地 API。你只需要在 Alpha 里配置微信 key，然后把 **Alpha API Token** 和 **DeepSeek API Key** 填到本项目。

<p align="center">
  <img src="assets/cover.png" alt="知间 · Alpha 插件">
</p>

## 功能

- **按会话全量分析**：消息同步 → 本地语音转写 → 分片摘要 → 汇总评分
- **双指数**：亲近度（−100 ~ +100）与心动指数（0 ~ 100）
- **趋势**：最近 300 条消息分窗口采样，输出每 100 条消息的变化量（斜率箭头）
- **关系判定**：朋友/同学/同事/暧昧/恋人/家人；暧昧与恋人变化必须通过原文核验 + 稳定性观察
- **实时 / 省额度两种模式**：eco 模式用分片 gist + 滚动摘要 + 批次合并，降低 token 消耗
- **手动控制**：实时分析开关、模式切换；每人每日 token 用量（W = 万）
- **菜单栏**：置顶会话分数、趋势箭头、快捷开关

## 快速开始

### 0. 准备

1. macOS（Apple Silicon 测试通过）
2. 安装并启动 **Alpha**，在 Alpha 中完成微信数据库解密（微信加密 key 配在 Alpha 里）
3. 从 Alpha 设置页复制 **API Token**
4. 准备一个 **DeepSeek API Key**
5. Python 3.9+

### 1. 安装

```bash
git clone https://github.com/Aruzestattvat/Alpha-Plugin.git
cd Alpha-Plugin
./setup.sh
```

`setup.sh` 会：

- 检查 Alpha 本地 API；
- 提示输入 `ALPHA_TOKEN` / `DEEPSEEK_API_KEY`，写入 `~/.config/wvbar/secrets.env`（权限 600）；
- 创建 `.venv` 并安装依赖；
- 编译菜单栏程序 `./wvbar`。

### 2. 启动

```bash
./start.sh
```

`./start.sh` 会启动后端和菜单栏，并打开仪表盘：

<http://127.0.0.1:8765>

### 3. 自检 / 停止

```bash
./doctor.sh   # 检查 Alpha、token、DeepSeek、ASR、进程
./stop.sh     # 停止后端与菜单栏
```

详细说明见 [docs/SETUP.md](docs/SETUP.md)。

## 目录结构

| 路径 | 说明 |
|---|---|
| `wvcore2.py` | 主后端：Alpha 客户端、同步、ASR、分析、HTTP/SSE、状态输出 |
| `context_trend.py` | 按消息上下文的趋势计算 |
| `relationship_*.py` | 关系稳定性、时间特征、边界证据核验 |
| `dashboard2.html` | 仪表盘 |
| `menubar.swift` / `wvbar` | 菜单栏程序与编译产物 |
| `build.sh` / `build_app.py` | 编译菜单栏 / 打包 `知间.app` |
| `setup.sh` / `start.sh` / `stop.sh` / `doctor.sh` | 一键安装、启动、停止、自检 |
| `tests/` | 单元测试（100 个） |
| `assets/` | Logo、封面与图标槽位说明 |
| `config.example.env` | 配置模板（不要把真实密钥提交） |
| `docs/SETUP.md` | Alpha / 微信 key / 常见问题 |
| `state.example.json` | 状态文件格式示例（虚构数据） |

## 默认模型策略（混合）

- **分片摘要 / 趋势采样 / 增量分析**：`deepseek-flash`（V4.1 Flash）——量大、任务简单，成本最低；
- **最终汇总 / 关系判断 / rich 回退**：`deepseek-v4-pro`——涉及长历史综合与关系边界，精度优先。

可用环境变量分别覆盖：

```bash
WV_MODEL_CHUNK=deepseek-flash
WV_MODEL_FINAL=deepseek-v4-pro
WV_ECO_MODEL=deepseek-flash
```

## 配置（环境变量 / secrets.env）

| 变量 | 默认 | 说明 |
|---|---|---|
| `ALPHA_BASE` | `http://127.0.0.1:5031` | Alpha 本地 API 地址 |
| `ALPHA_TOKEN` | — | Alpha API Token |
| `DEEPSEEK_API_KEY` | — | DeepSeek Key |
| `WV_SECRETS_PATH` | `~/.config/wvbar/secrets.env` | 密钥文件位置 |
| `WV_REALTIME_PROFILE` | `accurate` | `eco`=省额度；`accurate`=每条全量 |
| `WV_MODEL_CHUNK` | `deepseek-flash` | 分片摘要模型（V4.1 Flash） |
| `WV_MODEL_FINAL` | `deepseek-v4-pro` | 汇总/关系判断/rich 回退（V4 Pro，精度优先） |
| `WV_ECO_MODEL` | `deepseek-flash` | 增量模型（V4.1 Flash，成本优先） |
| `WV_ECO_BATCH_MESSAGES` / `WV_ECO_BATCH_SECONDS` | 5 / 90 | 实时批次合并 |
| `WV_SCORE_GAIN` | 1.15 | 分数拉伸系数 |
| `WV_RELATION_STRICT` | 1 | 严格原文核验 |
| `WV_TREND_MESSAGES` / `WV_TREND_WINDOWS` | 300 / 5 | 趋势采样窗口 |
| `WV_TREND_MIN_NEW` | 30 | 新增多少条后重采样 |
| `WV_TREND_SAMPLING` | 1 | 关闭趋势采样设为 0 |
| `WV_ASR_MODEL_DIR` | Alpha 语音模型目录 | Qwen3-ASR 模型路径 |
| `WV_HTTP_PORT` / `WV_DB_PATH` / `WV_STATE_PATH` / `WV_WATCHED_PATH` | — | 端口与运行时路径 |

## 隐私与安全

- 仓库**不提交**：`state.json`、`watched.json`、`realtime.json`、`*.sqlite3*`、`avatars/`、`shots/`、日志、密钥文件、Swift 编译缓存。
- 微信 **SQLCipher 加密 key** 只存在于 Alpha；本项目没有任何读取或存储微信 key 的代码。
- `ALPHA_TOKEN` / `DEEPSEEK_API_KEY` 只保存在本机 `~/.config/wvbar/secrets.env`（600），不进入仓库。
- 分析内容会发送给 DeepSeek；除此以外不会上传聊天数据。
- 公开部署前请再次确认 `.gitignore` 生效。

## 测试

```bash
python3 -m unittest discover -s tests -p "test_*.py"
```

## 许可

未指定 License。公开分发前请补充合适的许可证（如 MIT / 私有仓库）。
