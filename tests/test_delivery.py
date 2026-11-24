import ast
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from delivery import QueryProgress, save_originals, split_message


class DeliveryTests(unittest.TestCase):
    def test_split_round_trip_with_large_lines_and_unicode(self):
        for text in ("", "x" * 6000, "第一行\n\n" * 500, "武器伤害占比" * 1000 + "\n尾行"):
            chunks = split_message(text)
            self.assertEqual("".join(chunks), text)
            self.assertTrue(all(len(item) <= 1200 for item in chunks))

    def test_progress_is_throttled_and_can_be_disabled(self):
        event = SimpleNamespace(send=AsyncMock(), plain_result=lambda value: value)
        now = [0]
        progress = QueryProgress(event, interval=8, clock=lambda: now[0], mode="详细进度")
        async def run():
            self.assertTrue(await progress("开始", force=True))
            now[0] = 3
            self.assertFalse(await progress("1/10"))
            now[0] = 8
            self.assertTrue(await progress("4/10"))
            self.assertFalse(await progress("进入下一阶段", force=True))
            now[0] = 16
            self.assertTrue(await progress("进入下一阶段", force=True))
            progress.enabled = False
            self.assertFalse(await progress("已关闭", force=True))
        asyncio.run(run())
        self.assertEqual(event.send.await_count, 3)

    def test_progress_send_failure_does_not_interrupt_query(self):
        event = SimpleNamespace(send=AsyncMock(side_effect=RuntimeError("offline")), plain_result=lambda value: value)
        progress = QueryProgress(event, mode="详细进度")
        self.assertFalse(asyncio.run(progress("开始")))
        self.assertFalse(progress.enabled)

    def test_original_expiry_only_cleans_own_old_pngs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "naraka-original-old.png"
            old.write_bytes(b"old")
            other = root / "other.png"
            other.write_bytes(b"keep")
            os.utime(old, (1, 1))
            paths = save_originals(root, [b"exact png bytes"], now=100000)
            self.assertFalse(old.exists())
            self.assertEqual(other.read_bytes(), b"keep")
            self.assertEqual(paths[0].read_bytes(), b"exact png bytes")
            self.assertNotEqual(paths[0], save_originals(root, [b"another"], now=100000)[0])

    def test_force_refresh_command_is_only_a_trailing_option(self):
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8-sig"))
        plugin = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "NarakaPlugin")
        method = next(n for n in plugin.body if getattr(n, "name", "") == "on_message")
        method.decorator_list = []
        env = dict(AstrMessageEvent=object, re=__import__("re"))
        exec(compile(ast.Module(body=[method], type_ignores=[]), "main.py", "exec"), env)
        captured = []
        async def query(*args, **kwargs):
            captured.append((args, kwargs))
            yield "ok"
        fake = SimpleNamespace(config={}, _allowed_group=lambda event: True, _query_records=query, _note=lambda event, text: text)
        async def collect(text):
            return [value async for value in env["on_message"](fake, SimpleNamespace(message_str=text))]
        for text in ("战绩查询 玩家 双排 强制刷新", "/详细查询 玩家 --refresh", "详细查询 强制刷新", "战绩分析 玩家 双排"):
            self.assertEqual(asyncio.run(collect(text)), ["ok"])
        self.assertEqual(captured[0][0][1:], ("玩家", "双排"))
        self.assertTrue(captured[0][1]["force_refresh"])
        self.assertTrue(captured[1][1]["force_refresh"])
        self.assertFalse(captured[2][1]["force_refresh"])
        self.assertEqual(captured[2][0][1], "强制刷新")
        self.assertTrue(captured[3][1]["analysis"])

    def test_webui_has_delivery_controls(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8-sig"))
        self.assertTrue(schema["enable_query_progress"]["default"])
        self.assertFalse(schema["send_original_image_file"]["default"])
        self.assertEqual(schema["query_progress_interval_seconds"]["default"], 8.0)
        self.assertEqual(schema["query_progress_mode"]["default"], "等待时提示一次")
        self.assertEqual(schema["query_progress_wait_seconds"]["default"], 15.0)


if __name__ == "__main__":
    unittest.main()
