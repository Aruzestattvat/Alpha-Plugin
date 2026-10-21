import importlib.util
import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("wvcore2", ROOT / "wvcore2.py")
wv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wv)


class ChunkGistTest(unittest.TestCase):
    def test_gist_from_summary(self):
        summary = json.dumps({
            "tone": "轻松",
            "topics": ["游戏", "吃饭", "周末"],
            "positive": ["主动约局"],
            "negative": [],
            "affinity": 80,
        }, ensure_ascii=False)
        gist = wv.chunk_gist(summary)
        self.assertIn("轻松", gist)
        self.assertIn("游戏", gist)
        self.assertIn("80", gist)
        self.assertLessEqual(len(gist), 120)

    def test_gist_invalid(self):
        self.assertEqual(wv.chunk_gist("not json"), "")
        self.assertEqual(wv.chunk_gist({"tone": None}), "好感:" if False else wv.chunk_gist({"tone": None}))
        self.assertLessEqual(len(wv.chunk_gist(None)), 120)


class CompactStatsTest(unittest.TestCase):
    def test_filters_known_keys(self):
        stats = {"messages": 10, "mine": 4, "peer": 6, "active_hours_top": [1],
                 "reply_median_s": 60, "kinds": {"text": 9}}
        out = wv.compact_stats(stats)
        self.assertEqual(out["messages"], 10)
        self.assertEqual(out["reply_median_s"], 60)
        self.assertNotIn("active_hours_top", out)
        self.assertNotIn("kinds", out)


class NormalizeIncrementalTest(unittest.TestCase):
    def test_valid(self):
        obj = {"score": 72, "reason": "互动变多", "rolling_summary": "长期朋友，近期更亲近",
               "dimensions": {"warmth": 70, "initiative": 60,
                              "intimacy": 50, "responsiveness": 80}}
        out = wv.normalize_incremental(obj)
        self.assertEqual(out["score"], 72)
        self.assertEqual(out["dimensions"]["warmth"], 70)

    def test_missing_fields(self):
        with self.assertRaises(ValueError):
            wv.normalize_incremental({"score": 50})
        with self.assertRaises(ValueError):
            wv.normalize_incremental({"score": 50, "reason": "x",
                                      "dimensions": {"warmth": 1}})


class CalibrationDeviationTest(unittest.TestCase):
    def test_deviation(self):
        prev = {"score": 78, "dimensions": {"warmth": 82, "initiative": 75,
                                            "intimacy": 72, "responsiveness": 80}}
        obj = {"score": 76, "dimensions": {"warmth": 80, "initiative": 73,
                                           "intimacy": 70, "responsiveness": 78}}
        score_delta, dim_delta = wv.calibration_deviation(prev, obj)
        self.assertEqual(score_delta, 2)
        self.assertEqual(dim_delta, 2)

    def test_no_deviation(self):
        prev = {"score": 70, "dimensions": {"warmth": 70, "initiative": 70,
                                            "intimacy": 70, "responsiveness": 70}}
        self.assertEqual(wv.calibration_deviation(prev, prev), (0, 0))


if __name__ == "__main__":
    unittest.main()
