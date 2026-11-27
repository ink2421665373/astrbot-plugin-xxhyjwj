"""改名校验：映射命中的角色数据过旧时，用当前角色名重新绑定刷新一次。"""
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
record_query = importlib.import_module("naraka_test_package.record_query")


def build_view(names, **config):
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8-sig"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "NarakaPlugin")
    cls = ast.ClassDef(name="Harness", bases=[], keywords=[], decorator_list=[],
                       body=[node for node in cls.body if getattr(node, "name", "") in names])
    env = dict(asynccontextmanager=asynccontextmanager, asyncio=asyncio, time=time, logger=Mock(),
               newest_match_ms=record_query.newest_match_ms,
               HeyboxError=record_query.__dict__.get("HeyboxError", Exception),
               aiohttp=type("aiohttp", (), {"ClientError": Exception}))
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])), "main.py", "exec"), env)
    view = env["Harness"]()
    view.config = dict(config)
    return view


class Outcome:
    def __init__(self, ok=True, state="ok", message=""):
        self.ok = ok
        self.state = state
        self.message = message


class FakeBinding:
    def __init__(self, outcome=None, outcomes=None):
        self.outcome = outcome or Outcome()
        self.outcomes = outcomes
        self.calls = []

    @asynccontextmanager
    async def bound(self, session, nickname, server_id=None, role_id=""):
        self.calls.append((nickname, server_id, role_id))
        if self.outcomes:
            yield self.outcomes.pop(0)
            return
        yield self.outcome


class StaleRowsTests(unittest.TestCase):
    def test_newest_match_ms_picks_latest_and_ignores_junk(self):
        now = int(time.time())
        rows = [
            {"time": now - 86400 * 30},
            {"time": now - 60},
            {"time": "坏值"},
            {"time": None},
            {"no_time": 1},
            "not-a-dict",
        ]
        newest = record_query.newest_match_ms(rows)
        self.assertIsNotNone(newest)
        self.assertAlmostEqual(newest / 1000.0, now - 60, delta=2)
        self.assertIsNone(record_query.newest_match_ms([]))

    def test_rows_stale_respects_threshold_and_off_switch(self):
        view = build_view({"_rows_stale"})
        old = [{"time": int(time.time()) - 86400 * 11}]
        fresh = [{"time": int(time.time()) - 3600}]
        self.assertTrue(view._rows_stale(old, 3))
        self.assertFalse(view._rows_stale(fresh, 3))
        self.assertFalse(view._rows_stale(old, 0))
        self.assertTrue(view._rows_stale([], 3))

    def test_mapping_stale_days_defaults_and_clamps(self):
        view = build_view({"_number_setting", "_mapping_stale_days"})
        self.assertEqual(view._mapping_stale_days(), 3.0)
        view.config = {"mapping_stale_days": 0}
        self.assertEqual(view._mapping_stale_days(), 0.0)
        view.config = {"mapping_stale_days": "坏值"}
        self.assertEqual(view._mapping_stale_days(), 3.0)


class MappingRefreshTests(unittest.TestCase):
    def make(self, outcome=None, sequence=None):
        names = {"_refresh_mapping_data", "_mapping_aliases", "_rebind_after_already_bound"}
        view = build_view(names)
        view.config = {"auto_bind": True,
                       "nickname_mappings": [
                           {"nickname": "lzn", "role_id": "95bs"},
                           {"nickname": "李知恩", "role_id": "95bs"},
                           {"nickname": "君墨", "role_id": "slil"},
                       ]}
        view._binding = FakeBinding(outcome)
        if sequence:
            view._binding.outcomes = list(sequence)
        view.refreshed = []
        view.unbound = []
        view._auto_bind = lambda: True  # type: ignore[assignment]

        class FakeApi:
            async def unbind_game(self, session, game_id, server_id=None):
                view.unbound.append(game_id)
                return {"state": "ok"}

        view._api = FakeApi()

        async def fake_refresh(session, role_id, server):
            view.refreshed.append((role_id, server))

        view._refresh = fake_refresh  # type: ignore[assignment]
        return view

    def run_async(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_refresh_binds_with_current_name_then_updates(self):
        view = self.make()
        ok = self.run_async(view._refresh_mapping_data(None, "睡不醒的某阳", "95bs000012710300280163", "cn"))
        self.assertTrue(ok)
        self.assertEqual(view._binding.calls, [("睡不醒的某阳", "cn", "95bs000012710300280163")])
        self.assertEqual(view.refreshed, [("95bs000012710300280163", "cn")])

    def test_bind_failure_skips_update(self):
        view = self.make(Outcome(ok=False, state="bound_elsewhere", message="已被绑定"))
        ok = self.run_async(view._refresh_mapping_data(None, "lzn", "95bs000012710300280163", "cn"))
        self.assertFalse(ok)
        self.assertEqual(view.refreshed, [])

    def test_auto_bind_off_does_nothing(self):
        view = self.make()
        view._auto_bind = lambda: False  # type: ignore[assignment]
        ok = self.run_async(view._refresh_mapping_data(None, "lzn", "95bs", "cn"))
        self.assertFalse(ok)
        self.assertEqual(view._binding.calls, [])

    def test_empty_nickname_is_ignored(self):
        view = self.make()
        ok = self.run_async(view._refresh_mapping_data(None, "   ", "95bs", "cn"))
        self.assertFalse(ok)
        self.assertEqual(view._binding.calls, [])

    def test_aliases_only_for_same_role(self):
        view = self.make()
        self.assertEqual(view._mapping_aliases("95bs"), ["lzn", "李知恩"])
        self.assertEqual(view._mapping_aliases("slil"), ["君墨"])
        self.assertEqual(view._mapping_aliases("missing"), [])

    def test_already_bound_falls_back_to_unbind_then_rebind(self):
        view = self.make(sequence=[Outcome(ok=False, state="error", message="你已经绑定过游戏账号了"), Outcome()])
        ok = self.run_async(view._refresh_mapping_data(None, "睡不醒的某阳", "95bs", "cn"))
        self.assertTrue(ok)
        self.assertEqual(view.unbound, ["lzn", "李知恩"])
        self.assertEqual([c[0] for c in view._binding.calls], ["睡不醒的某阳", "睡不醒的某阳"])
        self.assertEqual(view.refreshed, [("95bs", "cn")])

    def test_already_bound_without_other_alias_gives_up(self):
        view = self.make(sequence=[Outcome(ok=False, state="error", message="你已经绑定过游戏账号了")])
        ok = self.run_async(view._refresh_mapping_data(None, "君墨", "slil", "cn"))
        self.assertFalse(ok)
        self.assertEqual(view.unbound, [])
        self.assertEqual(view.refreshed, [])


if __name__ == "__main__":
    unittest.main()
