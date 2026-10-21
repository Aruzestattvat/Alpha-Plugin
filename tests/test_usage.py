import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("wvcore2", ROOT / "wvcore2.py")
wv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wv)


class UsageStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = pathlib.Path(self.tmp.name) / "u.sqlite3"
        self.patcher = patch.object(wv, "DB_PATH", self.db)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.store = wv.Store()
        self.addCleanup(self.store.close)

    def test_add_and_query(self):
        self.store.add_usage("s1", 1000, "deepseek-flash")
        self.store.add_usage("s1", 500, "deepseek-v4-pro")
        self.store.add_usage("s2", 300, "deepseek-flash")
        self.assertEqual(self.store.usage("s1", 1), {"tokens": 1500, "calls": 2})
        self.assertEqual(self.store.usage("s2", 7)["tokens"], 300)
        self.assertEqual(self.store.usage("s1", 1)["calls"], 2)


class UsageCallbackTest(unittest.TestCase):
    def test_llm_reports_usage(self):
        payload = {"choices": [{"message": {"content": '{"ok": true}'},
                               "finish_reason": "stop"}],
                   "usage": {"total_tokens": 1234}}
        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps(payload).encode()
        seen = []
        with patch.object(wv.urllib.request, "urlopen", return_value=Resp()):
            out = wv.llm("k", "m", "s", "u", on_usage=lambda t, m: seen.append((t, m)))
        self.assertEqual(out, {"ok": True})
        self.assertEqual(seen, [(1234, "m")])


if __name__ == "__main__":
    unittest.main()
