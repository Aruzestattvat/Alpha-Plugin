import json
import unittest
from unittest.mock import patch
from test_wvcore2 import Isolated, w
import score_stability as s

T = 1700000000


def row(i, seconds, text="新消息", speaker="1", kind="text"):
    return (str(i), int(seconds*1000), speaker, kind, text)


class DampingTests(unittest.TestCase):
    def test_half_rounding_preserves_the_movement_limit(self):
        self.assertEqual(s.display({"level":81.5})-s.display({"level":78.5}),3)

    def test_sudden_drop_is_bounded_and_repeating_old_evidence_does_not_move(self):
        rows = [row(0, T)]
        state = s.initial(80, rows, T)
        rows += [row(i, T+1) for i in range(1,41)]
        state = s.update(state, 20, rows, T+1)
        self.assertEqual(round(state["level"]), 77)
        for i in range(100):
            state = s.update(state, -100 if i%2 else 100, rows, T+86400*i)
            self.assertEqual(round(state["level"]), 77)
            self.assertEqual(state["new_messages"], 0)

    def test_dense_and_reversing_evidence_share_absolute_movement_budget(self):
        rows = [row(0, T)]
        state = s.initial(80, rows, T)
        movement = 0
        for i in range(1,1001):
            rows.append(row(i, T+i))
            before = state["level"]
            state = s.update(state, -100 if i%2 else 100, rows, T+i)
            movement += abs(state["level"]-before)
        self.assertLessEqual(movement, 3+6*1000/86400+1e-8)

    def test_budget_does_not_reset_at_midnight(self):
        state = s.initial(80, [], T)
        rows = [row(i, T+1) for i in range(40)]
        state = s.update(state, -100, rows, T+1)
        before = state["level"]
        rows += [row(i, T+61) for i in range(40,80)]
        state = s.update(state, -100, rows, T+61)
        self.assertLessEqual(before-state["level"], 6*60/86400+1e-8)

    def test_long_silence_does_not_bank_a_large_jump(self):
        state = s.initial(80, [], T)
        now = T+90*86400
        state = s.update(state, -100, [row(i, now) for i in range(500)], now)
        self.assertEqual(round(state["level"]), 77)

    def test_sustained_week_of_evidence_can_change_baseline(self):
        state = s.initial(80, [], T)
        rows = []
        total_movement = 0
        for hour in range(1,169):
            now = T+hour*3600
            rows += [row(len(rows)+i, now) for i in range(10)]
            before = state["level"]
            state = s.update(state, 20, rows, now)
            total_movement += abs(state["level"]-before)
        self.assertGreaterEqual(state["level"], 20)
        self.assertLess(state["level"], 45)
        self.assertLessEqual(total_movement, 3+6*7)

    def test_clock_rollback_system_media_and_old_imports_do_not_add_weight(self):
        rows = [row(0, T)]
        state = s.initial(80, rows, T)
        rows += [row(1, T-10), row(2, T+1, speaker="0"), row(3, T+1, kind="media")]
        state = s.update(state, -100, rows, T+1)
        self.assertEqual(state["level"], 80)
        rows += [row(4, T+2)]
        state = s.update(state, -100, rows, T-100)
        self.assertEqual(state["level"], 80)
        self.assertEqual(state["refilled_at"], T)

    def test_fractional_progress_and_identical_timestamps_survive_serialization(self):
        state = s.initial(80, [], T)
        state = s.update(state, 40, [row(1,T)], T)
        self.assertEqual(round(state["level"]), 80)
        state = json.loads(json.dumps(state))
        state = s.update(state, 40, [row(1,T),row(2,T)], T)
        self.assertEqual(round(state["level"]), 79)
        self.assertEqual(state["new_messages"], 1)


class StoreDampingTests(Isolated):
    def add(self, store, i, now):
        store.upsert_message("a",dict(id=str(i),sent_at_unix_ms=int(now*1000),
                                    direction="1",kind="text",text="新增内容"))

    def test_restart_reanalysis_and_exact_delta(self):
        store=w.Store()
        self.addCleanup(store.close)
        with patch.object(w.time,"time",return_value=T):
            self.add(store,0,T)
            store.set_score("a",80,999,"baseline")
            self.assertEqual(store.score("a")["delta"],0)
        with patch.object(w.time,"time",return_value=T+1):
            for i in range(1,41):self.add(store,i,T+1)
            store.set_score("a",20,-60,"raw drop")
            value=store.score("a")
            self.assertEqual((value["score"],value["delta"]),(77,-3))
        store.close()
        with patch.object(w.time,"time",return_value=T+86400):
            store=w.Store();self.addCleanup(store.close)
            count=store.conn.execute("SELECT count(*) FROM score_history").fetchone()[0]
            store.set_score("a",-100,-177,"manual reanalysis")
            value=store.score("a")
            self.assertEqual((value["score"],value["delta"]),(77,0))
            self.assertEqual(value["score_stability"]["raw_score"],-100)
            self.assertEqual(store.conn.execute("SELECT count(*) FROM score_history").fetchone()[0],count)

    def test_upgrade_preserves_score_without_model_or_extra_history(self):
        store=w.Store()
        with patch.object(w.time,"time",return_value=T):
            self.add(store,0,T)
            store.set_score("a",86,0,"legacy")
        with store.conn:store.conn.execute("DELETE FROM score_filter_state")
        count=store.conn.execute("SELECT count(*) FROM score_history").fetchone()[0]
        store.close()
        with patch.object(w,"llm",side_effect=AssertionError("no API")):
            store=w.Store();self.addCleanup(store.close)
        self.assertEqual(store.score("a")["score"],86)
        self.assertEqual(store.conn.execute("SELECT count(*) FROM score_history").fetchone()[0],count)

    def test_failed_score_write_rolls_back_evidence_and_budget(self):
        store=w.Store();self.addCleanup(store.close)
        self.add(store,0,T)
        store.set_score("a",80,0,"baseline")
        before=store.conn.execute("SELECT data FROM score_filter_state").fetchone()[0]
        for i in range(1,20):self.add(store,i,T+1)
        store.conn.execute("CREATE TRIGGER fail_score BEFORE UPDATE ON scores BEGIN SELECT RAISE(ABORT,'test'); END")
        with self.assertRaises(w.sqlite3.IntegrityError):store.set_score("a",-100,0,"failure")
        self.assertEqual(store.conn.execute("SELECT data FROM score_filter_state").fetchone()[0],before)
        self.assertEqual(store.score("a")["score"],80)

    def test_damping_lag_does_not_trigger_expensive_full_calibration(self):
        previous={"score":80,"score_stability":{"raw_score":20},"dimensions":{}}
        obj={"score":20,"dimensions":{}}
        self.assertEqual(w.calibration_deviation(previous,obj),(0,0))
