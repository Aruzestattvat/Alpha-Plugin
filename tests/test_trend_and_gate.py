import importlib.util
import json
import pathlib
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("wvcore2", ROOT / "wvcore2.py")
wv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wv)


class TrendTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.patcher = patch.object(wv, "DB_PATH", pathlib.Path(self.tmp.name) / "t.sqlite3")
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.store = wv.Store()
        self.addCleanup(self.store.close)

    def test_ready_context_trend(self):
        now = int(time.time())
        for i, (msgs, score) in enumerate([(0, 60), (50, 62), (100, 64), (150, 66), (200, 68)]):
            self.store.record_history("s1", score, now - (4 - i) * 86400, msgs=msgs)
        trend = self.store.trend("s1")
        self.assertEqual(trend["status"], "ready")
        self.assertGreater(trend["slope_per_100_msgs"], 0)
        self.assertEqual(trend["symbol"], "↗")
        self.assertIn("prediction", trend)
        self.assertEqual(trend["prediction"]["messages"], 100)

    def test_insufficient_context_trend(self):
        now = int(time.time())
        for i, (msgs, score) in enumerate([(0, 60), (50, 62)]):
            self.store.record_history("s1", score, now - (1 - i) * 86400, msgs=msgs)
        self.assertEqual(self.store.trend("s1")["status"], "insufficient")

    def test_same_message_position_does_not_add_samples(self):
        now = int(time.time())
        for i, score in enumerate([60, 61, 62, 63]):
            self.store.record_history("s1", score, now - 3600 + i, msgs=100)
        self.assertEqual(self.store.trend("s1")["status"], "insufficient")


class GateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        for name, value in dict(ROOT=root, DB_PATH=root / "db.sqlite", STATE=root / "state.json",
                                WATCHED=root / "watched.json", LOG_PATH=root / "test.log",
                                AVATARS=root / "avatars", ASR_MODEL_DIR=root / "missing",
                                HTTP_PORT=0, RELATION_STRICT=True).items():
            p = patch.object(wv, name, value); p.start(); self.addCleanup(p.stop)
        p = patch.object(wv, "load_secrets",
                         return_value={"ALPHA_TOKEN": "t", "DEEPSEEK_API_KEY": "t"})
        p.start(); self.addCleanup(p.stop)
        p = patch.object(wv, "log"); p.start(); self.addCleanup(p.stop)
        self.d = wv.Daemon()
        self.addCleanup(self.d.store.close)
        self.addCleanup(lambda: self.d.pool.shutdown(wait=True))

    def _msg(self, mid, direction, text, ts):
        return dict(id=mid, platform_message_id=mid, sent_at_unix_ms=ts,
                    direction=direction, kind="text", text=text)

    def _previous(self):
        return wv.normalize_relationship(dict(type="friend", confidence=90,
            reason="长期朋友", evidence=["互动稳定"]))

    def test_flirting_requires_grounded_romantic_evidence(self):
        now = int(time.time() * 1000)
        rows = [
            ("o1", now - 50 * 3600 * 1000, "1", "text", "最近总是想你，想见你"),
            ("m1", now - 49 * 3600 * 1000, "2", "text", "我也想你，周末约会吧"),
            ("o2", now - 25 * 3600 * 1000, "1", "text", "见到你心跳很快"),
            ("m2", now - 24 * 3600 * 1000, "2", "text", "我也是，很喜欢和你在一起"),
        ]
        proposal = wv.normalize_relationship(dict(type="flirting", confidence=92,
            reason="双向暧昧", evidence=["双向浪漫表达"],
            romantic_evidence=[{"message_id": "o1", "quote": "最近总是想你，想见你"},
                               {"message_id": "m1", "quote": "我也想你，周末约会吧"},
                               {"message_id": "o2", "quote": "见到你心跳很快"},
                               {"message_id": "m2", "quote": "我也是，很喜欢和你在一起"}]))
        self.d._review_relationship_evidence = lambda grounded: {
            ref["message_id"]: {"message_id": ref["message_id"], "category": "romantic",
                                "direct": True, "affirmative": True} for ref, row in grounded}
        accepted, reason = self.d._gate_relationship("a", proposal, self._previous(), rows)
        self.assertEqual(accepted["type"], "flirting")

    def test_ordinary_quotes_rejected(self):
        now = int(time.time() * 1000)
        rows = [("o1", now - 50 * 3600 * 1000, "1", "text", "明天开会吗"),
                ("m1", now - 49 * 3600 * 1000, "2", "text", "开，九点"),
                ("o2", now - 25 * 3600 * 1000, "1", "text", "帮我带个饭"),
                ("m2", now - 24 * 3600 * 1000, "2", "text", "好")]
        proposal = wv.normalize_relationship(dict(type="flirting", confidence=92,
            reason="误判", evidence=["日常互助"],
            romantic_evidence=[{"message_id": "o1", "quote": "明天开会吗"},
                               {"message_id": "m1", "quote": "开，九点"},
                               {"message_id": "o2", "quote": "帮我带个饭"},
                               {"message_id": "m2", "quote": "好"}]))
        self.d._review_relationship_evidence = lambda grounded: {
            ref["message_id"]: {"message_id": ref["message_id"], "category": "ordinary",
                                "direct": True, "affirmative": True} for ref, row in grounded}
        accepted, reason = self.d._gate_relationship("a", proposal, self._previous(), rows)
        self.assertEqual(accepted["type"], "friend")
        self.assertIn("boundary_check", accepted)


if __name__ == "__main__":
    unittest.main()
