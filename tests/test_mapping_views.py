import asyncio
import copy
import importlib
import json
import re
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from contextlib import asynccontextmanager
from packaging.specifiers import SpecifierSet

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mapping_config import collect_season_catalog, mapping_snapshot, validate_mappings

# Load the real mixin without loading AstrBot or main.py.
package = ModuleType("naraka_test_package")
package.__path__ = [str(ROOT)]
sys.modules.setdefault("naraka_test_package", package)
MappingViewMixin = importlib.import_module("naraka_test_package.mapping_views").MappingViewMixin


class FakeConfig(dict):
    def save_config(self, replacement=None):
        self.update(copy.deepcopy(replacement or {}))
        if self.fail:
            raise OSError("disk failure")
        self.path.write_text(json.dumps(self), encoding="utf-8")


class MappingViewTests(unittest.TestCase):
    def test_beta_and_stable_version_declarations(self):
        metadata = (ROOT / "metadata.yaml").read_text(encoding="utf-8-sig")
        requirement = re.search(r'astrbot_version: "([^"]+)"', metadata).group(1)
        spec = SpecifierSet(requirement)
        for version in ("4.29.0-beta.1", "4.29.0-beta.2", "4.29.0", "4.29.1"):
            self.assertTrue(spec.contains(version), version)
        self.assertFalse(spec.contains("4.28.0"))
        self.assertTrue((ROOT / "pages/mappings/index.html").is_file())
        self.assertFalse((ROOT / "views/mappings/index.html").exists())

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = FakeConfig(hero_mappings=[dict(__template_key="hero", hero_id="03", hero_name="宁红夜")],
                                 season_mappings=[], data_mode="heybox", unrelated="keep")
        self.config.path = Path(self.temp.name) / "config.json"
        self.config.fail = False
        self.view = MappingViewMixin()
        self.view.config = self.config
        self.view._mapping_view_lock = asyncio.Lock()
        self.view._query_lock = asyncio.Lock()
        self.view.data_mode = "heybox"
        self.view.logged_in = True
        self.view._reload_web_auth = lambda: "saved" if self.view.logged_in else "missing"
        self.view._auth = {}
        self.view._start_query_cooldown = lambda: 0
        self.view._error_text = str
        @asynccontextmanager
        async def session():
            yield None
        self.view._session = session
        self.view._resolve_player = AsyncMock(return_value=(dict(role_id="example", server="163"), ""))
        self.view._home_data = AsyncMock(return_value=dict(seasons=[dict(key=f"s{i}", value=f"赛季{i}") for i in range(35)]))
        self.payload = {}
        async def request_json(**kwargs):
            return copy.deepcopy(self.payload)
        self.view._mapping_web = SimpleNamespace(
            request=SimpleNamespace(json=request_json), json_response=lambda value: (200, value),
            error_response=lambda text, status_code=400: (status_code, text))

    def tearDown(self):
        self.temp.cleanup()

    def save(self, **updates):
        self.payload = dict(mapping_snapshot(self.config), **updates)
        return asyncio.run(self.view.view_save_mappings())

    def sync(self):
        self.payload = dict(nickname="测试玩家", mode="12", revision=mapping_snapshot(self.config)["revision"])
        return asyncio.run(self.view.view_sync_seasons())

    def test_catalog_sync_collects_every_option_and_persists(self):
        status, result = self.sync()
        self.assertEqual(status, 200)
        self.assertEqual(result["catalog_count"], 35)
        self.assertEqual(result["added_count"], 35)
        self.assertEqual(result["season_mappings"][-1]["season_id"], "s34")
        disk = json.loads(self.config.path.read_text(encoding="utf-8"))
        self.assertEqual(len(disk["season_mappings"]), 35)
        self.view._home_data.assert_awaited_once_with(None, dict(role_id="example", server="163", heybox_id=""), "12", "pre-01")

    def test_catalog_sync_preserves_custom_names_and_current_choice(self):
        self.config["season_mappings"] = [dict(season_id="s3", season_name="手动名称", is_current=True),
                                            dict(season_id="custom", season_name="手动编码", is_current=False)]
        status, result = self.sync()
        self.assertEqual(status, 200)
        self.assertEqual(result["added_count"], 34)
        chosen = next(item for item in result["season_mappings"] if item["season_id"] == "s3")
        self.assertEqual(chosen["season_name"], "手动名称")
        self.assertTrue(chosen["is_current"])
        self.assertTrue(any(item["season_id"] == "custom" for item in result["season_mappings"]))

    def test_catalog_sync_is_idempotent(self):
        self.assertEqual(self.sync()[1]["added_count"], 35)
        self.assertEqual(self.sync()[1]["added_count"], 0)
        self.assertEqual(len(self.config["season_mappings"]), 35)

    def test_catalog_sync_requires_login(self):
        self.view.logged_in = False
        status, message = self.sync()
        self.assertEqual(status, 400)
        self.assertIn("永劫登录", message)
        self.view._home_data.assert_not_awaited()

    def test_none_mode_is_not_misreported_as_missing_login(self):
        self.view.data_mode = "none"
        status, message = self.sync()
        self.assertEqual(status, 400)
        self.assertIn("数据模式为 none", message)
        self.assertIn("已有登录无需重复扫码", message)
        self.view._home_data.assert_not_awaited()

    def test_sync_reloads_login_before_rejecting_old_memory_state(self):
        self.view.logged_in = False
        def reload():
            self.view.logged_in = True
            return "saved"
        self.view._reload_web_auth = reload
        status, _ = self.sync()
        self.assertEqual(status, 200)
        self.view._home_data.assert_awaited_once()

    def test_status_endpoint_only_exposes_local_diagnostics(self):
        status, body = asyncio.run(self.view.view_login_status())
        self.assertEqual(status, 200)
        self.assertEqual(set(body), {"data_mode", "has_credentials", "credential_file_state", "online_verified"})
        self.assertFalse(body["online_verified"])
        self.assertTrue(body["has_credentials"])

    def test_catalog_sync_no_players_does_not_bind_or_modify(self):
        self.view._resolve_player.return_value = (None, "")
        status, message = self.sync()
        self.assertEqual(status, 400)
        self.assertIn("不会自动绑定", message)
        self.assertEqual(self.config["season_mappings"], [])
        self.view._home_data.assert_not_awaited()

    def test_empty_catalog_does_not_erase_existing_settings(self):
        self.config["season_mappings"] = [dict(season_id="old", season_name="旧配置", is_current=False)]
        self.view._home_data.return_value = {"seasons": []}
        before = copy.deepcopy(self.config["season_mappings"])
        status, _ = self.sync()
        self.assertEqual(status, 400)
        self.assertEqual(self.config["season_mappings"], before)

    def test_catalog_save_failure_rolls_back(self):
        self.config.fail = True
        status, message = self.sync()
        self.assertEqual(status, 400)
        self.assertIn("保存配置失败", message)
        self.assertEqual(self.config["season_mappings"], [])

    def test_catalog_deduplicates_and_does_not_infer_current(self):
        rows, result = collect_season_catalog([], [dict(key="pre-01", value="全部"),
                                                  dict(key="s1", value="赛季1", selected=True),
                                                  dict(key="s1", value="赛季1"), dict(key=0, value="赛季零")])
        self.assertEqual(len(rows), 3)
        self.assertEqual(result["catalog_count"], 3)
        self.assertFalse(any(item["is_current"] for item in rows))
        self.assertEqual(rows[-1]["season_id"], "0")

    def test_invalid_catalog_rows_are_reported_not_silently_hidden(self):
        rows, stats = collect_season_catalog([], [dict(key="s1", value="赛季1"), {}, None])
        self.assertEqual(len(rows), 1)
        self.assertEqual(stats["received_count"], 3)
        self.assertEqual(stats["skipped_count"], 2)

    def test_get_exposes_only_mapping_fields(self):
        status, body = asyncio.run(self.view.view_get_mappings())
        self.assertEqual(status, 200)
        self.assertEqual(set(body), {"hero_mappings", "season_mappings", "revision", "login_status"})

    def test_save_persists_and_preserves_unrelated_config(self):
        status, body = self.save(hero_mappings=[dict(hero_id="3", hero_name="新显示名")],
                                 season_mappings=[dict(season_id="verified-code", season_name="吞天", is_current=True)])
        self.assertEqual(status, 200)
        self.assertTrue(body["saved"])
        self.assertEqual(self.config["hero_mappings"][0]["hero_id"], "03")
        self.assertEqual(self.config["hero_mappings"][0]["hero_name"], "新显示名")
        disk = json.loads(self.config.path.read_text(encoding="utf-8"))
        self.assertEqual(disk["season_mappings"][0]["season_id"], "verified-code")
        self.assertEqual(disk["unrelated"], "keep")

    def test_save_failure_rolls_back_runtime_settings(self):
        before = copy.deepcopy(dict(self.config))
        self.config.fail = True
        status, _ = self.save(hero_mappings=[])
        self.assertEqual(status, 500)
        self.assertEqual(dict(self.config), before)

    def test_saved_webui_scope_reaches_next_query_and_survives_reload(self):
        fixture = importlib.import_module("test_current_season").CurrentSeasonTests()
        # An edited page payload has no effect before the save endpoint runs.
        selection = [dict(season_id="qianji", season_name="千机赛季", is_current=True)]
        self.payload = dict(mapping_snapshot(self.config), season_mappings=selection)
        self.assertEqual(self.config["season_mappings"], [])
        status, response = asyncio.run(self.view.view_save_mappings())
        self.assertEqual(status, 200)
        self.assertTrue(response["saved"])
        disk = json.loads(self.config.path.read_text(encoding="utf-8"))
        options = [dict(key="qianji", value="千机赛季", current=True)]
        for config in (self.config, disk):
            with self.subTest(reloaded=config is disk), patch("time.time", return_value=1793055600):
                result, calls = fixture.run_query(options=options, config=config)
                self.assertIn(("12", "qianji"), calls)
                self.assertIn("指定赛季概览｜千机赛季（WebUI配置）", result.report_text)
                self.assertNotIn("以下展示接口返回的全部赛季数据", result.report_text)
        # Clearing the saved manual flag restores the strict automatic guard.
        status, _ = self.save(season_mappings=[dict(selection[0], is_current=False)])
        self.assertEqual(status, 200)
        with patch("time.time", return_value=1793055600):
            result, calls = fixture.run_query(options=options, config=self.config)
        self.assertNotIn(("12", "qianji"), calls)
        self.assertIn("全部赛季概览", result.report_text)
        self.assertIn("未能确认当前赛季", result.report_text)
        with patch("time.time", return_value=1793055600), self.assertRaisesRegex(ValueError, "千机赛季标为当前"):
            fixture.choose(options, "当前", self.config["season_mappings"])

    def test_stale_editor_does_not_overwrite_newer_config(self):
        self.payload = mapping_snapshot(self.config)
        self.config["hero_mappings"][0]["hero_name"] = "其他页面修改"
        status, _ = asyncio.run(self.view.view_save_mappings())
        self.assertEqual(status, 409)
        self.assertEqual(self.config["hero_mappings"][0]["hero_name"], "其他页面修改")

    def test_invalid_request_is_rejected_without_changes(self):
        before = copy.deepcopy(dict(self.config))
        self.payload = []
        status, _ = asyncio.run(self.view.view_save_mappings())
        self.assertEqual(status, 400)
        self.assertEqual(dict(self.config), before)

    def test_validation_rejects_duplicate_and_multiple_current_codes(self):
        cases = [
            dict(hero_mappings=[dict(hero_id="03", hero_name="A"), dict(hero_id="3", hero_name="B")], season_mappings=[]),
            dict(hero_mappings=[], season_mappings=[dict(season_id="a", season_name="A", is_current=True), dict(season_id="b", season_name="B", is_current=True)]),
            dict(hero_mappings=[], season_mappings=[dict(season_id="a", season_name="A", is_current="false")]),
        ]
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                validate_mappings(payload)

    def test_pre01_current_choice_is_saved_and_preserved_during_sync(self):
        status, _ = self.save(season_mappings=[dict(season_id="pre-01", season_name="全部", is_current=True)])
        self.assertEqual(status, 200)
        disk = json.loads(self.config.path.read_text(encoding="utf-8"))
        self.assertTrue(disk["season_mappings"][0]["is_current"])
        rows, _ = collect_season_catalog(disk["season_mappings"], [dict(key="pre-01", value="全部")])
        self.assertTrue(rows[0]["is_current"])

    def test_schema_and_visual_view_share_config_fields(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(schema["season_mappings"]["type"], "template_list")
        self.assertEqual(schema["season_mappings"]["default"], [])
        self.assertEqual(schema["hero_mappings"]["templates"]["hero"]["display_item"], "hero_name")
        html = (ROOT / "pages/mappings/index.html").read_text(encoding="utf-8")
        js = (ROOT / "pages/mappings/app.js").read_text(encoding="utf-8")
        self.assertIn("英雄与赛季", html)
        self.assertIn("AstrBotPluginView", js)
        self.assertIn("AstrBotPluginPage", js)
        self.assertIn('apiPost("mappings/save"', js)
        self.assertNotIn("window.confirm", js)
        metadata = json.loads((ROOT / ".astrbot-plugin/i18n/zh-CN.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["views"]["mappings"]["title"], "英雄与赛季")
        self.assertEqual(metadata["pages"]["mappings"]["title"], "英雄与赛季")


if __name__ == "__main__":
    unittest.main()
