#!/usr/bin/env python3
"""
wvdaemon —— 微信好感度菜单栏的数据与打分后台。

数据源：Alpha 本机 HTTP API（127.0.0.1:5031，Bearer token）
打分：DeepSeek（OpenAI 兼容）按会话增量打分
输出：state.json（菜单栏读）+ SQLite（历史/证据）+ avatars/（头像缓存）

隐私：聊天文本只在内存中用于构造打分请求；不写日志、不打印。
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
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent
STATE = ROOT / "state.json"
AVATARS = ROOT / "avatars"
DB_PATH = ROOT / "wvdaemon.sqlite3"
LOG_PATH = ROOT / "daemon.log"
SECRETS = pathlib.Path.home() / ".config/wvbar/secrets.env"

ALPHA_BASE = "http://127.0.0.1:5031"
DEEPSEEK_BASE = "https://api.deepseek.com"
MODEL = "deepseek-flash"

MAX_CHATS = 12          # 菜单栏最多展示几个会话
INIT_MESSAGES = 30      # 初始打分取多少条
UPDATE_MESSAGES = 20    # 新消息时取多少条
DEBOUNCE_SECONDS = 3
RESCORE_MIN_INTERVAL = 20  # 同一会话两次打分最小间隔
KIND_PRIVATE = 1
KIND_GROUP = 2


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
    def __init__(self, token: str):
        self.token = token
        self.account_id = None

    def _open(self, path: str, timeout: int = 40):
        req = urllib.request.Request(
            ALPHA_BASE + path, headers={"Authorization": f"Bearer {self.token}"}
        )
        return urllib.request.urlopen(req, timeout=timeout)

    def get(self, path: str, timeout: int = 40):
        with self._open(path, timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def resolve_account(self):
        acc = self.get("/api/v1/accounts")
        self.account_id = acc.get("selected_account_id") or acc.get("selectedAccountId")
        if not self.account_id:
            items = acc.get("accounts") or []
            if items:
                self.account_id = items[0].get("id")
        return self.account_id

    def sessions(self, limit: int = 200):
        s = self.get(f"/api/v1/sessions?limit={limit}")
        return s.get("items") or []

    def messages(self, session_id: str, limit: int = 30):
        sid = urllib.parse.quote(session_id, safe="")
        m = self.get(f"/api/v1/sessions/{sid}/messages?limit={limit}")
        return m.get("messages") or []

    def media(self, uri: str) -> bytes:
        if uri.startswith("http://") or uri.startswith("https://"):
            req = urllib.request.Request(uri)
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        ident = urllib.parse.quote(uri, safe="")
        with self._open(f"/api/v1/media/{ident}", timeout=30) as r:
            return r.read()

    def push_stream(self, account_id: str, stop: threading.Event):
        url = f"{ALPHA_BASE}/api/v1/push/messages?accountId={urllib.parse.quote(account_id)}"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "text/event-stream",
            },
        )
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
                    payload = line[5:].strip()
                    try:
                        data = json.loads(payload)
                    except Exception:
                        continue
                    yield event, data


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
        self.conn.commit()

    def score(self, sid):
        row = self.conn.execute(
            "SELECT score, delta, reason FROM scores WHERE session_id=?", (sid,)
        ).fetchone()
        return {"score": row[0], "delta": row[1], "reason": row[2]} if row else None

    def set_score(self, sid, score, delta, reason):
        self.conn.execute(
            """INSERT INTO scores(session_id,score,delta,reason,updated_at) VALUES(?,?,?,?,?)
               ON CONFLICT(session_id) DO UPDATE SET
                 score=excluded.score, delta=excluded.delta,
                 reason=excluded.reason, updated_at=excluded.updated_at""",
            (sid, score, delta, reason, int(time.time())),
        )
        self.conn.commit()

    def mark_seen(self, msg_id, sid, ts):
        try:
            self.conn.execute("INSERT OR IGNORE INTO seen(msg_id,session_id,ts) VALUES(?,?,?)",
                              (msg_id, sid, int(ts)))
            self.conn.commit()
            return True
        except Exception:
            return False

    def upsert_session(self, s, avatar_path=None):
        self.conn.execute(
            """INSERT INTO sessions(id,name,kind,avatar,unread,last_ts,updated_at)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 name=excluded.name, kind=excluded.kind,
                 avatar=COALESCE(excluded.avatar, sessions.avatar),
                 unread=excluded.unread, last_ts=excluded.last_ts,
                 updated_at=excluded.updated_at""",
            (s["id"], s.get("display_name") or s["id"], s.get("kind"),
             avatar_path, s.get("unread_count") or 0,
             s.get("last_activity_unix_ms") or 0, int(time.time())),
        )
        self.conn.commit()


def deepseek_chat(api_key: str, system: str, user: str) -> dict:
    body = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
        "max_tokens": 2000,
        "response_format": {"type": "json_object"},
    }).encode("utf-8")
    req = urllib.request.Request(
        DEEPSEEK_BASE + "/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read().decode("utf-8"))
    choice = data["choices"][0]
    content = choice["message"].get("content") or ""
    finish = choice.get("finish_reason")
    if not content.strip():
        # 思考型模型可能把预算耗尽 → 用更大预算重试一次
        raise ValueError(f"empty content (finish={finish}, usage={data.get('usage')})")
    m = re.search(r"\{.*\}", content, re.S)
    if not m:
        raise ValueError(f"no json in response (finish={finish})")
    return json.loads(m.group(0))


SYSTEM_PROMPT = (
    "你是聊天关系分析器。根据对话记录评估「对方（或群聊整体）对用户的当前好感度」。"
    "分数范围 0-100：100=非常亲近、主动、热情、愿意分享；"
    "70=明显友好、有互动意愿；50=普通礼貌、日常寒暄；"
    "30=冷淡、敷衍、公事化；10=疏离、回避、不耐烦。"
    "只输出 JSON：{\"score\": 整数, \"delta\": 整数(-5..5), \"reason\": \"不超过18字的中文理由\"}。"
    "delta 表示相对上次的变化；reason 要具体（例如「对方主动分享日常」「回复变慢语气变冷」）。"
    "不要输出任何其他文字。"
)


def build_transcript(messages, self_name: str) -> str:
    lines = []
    for m in messages:
        content = m.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        sender = str(m.get("sender") or "")
        role = "我" if sender and sender == self_name else "对方"
        text = content.strip().replace("\n", " ")
        if len(text) > 120:
            text = text[:120] + "…"
        lines.append(f"{role}: {text}")
    return "\n".join(lines[-40:])


def parse_score(obj, prev):
    try:
        score = int(obj.get("score"))
    except Exception:
        return None
    score = max(0, min(100, score))
    if prev is None:
        delta = int(obj.get("delta") or 0)
    else:
        delta = score - int(prev)
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
        self.lock = threading.Lock()
        self.last_score_at = {}
        self.pending = {}  # sid -> earliest event ts

    # ---------- 初始化 ----------
    def init(self):
        self.alpha.resolve_account()
        acc = self.alpha.get("/api/v1/accounts")
        items = acc.get("accounts") or []
        if items:
            self.self_name = items[0].get("display_name") or ""
        log(f"账号: {self.alpha.account_id[:8]}… self_name_len={len(self.self_name)}")

        sessions = self.alpha.sessions(200)
        cand = [s for s in sessions if s.get("kind") in (KIND_PRIVATE, KIND_GROUP)]
        cand.sort(key=lambda s: (s.get("unread_count") or 0, s.get("last_activity_unix_ms") or 0), reverse=True)
        log(f"会话总数={len(sessions)} 候选={len(cand)}")

        done = 0
        for s in cand:
            if done >= MAX_CHATS:
                break
            msgs = self.alpha.messages(s["id"], INIT_MESSAGES)
            if not msgs:
                continue
            transcript = build_transcript(msgs, self.self_name)
            if len(transcript) < 20:
                continue
            prev = self.store.score(s["id"])
            user_prompt = (
                f"上次分数：{prev['score']}\n" if prev else "上次分数：无（这是第一次评估）\n"
            ) + "对话记录：\n" + transcript
            try:
                obj = deepseek_chat(self.ds_key, SYSTEM_PROMPT, user_prompt)
                parsed = parse_score(obj, prev["score"] if prev else None)
            except Exception as e:
                log(f"打分失败 {s['id'][:8]}…: {type(e).__name__}: {e}")
                parsed = None
            if parsed:
                score, delta, reason = parsed
                self.store.set_score(s["id"], score, delta, reason)
                done += 1
            avatar = self.fetch_avatar(s)
            self.store.upsert_session(s, avatar)
            for m in msgs:
                mid = m.get("platformMessageId")
                if mid:
                    self.store.mark_seen(mid, s["id"], m.get("timestamp") or 0)
        log(f"初始打分完成 {done} 个会话")
        self.write_state()

    def fetch_avatar(self, s) -> str:
        uri = s.get("avatar_uri")
        if not uri:
            return None
        digest = hashlib.sha1(s["id"].encode()).hexdigest()[:16]
        path = AVATARS / f"{digest}.png"
        if path.exists() and path.stat().st_size > 0:
            return str(path)
        try:
            data = self.alpha.media(uri)
            path.write_bytes(data)
            return str(path)
        except Exception as e:
            log(f"头像失败 {s['id'][:8]}…: {type(e).__name__}")
            return None

    # ---------- 状态输出 ----------
    def write_state(self):
        rows = self.store.conn.execute(
            """SELECT s.id, s.name, s.avatar, s.unread,
                      COALESCE(sc.score,50), COALESCE(sc.delta,0), COALESCE(sc.reason,'')
               FROM sessions s LEFT JOIN scores sc ON sc.session_id=s.id
               WHERE s.kind IN (?, ?) AND sc.session_id IS NOT NULL
               ORDER BY sc.score DESC LIMIT ?""",
            (KIND_PRIVATE, KIND_GROUP, MAX_CHATS),
        ).fetchall()
        chats = [{
            "id": r[0], "name": r[1], "avatar": r[2], "unread": r[3],
            "score": r[4], "delta": r[5], "reason": r[6],
        } for r in rows]
        state = {"updated_at": time.time(), "chats": chats}
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(STATE)
        log(f"state 已更新: {len(chats)} 个会话")

    # ---------- 增量 ----------
    def rescore(self, sid):
        now = time.time()
        last = self.last_score_at.get(sid, 0)
        if now - last < RESCORE_MIN_INTERVAL:
            time.sleep(max(1, RESCORE_MIN_INTERVAL - (now - last)))
        self.last_score_at[sid] = time.time()
        try:
            sess = next((s for s in self.alpha.sessions(200) if s["id"] == sid), None)
            if sess is None:
                return
            msgs = self.alpha.messages(sid, UPDATE_MESSAGES)
            transcript = build_transcript(msgs, self.self_name)
            if len(transcript) < 10:
                return
            prev = self.store.score(sid)
            user_prompt = (
                f"上次分数：{prev['score']}\n" if prev else "上次分数：无\n"
            ) + "对话记录：\n" + transcript
            obj = deepseek_chat(self.ds_key, SYSTEM_PROMPT, user_prompt)
            parsed = parse_score(obj, prev["score"] if prev else None)
            if parsed:
                score, delta, reason = parsed
                self.store.set_score(sid, score, delta, reason)
                avatar = self.fetch_avatar(sess)
                self.store.upsert_session(sess, avatar)
                self.write_state()
        except Exception as e:
            log(f"增量打分失败 {sid[:8]}…: {type(e).__name__}: {e}")

    def on_new_message(self, data):
        sid = data.get("sessionId") or data.get("session_id")
        mid = data.get("rawid") or data.get("platformMessageId")
        if not sid:
            return
        if mid and not self.store.mark_seen(mid, sid, data.get("timestamp") or 0):
            return
        self.pending[sid] = time.time()

    def flush_pending(self):
        now = time.time()
        ready = [sid for sid, ts in list(self.pending.items()) if now - ts >= DEBOUNCE_SECONDS]
        for sid in ready:
            self.pending.pop(sid, None)
            threading.Thread(target=self.rescore, args=(sid,), daemon=True).start()

    def debounce_loop(self):
        while True:
            try:
                self.flush_pending()
            except Exception as e:
                log(f"debounce 异常: {type(e).__name__}: {e}")
            time.sleep(1)

    def refresh_loop(self):
        while True:
            time.sleep(60)
            try:
                sessions = self.alpha.sessions(200)
                for s in sessions:
                    if s.get("kind") in (KIND_PRIVATE, KIND_GROUP):
                        if self.store.conn.execute(
                            "SELECT 1 FROM scores WHERE session_id=?", (s["id"],)
                        ).fetchone():
                            self.store.upsert_session(s)
                self.write_state()
            except Exception as e:
                log(f"刷新异常: {type(e).__name__}: {e}")

    def run(self):
        self.init()
        account_id = self.alpha.account_id
        stop = threading.Event()
        threading.Thread(target=self.debounce_loop, daemon=True).start()
        threading.Thread(target=self.refresh_loop, daemon=True).start()
        backoff = 5
        while True:
            try:
                log("SSE 订阅开始")
                for event, data in self.alpha.push_stream(account_id, stop):
                    backoff = 5
                    if event == "message.new":
                        self.on_new_message(data)
            except Exception as e:
                log(f"SSE 断开: {type(e).__name__}: {e}，{backoff}s 后重连")
                time.sleep(backoff)
                backoff = min(60, backoff * 2)
            else:
                time.sleep(1)


if __name__ == "__main__":
    DEVNULL = open(os.devnull, "w")
    try:
        Daemon().run()
    except KeyboardInterrupt:
        pass
    except Exception:
        log("fatal:\n" + traceback.format_exc())
        sys.exit(1)
