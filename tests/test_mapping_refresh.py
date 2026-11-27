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
mapping_config = importlib.import_module("naraka_test_package.mapping_config")


def build_view(names, **config):
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8-sig"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "NarakaPlugin")
    cls = ast.ClassDef(name="Harness", bases=[], keywords=[], decorator_list=[],
                       body=[node for node in cls.body if getattr(node, "name", "") in names])
    env = dict(asynccontextmanager=asynccontextmanager, asyncio=asyncio, time=time, logger=Mock(),
               newest_match_ms=record_query.newest_match_ms,
               nickname_key=mapping_config.nickname_key,
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

        @asynccontextmanager
        async def paced(**kwargs):
            yield

        view._paced = paced

        class FakeApi:
            unbind_ok = True
            binds = ("waiting", "ok")

            async def unbind_game(self, session, game_id, server_id=None):
                view.unbound.append(game_id)
                if not self.unbind_ok:
                    raise RuntimeError("unbind failed")
                return {}

            async def bind_game(self, session, game_id, server_id=None):
                view.bind_calls.append(game_id)
                return {"state": self.binds[0]}

            async def bind_game_state(self, session, game_id):
                if len(self.binds) > 1:
                    return {"state": self.binds[1]}
                return {"state": self.binds[0]}

        view._api = FakeApi()
        view.bind_calls = []

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
        view = self.make(sequence=[Outcome(ok=False, state="error", message="你已经绑定过游戏账号了")])
        ok = self.run_async(view._refresh_mapping_data(None, "睡不醒的某阳", "95bs", "cn"))
        self.assertTrue(ok)
        self.assertEqual(view.unbound, ["睡不醒的某阳", "lzn", "李知恩"])
        self.assertEqual(view.bind_calls, ["睡不醒的某阳"])
        self.assertEqual(view.refreshed, [("95bs", "cn")])

    def test_unbind_failure_gives_up(self):
        view = self.make(sequence=[Outcome(ok=False, state="error", message="你已经绑定过游戏账号了")])
        view._api.unbind_ok = False
        ok = self.run_async(view._refresh_mapping_data(None, "君墨", "slil", "cn"))
        self.assertFalse(ok)
        self.assertEqual(view.bind_calls, [])
        self.assertEqual(view.refreshed, [])


class FakeConfig(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.saved = None

    def save_config(self, patch=None):
        self.saved = dict(patch or {})
        if patch:
            self.update(patch)


class SwitchRoleTests(unittest.TestCase):
    def make(self, candidates=None, newest=None):
        view = build_view({"_maybe_switch_role", "_retarget_mapping", "_mapping_aliases"})
        view.config = FakeConfig({
            "nickname_mappings": [
                {"nickname": "lzn", "role_id": "old"},
                {"nickname": "睡不醒的某阳", "role_id": "old"},
                {"nickname": "君墨", "role_id": "slil"},
            ]
        })
        view.search_map = candidates or {}
        view.newest_map = newest or {}

        async def fake_search(session, alias, limit=10):
            return view.search_map.get(alias, [])

        async def fake_newest(session, role_id, server):
            return view.newest_map.get(role_id)

        view._public_search_rows = fake_search  # type: ignore[assignment]
        view._role_newest_ms = fake_newest  # type: ignore[assignment]
        return view

    def run_async(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_retarget_rewrites_only_matching_rows(self):
        view = self.make()
        self.assertTrue(view._retarget_mapping("old", "new"))
        pairs = [(r["nickname"], r["role_id"]) for r in view.config["nickname_mappings"]]
        self.assertEqual(pairs, [("lzn", "new"), ("睡不醒的某阳", "new"), ("君墨", "slil")])
        self.assertIn("nickname_mappings", view.config.saved)
        self.assertFalse(view._retarget_mapping("old", "new"))

    def test_newer_namesake_wins_and_mapping_follows(self):
        view = self.make(
            candidates={
                "lzn": [{"role_id": "new", "role_name": "lzn", "server": "163"}],
                "睡不醒的某阳": [],
            },
            newest={"new": 2000.0},
        )
        row = self.run_async(view._maybe_switch_role(None, "old", "睡不醒的某阳", "163", 1000.0))
        self.assertEqual(row["role_id"], "new")
        self.assertTrue(all(r["role_id"] != "old" for r in view.config["nickname_mappings"]))

    def test_older_namesake_is_ignored(self):
        view = self.make(
            candidates={"lzn": [{"role_id": "new", "role_name": "lzn"}]},
            newest={"new": 500.0},
        )
        self.assertIsNone(self.run_async(view._maybe_switch_role(None, "old", "lzn", "163", 1000.0)))

    def test_different_name_is_not_treated_as_rename(self):
        view = self.make(
            candidates={"lzn": [{"role_id": "other", "role_name": "lzn3"}]},
            newest={"other": 9999.0},
        )
        self.assertIsNone(self.run_async(view._maybe_switch_role(None, "old", "lzn", "163", 1000.0)))

    def test_current_role_is_skipped(self):
        view = self.make(
            candidates={"lzn": [{"role_id": "old", "role_name": "lzn"}]},
            newest={"old": 9999.0},
        )
        self.assertIsNone(self.run_async(view._maybe_switch_role(None, "old", "lzn", "163", 1000.0)))


if __name__ == "__main__":
    unittest.main()
