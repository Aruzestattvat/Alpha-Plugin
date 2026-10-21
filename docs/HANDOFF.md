# 微信好感度菜单栏应用 — 交接文档（给 Codex）

> 生成时间：2026-10-20 · 当前分析任务仍在后台运行

## 1. 位置与入口

| 项 | 地址 |
|---|---|
| 项目根目录 | `$PROJECT_ROOT` |
| 仪表盘（本机 Web UI） | http://127.0.0.1:8765 |
| 菜单栏程序 | `./wvbar`（Swift 编译产物，读取同目录 `state.json`） |
| 密钥配置（勿提交） | `~/.config/wvbar/secrets.env`（权限 600，含 `ALPHA_TOKEN` / `DEEPSEEK_API_KEY`） |
| 日志 | `daemon2.log`（当前后端）、`daemon.log`（旧版） |
| 数据库 | `wvcore2.sqlite3`（当前）、`wvcore.sqlite3` / `wvdaemon.sqlite3`（旧版，可删） |
| 头像缓存 | `avatars/`（131 个，约 7.7MB） |
| 旧版脚本 | `wvcore.py`、`wvdaemon.py`、`dashboard.html`（v1，保留参考，可删） |

## 2. 运行中的进程与启停

当前进程：

```
wvcore2.py    —— Python 后端：Alpha API 同步 + 本地 ASR + LLM 分片分析 + Web 仪表盘(8765) + 写 state.json
wvbar <state.json> —— Swift 菜单栏 App：每秒读 state.json，显示置顶会话头像+分数
```

启停命令（在项目目录执行，需先 `set -a; . ~/.config/wvbar/secrets.env; set +a`）：

```bash
# 停止
pkill -f wvcore2.py; pkill -f "wvbar .*state.json"

# 启动后端（后台）
nohup python3 wvcore2.py >> /tmp/wvcore2.out 2>&1 &

# 启动菜单栏（后台）
nohup ./wvbar "$PWD/state.json" > /tmp/wvbar.log 2>&1 &

# 重新编译菜单栏
TMPDIR="$PWD/.tmp" swiftc -O \
  -sdk /Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk \
  -module-cache-path "$PWD/.modulecache" \
  -framework Cocoa menubar.swift -o wvbar
```

> 注意：CLT 默认 SDK（MacOSX27.0）与当前 swiftc 6.3.3 不匹配，必须显式指定 `MacOSX26.5.sdk`，否则编译报错。

## 3. 架构与数据流

```
Alpha.app（本机 HTTP API 127.0.0.1:5031，Bearer token）
   │  会话列表 / 全量消息 / 媒体(SILK→WAV) / SSE 新消息推送
   ▼
wvcore2.py
   ├─ 全量同步消息（media=false，1000 条/页，offset 分页）
   ├─ 语音：按 seq 单条取 media=true → WAV → 本地 Qwen3-ASR 转写
   ├─ 分片分析：每 150 条一片（重叠 10 条）→ deepseek-flash
   ├─ 统计特征：消息数/我对方比/回复中位数/谁先开口/活跃时段/语音数/跨度
   ├─ 汇总：分片摘要 + 统计 → deepseek-v4-pro（关系与边界判断精度优先）（score/delta/reason/dimensions/trajectory）
   ├─ 增量：SSE 收到新消息 → 防抖 3s → 重算最后一片 + 重汇总
   └─ 输出
        ├─ state.json（菜单栏读）
        ├─ wvcore2.sqlite3（消息/分片/评分）
        └─ Web 仪表盘 8765（勾选会话、进度、依据）
```

## 4. 关键实现细节 / 坑

### Alpha HTTP API
- 鉴权：`Authorization: Bearer <ALPHA_TOKEN>`；`/health` 免鉴权。
- 会话：`GET /api/v1/sessions?limit=200`，字段 `id/display_name/kind/unread_count/last_activity_unix_ms/avatar_uri`。
  - `kind`：1=私聊、2=群聊、3=公众号/品牌、4=其他。前端只展示 1/2。
