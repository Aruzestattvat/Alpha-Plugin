"""Message-context trend: score change per 100 messages, independent of calendar.

Observations are taken from real scoring runs, each carrying the number of messages
processed at that time. Repeated recalculations at the same message count do not add
samples, so the trend cannot be manufactured by clicking "recalculate" repeatedly.
"""
import math
import statistics
import time

MIN_POINTS = 4
MIN_SPAN_MESSAGES = 80       # 至少覆盖 80 条消息的上下文跨度
HORIZON_MESSAGES = 100       # 预测"再聊 100 条消息"后的分数
FLAT_PER_100 = 1.5           # 每 100 条变化小于 1.5 分视作平稳（签名尺度）
STALE_SECONDS = 14 * 86400   # 仅用于提示"近期没有新消息"，不参与斜率


def context_trend(observations, now=None, lo=-100, hi=100, min_span=MIN_SPAN_MESSAGES):
    """observations: [(messages_count, ts_seconds, score)] -> trend dict.

    lo/hi 定义该指标的取值范围（亲近度 -100..100，心动 0..100）。
    """
    now = time.time() if now is None else now
    by_msgs = {}
    latest_ts = 0
    for item in observations:
        try:
            msgs, ts, score = int(item[0]), int(item[1]), int(item[2])
        except (TypeError, ValueError, IndexError):
            continue
        if msgs < 0 or not (lo <= score <= hi):
            continue
        latest_ts = max(latest_ts, ts)
        # Keep the latest observation at the same message position.
        if msgs not in by_msgs or ts >= by_msgs[msgs][0]:
            by_msgs[msgs] = (ts, score)
    points = [(m, by_msgs[m][1]) for m in sorted(by_msgs)]
    result = dict(status="insufficient", label="样本不足", symbol="·",
                  angle_degrees=None, slope_per_100_msgs=None,
                  prediction=None, points=len(points), span_messages=0,
                  description=f"至少需要 {MIN_POINTS} 个不同消息位置的评分、"
                              f"覆盖 {min_span} 条消息；重复重算不增加样本。")
    if len(points) < 3:
        return result
    span = points[-1][0] - points[0][0]
    result["span_messages"] = span
    provisional = len(points) < MIN_POINTS or span < min_span
    if span <= 0:
        return result
    if latest_ts and now - latest_ts > STALE_SECONDS and not provisional:
        result.update(status="stale", label="近期无新消息",
                      description="超过 14 天没有新的评分上下文，暂不外推；历史趋势仍保留。")
        return result
    origin = points[0][0]
    xy = [((m - origin) / 100.0, y) for m, y in points]
    slopes = [(b[1] - a[1]) / (b[0] - a[0])
              for i, a in enumerate(xy) for b in xy[i + 1:] if b[0] > a[0]]
    if not slopes:
        return result
    slope = statistics.median(slopes)
    intercept = statistics.median(y - slope * x for x, y in xy)
    residual = statistics.median(abs(y - (intercept + slope * x)) for x, y in xy)
    dispersion = statistics.median(abs(s - slope) for s in slopes)
    span_units = span / 100.0
    noise = max(1.0, 1.4826 * residual)
    if residual > 3 and abs(slope) * span_units < 2 * noise and not provisional:
        result.update(status="noisy", label="波动较大",
                      description="最近几个上下文的评分方向不一致，暂不预测。")
        return result
    flat = abs(slope) < FLAT_PER_100 or abs(slope) * span_units < 2 * noise
    direction = 0 if flat else (1 if slope > 0 else -1)
    angle = 0 if flat else round(max(-65, min(65, math.degrees(math.atan(slope / 2)))), 1)
    label = "平稳" if flat else ("升温" if direction > 0 else "降温")
    center = max(lo, min(hi, intercept + slope * (span_units + HORIZON_MESSAGES / 100.0)))
    uncertainty = max(2, 2 * noise, dispersion * (span_units + HORIZON_MESSAGES / 100.0))
    result.update(
        status="provisional" if provisional else "ready",
        label="样本偏少" if provisional else label,
        symbol="→" if flat else ("↗" if direction > 0 else "↘"),
        angle_degrees=angle, slope_per_100_msgs=round(slope, 2),
        prediction={"messages": HORIZON_MESSAGES, "score": round(center, 1),
                    "low": round(max(lo, center - uncertainty), 1),
                    "high": round(min(hi, center + uncertainty), 1)},
        description=(f"{len(points)} 个评分点、覆盖 {span} 条消息；"
                     f"稳健斜率 {slope:.2f} 分/100 条消息；"
                     f"按当前趋势再聊 {HORIZON_MESSAGES} 条的粗略外推，区间反映波动。"),
    )
    if provisional:
        result["description"] = ("样本偏少，方向仅供参考；" + result["description"])
    return result
