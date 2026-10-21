# 安装与 Alpha 联动指南

目标：**下载本项目 → 配置 Alpha API Token 和 DeepSeek Key → 启动 → 在 Alpha 里完成微信 key 配置 → 直接联动**。

---

## 0. 先理解两个 key

| Key | 配在哪里 | 本项目是否需要 |
|---|---|---|
| **微信 SQLCipher 加密 key** | **Alpha 内部**（Alpha 负责解密微信数据库） | ❌ 不需要，本仓库没有任何读取/存储微信 key 的代码 |
| **Alpha API Token** | 本项目 `secrets.env` 的 `ALPHA_TOKEN` | ✅ 需要 |
| **DeepSeek API Key** | 本项目 `secrets.env` 的 `DEEPSEEK_API_KEY` | ✅ 需要 |

所以流程是：

```
微信 4.x 数据库
   │  微信加密 key（在 Alpha 中导入）
   ▼
Alpha.app（负责解密 + 本地 HTTP API）
   │  http://127.0.0.1:5031（Bearer ALPHA_TOKEN）
   ▼
本项目 wvcore2.py（读取已解密消息 → ASR → DeepSeek 分析）
```

---

## 1. 准备 Alpha

1. 安装 Alpha（官方发布页：[**xunchahaha/alpha-releases**](https://github.com/xunchahaha/alpha-releases)），并确认它可以正常读取本机微信 4.x 数据；
2. 在 Alpha 中导入/配置**微信加密 key**（Alpha 的设置页/数据源页）；
3. 启动 Alpha，确认本地 API 可访问：

```bash
curl http://127.0.0.1:5031/health
```

4. 在 Alpha 设置里复制 **API Token**（形如一串本地令牌）。

> Alpha 可能需要特定权限（部分版本需要 SIP/管理员权限）。这属于 Alpha 的安装问题，本项目不修改 Alpha、也不接触微信 key。

如果 Alpha 不在默认端口，设置：

```bash
export ALPHA_BASE=http://127.0.0.1:5031
```

---

## 2. 准备 DeepSeek Key

到 DeepSeek 开放平台创建 API Key。本项目只在分析时调用 DeepSeek。

---

## 3. 安装本项目

```bash
git clone https://github.com/Aruzestattvat/Alpha-Plugin.git
cd Alpha-Plugin
./setup.sh
```

`setup.sh` 会：

1. 检查 `ALPHA_BASE/health`；
2. 创建 `~/.config/wvbar/secrets.env`（权限 600），写入：
   ```env
   ALPHA_BASE=http://127.0.0.1:5031
   ALPHA_TOKEN=...
   DEEPSEEK_API_KEY=...
   ```
3. 创建 `.venv`、安装 `requirements.txt`；
4. 调用 `build.sh` 编译菜单栏程序。

也可以不用提示、直接通过环境变量安装：

```bash
ALPHA_TOKEN=xxx DEEPSEEK_API_KEY=yyy ./setup.sh
```

---

## 4. 启动与验证

```bash
./start.sh
```

会启动：

- Python 后端（`127.0.0.1:8765`）
- Swift 菜单栏程序
- 自动打开仪表盘

自检：

```bash
./doctor.sh
```

检查项：

1. Alpha `/health`
2. `ALPHA_TOKEN` 是否可用
3. `DEEPSEEK_API_KEY` 是否可用
4. Python/菜单栏/后端进程
5. ASR 模型（可选）

停止：

```bash
./stop.sh
```

---

## 5. 选择会话并分析

1. 打开 <http://127.0.0.1:8765>；
2. 在列表中勾选要分析的会话（已选会话会沉到列表底部）；
3. 保存选择 → 全量分析；
4. 之后可开启实时分析（省额度 / 高精度模式切换）。

---

## 6. 可选：语音转写

本项目用 Qwen3-ASR 0.6B ONNX 本地转写语音。默认查找：

```text
~/Library/Application Support/Alpha/voice/models/qwen3-asr-0.6b/
```

也可以指定：

```bash
export WV_ASR_MODEL_DIR=/path/to/qwen3-asr-0.6b
```

没有模型时语音消息会被跳过，其他功能正常。

---

## 7. 可选：打包成 .app

```bash
python3 build_app.py --destination "/Applications/知间.app"
```

`assets/zhijian-logo.png` 是图标槽位，替换后重新打包即可（见 `assets/ICON_SLOT.md`）。

---

## 8. 常见问题

### Alpha health 不通

- Alpha 是否已经启动？
- 端口是否被改动？用 `ALPHA_BASE` 指定正确地址；
- 部分 Alpha 版本需要管理员权限 / 允许本机 HTTP。

### `ALPHA_TOKEN` 401

- 重新从 Alpha 设置复制 Token；
- 确认没有多余空格/引号；
- 修改 `~/.config/wvbar/secrets.env` 后重启后端：

```bash
./stop.sh && ./start.sh
```

### Alpha 提示数据源身份变化 / 读不到微信

这是 **Alpha 与微信 key** 的问题，不是本项目：

- 回到 Alpha 重新导入微信加密 key；
- 确认微信版本与 Alpha 支持的版本一致；
- 不要在本项目里找微信 key 的配置项，它不存在。

### DeepSeek 额度不足

- `doctor.sh` 会检查余额；
- 充值后点仪表盘的“检查额度并重试”。

### 菜单栏没有出现

- 确认 `./wvbar` 已生成（`bash build.sh`）；
- 检查系统菜单栏空间/权限；
- 查看 `./start.sh` 输出。

---

## 9. 安全建议

- `~/.config/wvbar/secrets.env` 权限必须是 600，且不要提交到 git；
- 不要把 Alpha Token、DeepSeek Key、微信 key 写进 README、截图或 issue；
- 本项目默认 `.gitignore` 已排除 `state.json`、数据库、头像、日志、截图、锁文件和密钥；
- 如果 token 曾经泄露，请在对应平台立即轮换。
