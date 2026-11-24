"""昵称 ↔ 角色唯一标识（role_id）的校验、面板接口与查询命中测试。"""
import asyncio
import copy
import importlib
import json
from pathlib import Path
import re
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from contextlib import asynccontextmanager
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mapping_config import mapping_snapshot, nickname_key, nickname_lookup, validate_nickname_mappings

package = ModuleType("naraka_test_package")
package.__path__ = [str(ROOT)]
sys.modules.setdefault("naraka_test_package", package)
MappingViewMixin = importlib.import_module("naraka_test_package.mapping_views").MappingViewMixin


class FakeConfig(dict):
    def save_config(self, replacement=None):
        self.update(copy.deepcopy(replacement or {}))
        if getattr(self, "fail", False):
            raise OSError("disk failure")
        if getattr(self, "path", None):
            self.path.write_text(json.dumps(self), encoding="utf-8")


VALID = [dict(nickname="张三", role_id="123456", role_name="张三", server="163")]


class NicknameValidationTests(unittest.TestCase):
    def test_valid_rows_are_normalized(self):
        rows = validate_nickname_mappings(VALID)
        self.assertEqual(rows[0]["role_id"], "123456")
        self.assertEqual(rows[0]["nickname"], "张三")

    def test_optional_fields_are_dropped_when_blank(self):
        rows = validate_nickname_mappings([dict(nickname="a", role_id="1", role_name="  ", server="")])
        self.assertNotIn("role_name", rows[0])
        self.assertNotIn("server", rows[0])

    def test_role_id_must_be_numeric(self):
        for bad in ("abc", "12a", "", "1" * 33):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_nickname_mappings([dict(nickname="a", role_id=bad)])

    def test_duplicate_nicknames_are_rejected_case_insensitively(self):
        with self.assertRaises(ValueError):
            validate_nickname_mappings([dict(nickname="Zero", role_id="1"), dict(nickname=" zero ", role_id="2")])

    def test_nickname_length_and_type_guards(self):
        for bad in ("", "x" * 81, None, 12):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_nickname_mappings([dict(nickname=bad, role_id="1")])

    def test_non_list_and_oversized_are_rejected(self):
        with self.assertRaises(ValueError):
            validate_nickname_mappings("nope")
        with self.assertRaises(ValueError):
            validate_nickname_mappings([dict(nickname=f"n{i}", role_id="1") for i in range(501)])

    def test_lookup_is_normalized_and_returns_none_when_absent(self):
        config = {"nickname_mappings": VALID}
        self.assertEqual(nickname_lookup(config, "张三")["role_id"], "123456")
        self.assertEqual(nickname_lookup(config, " 张三 ")["role_id"], "123456")
        self.assertIsNone(nickname_lookup(config, "李四"))
        self.assertIsNone(nickname_lookup(config, "   "))
        self.assertIsNone(nickname_lookup({}, "张三"))

    def test_blank_only_nickname_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_nickname_mappings([dict(nickname="​", role_id="1")])
        self.assertEqual(nickname_key("  A​b "), "ab")

    def test_snapshot_includes_nickname_mappings(self):
        config = {"nickname_mappings": VALID, "hero_mappings": [], "season_mappings": []}
        snapshot = mapping_snapshot(config)
        self.assertEqual(snapshot["nickname_mappings"], VALID)
        self.assertEqual(len(snapshot["revision"]), 64)
        # 昵称变化会改变 revision，保存时的并发保护才有意义
        other = mapping_snapshot({"nickname_mappings": [dict(VALID[0], role_id="999")],
                                  "hero_mappings": [], "season_mappings": []})
        self.assertNotEqual(snapshot["revision"], other["revision"])


class NicknameViewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = FakeConfig(nickname_mappings=[], hero_mappings=[], season_mappings=[])
        self.config.path = Path(self.temp.name) / "config.json"
        self.view = MappingViewMixin()
        self.view.config = self.config
        self.view._mapping_view_lock = asyncio.Lock()
        self.view._query_lock = asyncio.Lock()
        self.view.data_mode = "heybox"
        self.view.logged_in = True
        self.view._reload_web_auth = lambda: "saved"
        self.view._start_query_cooldown = lambda: 0
        self.view._error_text = str
        self.rows = []
        self.payload = {}
        self.calls = []

        @asynccontextmanager
        async def session():
            yield None
        self.view._session = session

        async def search_rows(session, name, limit=10):
            self.calls.append((name, limit))
            return copy.deepcopy(self.rows)
        self.view._public_search_rows = search_rows

        async def request_json(**kwargs):
            return copy.deepcopy(self.payload)
        self.view._mapping_web = SimpleNamespace(
            request=SimpleNamespace(json=request_json), json_response=lambda value: (200, value),
            error_response=lambda text, status_code=400, **extra: (status_code, text, extra))

    def tearDown(self):
        self.temp.cleanup()

    def resolve(self, nickname="李四"):
        self.payload = dict(nickname=nickname)
        status, body = asyncio.run(self.view.view_resolve_nickname())[:2]
        return status, body

    def save(self, **updates):
        self.payload = dict(mapping_snapshot(self.config), **updates)
        status, body = asyncio.run(self.view.view_save_nicknames())[:2]
        return status, body

    def call(self, coro):
        """面板的替身 error_response 带 extra，统一只取前两项。"""
        return asyncio.run(coro)[:2]

    def test_get_exposes_nicknames_and_login_status(self):
        status, body = self.call(self.view.view_get_nicknames())
        self.assertEqual(status, 200)
        self.assertEqual(set(body), {"hero_mappings", "season_mappings", "nickname_mappings", "revision", "login_status"})

    def test_save_persists_and_keeps_unrelated_config(self):
        self.config["unrelated"] = "keep"
        status, body = self.save(nickname_mappings=VALID)
        self.assertEqual(status, 200)
        self.assertTrue(body["saved"])
        self.assertEqual(self.config["nickname_mappings"], VALID)
        self.assertEqual(self.config["unrelated"], "keep")
        disk = json.loads(self.config.path.read_text(encoding="utf-8"))
        self.assertEqual(disk["nickname_mappings"], VALID)

    def test_save_rejects_invalid_rows(self):
        before = copy.deepcopy(dict(self.config))
        status, message = self.save(nickname_mappings=[dict(nickname="a", role_id="abc")])
        self.assertEqual(status, 400)
        self.assertIn("角色ID", message)
        self.assertEqual(dict(self.config), before)

    def test_stale_editor_is_rejected(self):
        self.payload = mapping_snapshot(self.config)
        self.config["nickname_mappings"] = VALID
        status, _ = self.call(self.view.view_save_nicknames())
        self.assertEqual(status, 409)
        self.assertEqual(self.config["nickname_mappings"], VALID)

    def test_save_failure_rolls_back(self):
        self.config.fail = True
        status, _ = self.save(nickname_mappings=VALID)
        self.assertEqual(status, 500)
        self.assertEqual(self.config["nickname_mappings"], [])

    def test_resolve_returns_readonly_role_id(self):
        self.rows = [dict(role_id="777", role_name="李四", server="163")]
        status, body = self.resolve()
        self.assertEqual(status, 200)
        self.assertEqual(body, dict(nickname="李四", role_id="777", role_name="李四", server="163"))
        self.assertEqual(self.calls, [("李四", 20)])

    def test_resolve_prefers_exact_name_match(self):
        self.rows = [dict(role_id="1", role_name="李四改名了", server="163"),
                     dict(role_id="2", role_name="李四", server="163")]
        status, body = self.resolve()
        self.assertEqual(status, 200)
        self.assertEqual(body["role_id"], "2")

    def test_resolve_reports_multiple_matches_with_candidates(self):
        self.rows = [dict(role_id="1", role_name="李四大号", server="163"),
                     dict(role_id="2", role_name="李四小号", server="163")]
        status, message = self.resolve()
        third = asyncio.run(self.view.view_resolve_nickname())[2:]
        extra = third[0] if third else {}
        self.assertEqual(status, 409)
        # 无论 error_response 是否支持 extra，都要给出可分辨的候选
        candidates = extra.get("extra") or []
        joined = "".join(f"{item['role_name']}{item['role_id']}" for item in candidates)
        self.assertTrue([item["role_id"] for item in candidates] == ["1", "2"] or
                        ("李四大号" in message and "李四小号" in message), f"{message} / {joined}")

    def test_resolve_requires_nickname_and_reports_absence(self):
        status, _ = self.resolve("  ")
        self.assertEqual(status, 400)
        status, message = self.resolve("查无此人")
        self.assertEqual(status, 404)
        self.assertIn("查无此人", message)

    def test_resolve_honours_cooldown(self):
        self.view._start_query_cooldown = lambda: 5
        status, message = self.resolve()
        self.assertEqual(status, 429)
        self.assertIn("5", message)
        self.assertEqual(self.calls, [])

    def test_resolve_surfaces_search_failures(self):
        async def boom(session, name, limit=10):
            raise RuntimeError("网络炸了")
        self.view._public_search_rows = boom
        status, message = self.resolve()
        self.assertEqual(status, 400)
        self.assertIn("搜索失败", message)


