import pathlib
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
from relationship_time import temporal_features, DAY_MS
from test_wvcore2 import Isolated, messages
import wvcore2 as w

NOW=1800000000
END=NOW*1000

def row(mid, days, direction='1', offset=0):
    return (mid, END-int(days*DAY_MS)+offset, direction, 'text', '有效内容', None, None)


class TemporalTests(unittest.TestCase):
    def test_real_time_windows_and_old_history_does_not_count_as_current(self):
        rows=[row('today',.1),row('recent',3,'2'),row('previous',10),row('month',20),row('quarter',60),row('old',120)]
        t=temporal_features(rows,NOW)
        self.assertEqual(t['windows']['7d']['messages'],2)
        self.assertEqual(t['previous_7d']['messages'],1)
        self.assertEqual(t['windows']['30d']['messages'],4)
        self.assertEqual(t['windows']['90d']['messages'],5)
        self.assertEqual(t['days_since_last_contact'],.1)
        self.assertAlmostEqual(t['history_span_days'],119.9)
        stale=temporal_features([row('old',120)],NOW)
        self.assertEqual(stale['windows']['90d']['messages'],0)
        self.assertEqual(stale['days_since_last_contact'],120)

    def test_future_system_and_missing_timestamps_do_not_create_activity(self):
        rows=[row('good',2),row('system',0,'0'),row('future',-1),('missing',0,'1','text','x',None,None)]
        t=temporal_features(rows,NOW)
        self.assertEqual(t['last_contact_ms'],rows[0][1])
        self.assertEqual(t['windows']['7d']['messages'],1)

    def test_single_burst_and_sustained_days_are_distinct(self):
        burst=[row(str(i),2,'1' if i%2 else '2',i*1000) for i in range(60)]
        spread=[row(str(i),i%3+1,'1' if (i//3)%2 else '2') for i in range(60)]
        a=temporal_features(burst,NOW)['windows']['7d']
        b=temporal_features(spread,NOW)['windows']['7d']
        self.assertEqual(a['messages'],b['messages'])
        self.assertEqual(a['two_way_active_days'],1)
        self.assertEqual(b['two_way_active_days'],3)

    def test_reply_delay_excludes_overnight_gaps_and_tracks_each_side(self):
        rows=[row('a',2,'2'),row('b',2,'1',60000),row('c',2,'2',180000),row('d',1,'1')]
        result=temporal_features(rows,NOW)['windows']['7d']
        self.assertEqual(result['peer']['reply_median_seconds'],60)
        self.assertEqual(result['mine']['reply_median_seconds'],120)
        self.assertEqual(result['peer']['reply_samples'],1)
        self.assertEqual(result['mine']['conversation_starts'],1)
        self.assertEqual(result['peer']['conversation_starts'],1)

    def test_window_boundary_is_not_a_new_conversation(self):
        rows=[row('a',7,'2',-1000),row('b',7,'1',1000)]
        result=temporal_features(rows,NOW)['windows']['7d']
        self.assertEqual(result['messages'],1)
        self.assertEqual(result['peer']['conversation_starts'],0)
        self.assertEqual(result['peer']['reply_median_seconds'],2)

    def test_empty_data_does_not_fabricate_contact_or_response_time(self):
        t=temporal_features([],NOW)
        self.assertIsNone(t['last_contact_ms'])
        self.assertIsNone(t['days_since_last_contact'])
        self.assertIsNone(t['windows']['7d']['peer']['reply_median_seconds'])


class TemporalIntegrationTests(Isolated):
    def test_evidence_statistics_advance_with_time_without_changing_saved_relationship(self):
        d=self.daemon();d.set_watched(['a'])
        for msg in messages(10):d.store.upsert_message('a',msg)
        rel=w.normalize_relationship(dict(type='friend',confidence=90,reason='朋友',evidence=['互相帮助'],time_reason='近期证据有限，旧记录不代表当前暧昧'))
        d.store.set_score('a',80,0,'test',relationship=rel)
        before=d.store.score('a')
        before.pop('trend', None)
        ts=messages(10)[-1]['sent_at_unix_ms']/1000
        with patch.object(w.time,'time',return_value=ts+86400):
            first=d.evidence_payload('a')
        with patch.object(w.time,'time',return_value=ts+40*86400):
            later=d.evidence_payload('a')
        self.assertEqual(first['temporal']['windows']['7d']['messages'],10)
        self.assertEqual(later['temporal']['windows']['30d']['messages'],0)
        later_score=dict(later['score']); later_score.pop('trend', None)
        self.assertEqual(later_score,before)
        self.assertEqual(later['score']['relationship']['time_reason'],rel['time_reason'])

    def test_model_context_distinguishes_analysis_date_from_last_message_date(self):
        d=self.daemon();d.set_watched(['a'])
        for msg in messages(10):d.store.upsert_message('a',msg)
        with patch.object(w.time,'time',return_value=NOW):
            context=d.relationship_context('a',d.store.messages('a'))
        self.assertIn('"as_of_ms": '+str(END),context)
        self.assertIn('"last_contact_ms": '+str(messages(10)[-1]['sent_at_unix_ms']),context)
        self.assertIn('"messages": 0',context)


if __name__=='__main__':unittest.main()
