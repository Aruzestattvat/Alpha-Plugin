"""Descriptive robust trends from actual observations, never model-made trajectories."""
import math
import statistics
import time

DAY = 86400


def score_trend(observations, now=None):
    now = time.time() if now is None else now
    # observations: (evidence_at seconds, score). Weight days equally, not requests.
    buckets = {}
    for ts, value in observations:
        if now-30*DAY <= ts <= now and 0 <= value <= 100:
            buckets.setdefault(int(ts//DAY), []).append(value)
    points = [(day*DAY+DAY/2, statistics.median(values)) for day, values in sorted(buckets.items())]
    result = dict(status='insufficient', label='样本不足', symbol='·', angle_degrees=None,
                  slope_per_day=None, prediction=None, days=len(points),
                  points=[{'day': int(t//DAY), 'score': y} for t,y in points],
                  description='至少需要 4 个不同日期、跨度 3 天的实际评分；重复重算不增加样本。')
    if len(points)<4 or points[-1][0]-points[0][0]<3*DAY:
        return result
    # Use exact observation time to determine staleness, not the bucket midpoint.
    latest = max(ts for ts, value in observations if now-30*DAY <= ts <= now and 0 <= value <= 100)
    if now-latest>7*DAY:
        result.update(status='stale', label='近期样本不足', description='超过 7 天没有新的评分依据，暂停外推。')
        return result
    origin=points[-1][0]
    xy=[((t-origin)/DAY,y) for t,y in points]
    slopes=[(b[1]-a[1])/(b[0]-a[0]) for i,a in enumerate(xy) for b in xy[i+1:]]
    slope=statistics.median(slopes)
    intercept=statistics.median(y-slope*x for x,y in xy)
    residual=statistics.median(abs(y-(intercept+slope*x)) for x,y in xy)
    dispersion=statistics.median(abs(s-slope) for s in slopes)
    span=xy[-1][0]-xy[0][0]
    noise=max(1.0,1.4826*residual)
    if residual>3 and abs(slope)*span < 2*noise:
        result.update(status='noisy',label='波动较大',description='近期评分方向不一致，暂不预测。')
        return result
    flat=abs(slope)<0.5 or abs(slope)*span<2*noise
    direction=0 if flat else (1 if slope>0 else -1)
    angle=0 if flat else round(max(-65,min(65,math.degrees(math.atan(slope/2)))),1)
    label='平稳' if flat else ('升温' if direction>0 else '降温')
    horizon=int(min(7,span))
    center=max(0,min(100,intercept+(0 if flat else slope)*horizon))
    uncertainty=max(2,2*noise,dispersion*horizon)
    result.update(status='ready',label=label,symbol='→' if flat else ('↗' if direction>0 else '↘'),
        angle_degrees=angle,slope_per_day=round(slope,2),
        prediction={'days':horizon,'score':round(center,1),'low':round(max(0,center-uncertainty),1),
                    'high':round(min(100,center+uncertainty),1)},
        description=f'{len(points)} 个评分日期，稳健斜率 {slope:+.2f} 分/天；按当前趋势外推 {horizon} 天。区间反映波动，不是概率置信区间；不预测关系身份。')
    return result
