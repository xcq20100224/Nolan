# -*- coding: utf-8 -*-
"""
progress 进度总线单元测试：emit/drain/begin/end 语义、线程安全、
无订阅零开销、异常不抛。纯内存，零网络零 LLM。
运行：python -m unittest test_progress -v   （在 jarvis/ 目录下）
"""
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import progress


class TestLifecycle(unittest.TestCase):
    """begin/end/drain 生命周期语义。"""

    def tearDown(self):
        progress.end()
        progress.drain()  # 收尾清空，防用例间串味

    def test_begin_activates(self):
        progress.begin()
        self.assertTrue(progress.is_active())

    def test_end_deactivates(self):
        progress.begin()
        progress.end()
        self.assertFalse(progress.is_active())

    def test_begin_clears_leftover(self):
        # 上一轮结束队列里残留的事件，begin 时清空，不泄漏到下一轮
        progress.begin()
        progress.emit("残留事件")
        progress.end()
        progress.begin()
        self.assertEqual(progress.drain(), [])

    def test_drain_empty_returns_list(self):
        progress.begin()
        self.assertEqual(progress.drain(), [])

    def test_end_is_idempotent(self):
        progress.end()
        progress.end()  # 重复 end 不抛
        self.assertFalse(progress.is_active())


class TestEmitDrain(unittest.TestCase):
    """emit/drain 事件语义。"""

    def setUp(self):
        progress.begin()

    def tearDown(self):
        progress.end()
        progress.drain()

    def test_emit_basic_step(self):
        progress.emit("正在精写第 3/10 页")
        evs = progress.drain()
        self.assertEqual(evs, [{"step": "正在精写第 3/10 页"}])

    def test_emit_with_counters(self):
        progress.emit("正在精写第 3/10 页：标题", i=3, n=10)
        evs = progress.drain()
        self.assertEqual(evs, [{"step": "正在精写第 3/10 页：标题", "i": 3, "n": 10}])

    def test_emit_only_i(self):
        progress.emit("单计数", i=1)
        self.assertEqual(progress.drain(), [{"step": "单计数", "i": 1}])

    def test_drain_is_atomic_take_all(self):
        progress.emit("一")
        progress.emit("二")
        progress.emit("三")
        evs = progress.drain()
        self.assertEqual([e["step"] for e in evs], ["一", "二", "三"])
        self.assertEqual(progress.drain(), [])  # 取走后队列空

    def test_order_preserved(self):
        for k in range(50):
            progress.emit(f"事件{k}", i=k)
        evs = progress.drain()
        self.assertEqual([e["i"] for e in evs], list(range(50)))

    def test_bad_counters_dropped_silently(self):
        progress.emit("坏计数", i="abc", n=object())
        evs = progress.drain()
        self.assertEqual(evs, [{"step": "坏计数"}])  # 转不动的字段静默丢弃

    def test_counter_coercion(self):
        progress.emit("字符串数字", i="3", n=10.0)
        evs = progress.drain()
        self.assertEqual(evs, [{"step": "字符串数字", "i": 3, "n": 10}])

    def test_empty_step_dropped(self):
        progress.emit("")
        progress.emit("   ")
        progress.emit(None)
        self.assertEqual(progress.drain(), [])

    def test_long_step_truncated(self):
        progress.emit("长" * 200)
        evs = progress.drain()
        self.assertEqual(len(evs), 1)
        self.assertEqual(len(evs[0]["step"]), 60)


class TestNoSubscription(unittest.TestCase):
    """无订阅时：emit 零开销空操作。"""

    def tearDown(self):
        progress.end()
        progress.drain()

    def test_emit_inactive_is_noop(self):
        progress.end()  # 明确无订阅
        progress.emit("没人听")
        progress.begin()
        self.assertEqual(progress.drain(), [])  # 订阅期间的消息没有滞留

    def test_emit_inactive_zero_overhead(self):
        # 零开销的量化验证：未订阅时 10 万次 emit 应在亚秒级完成
        # （若误实现为无条件入队，这里会堆积 10 万事件且明显变慢）
        progress.end()
        t0 = time.perf_counter()
        for k in range(100000):
            progress.emit(f"噪声{k}")
        elapsed = time.perf_counter() - t0
        progress.begin()
        self.assertEqual(progress.drain(), [])
        self.assertLess(elapsed, 2.0, f"未订阅 emit 过慢：{elapsed:.2f}s")


class TestThreadSafety(unittest.TestCase):
    """多线程并发 emit：不丢、不错序（单队列保证全局入队序）。"""

    def tearDown(self):
        progress.end()
        progress.drain()

    def test_concurrent_emit_no_loss(self):
        progress.begin()
        n_threads, n_each = 8, 500

        def _producer(tid):
            for k in range(n_each):
                progress.emit(f"t{tid}-{k}")

        threads = [threading.Thread(target=_producer, args=(t,))
                   for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        evs = progress.drain()
        self.assertEqual(len(evs), n_threads * n_each)  # 一条不丢
        # 每个生产者内部的消息保持各自相对顺序（FIFO 不乱）
        for tid in range(n_threads):
            mine = [e["step"] for e in evs if e["step"].startswith(f"t{tid}-")]
            self.assertEqual(mine, [f"t{tid}-{k}" for k in range(n_each)])

    def test_emit_racing_end_never_raises(self):
        # emit 与 end 并发赛跑：丢消息可以，抛异常不行
        progress.begin()
        stop = threading.Event()
        errors = []

        def _producer():
            while not stop.is_set():
                try:
                    progress.emit("赛跑")
                except Exception as e:  # pragma: no cover - 真出异常就记录
                    errors.append(e)

        ts = [threading.Thread(target=_producer) for _ in range(4)]
        for t in ts:
            t.start()
        for _ in range(200):
            progress.end()
            progress.begin()
        progress.end()
        stop.set()
        for t in ts:
            t.join()
        self.assertEqual(errors, [])


class TestNeverRaises(unittest.TestCase):
    """异常不抛：各种恶意入参全部静默。"""

    def tearDown(self):
        progress.end()
        progress.drain()

    def test_emit_weird_args(self):
        progress.begin()
        # str() 会炸的对象
        class _Boom:
            def __str__(self):
                raise RuntimeError("炸了")
        progress.emit(_Boom())          # 不抛
        progress.emit("正常", i=_Boom(), n=_Boom())  # 不抛，坏字段丢弃
        evs = progress.drain()
        self.assertEqual(evs, [{"step": "正常"}])

    def test_drain_never_raises_without_begin(self):
        progress.end()
        self.assertEqual(progress.drain(), [])  # 无订阅也能安全 drain


if __name__ == "__main__":
    unittest.main(verbosity=2)
