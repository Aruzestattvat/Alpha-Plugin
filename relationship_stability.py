"""Deterministic relationship transitions; model proposals are not committed labels."""
import copy

MIN_CONFIDENCE = 80
EVENT_CONFIDENCE = 90
MIN_NEW_MESSAGES = 10
MIN_CONFIRMATIONS = 3
MIN_OBSERVATION_SECONDS = 24 * 3600
# 从暧昧退回朋友/普通联系需要更长的冷静观察，避免来回抖动。
FLIRTING_EXIT_OBSERVATION_SECONDS = 14 * 24 * 3600


def boundary(rows):
    latest = max((r[1] or 0 for r in rows), default=0)
    return {"ts": latest, "ids": sorted(str(r[0]) for r in rows if (r[1] or 0) == latest)}


def after_boundary(rows, mark):
    ids = set(mark.get("ids", []))
    return [r for r in rows if (r[1] or 0) > mark.get("ts", 0)
            or ((r[1] or 0) == mark.get("ts", 0) and str(r[0]) not in ids)]


def annotate(relation, status, message, candidate=None):
    if not relation:
        return None
    result = copy.deepcopy(relation)
    result.pop("change_event", None)
    result.pop("_verified_event", None)
    result["stability"] = {"status": status, "message": message}
    if candidate:
        result["stability"].update(candidate_type=candidate["type"],
            candidate_label=candidate["label"], confirmations=candidate["votes"],
            required_confirmations=MIN_CONFIRMATIONS,
            evidence_span_hours=round(max(0, candidate.get("last_message_ts", 0)-candidate.get("first_message_ts", 0))/3600000, 1))
    return result


def verified_event(proposal, current_type, event_rows, fresh):
    """Only fresh, verbatim, attributed evidence can bypass the observation window."""
    reviewed_breakup = (proposal.get("_verified_event") == "end_partner"
                        and (proposal.get("change_event") or {}).get("kind") == "end_partner")
    if (proposal.get("confidence", 0) < EVENT_CONFIDENCE and not reviewed_breakup) or current_type == "family":
        return False
    event = proposal.get("change_event") or {}
    kind = event.get("kind")
    if kind == "confirm_partner":
        if proposal["type"] != "partner":
            return False
    elif kind == "end_partner":
        if current_type != "partner" or proposal["type"] not in ("friend", "acquaintance"):
            return False
    else:
        return False
    rows = {str(r[0]): r for r in event_rows if r[2] in ("1", "2") and r[4]}
    directions = set()
    cited = set()
    for item in event.get("evidence", []):
        if not isinstance(item, dict):
            return False
        r = rows.get(str(item.get("message_id", "")))
        quote = item.get("quote")
        if not r or not isinstance(quote, str) or len(quote.strip()) < 4 or quote not in r[4]:
            return False
        directions.add(r[2])
        cited.add(str(r[0]))
    if not cited.intersection(str(r[0]) for r in fresh):
        return False
    return directions == {"1", "2"} if kind == "confirm_partner" else bool(directions)


def decide(previous, proposal, saved, rows, now):
    """Return (displayed relationship, persisted observation state).

    A timestamp/id boundary consumes each message once. Edits, model retries and
    restarting the process cannot create extra confirmations. Late historical
    imports are conservatively excluded from fresh evidence.
    """
    state = copy.deepcopy(saved or {})
    if not state:
        state = {"boundary": boundary(rows), "established_boundary": boundary(rows), "candidate": None}
        initial = previous or proposal
        return annotate(initial, "stable", "已建立关系基线，后续变化需持续证据确认。"), state
    if proposal and (proposal["type"] == "group" or not previous
                     or (previous["type"] == "unknown" and proposal.get("confidence", 0) >= MIN_CONFIDENCE)):
        state.update(boundary=boundary(rows), established_boundary=boundary(rows), candidate=None)
        return annotate(proposal, "stable", "已建立关系基线，后续变化需持续证据确认。"), state
    fresh = after_boundary(rows, state["boundary"])
    if not fresh:
        return copy.deepcopy(previous), state
    state["boundary"] = boundary(rows)
    useful = [r for r in fresh if r[2] in ("1", "2") and r[3] in ("text", "voice") and (r[4] or "").strip()]
    current = previous.get("type") if previous else "unknown"
    if proposal and proposal["type"] == "group":
        state["candidate"] = None
        return annotate(proposal, "stable", "群聊类型由会话信息确定。"), state
    if proposal and verified_event(proposal, current, after_boundary(rows, state["established_boundary"]), fresh):
        state["candidate"] = None
        state["established_boundary"] = boundary(rows)
        return annotate(proposal, "event_confirmed", "新消息出现明确关系事件，已核对原文后更新。"), state
    if not proposal or proposal["type"] == "unknown" or proposal.get("confidence", 0) < MIN_CONFIDENCE:
        state["candidate"] = None
        return annotate(previous or proposal, "uncertain", "本次证据不足，保留已确认的关系。"), state
    target = proposal["type"]
    if target == current or current == "unknown":
        state["candidate"] = None
        return annotate(proposal, "stable", "新消息支持当前关系。"), state
    # Kinship does not disappear, and romantic status needs an explicit event.
    if current in ("family", "partner") or target == "partner":
        state["candidate"] = None
        return annotate(previous, "uncertain", "尚无足以改变当前身份的明确证据，保留原关系。"), state
    candidate = state.get("candidate")
    if not candidate or candidate["type"] != target:
        candidate = {"type": target, "label": proposal["label"], "votes": 0,
                     "pending_messages": 0, "first_vote_at": None}
    dated = [r[1] for r in useful if r[1] and r[1] > 0]
    if dated:
        candidate.setdefault("first_message_ts", min(dated))
        candidate["last_message_ts"] = max(candidate.get("last_message_ts", 0), max(dated))
    candidate["pending_messages"] += len(useful)
    confirmed_this_round = candidate["pending_messages"] >= MIN_NEW_MESSAGES
    if confirmed_this_round:
        candidate["votes"] += 1
        candidate["pending_messages"] = 0
        if candidate["first_vote_at"] is None:
            candidate["first_vote_at"] = now
    state["candidate"] = candidate
    required = MIN_OBSERVATION_SECONDS
    if current == "flirting" and target in ("friend", "acquaintance"):
        required = FLIRTING_EXIT_OBSERVATION_SECONDS
    if (confirmed_this_round and candidate["votes"] >= MIN_CONFIRMATIONS and candidate["first_vote_at"] is not None
            and now - candidate["first_vote_at"] >= required
            and candidate.get("last_message_ts", 0)-candidate.get("first_message_ts", 0) >= required*1000):
        state["candidate"] = None
        state["established_boundary"] = boundary(rows)
        span = "14 天" if required == FLIRTING_EXIT_OBSERVATION_SECONDS else "24 小时"
        return annotate(proposal, "confirmed", f"观察时间及新消息实际跨度均达到 {span}，经多轮独立观察后更新。"), state
    return annotate(previous, "observing", "正在观察关系变化，当前图标保持不变。", candidate), state
