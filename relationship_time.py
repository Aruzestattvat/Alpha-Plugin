"""Time features for relationship inference, derived from message timestamps."""
import statistics
import time

DAY_MS = 86400000
SESSION_GAP_MS = 4 * 3600000


def temporal_features(rows, now=None):
    now = time.time() if now is None else now
    end = int(now * 1000)
    valid = sorted((r for r in rows if r[2] in ('1', '2') and isinstance(r[1], (int, float))
                    and 0 < r[1] <= end), key=lambda r: (r[1], str(r[0])))
    starts = set()
    replies = {}
    for index, row in enumerate(valid):
        prev = valid[index-1] if index else None
        if prev is None or row[1] - prev[1] >= SESSION_GAP_MS:
            starts.add(str(row[0]))
        elif prev[2] != row[2]:
            replies[str(row[0])] = (row[1] - prev[1]) / 1000

    def window(start, stop):
        selected = [r for r in valid if start < r[1] <= stop]
        days = {}
        for r in selected:
            day = time.strftime('%Y-%m-%d', time.localtime(r[1] / 1000))
            days.setdefault(day, set()).add(r[2])
        result = {'messages': len(selected), 'active_days': len(days),
                  'two_way_active_days': sum(len(sides) == 2 for sides in days.values())}
        for direction, name in [('2', 'mine'), ('1', 'peer')]:
            own = [r for r in selected if r[2] == direction]
            delays = [replies[str(r[0])] for r in own if str(r[0]) in replies]
            result[name] = {'messages': len(own),
                           'conversation_starts': sum(str(r[0]) in starts for r in own),
                           'reply_samples': len(delays),
                           'reply_median_seconds': round(statistics.median(delays)) if delays else None}
        return result

    last = valid[-1][1] if valid else None
    return {'as_of_ms': end, 'timezone': time.tzname[time.localtime(now).tm_isdst],
            'first_contact_ms': valid[0][1] if valid else None,
            'last_contact_ms': last,
            'days_since_last_contact': round((end-last)/DAY_MS, 2) if last else None,
            'history_span_days': round((last-valid[0][1])/DAY_MS, 2) if valid else 0,
            'windows': {str(days)+'d': window(end-days*DAY_MS, end) for days in (7, 30, 90)},
            'previous_7d': window(end-14*DAY_MS, end-7*DAY_MS),
            'reply_method': '相邻异向消息、间隔小于4小时的描述性中位数；不代表在线时长、已读时间或情感。',
            'interpretation': '时间与频率仅辅助内容判断；过去亲密不等于现在暧昧，暂时沉默也不证明关系消失。'}