- 消息：`GET /api/v1/messages?talker=<sid>&limit=&offset=&media=true|false`
  - **默认按时间升序（最旧在前）**，必须用 offset 取尾部，不能直接 limit=N 当"最近 N 条"。
  - `media=true` 时单页上限约 500（800 会 422）；`media=false` 时 1000 正常。
  - 单条消息字段：`platform_message_id/id, sent_at_unix_ms, direction, kind, text, media.resources[]`。
  - `direction`：`"2"`=我，`"1"`=对方，`"0"`=系统。
  - `kind`：`text` / `voice` / `media` / `xml`。
  - 语音媒体：`media.resources[]` 中 `mimeType=audio/silk` 的 `url`，请求 `url + "?format=wav"` 得到 24kHz WAV（Alpha 只解码不转写）。
- 头像：会话的 `avatar_uri` 可能是 https（直接下载）或 opaque id（走 `/api/v1/media/<id>`）。
- SSE：`GET /api/v1/push/messages?accountId=<uuid>`，事件 `message.new`；需在 Alpha 设置里开启"主动推送"。
- 已知坑：整页 `media=true` 对某些会话会 422 → 本项目全量同步改为 media=false，语音单独按 `offset=seq` 单条取。
- 已知坑：Alpha macOS 版"本地转写组件"缺失，但其 Qwen3-ASR 模型已下载到本地，本项目直接用 sherpa-onnx 调用。

### 本地语音转写
- 模型：`~/Library/Application Support/Alpha/voice/models/qwen3-asr-0.6b/`
  （`conv_frontend.onnx` + `encoder.int8.onnx` + `decoder.int8.onnx` + `tokenizer/`，共 944MB）
- 运行时：`pip3 install --user sherpa-onnx`（已验证 macOS arm64 / Python 3.9 / v1.13.8）
- API：`sherpa_onnx.OfflineRecognizer.from_qwen3_asr(conv_frontend, encoder, decoder, tokenizer, num_threads=4)`
- 性能：加载 ~1.3s，5 秒语音转写 ~0.5s，CPU 即可。
- 注意：模型属于 Alpha 的数据目录，Alpha 清理/升级可能删除 → 建议复制到项目内并改 `ASR_MODEL_DIR`。

### LLM
- 端点：`https://api.deepseek.com/chat/completions`（OpenAI 兼容）
- 模型：分片/增量 `deepseek-flash`（V4.1 Flash），汇总/关系判断 `deepseek-v4-pro`（V4 Pro；均可从 `/models` 查到）
- 两者都是**思考型模型**：`max_tokens` 需给足（分片 2500 / 汇总 4000），否则 content 为空。
- `response_format: {"type":"json_object"}` 可用；返回内容需正则提取 `{...}` 兜底。
- 隐私：全量消息（含语音转写）会发送给 DeepSeek；如需全本地可换 Ollama（同一 OpenAI 兼容接口）。

### 数据库（wvcore2.sqlite3）
```sql
sessions(id, name, kind, avatar, unread, last_ts, updated_at)
scores(session_id, score, delta, reason, dimensions, summary, trajectory, updated_at)
messages(session_id, msg_id, ts, direction, kind, text, voice_url, seq)  -- 全量消息
chunks(session_id, idx, from_ts, to_ts, count, summary)                  -- 分片摘要 JSON
analysis(session_id, status, chunks_total, chunks_done, msgs_total, error, started_at, finished_at)
transcripts(msg_id, text, created_at)                                     -- ASR 缓存
seen(msg_id, session_id, ts)                                             -- SSE 去重
```
- 使用线程本地 SQLite 连接（`Store.conn` property）避免多线程共用连接导致段错误。
- WAL 模式 + `busy_timeout=30000`。

### HTTP 接口（仪表盘后端）
- `GET /api/sessions` 会话列表 + 分析状态/进度
- `GET /api/state` 菜单栏 state（含 chats）
- `GET /api/evidence/<sid>` 统计、分片摘要、最近 40 条消息（含语音转写）
- `POST /api/watch {ids:[]}` 保存勾选
- `POST /api/analyze {id, force}` 单会话全量分析
- `POST /api/analyze_all` 全部已选会话分析

