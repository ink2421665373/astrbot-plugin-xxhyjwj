import ast
import asyncio
import importlib
import json
import logging
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace, MethodType
from contextlib import asynccontextmanager
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from diagnostics import DiagnosticStore, LIMIT

package = ModuleType("naraka_test_package")
package.__path__ = [str(ROOT)]
sys.modules.setdefault("naraka_test_package", package)
MappingViewMixin = importlib.import_module("naraka_test_package.mapping_views").MappingViewMixin


class DiagnosticTests(unittest.TestCase):
    def test_routes_use_the_existing_astrbot_web_api_registration(self):
        api = ModuleType("astrbot.api")
        api.web = SimpleNamespace()
        calls = []
        context = SimpleNamespace(register_web_api=lambda *args: calls.append(args))
        view = MappingViewMixin()
        with patch.dict(sys.modules, {"astrbot": ModuleType("astrbot"), "astrbot.api": api}):
            self.assertTrue(view._setup_mapping_view(context))
        routes = {args[0]: args[2] for args in calls}
        self.assertEqual(routes["/astrbot_plugin_naraka/diagnostics"], ["GET"])
        self.assertEqual(routes["/astrbot_plugin_naraka/diagnostics/clear"], ["POST"])
        self.assertEqual(view._diagnostics.snapshot()["records"], [])

    def test_missing_zero_and_unexpected_values_are_distinct(self):
        store = DiagnosticStore()
        store.append("detail", "12", "", dict(data=[dict(desc="振刀", value="0.0"),
                      dict(desc="总伤害", value=None), dict(desc="未识别字段", value={"token":"SECRET"})]))
        record = store.snapshot()["records"][0]
        values = {row["name"]: row for row in record["metrics"]}
        self.assertEqual(values["振刀"]["state"], "zero")
        self.assertEqual(values["总伤害"]["state"], "null")
        self.assertEqual(values["击杀"]["state"], "absent")
        self.assertEqual(values["未识别字段"]["state"], "not_numeric")
        self.assertNotIn("SECRET", json.dumps(record))
        self.assertEqual(record["fields"]["data"], "present")

    def test_only_allowlisted_data_is_exposed(self):
        store = DiagnosticStore()
        body = dict(cookies={"token":"COOKIE_SECRET"}, role_id="ROLE_SECRET", name="NAME_SECRET",
                    authorization="AUTH_SECRET", player_info={"name":"PLAYER_SECRET"},
                    seasons=[dict(key="pre-01", value="全部", selected=True, token="SEASON_SECRET"),
                             dict(key="qianji", value="千机", is_current="false")],
                    overview=[dict(desc="场均振刀", value=0), dict(desc="测试", value="Bearer VALUE_SECRET"),
                              dict(desc="access_token", value="METRIC_SECRET")])
        store.append("home", "12", "pre-01", body)
        data = store.snapshot()
        text = json.dumps(data, ensure_ascii=False)
        self.assertNotIn("SECRET", text)
        self.assertNotIn("access_token", text)
        row = data["records"][0]
        self.assertEqual(row["seasons"][0], dict(code="pre-01", name="全部", selected="true"))
        self.assertEqual(row["seasons"][1]["is_current"], "false")
        self.assertEqual(row["metric_count"], 3)
        self.assertTrue(data["memory_only"])
        self.assertTrue(row["captured_at"].endswith("+00:00"))
        data["records"][0]["metrics"].clear()
        self.assertTrue(store.snapshot()["records"][0]["metrics"])

    def test_bounds_clear_and_malformed_response(self):
        store = DiagnosticStore()
        for i in range(LIMIT + 5):
            store.append("home", "12", "pre-01", dict(seasons={}, overview=None))
        record = store.snapshot()["records"][0]
        self.assertEqual(len(store.snapshot()["records"]), LIMIT)
        self.assertEqual(record["fields"]["seasons"], "wrong_type")
        self.assertEqual(record["fields"]["overview"], "null")
        self.assertEqual(record["fields"]["player_info"], "missing")
        store.append("home", "12", "pre-01", dict(seasons=[dict(key=str(i), value=str(i)) for i in range(501)],
                    overview=[dict(desc=str(i), value=i) for i in range(201)]))
        record = store.snapshot()["records"][0]
        self.assertTrue(record["seasons_truncated"])
        self.assertTrue(record["metrics_truncated"])
        self.assertEqual(len(record["seasons"]), 500)
        store.clear()
        self.assertEqual(store.snapshot()["records"], [])
        store.append("detail", "12", "", [])
        self.assertEqual(store.snapshot()["records"][0]["response_state"], "not_object")

    def probe(self):
        view = MappingViewMixin()
        view._diagnostics = DiagnosticStore()
        view._mapping_web = SimpleNamespace(json_response=lambda value: value)
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8-sig"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "NarakaPlugin")
        methods = [n for n in cls.body if getattr(n, "name", "") in ("_home_data", "_fetch_detail")]
        env = dict(logger=logging.getLogger(__name__))
        exec(compile(ast.Module(body=methods, type_ignores=[]), "main.py", "exec"), env)
        for name in ("_home_data", "_fetch_detail"):
            setattr(view, name, MethodType(env[name], view))
        @asynccontextmanager
        async def paced(**kwargs):
            yield
        view._paced = paced
        view._remember_season_catalog = AsyncMock()
        return view

    def test_actual_query_hooks_capture_home_and_detail_without_extra_requests(self):
        view = self.probe()
        home = dict(seasons=[dict(key="pre-01", value="全部")], overview=[dict(desc="场均振刀", value=0)])
        detail = dict(data=[dict(desc="振刀", value=3)])
        view._api = SimpleNamespace(home_data=AsyncMock(return_value=home), match_detail=AsyncMock(return_value=detail))
        common = dict(role_id="ROLE_SECRET", server="163", heybox_id="AUTH_SECRET")
        self.assertIs(asyncio.run(view._home_data(None, common, "12", "pre-01")), home)
        self.assertIs(asyncio.run(view._fetch_detail(None, common, dict(match_id="MATCH_SECRET", battle_tid="12"))), detail)
        view._api.home_data.assert_awaited_once()
        view._api.match_detail.assert_awaited_once()
        snapshots = asyncio.run(view.view_diagnostics())
        self.assertEqual([r["kind"] for r in snapshots["records"]], ["detail", "home"])
        self.assertNotIn("SECRET", json.dumps(snapshots))
        self.assertEqual(snapshots["records"][0]["metrics"][0]["value"], 3)
        self.assertEqual(asyncio.run(view.view_clear_diagnostics())["records"], [])

    def test_request_failure_preserves_exception_and_redacts_message(self):
        view = self.probe()
        failure = TimeoutError("token=ERROR_SECRET")
        view._api = SimpleNamespace(home_data=AsyncMock(side_effect=failure))
        with self.assertRaises(TimeoutError) as caught:
            asyncio.run(view._home_data(None, dict(role_id="secret", server="163"), "12", "pre-01"))
        self.assertIs(caught.exception, failure)
        row = view._diagnostics.snapshot()["records"][0]
        self.assertEqual(row["status"], "error")
        self.assertEqual(row["error"], "TimeoutError")
        self.assertNotIn("ERROR_SECRET", json.dumps(row))

    def test_diagnostics_failure_does_not_break_query(self):
        view = self.probe()
        view._diagnostics = SimpleNamespace(append=lambda *args, **kwargs: 1 / 0)
        body = dict(seasons=[])
        view._api = SimpleNamespace(home_data=AsyncMock(return_value=body))
        self.assertIs(asyncio.run(view._home_data(None, dict(role_id="test", server="163"), "12", "pre-01")), body)

    def test_cache_clear_preserves_diagnostics_and_rejects_active_queries(self):
        view = self.probe()
        view._query_lock = asyncio.Lock()
        view._detail_cache = {("role", "match"): {"data": []}}
        view._diagnostics.append("home", "12", "pre-01", {})
        view._mapping_web.error_response = lambda message, status_code: (status_code, message)
        self.assertEqual(asyncio.run(view.view_diagnostics())["cache_entries"], 1)
        async def run():
            async with view._query_lock:
                self.assertEqual((await view.view_clear_cache())[0], 409)
                self.assertEqual(len(view._detail_cache), 1)
            return await view.view_clear_cache()
        result = asyncio.run(run())
        self.assertEqual(result, dict(cleared_count=1, cache_entries=0))
        self.assertEqual(len(view._diagnostics.snapshot()["records"]), 1)

    def test_visual_page_and_localization_are_packaged(self):
        for language in ("zh-CN", "en-US"):
            data = json.loads((ROOT / ".astrbot-plugin/i18n" / (language + ".json")).read_text(encoding="utf-8-sig"))
            for key in ("views", "pages"):
                self.assertIn("diagnostics", data[key])
        js = (ROOT / "pages/diagnostics/app.js").read_text(encoding="utf-8")
        self.assertIn('apiGet("diagnostics")', js)
        self.assertNotIn("innerHTML", js)
        self.assertNotIn("localStorage", js)


if __name__ == "__main__":
    unittest.main()
