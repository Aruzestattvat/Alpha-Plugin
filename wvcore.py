#!/usr/bin/env python3
"""
wvcore —— 微信好感度后台（仪表盘 + 打分 + 菜单栏数据）

- 仪表盘：http://127.0.0.1:8765  自主勾选要分析的会话
- 数据源：Alpha 本机 HTTP API（127.0.0.1:5031，Bearer token）
- 打分：DeepSeek（OpenAI 兼容），只对勾选的会话评分
- 输出：state.json（菜单栏读）+ wvcore.sqlite3（历史/证据）+ avatars/
"""
import hashlib
import json
import os
import pathlib
import re
import sqlite3
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parent
STATE = ROOT / "state.json"
AVATARS = ROOT / "avatars"
DB_PATH = ROOT / "wvcore.sqlite3"
WATCHED = ROOT / "watched.json"
LOG_PATH = ROOT / "daemon.log"
DASHBOARD = ROOT / "dashboard.html"
SECRETS = pathlib.Path.home() / ".config/wvbar/secrets.env"

ALPHA_BASE = "http://127.0.0.1:5031"
DEEPSEEK_BASE = "https://api.deepseek.com"
MODEL = "deepseek-flash"
HTTP_PORT = 8765

INIT_MESSAGES = 30
UPDATE_MESSAGES = 20
DEBOUNCE_SECONDS = 3
RESCORE_MIN_INTERVAL = 20
KIND_PRIVATE, KIND_GROUP = 1, 2
MAX_MESSAGES_TRANSCRIPT = 40


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n"
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line)
    print(line, end="", flush=True)


def load_secrets() -> dict:
    out = {}
    if SECRETS.exists():
        for line in SECRETS.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


class Alpha:
    def __init__(self, token):
        self.token = token
        self.account_id = None

    def _open(self, path, timeout=40):
        req = urllib.request.Request(
            ALPHA_BASE + path, headers={"Authorization": f"Bearer {self.token}"})
        return urllib.request.urlopen(req, timeout=timeout)

    def get(self, path, timeout=40):
        with self._open(path, timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def resolve_account(self):
        acc = self.get("/api/v1/accounts")
        self.account_id = acc.get("selected_account_id") or acc.get("selectedAccountId")
        items = acc.get("accounts") or []
        if not self.account_id and items:
            self.account_id = items[0].get("id")
        return (items[0].get("display_name") if items else "") or ""

    def sessions(self, limit=200):
        return self.get(f"/api/v1/sessions?limit={limit}").get("items") or []

    def messages(self, sid, limit=30):
        q = urllib.parse.quote(sid, safe="")
        return self.get(
            f"/api/v1/messages?talker={q}&limit={limit}&media=true"
        ).get("messages") or []

    def raw_messages(self, sid, limit, offset=0, media=True):
        q = urllib.parse.quote(sid, safe="")
        m = "true" if media else "false"
        return self.get(
            f"/api/v1/messages?talker={q}&limit={limit}&offset={offset}&media={m}"
        )

    def message_total(self, sid) -> int:
        """分页数出会话总消息数（media=false，单页可到 1000+）。"""
        total = 0
        offset = 0
        while True:
            r = self.raw_messages(sid, 1000, offset, media=False)
            msgs = r.get("messages") or []
            total += len(msgs)
            if not r.get("hasMore") or not msgs:
                break
            offset = r.get("nextOffset")
            if offset is None:
                break
        return total

    def media_wav_bytes(self, url: str) -> bytes:
        sep = "&" if "?" in url else "?"
        req = urllib.request.Request(
            ALPHA_BASE + url + sep + "format=wav",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read()

    def media(self, uri):
        if uri.startswith("http://") or uri.startswith("https://"):
            with urllib.request.urlopen(urllib.request.Request(uri), timeout=30) as r:
                return r.read()
        ident = urllib.parse.quote(uri, safe="")
        with self._open(f"/api/v1/media/{ident}", timeout=30) as r:
            return r.read()

    def push_stream(self, account_id, stop):
        url = f"{ALPHA_BASE}/api/v1/push/messages?accountId={urllib.parse.quote(account_id)}"
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self.token}",
            "Accept": "text/event-stream",
        })
        with urllib.request.urlopen(req, timeout=600) as r:
            event = None
            while not stop.is_set():
                raw = r.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    try:
                        yield event, json.loads(line[5:].strip())
                    except Exception:
                        continue