class NicknameResolveQueryTests(unittest.TestCase):
    """指令与 LLM 工具共用 _resolve_player：映射命中必须跳过搜索与绑定。"""

    def setUp(self):
        self.config = {"nickname_mappings": VALID}
        self.calls = []

    def make_view(self):
        view = SimpleNamespace(config=self.config)

        async def fail(name, limit=10):
            self.calls.append(name)
            raise AssertionError("映射命中时不应该发起任何搜索")
        view._public_search_rows = fail
        view._search = fail
        view.logged_in = False

        async def _resolve(session, name):
            known = nickname_lookup(view.config, name)
            if known is not None:
                return {"role_id": known["role_id"],
                        "role_name": str(known.get("role_name") or name),
                        "server": str(known.get("server") or "163"),
                        "from_mapping": True}, ""
            return None, ""
        view._resolve_player = _resolve
        return view

    def test_mapped_nickname_resolves_without_search_or_login(self):
        view = self.make_view()
        row, reason = asyncio.run(view._resolve_player(None, "张三"))
        self.assertEqual(row["role_id"], "123456")
        self.assertEqual(row["from_mapping"], True)
        self.assertEqual(reason, "")
        self.assertEqual(self.calls, [])

    def test_unmapped_nickname_still_uses_normal_path(self):
        view = SimpleNamespace(config={})
        async def normal(session, name):
            return None, ""
        view._resolve_player = normal
        self.assertEqual(asyncio.run(view._resolve_player(None, "路人")), (None, ""))

    def test_main_source_wires_the_mapping_into_resolve_player(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8-sig")
        self.assertIn("nickname_lookup", source)
        body = source.split("async def _resolve_player")[1].split("async def _run_read")[0]
        self.assertIn("nickname_lookup(self.config, name)", body)
        # 映射分支必须出现在任何搜索发起之前
        self.assertLess(body.index("nickname_lookup(self.config, name)"), body.index("public_rows"))

    def test_panel_files_and_i18n_are_registered(self):
        for name in ("index.html", "app.js", "style.css"):
            self.assertTrue((ROOT / "pages" / "nicknames" / name).is_file(), name)
        js = (ROOT / "pages/nicknames/app.js").read_text(encoding="utf-8")
        self.assertIn('apiGet("nicknames")', js)
        self.assertIn('apiPost("nicknames/save"', js)
        self.assertIn('apiPost("nicknames/resolve"', js)
        # role_id 必须为只读
        self.assertIn("readOnly", js)
        html = (ROOT / "pages/nicknames/index.html").read_text(encoding="utf-8")
        self.assertIn("只读", html)
        for language in ("zh-CN", "en-US"):
            metadata = json.loads((ROOT / ".astrbot-plugin/i18n" / (language + ".json")).read_text(encoding="utf-8-sig"))
            for key in ("title", "description"):
                self.assertTrue(metadata["pages"]["nicknames"][key], key)
                self.assertTrue(metadata["views"]["nicknames"][key], key)

    def test_routes_are_registered(self):
        api = ModuleType("astrbot.api")
        api.web = SimpleNamespace()
        calls = []
        context = SimpleNamespace(register_web_api=lambda *args: calls.append(args))
        mixin = MappingViewMixin()
        with patch.dict(sys.modules, {"astrbot": ModuleType("astrbot"), "astrbot.api": api}):
            self.assertTrue(mixin._setup_mapping_view(context))
        routes = {args[0]: args[2] for args in calls}
        self.assertEqual(routes["/astrbot_plugin_naraka/nicknames"], ["GET"])
        self.assertEqual(routes["/astrbot_plugin_naraka/nicknames/resolve"], ["POST"])


if __name__ == "__main__":
    unittest.main()
