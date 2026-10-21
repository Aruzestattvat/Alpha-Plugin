#!/usr/bin/env python3
"""A/B 校验：紧凑上下文（eco）能否复现完整上下文（accurate）的评分。

做法：对已有全量结果的会话，用「上次评分 + 历史总述 + 全部分片 gist + 最近消息」
构造 eco 增量 prompt，调用小模型，与数据库中 accurate 全量结果比较分数/维度。

只读数据库；会调用 DeepSeek（默认 deepseek-flash，约 2k tokens/次）。
用法：python3 ab_replay.py [session_id] [--rich]
  --rich 额外调用一次完整 v4-pro 汇总做对照（约 40k tokens，慎用）
"""
import importlib.util
import json
import os
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("wvcore2", ROOT / "wvcore2.py")
wv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wv)


def load_dotenv():
    out = {}
    p = pathlib.Path.home() / ".config/wvbar/secrets.env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def main():
    secrets = load_dotenv()
    key = secrets.get("DEEPSEEK_API_KEY") or os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        raise SystemExit("缺少 DEEPSEEK_API_KEY")
    db = sqlite3.connect(ROOT / "wvcore2.sqlite3")
    sid = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else None
    rich = "--rich" in sys.argv
    if not sid:
        row = db.execute("SELECT session_id FROM scores ORDER BY updated_at DESC LIMIT 1").fetchone()
        if not row:
            raise SystemExit("数据库里没有已评分会话")
        sid = row[0]
    meta = db.execute("SELECT name FROM sessions WHERE id=?", (sid,)).fetchone()
    rows = db.execute("SELECT msg_id,ts,direction,kind,text,voice_url,seq FROM messages "
                      "WHERE session_id=? ORDER BY ts,msg_id", (sid,)).fetchall()
    score_row = db.execute("SELECT score,dimensions,summary,reason FROM scores WHERE session_id=?",
                           (sid,)).fetchone()
    if not rows or not score_row:
        raise SystemExit("该会话没有消息或评分")
    base_score, base_dims_json, base_summary, base_reason = score_row
    base_dims = json.loads(base_dims_json) if base_dims_json else {}
    gists = db.execute("SELECT idx,COALESCE(gist,'') FROM chunks WHERE session_id=? ORDER BY idx",
                       (sid,)).fetchall()
    total = len(rows)
    stats = wv.compact_stats(wv.stats_of(rows))
    recent = wv.render_chunk(rows, max(0, total - 15), total)
    prompt = (
        "会话类型：私聊\n"
        f"上次评估：分数 {base_score}；理由：{base_reason or ''}；维度：{json.dumps(base_dims, ensure_ascii=False)}\n"
        f"历史总述：{base_summary[:600]}\n"
        "当前关系：朋友\n"
        f"统计：{json.dumps(stats, ensure_ascii=False)}\n"
        "变化片段摘要：\n" + "\n".join(f"- {g}" for _, g in gists[-4:]) + "\n"
        f"最近消息：\n{recent}"
    )
    print(f"会话: {meta[0] if meta else sid} | 消息 {total} | 分片 {len(gists)}")
    print(f"eco prompt 字符: {len(prompt)} | 估算 tokens ≈ {len(prompt)/1.7:.0f}")
    obj = wv.llm(key, wv.MODEL_INCREMENTAL, wv.INCREMENTAL_SYSTEM, prompt,
                  max_tokens=1500, validator=wv.normalize_incremental)
    print(f"eco 结果: score={obj['score']} dims={obj['dimensions']}")
    print(f"accurate 结果: score={base_score} dims={base_dims}")
    d_score = obj["score"] - base_score
    d_dims = {k: obj["dimensions"].get(k, 0) - base_dims.get(k, 0) for k in base_dims}
    print(f"偏差: score {d_score:+d} | dims {d_dims}")
    print("判定:", "✅ 在容差内（|Δscore|≤5 且各维度 |Δ|≤10）"
          if abs(d_score) <= 5 and all(abs(v) <= 10 for v in d_dims.values()) else "⚠️ 偏差偏大，需要调整")
    if rich:
        # 完整 rich prompt 对照（与生产 accurate 相同构造）
        rel_ctx = ""
        score_prev = {"score": base_score, "dimensions": base_dims, "summary": base_summary,
                      "reason": base_reason, "relationship": None}
        parts = ["会话类型：私聊", "统计特征：\n" + json.dumps(wv.stats_of(rows), ensure_ascii=False)]
        for idx, g in gists:
            parts.append(f"[片段{idx+1}] {g}")
        rich_prompt = "\n".join(parts)
        print(f"\nrich prompt（gist 版）字符: {len(rich_prompt)} | tokens ≈ {len(rich_prompt)/1.7:.0f}")
        rich_obj = wv.llm(key, wv.MODEL_FINAL, wv.FINAL_SYSTEM, rich_prompt,
                          max_tokens=8192, validator=wv.normalize_score)
        print(f"rich score={rich_obj['score']} dims={rich_obj['dimensions']}")


if __name__ == "__main__":
    main()