class Store:
    def __init__(self):
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS sessions(
            id TEXT PRIMARY KEY, name TEXT, kind INTEGER, avatar TEXT,
            unread INTEGER, last_ts INTEGER, updated_at INTEGER)""")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS scores(
            session_id TEXT PRIMARY KEY, score INTEGER, delta INTEGER,
            reason TEXT, updated_at INTEGER)""")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS seen(
            msg_id TEXT PRIMARY KEY, session_id TEXT, ts INTEGER)""")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS transcripts(
            msg_id TEXT PRIMARY KEY, text TEXT, created_at INTEGER)""")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS session_totals(
            session_id TEXT PRIMARY KEY, total INTEGER, last_activity INTEGER,
            updated_at INTEGER)""")
        self.conn.commit()

    def score(self, sid):
        r = self.conn.execute("SELECT score,delta,reason FROM scores WHERE session_id=?",
                              (sid,)).fetchone()
        return {"score": r[0], "delta": r[1], "reason": r[2]} if r else None

    def set_score(self, sid, score, delta, reason):
        self.conn.execute("""INSERT INTO scores(session_id,score,delta,reason,updated_at)
            VALUES(?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET
            score=excluded.score, delta=excluded.delta, reason=excluded.reason,
            updated_at=excluded.updated_at""",
            (sid, score, delta, reason, int(time.time())))
        self.conn.commit()

    def total(self, sid):
        r = self.conn.execute(
            "SELECT total,last_activity FROM session_totals WHERE session_id=?",
            (sid,)).fetchone()
        return (r[0], r[1]) if r else (None, None)

    def set_total(self, sid, total, last_activity):
        self.conn.execute(
            """INSERT INTO session_totals(session_id,total,last_activity,updated_at)
               VALUES(?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET
               total=excluded.total, last_activity=excluded.last_activity,
               updated_at=excluded.updated_at""",
            (sid, total, last_activity or 0, int(time.time())))
        self.conn.commit()

    def transcript(self, mid):
        r = self.conn.execute("SELECT text FROM transcripts WHERE msg_id=?", (mid,)).fetchone()
        return r[0] if r else None

    def set_transcript(self, mid, text):
        self.conn.execute(
            "INSERT OR REPLACE INTO transcripts(msg_id,text,created_at) VALUES(?,?,?)",
            (mid, text, int(time.time())))
        self.conn.commit()

    def mark_seen(self, mid, sid, ts):
        try:
            self.conn.execute("INSERT OR IGNORE INTO seen(msg_id,session_id,ts) VALUES(?,?,?)",
                              (mid, sid, int(ts)))
            self.conn.commit()
            return True
        except Exception:
            return False

    def upsert_session(self, s, avatar=None):
        self.conn.execute("""INSERT INTO sessions(id,name,kind,avatar,unread,last_ts,updated_at)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            name=excluded.name, kind=excluded.kind,
            avatar=COALESCE(excluded.avatar,sessions.avatar),
            unread=excluded.unread, last_ts=excluded.last_ts,
            updated_at=excluded.updated_at""",
            (s["id"], s.get("display_name") or s["id"], s.get("kind"), avatar,
             s.get("unread_count") or 0, s.get("last_activity_unix_ms") or 0,
             int(time.time())))
        self.conn.commit()

    def known_sessions(self):
        return {r[0]: {"id": r[0], "name": r[1], "kind": r[2], "avatar": r[3],
                       "unread": r[4], "last_ts": r[5]}
                for r in self.conn.execute(
                    "SELECT id,name,kind,avatar,unread,last_ts FROM sessions")}


ASR_MODEL_DIR = pathlib.Path.home() / (
    "Library/Application Support/Alpha/voice/models/qwen3-asr-0.6b")


class Asr:
    """本地 Qwen3-ASR（sherpa-onnx）。懒加载，首次转写时载入。"""

    def __init__(self, model_dir=None):
        self.model_dir = pathlib.Path(model_dir or ASR_MODEL_DIR)
        self.rec = None
        self._lock = threading.Lock()
        self.enabled = self.model_dir.exists()

    def ensure(self):
        if not self.enabled:
            raise RuntimeError("ASR 模型目录不存在")
        if self.rec is not None:
            return
        with self._lock:
            if self.rec is not None:
                return
            import sherpa_onnx
            m = self.model_dir
            t0 = time.time()
            self.rec = sherpa_onnx.OfflineRecognizer.from_qwen3_asr(
                conv_frontend=str(m / "conv_frontend.onnx"),
                encoder=str(m / "encoder.int8.onnx"),
                decoder=str(m / "decoder.int8.onnx"),
                tokenizer=str(m / "tokenizer"),
                num_threads=4,
                decoding_method="greedy_search",
            )
            log(f"ASR 模型加载完成 {time.time()-t0:.2f}s")

    def transcribe_wav(self, data: bytes) -> str:
        import io, wave, array
        self.ensure()
        with wave.open(io.BytesIO(data), "rb") as w:
            rate = w.getframerate()
            ch = w.getnchannels()
            raw = w.readframes(w.getnframes())
        a = array.array("h")
        a.frombytes(raw)
        if ch > 1:
            a = a[::ch]
        samples = [x / 32768.0 for x in a]
        st = self.rec.create_stream()
        st.accept_waveform(rate, samples)
        self.rec.decode_stream(st)
        return (st.result.text or "").strip()


SYSTEM_PROMPT = (
    "你是聊天关系分析器。根据对话记录评估「对方（或群聊整体）对用户的当前好感度」。"
    "分数范围 0-100：100=非常亲近、主动、热情、愿意分享；"
    "70=明显友好、有互动意愿；50=普通礼貌、日常寒暄；"
    "30=冷淡、敷衍、公事化；10=疏离、回避、不耐烦。"
    "只输出 JSON：{\"score\": 整数, \"delta\": 整数(-5..5), \"reason\": \"不超过18字的中文理由\"}。"
    "delta 表示相对上次的变化；reason 要具体（例如「对方主动分享日常」「回复变慢语气变冷」）。"
    "不要输出任何其他文字。"
)


def deepseek_chat(api_key, system, user):
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0.2,
        "max_tokens": 2000,
        "response_format": {"type": "json_object"},
    }).encode("utf-8")
    req = urllib.request.Request(
        DEEPSEEK_BASE + "/chat/completions", data=body,
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read().decode("utf-8"))
    choice = data["choices"][0]
    content = choice["message"].get("content") or ""
    if not content.strip():
        raise ValueError(f"empty content (finish={choice.get('finish_reason')})")
    m = re.search(r"\{.*\}", content, re.S)
    if not m:
        raise ValueError("no json in response")
    return json.loads(m.group(0))


def build_transcript(messages, transcript_of):
    lines = []
    for m in messages:
        text = transcript_of(m)
        if not text:
            continue
        role = "我" if str(m.get("direction")) == "2" else "对方"
        text = text.strip().replace("\n", " ")
        if len(text) > 120:
            text = text[:120] + "…"
        lines.append(f"{role}: {text}")
    return "\n".join(lines[-MAX_MESSAGES_TRANSCRIPT:])


def parse_score(obj, prev):
    try:
        score = max(0, min(100, int(obj.get("score"))))
    except Exception:
        return None
    delta = int(obj.get("delta") or 0) if prev is None else score - int(prev)
    delta = max(-5, min(5, delta))
    reason = str(obj.get("reason") or "").strip()[:40]
    return score, delta, reason


class Daemon:
    def __init__(self):
        secrets = load_secrets()
        self.alpha = Alpha(secrets.get("ALPHA_TOKEN", ""))
        self.ds_key = secrets.get("DEEPSEEK_API_KEY", "")
        if not self.alpha.token:
            raise SystemExit("缺少 ALPHA_TOKEN")
        if not self.ds_key:
            raise SystemExit("缺少 DEEPSEEK_API_KEY")
        AVATARS.mkdir(exist_ok=True)
        self.store = Store()
        self.self_name = ""
        self.watched = set()
        self.pending_score = set()
        self.pending_msgs = {}
        self.last_score_at = {}
        self.sessions_cache = {}
        self.asr = Asr() if os.environ.get("WV_ASR", "1") != "0" else Asr(model_dir="/nonexistent")
        if self.asr.enabled:
            log(f"ASR 已启用: {self.asr.model_dir.name}")
        else:
            log("ASR 未启用（未找到本地模型）")
        self.load_watched()

    def load_watched(self):
        try:
            self.watched = set(json.loads(WATCHED.read_text(encoding="utf-8")).get("ids") or [])
        except Exception:
            self.watched = set()

    def save_watched(self):
        tmp = WATCHED.with_suffix(".tmp")
        tmp.write_text(json.dumps({"ids": sorted(self.watched)}, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(WATCHED)

    def recent_messages(self, sid, limit=30):
        """取会话最后 limit 条消息（含媒体链接）。"""
        sess = self.sessions_cache.get(sid) or {}
        la = sess.get("last_activity_unix_ms")
        total, cached_la = self.store.total(sid)
        if total is None:
            total = self.alpha.message_total(sid)
            self.store.set_total(sid, total, la)
        else:
            # 增量计数：只数新增部分
            off = total
            while True:
                r = self.alpha.raw_messages(sid, 500, off, media=True)
                msgs = r.get("messages") or []
                total += len(msgs)
                if not r.get("hasMore") or not msgs:
                    break
                off = r.get("nextOffset")
                if off is None:
                    break
            self.store.set_total(sid, total, la)
        offset = max(0, total - limit)
        msgs = self.alpha.raw_messages(sid, limit, offset, media=True).get("messages") or []
        if len(msgs) < limit and total > limit:
            # 总数漂移，重算一次
            total = self.alpha.message_total(sid)
            self.store.set_total(sid, total, la)
            msgs = self.alpha.raw_messages(
                sid, limit, max(0, total - limit), media=True).get("messages") or []
        return msgs

    def transcript_of(self, msg):
        kind = msg.get("kind")
        if kind == "text":
            text = msg.get("text")
            return text if isinstance(text, str) else ""
        if kind != "voice":
            return ""
        mid = msg.get("platform_message_id") or msg.get("id") or ""
        if mid:
            cached = self.store.transcript(mid)
            if cached is not None:
                return "[语音] " + cached
        res = (msg.get("media") or {}).get("resources") or []
        voice = None
        for r in res:
            mime = (r.get("mimeType") or "").lower()
            if "silk" in mime or "audio" in mime or "voice" in mime:
                voice = r
                break
        if voice is None and res:
            voice = res[0]
        if not voice or not voice.get("url"):
            return "[语音]"
        try:
            wav = self.alpha.media_wav_bytes(voice["url"])
            text = self.asr.transcribe_wav(wav)
            if mid:
                self.store.set_transcript(mid, text)
            return "[语音] " + text
        except Exception as e:
            log(f"语音转写失败 {str(mid)[:8]}: {type(e).__name__}: {e}")
            return "[语音]"

    def avatar_path(self, sid, uri):
        if not uri:
            return None
        digest = hashlib.sha1(sid.encode()).hexdigest()[:16]
        p = AVATARS / f"{digest}.img"
        if p.exists() and p.stat().st_size > 0:
            return str(p)
        try:
            p.write_bytes(self.alpha.media(uri))
            return str(p)
        except Exception as e:
            log(f"头像失败 {sid[:8]}: {type(e).__name__}")
            return None

    def score_one(self, sid, force=False):
        sess = self.sessions_cache.get(sid)
        if not sess:
            sess = next((s for s in self.alpha.sessions(200) if s["id"] == sid), None)
            if not sess:
                return
            self.sessions_cache[sid] = sess
        prev = self.store.score(sid)
        if prev and not force:
            return
        msgs = self.recent_messages(sid, INIT_MESSAGES)
        transcript = build_transcript(msgs, self.transcript_of)
        if len(transcript) < 10:
            log(f"跳过 {sid[:8]}: 无可用消息")
            return
        user = (f"上次分数：{prev['score']}\n" if prev else "上次分数：无（首次评估）\n") + \
               "对话记录：\n" + transcript
        try:
            obj = deepseek_chat(self.ds_key, SYSTEM_PROMPT, user)
            parsed = parse_score(obj, prev["score"] if prev else None)
        except Exception as e:
            log(f"打分失败 {sid[:8]}: {type(e).__name__}: {e}")
            return
        if not parsed:
            return
        score, delta, reason = parsed
        self.store.set_score(sid, score, delta, reason)
        avatar = self.avatar_path(sid, sess.get("avatar_uri"))
        self.store.upsert_session(sess, avatar)
        for m in msgs:
            mid = m.get("platform_message_id") or m.get("id")
            if mid:
                self.store.mark_seen(mid, sid, m.get("sent_at_unix_ms") or 0)
        log(f"打分 {sid[:8]}: {score} ({delta:+d})")

    def ensure_scored(self, ids):
        todo = [i for i in ids if i not in self.pending_score]
        if not todo:
            return
        self.pending_score.update(todo)

        def work():
            try:
                for sid in todo:
                    if sid in self.watched:
                        self.score_one(sid)
                self.refresh_session_meta()
                self.write_state()
            finally:
                self.pending_score.difference_update(todo)

        threading.Thread(target=work, daemon=True).start()

    def refresh_session_meta(self):
        try:
            sessions = self.alpha.sessions(200)
            self.sessions_cache = {s["id"]: s for s in sessions}
            for sid in self.watched:
                s = self.sessions_cache.get(sid)
                if s:
                    row = self.store.conn.execute(
                        "SELECT avatar FROM sessions WHERE id=?", (sid,)).fetchone()
                    self.store.upsert_session(s, row[0] if row else None)
        except Exception as e:
            log(f"刷新会话失败: {type(e).__name__}: {e}")

    def write_state(self):
        rows = []
        for sid in self.watched:
            sc = self.store.score(sid)
            meta = self.store.conn.execute(
                "SELECT name,avatar,unread,kind FROM sessions WHERE id=?", (sid,)).fetchone()
            if not sc or not meta:
                continue
            rows.append({"id": sid, "name": meta[0], "avatar": meta[1], "unread": meta[2],
                         "kind": meta[3], "score": sc["score"], "delta": sc["delta"],
                         "reason": sc["reason"]})
        rows.sort(key=lambda r: r["score"], reverse=True)
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"updated_at": time.time(), "chats": rows},
                                  ensure_ascii=False), encoding="utf-8")
        tmp.replace(STATE)
        log(f"state 更新: {len(rows)} 个选中会话")

    def on_message(self, data):
        sid = data.get("sessionId") or data.get("session_id")
        mid = data.get("rawid") or data.get("platformMessageId")
        if not sid or sid not in self.watched:
            return
        if mid and not self.store.mark_seen(mid, sid, data.get("timestamp") or 0):
            return
        self.pending_msgs[sid] = time.time()

    def debounce_loop(self):
        while True:
            try:
                now = time.time()
                ready = [s for s, t in list(self.pending_msgs.items())
                         if now - t >= DEBOUNCE_SECONDS]
                for sid in ready:
                    self.pending_msgs.pop(sid, None)
                    threading.Thread(target=self.rescore, args=(sid,), daemon=True).start()
            except Exception as e:
                log(f"debounce: {e}")
            time.sleep(1)

    def rescore(self, sid):
        if sid not in self.watched:
            return
        wait = RESCORE_MIN_INTERVAL - (time.time() - self.last_score_at.get(sid, 0))
        if wait > 0:
            time.sleep(wait)
        self.last_score_at[sid] = time.time()
        try:
            sess = self.sessions_cache.get(sid) or next(
                (s for s in self.alpha.sessions(200) if s["id"] == sid), None)
            if not sess:
                return
            msgs = self.recent_messages(sid, UPDATE_MESSAGES)
            transcript = build_transcript(msgs, self.transcript_of)
            if len(transcript) < 10:
                return
            prev = self.store.score(sid)
            user = (f"上次分数：{prev['score']}\n" if prev else "上次分数：无\n") + \
                   "对话记录：\n" + transcript
            obj = deepseek_chat(self.ds_key, SYSTEM_PROMPT, user)
            parsed = parse_score(obj, prev["score"] if prev else None)
            if parsed:
                score, delta, reason = parsed
                self.store.set_score(sid, score, delta, reason)
                self.write_state()
        except Exception as e:
            log(f"增量打分失败 {sid[:8]}: {type(e).__name__}: {e}")

    def refresh_loop(self):
        while True:
            time.sleep(60)
            try:
                self.refresh_session_meta()
                self.write_state()
            except Exception as e:
                log(f"refresh: {e}")

    # ---------- HTTP ----------
    def evidence_payload(self, sid):
        """返回最近一次打分实际使用的消息（用于核对）。"""
        sc = self.store.score(sid) or {}
        msgs = self.recent_messages(sid, INIT_MESSAGES)
        out = []
        for m in msgs:
            kind = m.get("kind")
            mid = m.get("platform_message_id") or m.get("id") or ""
            text = ""
            if kind == "text":
                text = m.get("text") or ""
            elif kind == "voice":
                cached = self.store.transcript(mid)
                text = "[语音] " + (cached if cached is not None else "（未转写）")
            else:
                text = f"[{kind}]"
            out.append({
                "time": m.get("sent_at_unix_ms") or 0,
                "role": "我" if str(m.get("direction")) == "2" else "对方",
                "kind": kind,
                "text": text,
                "msg_id": mid,
            })
        return {"session_id": sid, "score": sc, "messages": out}

    def start_http(self):
        daemon = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype="application/json; charset=utf-8"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _json(self, obj, code=200):
                self._send(code, json.dumps(obj, ensure_ascii=False).encode())

            def do_GET(self):
                try:
                    if self.path in ("/", "/index.html"):
                        self._send(200, DASHBOARD.read_bytes(), "text/html; charset=utf-8")
                    elif self.path == "/api/sessions":
                        self._json(self.sessions_payload())
                    elif self.path == "/api/state":
                        self._json(self.state_payload())
                    elif self.path.startswith("/api/evidence/"):
                        self._json(daemon.evidence_payload(
                            urllib.parse.unquote(self.path[len("/api/evidence/"):])))
                    elif self.path.startswith("/api/avatar/"):
                        self.avatar(urllib.parse.unquote(self.path[len("/api/avatar/"):]))
                    else:
                        self._json({"error": "not found"}, 404)
                except Exception as e:
                    self._json({"error": f"{type(e).__name__}: {e}"}, 500)

            def do_POST(self):
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(n) or b"{}")
                    if self.path == "/api/watch":
                        ids = [i for i in (body.get("ids") or []) if isinstance(i, str)]
                        daemon.watched = set(ids)
                        daemon.save_watched()
                        daemon.ensure_scored(daemon.watched)
                        daemon.write_state()
                        self._json({"ok": True, "watched": len(daemon.watched)})
                    elif self.path == "/api/rescore":
                        ids = [body["id"]] if body.get("id") else list(daemon.watched)
                        daemon.ensure_scored(set(ids))
                        self._json({"ok": True, "rescoring": len(ids)})
                    else:
                        self._json({"error": "not found"}, 404)
                except Exception as e:
                    self._json({"error": f"{type(e).__name__}: {e}"}, 500)

            def sessions_payload(self):
                sessions = daemon.alpha.sessions(200)
                known = daemon.store.known_sessions()
                scores = {r[0]: {"score": r[1], "delta": r[2], "reason": r[3]}
                          for r in daemon.store.conn.execute(
                              "SELECT session_id,score,delta,reason FROM scores")}
                out = []
                for s in sessions:
                    if s.get("kind") not in (KIND_PRIVATE, KIND_GROUP):
                        continue
                    sid = s["id"]
                    sc = scores.get(sid)
                    out.append({
                        "id": sid,
                        "name": s.get("display_name") or sid,
                        "kind": s.get("kind"),
                        "unread": s.get("unread_count") or 0,
                        "last_ts": s.get("last_activity_unix_ms") or 0,
                        "watched": sid in daemon.watched,
                        "scored": bool(sc),
                        "score": sc["score"] if sc else None,
                        "delta": sc["delta"] if sc else 0,
                        "reason": sc["reason"] if sc else "",
                        "has_avatar": bool(s.get("avatar_uri")),
                        "pending": sid in daemon.pending_score,
                    })
                out.sort(key=lambda x: (x["watched"], x["unread"], x["last_ts"]), reverse=True)
                return {"sessions": out, "count": len(out), "watched": len(daemon.watched)}

            def state_payload(self):
                try:
                    st = json.loads(STATE.read_text(encoding="utf-8"))
                except Exception:
                    st = {"chats": []}
                st["pending"] = sorted(daemon.pending_score)
                return st

            def avatar(self, sid):
                uri = None
                s = daemon.sessions_cache.get(sid)
                if s:
                    uri = s.get("avatar_uri")
                if not uri:
                    for s in daemon.alpha.sessions(200):
                        if s["id"] == sid:
                            uri = s.get("avatar_uri")
                            daemon.sessions_cache[sid] = s
                            break
                if not uri:
                    self._send(404, b"", "image/png")
                    return
                p = daemon.avatar_path(sid, uri)
                if not p:
                    self._send(404, b"", "image/png")
                    return
                data = pathlib.Path(p).read_bytes()
                mime = "image/png"
                if data[:3] == b"\xff\xd8\xff":
                    mime = "image/jpeg"
                elif data[:4] == b"GIF8":
                    mime = "image/gif"
                self._send(200, data, mime)

        httpd = ThreadingHTTPServer(("127.0.0.1", HTTP_PORT), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        log(f"仪表盘: http://127.0.0.1:{HTTP_PORT}")

    def run(self):
        self.self_name = self.alpha.resolve_account()
        self.refresh_session_meta()
        log(f"账号就绪 self_name_len={len(self.self_name)} 会话缓存={len(self.sessions_cache)}")
        self.start_http()
        self.write_state()
        if not self.watched:
            log("尚未勾选会话 → 请在仪表盘选择")
        else:
            self.ensure_scored(self.watched)
        threading.Thread(target=self.debounce_loop, daemon=True).start()
        threading.Thread(target=self.refresh_loop, daemon=True).start()

        backoff = 5
        while True:
            try:
                log("SSE 订阅开始")
                for event, data in self.alpha.push_stream(self.alpha.account_id, threading.Event()):
                    backoff = 5
                    if event == "message.new":
                        self.on_message(data)
            except Exception as e:
                log(f"SSE 断开: {type(e).__name__}: {e}，{backoff}s 后重连")
                time.sleep(backoff)
                backoff = min(60, backoff * 2)
            else:
                time.sleep(1)


if __name__ == "__main__":
    try:
        Daemon().run()
    except KeyboardInterrupt:
        pass
    except Exception:
        log("fatal:\n" + traceback.format_exc())
        sys.exit(1)
