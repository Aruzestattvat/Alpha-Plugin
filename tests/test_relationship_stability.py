import json
import pathlib
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import wvcore2 as w
import relationship_stability as stability
from test_wvcore2 import Isolated, messages, score, chunk


def relation(kind='friend', confidence=90, event=None):
    return w.normalize_relationship(dict(type=kind, confidence=confidence, reason='实际聊天证据',
        evidence=['双方互动的实际证据'], change_event=event))


class StabilityTests(Isolated):
    def setup_store(self, kind='friend'):
        store=w.Store();self.addCleanup(store.close)
        self.append(store,0,10)
        store.set_score('a',70,0,'初始结果',relationship=relation(kind))
        return store

    def append(self, store, start, end):
        for seq,msg in enumerate(messages(end)[start:end],start):store.upsert_message('a',msg,seq)

    def propose(self, store, proposal, now):
        with patch.object(w.time,'time',return_value=now):store.set_relationship('a',proposal)
        return store.score('a')['relationship']

    def test_repeated_analysis_and_restart_cannot_add_votes(self):
        store=self.setup_store();self.append(store,10,20)
        result=self.propose(store,relation('flirting'),100)
        self.assertEqual(result['stability']['confirmations'],1)
        for i in range(6):self.propose(store,relation('flirting'),100000+i)
        store.close();store=w.Store();self.addCleanup(store.close)
        result=self.propose(store,relation('flirting'),200000)
        self.assertEqual(result['type'],'friend')
        self.assertEqual(result['stability']['confirmations'],1)

    def test_three_windows_and_full_day_required(self):
        store=self.setup_store()
        for start,now in [(10,100),(20,200),(30,300)]:
            self.append(store,start,start+10)
            result=self.propose(store,relation('flirting'),now)
        self.assertEqual(result['type'],'friend')
        self.assertEqual(result['stability']['confirmations'],3)
        self.assertEqual(self.propose(store,relation('flirting'),90000)['type'],'friend')
        for seq,msg in enumerate(messages(50)[40:],40):
            msg['sent_at_unix_ms'] += 86400000
            store.upsert_message('a',msg,seq)
        result=self.propose(store,relation('flirting'),90000)
        self.assertEqual(result['type'],'flirting')
        self.assertEqual(result['stability']['status'],'confirmed')

    def test_elapsed_program_time_cannot_turn_a_short_burst_into_sustained_evidence(self):
        store=self.setup_store()
        for start,now in [(10,100),(20,90000),(30,180000)]:
            self.append(store,start,start+10)
            result=self.propose(store,relation('flirting'),now)
        self.assertEqual(result['type'],'friend')
        self.assertEqual(result['stability']['confirmations'],3)
        self.assertLess(result['stability']['evidence_span_hours'],1)

    def test_small_batches_accumulate_but_single_large_batch_is_one_vote(self):
        store=self.setup_store()
        for i in range(10,19):
            self.append(store,i,i+1)
            result=self.propose(store,relation('flirting'),100+i)
            self.assertEqual(result['stability']['confirmations'],0)
        self.append(store,19,100)
        result=self.propose(store,relation('flirting'),100000)
        self.assertEqual(result['stability']['confirmations'],1)
        self.assertEqual(result['type'],'friend')

    def test_conflicting_or_uncertain_proposal_breaks_confirmation_streak(self):
        for changed in [relation('friend'),relation('classmate'),relation('unknown'),relation('flirting',70),None]:
            with self.subTest(changed=changed):
                store=self.setup_store()
                self.append(store,10,20);self.propose(store,relation('flirting'),100)
                self.append(store,20,30);self.propose(store,changed,200)
                self.append(store,30,40)
                result=self.propose(store,relation('flirting'),90000)
                self.assertEqual(result['type'],'friend')
                self.assertEqual(result['stability']['confirmations'],1)
                # Use a fresh session for the next subcase.
                with store.conn:
                    for table in ['scores','messages','relationship_state']:
                        store.conn.execute('DELETE FROM '+table)

    def test_score_updates_independently_and_bad_relationship_does_not_erase_label(self):
        store=self.setup_store();self.append(store,10,20)
        store.set_score('a',25,-45,'近期回应减少',relationship=relation('acquaintance'))
        result=store.score('a')
        self.assertEqual(result['score'],67);self.assertEqual(result['relationship']['type'],'friend')
        self.assertEqual(result['score_stability']['raw_score'],25)
        self.append(store,20,30)
        store.set_score('a',26,1,'分数可正常更新',relationship=None)
        self.assertEqual(store.score('a')['relationship']['type'],'friend')

    def test_partner_and_family_do_not_downgrade_from_low_warmth(self):
        for kind in ['partner','family']:
            store=self.setup_store(kind)
            for start,now in [(10,100),(20,90000),(30,180000)]:
                self.append(store,start,start+10)
                result=self.propose(store,relation('acquaintance'),now)
                self.assertEqual(result['type'],kind)
            with store.conn:
                for table in ['scores','messages','relationship_state']:store.conn.execute('DELETE FROM '+table)

    def test_repeated_partner_proposal_requires_explicit_event(self):
        store=self.setup_store()
        for start,now in [(10,100),(20,90000),(30,180000)]:
            self.append(store,start,start+10)
            result=self.propose(store,relation('partner'),now)
        self.assertEqual(result['type'],'friend')

    def test_new_two_sided_confirmation_can_change_immediately_across_notifications(self):
        store=self.setup_store()
        a=messages(11)[-1];a['text']='我们正式成为恋人吧';a['direction']='1'
        store.upsert_message('a',a,10)
        self.propose(store,relation('friend'),100)
        b=messages(12)[-1];b['text']='我同意，我们正式交往';b['direction']='2'
        store.upsert_message('a',b,11)
        event=dict(kind='confirm_partner',evidence=[dict(message_id=a['id'],quote=a['text']),dict(message_id=b['id'],quote=b['text'])])
        result=self.propose(store,relation('partner',95,event),101)
        self.assertEqual(result['type'],'partner')
        self.assertEqual(result['stability']['status'],'event_confirmed')

    def test_fabricated_unilateral_and_historical_events_cannot_bypass(self):
        store=self.setup_store()
        a=messages(11)[-1];a['text']='我们正式成为恋人吧';a['direction']='1'
        store.upsert_message('a',a,10)
        for refs in [[dict(message_id=a['id'],quote='编造的原文确认')],
                     [dict(message_id=a['id'],quote=a['text'])],
                     [dict(message_id='a-0',quote='a 内容 0'),dict(message_id='a-1',quote='a 内容 1')]]:
            saved=json.loads(store.conn.execute('SELECT data FROM relationship_state').fetchone()[0])
            event=dict(kind='confirm_partner',evidence=refs)
            result,_=stability.decide(relation(),relation('partner',95,event),saved,store.messages('a'),100)
            self.assertEqual(result['type'],'friend')

    def test_explicit_breakup_and_old_romance_cannot_reactivate(self):
        store=self.setup_store('partner')
        msg=messages(11)[-1];msg['text']='我们分手吧，我决定结束这段恋爱关系';msg['direction']='1'
        store.upsert_message('a',msg,10)
        event=dict(kind='end_partner',evidence=[dict(message_id=msg['id'],quote=msg['text'])])
        result=self.propose(store,relation('acquaintance',95,event),100)
        self.assertEqual(result['type'],'acquaintance')
        self.append(store,11,12)
        old=dict(kind='confirm_partner',evidence=[dict(message_id='a-0',quote='a 内容 0'),dict(message_id='a-1',quote='a 内容 1')])
        self.assertEqual(self.propose(store,relation('partner',95,old),101)['type'],'acquaintance')

    def test_same_timestamp_ids_count_once_system_and_media_do_not_vote(self):
        store=self.setup_store()
        for i in range(10,20):
            msg=messages(i+1)[-1];msg['sent_at_unix_ms']=1700000009000
            msg['kind']='media' if i%2 else 'text';msg['direction']='0' if i%2==0 else '1'
            store.upsert_message('a',msg,i)
        result=self.propose(store,relation('flirting'),100)
        self.assertEqual(result['stability']['confirmations'],0)
        self.append(store,20,30)
        result=self.propose(store,relation('flirting'),200)
        self.assertEqual(result['stability']['confirmations'],1)

    def test_initial_legacy_label_is_preserved_on_migration(self):
        store=self.setup_store('friend')
        with store.conn:store.conn.execute('DELETE FROM relationship_state')
        store.close();store=w.Store();self.addCleanup(store.close)
        result=self.propose(store,relation('flirting'),100000)
        self.assertEqual(result['type'],'friend')
        self.assertIsNone(json.loads(store.conn.execute('SELECT data FROM relationship_state').fetchone()[0])['candidate'])

    def test_manual_classification_then_full_scoring_share_one_observation(self):
        d=self.daemon();d.set_watched(['a'])
        d.store.upsert_session(dict(id='a',kind=1))
        self.append(d.store,0,10)
        d.store.set_score('a',70,0,'初始',relationship=relation())
        self.append(d.store,10,20)
        with patch.object(w,'llm',return_value=relation('flirting')):
            d.submit_analysis('a',relationship_only=True);self.wait_idle(d)
        d.sync_session=lambda sid:None
        def model(key,model,system,user,**kwargs):
            obj=chunk() if model==w.MODEL_CHUNK else dict(score(95),relationship=relation('flirting'))
            return kwargs['validator'](obj)
        with patch.object(w,'llm',side_effect=model):
            d.submit_analysis('a',force=True);self.wait_idle(d)
        result=json.loads(w.STATE.read_text())['chats'][0]
        self.assertEqual(result['score'],72)
        self.assertEqual(result['score_stability']['raw_score'],95)
        self.assertEqual(result['relationship']['type'],'friend')
        self.assertEqual(result['relationship']['stability']['confirmations'],1)

    def test_state_and_score_are_atomic_on_failure(self):
        store=self.setup_store();self.append(store,10,20)
        before=store.conn.execute('SELECT data FROM relationship_state').fetchone()[0]
        store.conn.execute("CREATE TRIGGER fail_score BEFORE UPDATE ON scores BEGIN SELECT RAISE(ABORT,'test'); END")
        with self.assertRaises(w.sqlite3.IntegrityError):
            store.set_score('a',71,1,'新结果',relationship=relation('flirting'))
        self.assertEqual(store.conn.execute('SELECT data FROM relationship_state').fetchone()[0],before)
        self.assertEqual(store.score('a')['score'],70)


if __name__=='__main__':unittest.main()
