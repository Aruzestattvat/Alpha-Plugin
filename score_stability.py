"""Local, persistent damping of signed closeness scores; no model calls.

Only fresh attributed text/voice supplies evidence. An exponential evidence
weight is combined with an absolute-movement token bucket: at most 3 points
available at once, replenished at 6 points/day. Reversals spend the same budget
as movement in one direction. A long silence cannot bank a large jump.
"""
import copy
import math

BURST_POINTS = 3.0
POINTS_PER_DAY = 6.0
EVIDENCE_SCALE = 100.0
MAX_ALPHA = 0.15
DAY = 86400.0


def eligible(rows, now):
    return [r for r in rows if r[2] in ("1", "2") and r[3] in ("text", "voice")
            and (r[4] or "").strip() and 0 < (r[1] or 0) <= now*1000]


def boundary(rows):
    ts = max((r[1] for r in rows), default=0)
    return {"ts": ts, "ids": sorted(str(r[0]) for r in rows if r[1] == ts)}


def initial(score, rows, now):
    return dict(level=float(score), raw_score=int(score), budget=BURST_POINTS,
                refilled_at=now, boundary=boundary(eligible(rows, now)),
                status="baseline", new_messages=0, alpha=0.0)


def update(saved, raw_score, rows, now):
    state = copy.deepcopy(saved)
    valid = eligible(rows, now)
    mark = state["boundary"]
    ids = set(mark["ids"])
    fresh = [r for r in valid if r[1] > mark["ts"]
             or (r[1] == mark["ts"] and str(r[0]) not in ids)]
    state.update(raw_score=int(raw_score), new_messages=len(fresh), alpha=0.0,
                 status="unchanged")
    if not fresh:
        return state
    next_mark = boundary(fresh)
    if next_mark["ts"] == mark["ts"]:
        next_mark["ids"] = sorted(set(next_mark["ids"]) | ids)
    state["boundary"] = next_mark
    elapsed = max(0, now-state["refilled_at"])
    available = min(BURST_POINTS, state["budget"] + elapsed*POINTS_PER_DAY/DAY)
    alpha = min(MAX_ALPHA, -math.expm1(-len(fresh)/EVIDENCE_SCALE))
    requested = alpha*(raw_score-state["level"])
    movement = max(-available, min(available, requested))
    state.update(level=max(-100.0, min(100.0, state["level"]+movement)),
                 budget=max(0.0, available-abs(movement)),
                 refilled_at=max(now, state["refilled_at"]), alpha=alpha,
                 status="limited" if abs(requested)>available else "smoothed")
    return state


def display(state):
    # Consistent half rounding avoids a 3-point float movement looking like 4
    # when Python's round-to-even crosses two opposite .5 boundaries.
    return int(math.floor(state["level"]+0.5))


def public(state):
    return {"raw_score": state["raw_score"], "status": state["status"],
            "new_messages": state["new_messages"],
            "message": "主分数为平滑后的长期亲近度；近期互动与心动指标可更快变化。"}
