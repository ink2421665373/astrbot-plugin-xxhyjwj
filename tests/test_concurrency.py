"""单局详情并发：节流按请求开始时刻预约，请求本身可以重叠。"""
import ast
import asyncio
from contextlib import asynccontextmanager
import importlib
from pathlib import Path
import sys
import time
from types import ModuleType
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
package = ModuleType("naraka_test_package")
package.__path__ = [str(ROOT)]
sys.modules.setdefault("naraka_test_package", package)
api = importlib.import_module("naraka_test_package.heybox_api")


class ConcurrencyTests(unittest.TestCase):
    def harness(self, **config):
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8-sig"))
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "NarakaPlugin")
        names = {"_number_setting", "_interval_b", "_interval_c", "_detail_concurrency", "_paced"}
        cls = ast.ClassDef(name="Harness", bases=[], keywords=[], decorator_list=[],
                           body=[node for node in cls.body if getattr(node, "name", "") in names])
        env = dict(asynccontextmanager=asynccontextmanager, asyncio=asyncio, time=time, logger=Mock(),
                   HeyboxError=api.HeyboxError, HeyboxRateLimited=api.HeyboxRateLimited)
        exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])), "main.py", "exec"), env)
        view = env["Harness"]()
        view.config = dict(config)
        view._request_lock = asyncio.Lock()
        view._last_request_at = view._last_detail_request_at = None
        view._rate_limited_until = 0.0
        return view

    def test_concurrency_config_is_clamped(self):
        self.assertEqual(self.harness()._detail_concurrency(), 3)
        self.assertEqual(self.harness(detail_concurrency=1)._detail_concurrency(), 1)
        self.assertEqual(self.harness(detail_concurrency=0)._detail_concurrency(), 1)
        self.assertEqual(self.harness(detail_concurrency=99)._detail_concurrency(), 8)
        self.assertEqual(self.harness(detail_concurrency="坏值")._detail_concurrency(), 3)

    def test_detail_requests_overlap_instead_of_queueing(self):
        view = self.harness(request_interval_seconds=0.01, detail_interval_seconds=0.03,
                            detail_concurrency=3)
        active = 0
        peak = 0
        starts: list[float] = []

        async def body():
            nonlocal active, peak
            starts.append(time.monotonic())
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.12)
            active -= 1

        async def run():
            async def one():
                async with view._paced(detail=True):
                    await body()
            await asyncio.gather(*(one() for _ in range(3)))

        began = time.monotonic()
        asyncio.run(run())
        spent = time.monotonic() - began
        self.assertGreaterEqual(peak, 2, "三个详情请求应当有重叠，实际全串行了")
        self.assertLess(spent, 0.30, f"并发后总耗时应明显低于串行，实际 {spent:.2f}s")
        gaps = [b - a for a, b in zip(starts, starts[1:])]
        # Windows 定时器精度约 15ms，这里只要求「不是同一瞬间一起猛发」
        self.assertTrue(all(gap >= -0.002 for gap in gaps), f"相邻请求间隔异常：{gaps}")

    def test_detail_concurrency_one_keeps_strict_serial_spacing(self):
        view = self.harness(request_interval_seconds=0.02, detail_interval_seconds=0.06,
                            detail_concurrency=1)
        marks: list[float] = []

        async def run():
            for _ in range(3):
                async with view._paced(detail=True):
                    marks.append(time.monotonic())

        asyncio.run(run())
        gaps = [b - a for a, b in zip(marks, marks[1:])]
        self.assertTrue(all(gap >= 0.04 for gap in gaps), f"串行模式间隔应≈C：{gaps}")

    def test_parallel_mode_folds_c_by_concurrency_but_never_below_b(self):
        view = self.harness(request_interval_seconds=0.05, detail_interval_seconds=0.09,
                            detail_concurrency=3)
        marks: list[float] = []

        async def run():
            for _ in range(3):
                async with view._paced(detail=True):
                    marks.append(time.monotonic())

        asyncio.run(run())
        gaps = [b - a for a, b in zip(marks, marks[1:])]
        # C/3 = 0.03 < B = 0.05，所以实际间隔由 B 兜底
        self.assertTrue(all(0.03 <= gap < 0.10 for gap in gaps), f"间隔应被 B 兜底：{gaps}")

    def test_rate_limit_still_blocks_after_parallel_requests(self):
        view = self.harness(request_interval_seconds=0.01, detail_interval_seconds=0.01,
                            detail_concurrency=3)

        async def boom():
            async with view._paced(detail=True):
                raise api.HeyboxRateLimited("请求过于频繁", path="/game/yjwj/match/detail")

        with self.assertRaises(api.HeyboxRateLimited):
            asyncio.run(boom())
        self.assertGreater(view._rate_limited_until, time.monotonic())

        async def again():
            async with view._paced(detail=True):
                pass

        with self.assertRaises(api.HeyboxRateLimited):
            asyncio.run(again())


if __name__ == "__main__":
    unittest.main()
