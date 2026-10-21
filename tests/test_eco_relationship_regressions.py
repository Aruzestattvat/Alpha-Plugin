import json
import time
from unittest.mock import patch

from test_wvcore2 import Isolated, w, score
from test_relationship_stability import relation


class EcoRelationshipRegressionTests(Isolated):
    def setUp(self):
        super().setUp()
        strict = patch.object(w, "RELATION_STRICT", True)
        strict.start()
        self.addCleanup(strict.stop)
        self.d = self.daemon()
        self.base = int(time.time()*1000)-5*86400000
        self.d.store.upsert_session(dict(id="a", kind=1))
        self.add("baseline", self.base, "1", "以前的交流")

    def add(self, mid, ts, speaker, text):
        self.d.store.upsert_message("a", dict(id=mid, sent_at_unix_ms=ts,
            direction=speaker, kind="text", text=text))

    def baseline(self, kind="friend"):
        self.d.store.set_score("a", 30, 0, "baseline", relationship=relation(kind))

    def review_as(self, category):
        self.d._review_relationship_evidence = lambda grounded: {
            ref["message_id"]: dict(category=category, direct=True, affirmative=True)
            for ref, row in grounded}

    def incremental(self, proposal, score_value=45):
        obj = dict(score=score_value, reason="新消息", rolling_summary="总述",
                   dimensions=dict(warmth=60, initiative=60, intimacy=60,
                                   responsiveness=60, romantic=70),
                   relationship=proposal)
        captured = []
        def call(key, model, system, prompt, **kw):
            captured.append(prompt)
            return kw["validator"](obj)
        previous = self.d.store.score("a")
        rows = self.d.store.messages("a")
        self.d.call_llm = call
        self.d._eco_incremental("a", rows, len(rows), ("a", None, 0, 1), previous, [])
        return self.d.store.score("a"), captured[0]

    def test_first_partner_and_flirting_need_grounded_evidence(self):
        for target in ("partner", "flirting"):
            held, rejected = self.d._gate_relationship("a", relation(target, 99), None, [])
            self.assertTrue(rejected)
            self.assertEqual(held["type"], "unknown")

    def test_eco_counts_once_preserves_score_and_clears_conflicting_streak(self):
        self.baseline()
        quotes = ["我对你心动，想和你约会", "我也喜欢你，想和你单独约会",
                  "这几天总想你，每次见你都心动", "我也是浪漫的喜欢，想继续约会"]
        refs = []
        for i in range(20):
            text = quotes[i//5] if i in (0, 5, 10, 15) else "今天聊聊日常"
            speaker = "1" if i < 5 or 10 <= i < 15 else "2"
            self.add(str(i), self.base+86400000+(i//10)*2*86400000+i*60000, speaker, text)
            if i in (0,5,10,15):
                refs.append(dict(message_id=str(i), quote=text))
        proposed = dict(relation("flirting", 95), romantic_evidence=refs)
        self.review_as("romantic")
        result, prompt = self.incremental(proposed)
        self.assertIn("[ID:0]", prompt)
        self.assertIn("[ID:15]", prompt)
        self.assertEqual(result["score"], 32)
        self.assertEqual(result["delta"], 2)
        self.assertEqual(result["score_stability"]["raw_score"], 45)
        self.assertEqual(result["relationship"]["type"], "friend")
        self.assertEqual(result["relationship"]["stability"]["confirmations"], 1)
        result, _ = self.incremental(proposed)
        self.assertEqual(result["relationship"]["stability"]["confirmations"], 1)
        self.add("new", self.base+4*86400000, "1", "普通朋友就挺好")
        result, _ = self.incremental(relation("friend"))
        state = json.loads(self.d.store.conn.execute("SELECT data FROM relationship_state").fetchone()[0])
        self.assertIsNone(state["candidate"])

    def test_eco_updates_explicit_partner_event_without_consuming_it_first(self):
        self.baseline()
        a, b = "我们正式成为恋人吧", "我同意，我们正式交往"
        self.add("ask", self.base+86400000, "1", a)
        self.add("yes", self.base+86460000, "2", b)
        event = dict(kind="confirm_partner", evidence=[
            dict(message_id="ask", quote=a), dict(message_id="yes", quote=b)])
        self.d._review_relationship_evidence = lambda grounded: {
            ref["message_id"]: dict(category="commitment" if ref["message_id"]=="ask" else "acceptance",
                                    direct=True, affirmative=True) for ref, row in grounded}
        result, _ = self.incremental(relation("partner", 95, event))
        self.assertEqual(result["relationship"]["type"], "partner")
        self.assertEqual(result["relationship"]["stability"]["status"], "event_confirmed")

    def test_verified_breakup_is_separate_from_next_identity_confidence(self):
        self.baseline("partner")
        text = "我们分手吧，我决定结束这段恋爱关系"
        self.add("end", self.base+86400000, "1", text)
        self.review_as("breakup")
        event = dict(kind="end_partner", evidence=[dict(message_id="end", quote=text)])
        result, _ = self.incremental(relation("acquaintance", 75, event), -20)
        self.assertEqual(result["relationship"]["type"], "acquaintance")
        self.assertEqual(result["relationship"]["stability"]["status"], "event_confirmed")
        self.assertNotIn("_verified_event", result["relationship"])
        self.assertEqual(result["score"], 30)  # Identity event does not erase accumulated closeness.
        self.assertEqual(result["score_stability"]["raw_score"], -20)

    def test_without_semantic_review_low_confidence_breakup_stays_partner(self):
        self.baseline("partner")
        text = "我不是要跟你分手"
        self.add("end", self.base+86400000, "1", text)
        self.review_as("ordinary")
        event = dict(kind="end_partner", evidence=[dict(message_id="end", quote=text)])
        result, _ = self.incremental(relation("acquaintance", 75, event))
        self.assertEqual(result["relationship"]["type"], "partner")

    def test_model_cannot_supply_internal_verification_marker(self):
        obj = dict(relation("acquaintance", 75), _verified_event="end_partner")
        self.assertNotIn("_verified_event", w.normalize_relationship(obj))

    def test_review_receives_full_text_and_neighboring_correction(self):
        self.baseline("partner")
        self.add("end", self.base+86400000, "1", "台词是：我们分手吧，我没有在说我们")
        self.add("next", self.base+86460000, "1", "刚才是在引用电影，不是要和你分手")
        proposal = relation("acquaintance", 99, dict(kind="end_partner", evidence=[
            dict(message_id="end", quote="我们分手吧")]))
        def call(key, model, system, prompt, **kw):
            self.assertIn("我没有在说我们", prompt)
            self.assertIn("刚才是在引用电影", prompt)
            return {"end": dict(category="ordinary", direct=False, affirmative=False)}
        self.d.call_llm = call
        result, rejected = self.d._gate_relationship("a", proposal, relation("partner"),
                                                    self.d.store.messages("a"))
        self.assertTrue(rejected)
        self.assertEqual(result["type"], "partner")

    def test_old_breakup_cannot_use_new_ordinary_message_as_fresh_evidence(self):
        text = "我们分手吧，我决定结束这段恋爱关系"
        self.add("old", self.base+1000, "1", text)
        self.baseline("partner")  # A later established relationship supersedes old event.
        self.add("fresh", self.base+86400000, "2", "今天下雨了")
        self.d._review_relationship_evidence = lambda grounded: {
            ref["message_id"]: dict(category="breakup" if ref["message_id"]=="old" else "ordinary",
                                    direct=True, affirmative=True) for ref, row in grounded}
        event = dict(kind="end_partner", evidence=[
            dict(message_id="old", quote=text), dict(message_id="fresh", quote="今天下雨了")])
        result, _ = self.incremental(relation("acquaintance", 88, event))
        self.assertEqual(result["relationship"]["type"], "partner")

    def test_romantic_dimension_survives_full_score_validation(self):
        obj = score(70)
        obj["dimensions"]["romantic"] = 88
        normalized = w.normalize_score(obj)
        self.assertEqual(normalized["dimensions"]["romantic"], 88)
        self.d.store.set_score("a", normalized["score"], 0, normalized["reason"],
                               normalized["dimensions"])
        self.assertEqual(self.d.store.score("a")["dimensions"]["romantic"], 88)
        self.assertEqual(self.d.store.conn.execute("SELECT romantic FROM score_history").fetchone()[0], 88)
