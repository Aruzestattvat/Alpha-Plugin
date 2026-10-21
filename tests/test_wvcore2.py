import concurrent.futures
import io
import json
import pathlib
import sqlite3
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
import wave
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import wvcore2 as w


def chunk():
    return dict(topics=['日常'], tone='友好', positive=['主动'], negative=[], notable=[], affinity=70)


def score(value=70):
    return dict(score=value, reason='交流友好', summary='双方互动稳定',
                dimensions=dict(warmth=70, initiative=65, intimacy=60, responsiveness=75),
                trajectory=[dict(period='2026-10', score=value)])


def messages(n, sid='a'):
    return [dict(id=f'{sid}-{i}', sent_at_unix_ms=1700000000000+i*1000,
                 direction='1' if i%2 else '2', kind='text', text=f'{sid} 内容 {i}') for i in range(n)]


class Isolated(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        for name, value in dict(ROOT=root, DB_PATH=root/'db.sqlite', STATE=root/'state.json',
                                WATCHED=root/'watched.json', LOG_PATH=root/'test.log',
                                AVATARS=root/'avatars', ASR_MODEL_DIR=root/'missing', HTTP_PORT=0,
                                RELATION_STRICT=False, SCORE_GAIN=1.0, RECENT_TREND_ENABLED=False,
                                MODEL_CHUNK='deepseek-flash', MODEL_FINAL='deepseek-v4-pro', MODEL_INCREMENTAL='deepseek-v4-pro',
                                DEBOUNCE_SECONDS=0).items():
            p = patch.object(w, name, value); p.start(); self.addCleanup(p.stop)
        p = patch.object(w, 'load_secrets', return_value={'ALPHA_TOKEN':'test', 'DEEPSEEK_API_KEY':'test'})
        p.start(); self.addCleanup(p.stop)
        p = patch.object(w, 'log'); p.start(); self.addCleanup(p.stop)

    def daemon(self):
        d = w.Daemon()
        self.addCleanup(d.store.close)
        self.addCleanup(lambda: d.pool.shutdown(wait=True))
        d.alpha.sessions = lambda limit=200: [dict(id=sid, display_name=sid, kind=1) for sid in d.watched_ids()]
        d.transcribe_voices = lambda sid, **kwargs: 0
        d.sync_recent = lambda sid: set()
        d.fetch_avatar = lambda session: None
        return d

    def wait_idle(self, d):
        until = time.monotonic()+15
        while time.monotonic()<until:
            with d.lock:
                if not d.active:
                    return
            time.sleep(.005)
        self.fail('analysis queue did not drain')


class StoreTests(Isolated):
    def test_concurrent_sqlite_connections_and_atomic_state(self):
        d = self.daemon()
        d.set_watched([f's{i}' for i in range(24)])
        stop = threading.Event(); errors = []
        def reader():
            while not stop.is_set():
                try:
                    data = json.loads(w.STATE.read_text())
                    self.assertEqual(len({x['id'] for x in data['chats']}), len(data['chats']))
                except Exception as e:
                    errors.append(e)
        t = threading.Thread(target=reader); t.start()
        def worker(i):
            sid=f's{i}'
            try:
                d.store.upsert_session(dict(id=sid, display_name=sid, kind=1))
                for msg in messages(12, sid):
                    d.store.upsert_message(sid, msg)
                d.store.set_score(sid, i, 0, sid)
                d.store.set_analysis(sid, status='done')
                d.write_state()
            finally:
                d.store.close()
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
                list(pool.map(worker, range(24)))
        finally:
            stop.set(); t.join()
        self.assertEqual(errors, [])
        data=json.loads(w.STATE.read_text())
        self.assertEqual({c['id']:c['score'] for c in data['chats']}, {f's{i}':i for i in range(24)})
        self.assertEqual(d.store.conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_connections_reject_cross_thread_use(self):
        store=w.Store(); self.addCleanup(store.close)
        conn=store.conn
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            with self.assertRaises(sqlite3.ProgrammingError):
                pool.submit(conn.execute, 'SELECT 1').result()

    def test_transcript_survives_resync_and_is_session_scoped(self):
        store=w.Store(); self.addCleanup(store.close)
        msg=dict(id='same', kind='voice', direction='1')
        for sid in ('a','b'):
            store.upsert_message(sid,msg)
            store.update_message_text(sid,'same',sid+' 转写')
            store.upsert_message(sid,msg)
        self.assertEqual(store.messages('a')[0][4], '[语音] a 转写')
        self.assertEqual(store.messages('b')[0][4], '[语音] b 转写')

    def test_legacy_transcription_migration(self):
        store=w.Store()
        store.upsert_message('a', dict(id='old',kind='voice'))
        with store.conn:
            store.conn.execute("UPDATE messages SET text='[语音] 历史转写'")
        store.close()
        store=w.Store(); self.addCleanup(store.close)
        store.upsert_message('a',dict(id='old',kind='voice'))
        self.assertEqual(store.messages('a')[0][4], '[语音] 历史转写')
        self.assertEqual(store.conn.execute('SELECT text FROM voice_transcripts').fetchone()[0], '历史转写')

    def test_sse_dedup(self):
        store=w.Store(); self.addCleanup(store.close)
        self.assertTrue(store.mark_seen('a:1','a',0))
        self.assertFalse(store.mark_seen('a:1','a',0))
        self.assertTrue(store.mark_seen('b:1','b',0))

    def test_process_lock(self):
        handle=w.acquire_instance_lock()
        try:
            with self.assertRaises(SystemExit):
                w.acquire_instance_lock()
        finally:
            handle.close()


class LLMTests(Isolated):
    def response(self, content, finish='stop'):
        return io.BytesIO(json.dumps({'choices':[{'finish_reason':finish, 'message':{'content':content}}]}).encode())

    def test_truncated_response_retried_with_larger_budget(self):
        with patch.object(w.urllib.request, 'urlopen', side_effect=[self.response('', 'length'), self.response(json.dumps(score(0)))]) as request, patch.object(w.time,'sleep'):
            result=w.llm('key','model','system','user',max_tokens=4096,validator=w.normalize_score)
        self.assertEqual(result['score'],0)
        bodies=[json.loads(c.args[0].data) for c in request.call_args_list]
        self.assertEqual([x['max_tokens'] for x in bodies], [4096,8192])
        self.assertEqual(bodies[0]['thinking'], {'type':'disabled'})

    def test_invalid_score_retried_not_defaulted_to_50(self):
        with patch.object(w.urllib.request, 'urlopen', side_effect=[self.response('{}'),self.response(json.dumps(score()))]), patch.object(w.time,'sleep'):
            self.assertEqual(w.llm('key','m','s','u',validator=w.normalize_score)['score'],70)

    def test_empty_response_has_bounded_retries(self):
        with patch.object(w.urllib.request, 'urlopen', side_effect=lambda *a,**k:self.response('')) as request, patch.object(w.time,'sleep'):
            with self.assertRaises(ValueError):
                w.llm('key','m','s','u')
        self.assertEqual(request.call_count,3)

    def test_quota_limit_and_auth_errors_reported_without_internal_retry(self):
        for status, code in [(402,'insufficient_quota'),(429,'rate_limit'),(401,'authentication')]:
            e=urllib.error.HTTPError('http://test',status,'failure',{'Retry-After':'7'},None)
            reported=[]
            with patch.object(w.urllib.request,'urlopen',side_effect=e) as request:
                with self.assertRaises(w.ApiUnavailable) as caught:
                    w.llm('key','m','s','u',on_error=reported.append)
            self.assertEqual(caught.exception.code,code)
            self.assertEqual(reported,[caught.exception])
            self.assertEqual(request.call_count,1)
            if status==429:self.assertEqual(caught.exception.retry_after,7)

    def test_transient_server_error_retried(self):
        e=urllib.error.HTTPError('http://test',503,'busy',{},None)
        with patch.object(w.urllib.request,'urlopen',side_effect=[e,self.response('{}')]) as request, patch.object(w.time,'sleep'):
            self.assertEqual(w.llm('key','m','s','u'),{})
            self.assertEqual(request.call_count,2)

    def test_invalid_numbers_rejected_zero_preserved(self):
        self.assertEqual(w.normalize_score(score(0))['score'],0)
        for value in (None,True,101,-1,50.5,'NaN',{},'70分'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                w.normalize_score(score(value))

    def test_nested_chunk_fields_normalized(self):
        data=chunk();data['positive']=[{'证据':['主动问候',['分享生活']]}]
        result=w.normalize_chunk(data)
        self.assertTrue(all(isinstance(x,str) for x in result['positive']))
        with self.assertRaises(ValueError):w.normalize_chunk({})


class AnalysisTests(Isolated):
    def fake_llm(self,key,model,system,user,**kwargs):
        result=chunk() if model==w.MODEL_CHUNK else score()
        return kwargs.get('validator',lambda x:x)(result)

    def fill(self,d,sid,n):
        d.store.upsert_session(dict(id=sid,display_name=sid,kind=1))
        for msg in messages(n,sid):d.store.upsert_message(sid,msg)

    def test_summary_failure_keeps_chunks_then_retry_outputs_score(self):
        d=self.daemon();d.set_watched(['a']);self.fill(d,'a',301)
        d.sync_session=lambda sid:None
        def fail_final(key,model,system,user,**kwargs):
            if model==w.MODEL_FINAL:raise ValueError('empty content')
            return self.fake_llm(key,model,system,user,**kwargs)
        with patch.object(w,'llm',side_effect=fail_final):
            d.submit_analysis('a');self.wait_idle(d)
        self.assertEqual(d.store.analysis('a')['status'],'error')
        self.assertIsNone(d.store.score('a'))
        self.assertEqual(len(d.store.chunk_summaries('a')),3)
        with patch.object(w,'llm',side_effect=self.fake_llm) as llm:
            d.submit_analysis('a');self.wait_idle(d)
        self.assertEqual(llm.call_count,1)
        self.assertEqual(d.store.analysis('a')['status'],'done')
        self.assertEqual(json.loads(w.STATE.read_text())['chats'][0]['score'],70)

    def test_failed_chunk_does_not_produce_partial_score(self):
        d=self.daemon();d.set_watched(['a']);self.fill(d,'a',200);d.sync_session=lambda sid:None
        with patch.object(w,'llm',side_effect=ValueError('bad chunk')):
            d.submit_analysis('a');self.wait_idle(d)
        self.assertIsNone(d.store.score('a'))
        self.assertEqual(d.store.chunk_summaries('a'),[])

    def test_failed_recalculation_preserves_last_valid_score(self):
        d=self.daemon();d.set_watched(['a']);self.fill(d,'a',20);d.sync_session=lambda sid:None
        with patch.object(w,'llm',side_effect=self.fake_llm):
            d.submit_analysis('a');self.wait_idle(d)
        with patch.object(w,'llm',side_effect=ValueError('invalid summary')):
            d.submit_analysis('a');self.wait_idle(d)
        self.assertEqual(d.store.analysis('a')['status'],'error')
        self.assertEqual(d.store.score('a')['score'],70)
        state=json.loads(w.STATE.read_text())['chats'][0]
        self.assertEqual(state['score'],70)
        self.assertEqual(state['status'],'error')

    def test_message_edit_invalidates_chunk_cache(self):
        d=self.daemon();d.set_watched(['a']);self.fill(d,'a',200);d.sync_session=lambda sid:None
        with patch.object(w,'llm',side_effect=self.fake_llm):
            d.submit_analysis('a');self.wait_idle(d)
        msg=messages(20,'a')[10];msg['text']='修改内容'
        d.store.upsert_message('a',msg)
        with patch.object(w,'llm',side_effect=self.fake_llm) as llm:
            d.submit_analysis('a');self.wait_idle(d)
        self.assertEqual(llm.call_count,2)  # edited first chunk and final summary

    def test_incremental_appends_chunks_and_reuses_unchanged_prefix(self):
        d=self.daemon();d.set_watched(['a']);self.fill(d,'a',400);d.sync_session=lambda sid:None
        with patch.object(w,'llm',side_effect=self.fake_llm):
            d.submit_analysis('a');self.wait_idle(d)
        self.fill(d,'a',650)
        with patch.object(w,'llm',side_effect=self.fake_llm) as llm:
            d.tail_update('a');self.wait_idle(d)
        self.assertEqual(len(d.store.chunk_summaries('a')),5)
        self.assertEqual(llm.call_count,4)  # three changed/new chunks + final summary
        self.assertEqual(d.store.analysis('a')['msgs_total'],650)

    def test_legacy_valid_chunks_reused_empty_chunks_retried(self):
        d=self.daemon();d.set_watched(['a']);self.fill(d,'a',160);d.sync_session=lambda sid:None
        rows=d.store.messages('a')
        d.store.set_chunk('a',0,rows[0][1],rows[-1][1],160,json.dumps(chunk()))
        d.store.set_chunk('a',1,rows[150][1],rows[-1][1],10,'{}')
        with patch.object(w,'llm',side_effect=self.fake_llm) as llm:
            d.submit_analysis('a');self.wait_idle(d)
        self.assertEqual(llm.call_count,2)

    def test_duplicate_submissions_and_parallel_limit(self):
        d=self.daemon();d.set_watched(['a','b','c','d'])
        release=threading.Event();self.addCleanup(release.set)
        mutex=threading.Lock();running=set();maximum=[0];calls=[]
        def analyze(sid,force=False,**kwargs):
            with mutex:
                self.assertNotIn(sid,running);running.add(sid);calls.append(sid)
                maximum[0]=max(maximum[0],len(running))
            if not release.wait(5):raise RuntimeError('test timed out')
            with mutex:running.remove(sid)
        d._analyze=analyze
        def submit(sid):
            try:return d.submit_analysis(sid)
            finally:d.store.close()
        with concurrent.futures.ThreadPoolExecutor(16) as pool:
            accepted=list(pool.map(submit,['a','b','c','d']*12))
        self.assertEqual(sum(accepted),4)
        release.set();self.wait_idle(d)
        self.assertEqual(sorted(calls),['a','b','c','d'])
        self.assertLessEqual(maximum[0],2)

    def test_event_during_analysis_is_replayed_once(self):
        d=self.daemon();d.set_watched(['a'])
        entered=threading.Event();release=threading.Event();calls=[]
        self.addCleanup(release.set)
        def analyze(sid,force=False,**kwargs):
            calls.append(sid);entered.set();release.wait(5)
        d._analyze=analyze
        d.submit_analysis('a');self.assertTrue(entered.wait(2))
        for i in range(20):d.on_message(dict(sessionId='a',rawid=str(i)))
        release.set();self.wait_idle(d)
        self.assertEqual(calls,['a','a'])

    def test_two_sessions_via_http_produce_separate_scores(self):
        d=self.daemon();d.set_watched(['a','b'])
        for sid in ['a','b']:self.fill(d,sid,170)
        d.sync_session=lambda sid:None
        def model(key,model,system,user,**kwargs):
            result=chunk() if model==w.MODEL_CHUNK else score(20 if 'a 内容' in user else 90)
            if model==w.MODEL_CHUNK:result['topics']=['a 内容' if 'a 内容' in user else 'b 内容']
            return kwargs['validator'](result)
        server=d.start_http()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        base=f'http://127.0.0.1:{server.server_port}'
        with patch.object(w,'llm',side_effect=model):
            req=urllib.request.Request(base+'/api/analyze_all',data=b'{}',headers={'Content-Type':'application/json'})
            with urllib.request.urlopen(req) as r:self.assertEqual(json.load(r)['started'],2)
            self.wait_idle(d)
        with urllib.request.urlopen(base+'/api/state') as r:
            result=json.load(r)
        self.assertEqual({x['id']:x['score'] for x in result['chats']},{'a':20,'b':90})
        with urllib.request.urlopen(base+'/api/sessions') as r:
            self.assertTrue(all(x['status']=='done' for x in json.load(r)['sessions']))


class AsrAndPagingTests(Isolated):
    def test_shared_asr_decode_is_serial(self):
        asr=w.Asr();asr.enabled=True
        running=[0];maximum=[0];mutex=threading.Lock()
        class Recognizer:
            def create_stream(self):
                return types.SimpleNamespace(accept_waveform=lambda *args:None,result=types.SimpleNamespace(text='text'))
            def decode_stream(self,stream):
                with mutex:
                    running[0]+=1;maximum[0]=max(maximum[0],running[0])
                time.sleep(.003)
                with mutex:running[0]-=1
        asr.rec=Recognizer()
        buf=io.BytesIO()
        with wave.open(buf,'wb') as wav:
            wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(24000);wav.writeframes(b'\0\0'*100)
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            results=list(pool.map(asr.transcribe_wav,[buf.getvalue()]*32))
        self.assertEqual(maximum[0],1);self.assertEqual(results,['text']*32)

    def test_pagination_fallback_advances_and_failure_is_not_silent(self):
        alpha=w.Alpha('test')
        alpha.raw_messages=unittest.mock.Mock(side_effect=[ValueError('large page'),
            {'messages':[{'id':'a'}],'hasMore':True,'nextOffset':1},
            {'messages':[{'id':'b'}],'hasMore':False}])
        self.assertEqual(list(alpha.iter_all_messages('s')),[(0,{'id':'a'}),(1,{'id':'b'})])
        self.assertEqual(alpha.raw_messages.call_args_list[1].args,('s',100,0))
        alpha.raw_messages=lambda *args,**kwargs:{'messages':[{}],'hasMore':True,'nextOffset':0}
        with self.assertRaises(ValueError):list(alpha.iter_all_messages('s'))

    def test_system_messages_excluded_from_reply_statistics(self):
        rows=[('1',1000,'2','text','我',None),('2',2000,'0','text','系统',None),('3',11000,'1','text','回复',None)]
        result=w.stats_of(rows)
        self.assertEqual(result['peer'],1);self.assertEqual(result['reply_median_s'],10)



class RelationshipTests(Isolated):
    def relation(self, kind='friend', confidence=85):
        return dict(type=kind, confidence=confidence, reason='持续分享日常并互相支持',
                    evidence=['片段1：双方分享生活并互相帮忙'])

    def test_taxonomy_and_low_confidence(self):
        for kind, (label, symbol) in w.RELATION_TYPES.items():
            result=w.normalize_relationship(self.relation(kind))
            self.assertEqual(result['label'],label)
            self.assertEqual(result['symbol'],symbol)
        result=w.normalize_relationship(self.relation('partner',50))
        self.assertEqual(result['type'],'unknown')
        self.assertEqual(result['label'],'暂不确定')

    def test_bad_relationship_does_not_discard_valid_score(self):
        for relationship in (None,{},dict(type=[]),dict(type='partner',confidence=99),self.relation('invalid')):
            result=w.normalize_score(dict(score(0),relationship=relationship))
            self.assertEqual(result['score'],0)
            self.assertIsNone(result['relationship'])

    def test_no_evidence_or_bad_confidence_is_rejected(self):
        for override in (dict(evidence=[]),dict(evidence=[{}]),dict(confidence=True),dict(confidence=101),dict(reason='')):
            with self.assertRaises(ValueError):
                w.normalize_relationship(dict(self.relation(),**override))

    def test_relationship_update_preserves_existing_score_and_chunks(self):
        d=self.daemon();d.set_watched(['a'])
        d.store.upsert_session(dict(id='a',display_name='a',kind=1))
        for msg in messages(20):d.store.upsert_message('a',msg)
        d.store.set_score('a',86,3,'已有结果',dict(warmth=80),summary='已有总结')
        d.store.set_chunk('a',0,1,20,20,json.dumps(chunk()))
        original_chunks=d.store.chunk_summaries('a')
        def classify(*args,**kwargs):return kwargs['validator'](self.relation())
        with patch.object(w,'llm',side_effect=classify) as model, patch.object(d,'sync_session',side_effect=AssertionError('must not sync')):
            d.submit_analysis('a',relationship_only=True);self.wait_idle(d)
        self.assertEqual(model.call_count,1)
        result=d.store.score('a')
        self.assertEqual((result['score'],result['delta'],result['summary']),(86,0,'已有总结'))
        self.assertEqual(result['relationship']['type'],'friend')
        self.assertEqual(d.store.chunk_summaries('a'),original_chunks)
        self.assertEqual(json.loads(w.STATE.read_text())['chats'][0]['relationship']['type'],'friend')

    def test_group_does_not_infer_individual_relationship(self):
        d=self.daemon();d.set_watched(['g'])
        d.store.upsert_session(dict(id='g',kind=2))
        d.store.set_score('g',70,0,'群氛围')
        with patch.object(w,'llm',side_effect=AssertionError('no LLM required')):
            d.submit_analysis('g',relationship_only=True);self.wait_idle(d)
        self.assertEqual(d.store.score('g')['relationship']['type'],'group')

    def test_full_analysis_persists_relation_independent_of_score(self):
        d=self.daemon();d.set_watched(['a'])
        d.store.upsert_session(dict(id='a',kind=1))
        for msg in messages(10):d.store.upsert_message('a',msg)
        d.sync_session=lambda sid:None
        def model(key,model,system,user,**kwargs):
            obj=chunk() if model==w.MODEL_CHUNK else dict(score(99),relationship=self.relation('friend'))
            return kwargs['validator'](obj)
        with patch.object(w,'llm',side_effect=model):
            d.submit_analysis('a');self.wait_idle(d)
        result=d.store.score('a')
        self.assertEqual(result['score'],99)
        self.assertEqual(result['relationship']['type'],'friend')

    def test_relationship_api_and_sessions_payload(self):
        d=self.daemon();d.set_watched(['a'])
        d.store.upsert_session(dict(id='a',kind=1))
        for msg in messages(10):d.store.upsert_message('a',msg)
        d.store.set_score('a',86,0,'test')
        server=d.start_http();self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        base=f'http://127.0.0.1:{server.server_port}'
        with patch.object(w,'llm',side_effect=lambda *a,**kw:kw['validator'](self.relation())):
            request=urllib.request.Request(base+'/api/relationship',data=b'{"id":"a"}',headers={'Content-Type':'application/json'})
            with urllib.request.urlopen(request) as r:self.assertEqual(json.load(r)['status'],'queued')
            self.wait_idle(d)
        with urllib.request.urlopen(base+'/api/sessions') as r:
            self.assertEqual(json.load(r)['sessions'][0]['relationship']['type'],'friend')
        with urllib.request.urlopen(base+'/api/evidence/a') as r:
            self.assertEqual(json.load(r)['score']['relationship']['confidence'],85)


class RealtimeTests(Isolated):
    def model(self, key, model, system, user, **kwargs):
        obj=chunk() if model==w.MODEL_CHUNK else score(81)
        return kwargs['validator'](obj)

    def prepare(self, n=400):
        d=self.daemon();d.set_watched(['a'])
        d.store.upsert_session(dict(id='a',kind=1))
        source=messages(n)
        for seq,msg in enumerate(source):d.store.upsert_message('a',msg,seq)
        d.sync_session=lambda sid:None
        d.sync_recent=types.MethodType(w.Daemon.sync_recent,d)
        def raw(sid,limit,offset,media=False):
            batch=source[offset:offset+limit]
            return dict(messages=batch,hasMore=offset+len(batch)<len(source),nextOffset=offset+len(batch))
        d.alpha.raw_messages=unittest.mock.Mock(side_effect=raw)
        return d,source

    def test_sse_multiline_and_connection_callback(self):
        stream=b': heartbeat\r\nevent: message.new\r\ndata: {"sessionId":"a",\r\ndata: "rawid":"a-1"}\r\n\r\n'
        opened=[]
        with patch.object(w.urllib.request,'urlopen',return_value=io.BytesIO(stream)):
            events=list(w.Alpha('test').push_stream('account',threading.Event(),lambda:opened.append(True)))
        self.assertEqual(opened,[True])
        self.assertEqual(events,[('message.new',{'sessionId':'a','rawid':'a-1'})])

    def test_tail_sync_only_reads_recent_page_and_new_voice(self):
        d,source=self.prepare(1200)
        source[0]['kind']='voice'
        d.store.upsert_message('a',source[0],0)
        source.extend(messages(1202)[1200:]);source[-1]['kind']='voice'
        self.assertEqual(d.sync_recent('a'),{'a-1201'})
        self.assertEqual(d.alpha.raw_messages.call_count,1)
        self.assertGreater(d.alpha.raw_messages.call_args.args[2],1000)
        self.assertEqual(len(d.store.messages('a')),1202)

    def test_changed_offset_falls_back_to_full_sync(self):
        d,source=self.prepare(400)
        source.pop(0)
        with patch.object(d,'sync_session') as full:
            self.assertIsNone(d.sync_recent('a'))
        full.assert_called_once_with('a')

    def test_notification_updates_score_and_duplicate_costs_nothing(self):
        d,source=self.prepare()
        with patch.object(w,'llm',side_effect=self.model):
            d.submit_analysis('a');self.wait_idle(d)
        source.append(messages(401)[-1])
        event=dict(sessionId='a',rawid='a-400',timestamp=source[-1]['sent_at_unix_ms']//1000+30)
        with patch.object(w,'llm',side_effect=self.model) as model, patch.object(d,'transcribe_voices',return_value=0) as asr:
            d.on_message(event);self.wait_idle(d)
            self.assertEqual(model.call_count,2)  # changed last chunk, then final score
            self.assertEqual(asr.call_args.kwargs['only_ids'],{'a-400'})
            self.assertEqual(d.store.analysis('a')['msgs_total'],401)
            self.assertEqual(d.expected_ids['a'],set())
            d.on_message(event);self.wait_idle(d)
            d.submit_analysis('a',incremental=True);self.wait_idle(d)
            self.assertEqual(model.call_count,2)
        state=json.loads(w.STATE.read_text())
        self.assertIsNotNone(state['realtime']['last_event_at'])
        self.assertIsNotNone(state['realtime']['last_analysis_at'])
        self.assertEqual(state['chats'][0]['score'],81)

    def test_index_lag_waits_without_scoring_then_automatically_retries(self):
        d,source=self.prepare()
        with patch.object(w,'llm',side_effect=self.model) as model:
            d.on_message(dict(sessionId='a',rawid='a-400'))
            self.wait_idle(d)
            self.assertEqual(d.store.analysis('a')['status'],'waiting')
            model.assert_not_called()
            source.append(messages(401)[-1])
            d.sync_retry['a']=0;d.retry_ready();self.wait_idle(d)
        self.assertEqual(d.store.analysis('a')['status'],'done')
        self.assertEqual(d.store.analysis('a')['msgs_total'],401)
        self.assertEqual(d.expected_ids['a'],set())

    def test_final_quota_failure_keeps_old_score_and_resumes_final_step(self):
        d,source=self.prepare()
        with patch.object(w,'llm',side_effect=self.model):
            d.submit_analysis('a');self.wait_idle(d)
        old_hash=d.store.score('a')['source_hash']
        source.append(messages(401)[-1])
        def fail_final(key,model,system,user,**kwargs):
            if model==w.MODEL_FINAL:
                issue=w.ApiUnavailable('insufficient_quota','余额不足')
                kwargs['on_error'](issue)
                # It is visible before the worker unwinds.
                self.assertEqual(json.loads(w.STATE.read_text())['api_issue']['code'],'insufficient_quota')
                raise issue
            return self.model(key,model,system,user,**kwargs)
        with patch.object(w,'llm',side_effect=fail_final):
            d.on_message(dict(sessionId='a',rawid='a-400'));self.wait_idle(d)
        self.assertEqual(d.store.analysis('a')['status'],'blocked')
        self.assertEqual(d.store.score('a')['source_hash'],old_hash)
        self.assertEqual(d.store.score('a')['score'],81)
        with patch.object(w.urllib.request,'urlopen',return_value=io.BytesIO(b'{"is_available":true}')), patch.object(w,'llm',side_effect=self.model) as model:
            self.assertTrue(d.check_balance());self.wait_idle(d)
        self.assertEqual(model.call_count,1)
        self.assertEqual(model.call_args.args[1],w.MODEL_FINAL)
        self.assertNotEqual(d.store.score('a')['source_hash'],old_hash)
        self.assertEqual(d.store.analysis('a')['status'],'done')
        self.assertFalse(d.deferred)

    def test_quota_gate_remembers_messages_and_recovers_after_balance_check(self):
        d,source=self.prepare(10)
        with patch.object(w.urllib.request,'urlopen',return_value=io.BytesIO(b'{"is_available":false}')):
            self.assertFalse(d.check_balance())
        with patch.object(w,'llm',side_effect=self.model) as model:
            for mid in ('a-10','a-11'):d.on_message(dict(sessionId='a',rawid=mid))
            self.assertFalse(d.active);model.assert_not_called()
            self.assertEqual(d.expected_ids['a'],{'a-10','a-11'})
            self.assertEqual(set(d.deferred),{'a'})
            source.extend(messages(12)[10:])
            with patch.object(w.urllib.request,'urlopen',return_value=io.BytesIO(b'{"is_available":true}')):
                self.assertTrue(d.check_balance())
            self.wait_idle(d)
        self.assertIsNone(d.api_issue)
        self.assertEqual(d.store.analysis('a')['msgs_total'],12)

    def test_rate_limit_cooldown_blocks_then_resumes(self):
        d,source=self.prepare(10)
        d.report_api_error(w.ApiUnavailable('rate_limit','限流',7))
        d.on_message(dict(sessionId='a',rawid='a-9'))
        d.retry_ready();self.assertFalse(d.active)
        with patch.object(w,'llm',side_effect=self.model):
            d.api_issue['retry_at']=0
            d.retry_ready();self.wait_idle(d)
        self.assertIsNone(d.api_issue)
        self.assertEqual(d.store.analysis('a')['status'],'done')

    def test_balance_network_failure_is_not_quota_failure(self):
        d=self.daemon()
        with patch.object(w.urllib.request,'urlopen',side_effect=urllib.error.URLError('offline')):
            self.assertIsNone(d.check_balance())
        self.assertIsNone(d.api_issue)

    def test_unwatched_events_do_not_trigger_work(self):
        d=self.daemon();d.set_watched(['a'])
        with patch.object(d,'submit_analysis') as submit:
            d.on_message(dict(sessionId='b',rawid='b-1'))
            d.on_message(None)
        submit.assert_not_called()


if __name__ == '__main__':
    unittest.main()
