import ast
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from delivery import QueryProgress


class QuietProgressTests(unittest.TestCase):
    def event(self):
        return SimpleNamespace(send=AsyncMock(), plain_result=lambda message: message)

    def test_quick_query_stays_silent_even_with_forced_stage_changes(self):
        event = self.event()
        async def run():
            progress = QueryProgress(event, wait_seconds=15)
            for i in range(20):
                await progress(f"阶段{i}", force=True)
            await progress.close()
            self.assertTrue(progress.task.done())
            await asyncio.sleep(0)
        asyncio.run(run())
        event.send.assert_not_awaited()

    def test_slow_query_sends_at_most_one_generic_notice(self):
        event = self.event()
        async def run():
            progress = QueryProgress(event, wait_seconds=0)
            progress.start()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            for i in range(20):
                await progress(f"详情{i}/20", force=True)
            await progress.close()
            await progress("已经结束", force=True)
        asyncio.run(run())
        event.send.assert_awaited_once()
        self.assertIn("查询仍在进行", event.send.call_args.args[0])
        self.assertNotIn("/20", event.send.call_args.args[0])

    def test_disabled_progress_has_no_background_timer(self):
        event = self.event()
        async def run():
            progress = QueryProgress(event, enabled=False, wait_seconds=0)
            await progress("开始", force=True)
            self.assertIsNone(progress.task)
            await progress.close()
        asyncio.run(run())
        event.send.assert_not_awaited()

    def test_delayed_send_failure_is_non_fatal(self):
        event = self.event()
        event.send.side_effect = RuntimeError("offline")
        async def run():
            progress = QueryProgress(event, wait_seconds=0)
            progress.start()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            await progress.close()
            self.assertFalse(progress.enabled)
        asyncio.run(run())

    def test_real_query_lifecycle_closes_timer_on_result_and_error(self):
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8-sig"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "NarakaPlugin")
        method = next(n for n in cls.body if getattr(n, "name", "") == "_query_records")
        instances = []
        def factory(*args, **kwargs):
            result = QueryProgress(*args, **kwargs)
            instances.append(result)
            return result
        env = dict(AstrMessageEvent=object, QueryProgress=factory,
                   looks_like_role_id=lambda value: False, _ReadResult=lambda **kwargs: SimpleNamespace(**kwargs),
                   HeyboxError=ValueError, aiohttp=SimpleNamespace(ClientError=ValueError),
                   asyncio=asyncio, _must_stop_after_error=lambda exc: False)
        exec(compile(ast.Module(body=[method], type_ignores=[]), "main.py", "exec"), env)
        @asynccontextmanager
        async def session():
            yield None
        for fail in (False, True, "query_failed"):
            event = self.event()
            fake = SimpleNamespace(_allowed_group=lambda event: True, _ack=AsyncMock(return_value=None),
                data_mode="heybox", logged_in=True, _query_lock=asyncio.Lock(), _start_query_cooldown=lambda: 0,
                config={}, _number_setting=lambda key, default, *args: default, _session=session,
                _binding=SimpleNamespace(recover=AsyncMock()), _residual_interval_seconds=lambda: 0,
                _run_read=AsyncMock(return_value=SimpleNamespace(ok=False, needs_bind=False, reason="失败") if fail == "query_failed" else SimpleNamespace(ok=True, texts=["完整结果"])),
                _note=lambda event, text: text,
                _body=AsyncMock(side_effect=RuntimeError("render failure")) if fail is True else AsyncMock(return_value="完整结果"))
            async def run():
                messages = []
                async for message in env["_query_records"](fake, event, "测试", ""):
                    if fail == "query_failed":
                        self.assertTrue(instances[-1].closed)
                    messages.append(message)
                return messages
            if fail is True:
                with self.assertRaises(RuntimeError):
                    asyncio.run(run())
            else:
                self.assertEqual(asyncio.run(run()), ["失败" if fail == "query_failed" else "完整结果"])
            self.assertTrue(instances[-1].closed)
            self.assertTrue(instances[-1].task.done())
            event.send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