### 菜单栏 App（menubar.swift）
- 读取 `state.json`（mtime 变化时刷新，1s 轮询）；无数据时显示渐变爱心 + "–"。
- 置顶逻辑：点击菜单项切换置顶；未置顶时显示最高分会话。
- 显示：头像/首字圆 + 分数 + 涨跌箭头；外圈颜色：≥75 绿、≥55 橙、<55 红。
- 支持自定义图标：`~/Library/Application Support/wvbar/icon.png`。
- 菜单项：立即刷新、打开仪表盘（127.0.0.1:8765）、退出。
- 已知：没有登录自启（需 launchd）；没有单实例锁（重复启动会出现多个菜单栏图标）。

## 5. 依赖安装记录
```bash
python3 -m pip install --user -i https://pypi.tuna.tsinghua.edu.cn/simple sherpa-onnx
# 注意：rumps 在 Python 3.9 + macOS 26 上装不上（pyobjc 无轮子），故菜单栏用原生 Swift
```

## 6. 已知问题 / 建议 Codex 优先处理

1. **登录自启 + 单实例**：写 launchd LaunchAgent 启动 wvcore2.py 与 wvbar；加 pid/lock 文件防重复。
2. **分片内容容错**：模型偶尔把 `positive/negative/notable/topics` 返回成对象/嵌套数组，dashboard2.html 直接 `join` 会显示 `[object Object]`，建议在服务端 normalize 成字符串数组。
3. **错误重试**：同步页/分片/汇总失败目前只记录不重试，建议加指数退避重试队列。
4. **增量分析精度**：`tail_update` 只重算"最后一片"并沿用旧分片数；新消息多时应追加新片而不是覆盖最后一片。
5. **进度日志**：只在分片完成计数，建议每片写一行日志，方便排查。
6. **成本/限流**：没有 token 统计与并发上限；建议记录每次调用 usage，增加预算上限。
7. **模型备份**：把 944MB 模型复制到项目内（或 `~/Library/Application Support/wvbar/models/`），避免 Alpha 更新删掉。
8. **清理旧代码**：`wvcore.py`、`wvdaemon.py`、`wvcore.sqlite3*`、`wvdaemon.sqlite3*`、`dashboard.html`、`state.example.json` 可删。
9. **安全**：`~/.config/wvbar/secrets.env` 与 Alpha token / DeepSeek key 不要提交到任何仓库；截图里已泄露过一次，建议轮换。
10. **测试**：建议对 `stats_of`、分片/汇总解析、消息分页（offset/升序）加单元测试；可用 mock Alpha server。
11. **语音媒体补齐**：按 `seq±2` 找媒体，微信删消息可能错位；可改为按 `platform_message_id` 从页面索引重建。
12. **群聊支持**：目前统计与 prompt 未区分群聊/私聊，群聊的好感度语义建议单独设计（群氛围 vs 成员）。

## 7. 快速验证

```bash
# 仪表盘
curl -s http://127.0.0.1:8765/api/sessions | head -c 300

# 某会话依据（含分片摘要/最近消息）
curl -s http://127.0.0.1:8765/api/evidence/<session_id> | head -c 500

# 查看分析进度
sqlite3 wvcore2.sqlite3 "select session_id,status,chunks_done,chunks_total,msgs_total from analysis;"

# 查看当前分数
sqlite3 wvcore2.sqlite3 "select session_id,score,reason from scores;"
```

## 8. 当前状态（交接时）

- 后端 `wvcore2.py` 正在跑：[已隐去] 已同步 8212 条、92 条语音全部转写、分片分析进行中（约 22/55）。
- 其余 3 个已选会话排队/待重跑（此前失败残留 `status=error`）。
- 菜单栏 `wvbar` 运行中，读取 `state.json`。
- `watched.json` 中为 4 个已勾选会话的 wxid。
