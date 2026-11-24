"""Offline checks; execute query orchestration without importing AstrBot."""
import ast
import asyncio
import logging
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from unittest.mock import AsyncMock
from contextlib import asynccontextmanager

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from record_query import *
from mapping_config import merge_season_options
import importlib
from types import ModuleType
package = ModuleType("naraka_test_package")
package.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules.setdefault("naraka_test_package", package)
build_analysis_data = importlib.import_module("naraka_test_package.ai_analysis").build_analysis_data


class ApiError(Exception):
    pass


class ApiRateLimited(ApiError):
    pass


class ApiLoginRequired(ApiError):
    pass


class CurrentSeasonTests(unittest.TestCase):
    def test_normal_home_query_also_collects_the_catalog(self):
        tree = ast.parse(Path(__file__).resolve().parents[1].joinpath("main.py").read_text(encoding="utf-8-sig"))
        plugin = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "NarakaPlugin")
        method = next(node for node in plugin.body if getattr(node, "name", "") == "_home_data")
        env = dict(globals(), logger=logging.getLogger(__name__))
        exec(compile(ast.Module(body=[method], type_ignores=[]), "main.py", "exec"), env)
        @asynccontextmanager
        async def paced():
            yield
        async def diagnosed(kind, mode, season, request):
            return await request()
        catalog = [dict(key=f"s{i}", value=f"赛季{i}") for i in range(30)]
        body = dict(seasons=catalog, player_info=dict(rating=3200))
        fake = SimpleNamespace(_paced=paced, _api=SimpleNamespace(home_data=AsyncMock(return_value=body)),
                               _remember_season_catalog=AsyncMock(), _diagnosed_call=diagnosed)
        result = asyncio.run(env["_home_data"](fake, None, dict(role_id="test", server="163"), "12", "pre-01"))
        self.assertEqual(result, body)
        fake._remember_season_catalog.assert_awaited_once_with(catalog)
        fake._remember_season_catalog.side_effect = ValueError("磁盘不可写")
        result = asyncio.run(env["_home_data"](fake, None, dict(role_id="test", server="163"), "12", "pre-01"))
        self.assertEqual(result, body)

    def run_query(self, *, mode="12", rows=True, missing=False, fail=False, mismatch=False,
                  options=None, season_request=None, config=None, pre_body=None, detailed=False, pool_count=1, pool_rows=None,
                  force_refresh=False, detail_mode="全部关闭", cache=None, detail_failure=False, progress=None, for_ai=False,
                  pool_requests=None, respect_requested_mode=False, home_error=None, pool_error=None, on_pool=None):
        tree = ast.parse(Path(__file__).resolve().parents[1].joinpath("main.py").read_text(encoding="utf-8-sig"))
        functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in ("_choose_season", "_configured_season", "_split_lines", "_has_value", "_is_rate_limited", "_must_stop_after_error")]
        plugin = next(n for n in tree.body if isinstance(n, ast.ClassDef) and any(getattr(f, "name", "") == "_run_read" for f in n.body))
        functions.extend(f for f in plugin.body if getattr(f, "name", "") in ("_run_read", "_season_report"))
        env = dict(globals(), _ReadResult=lambda **kw: SimpleNamespace(**kw), DEFAULT_SERVER="163", HeyboxError=ApiError, AstrMessageEvent=object,
                   aiohttp=SimpleNamespace(ClientError=ApiError), HeyboxRateLimited=ApiRateLimited, HeyboxLoginRequired=ApiLoginRequired,
                   logger=logging.getLogger(__name__))
        exec(compile(ast.Module(body=functions, type_ignores=[]), "main.py", "exec"), env)
        calls = []

        @asynccontextmanager
        async def session():
            yield None

        async def resolve(*args):
            return dict(role_id="example", role_name="测试", server="163"), ""

        async def pool(*args, **kwargs):
            if pool_requests is not None:
                pool_requests.append(kwargs)
            error = pool_error.get(kwargs.get("season")) if isinstance(pool_error, dict) else pool_error
            if error is not None:
                raise error
            if on_pool is not None:
                on_pool()
            if pool_rows is not None:
                return pool_rows.get(kwargs.get("season"), []) if isinstance(pool_rows, dict) else pool_rows
            return [dict(match_id=str(i), battle_tid=kwargs.get("battle_tid", "12") if respect_requested_mode else "12",
                         hero_id="1000003", damage=100, kill_times=1) for i in range(1, pool_count + 1)] if rows else []

        async def home(session, common, battle_tid, season):
            calls.append((battle_tid, season))
            error = home_error.get(season) if isinstance(home_error, dict) else home_error
            if error is not None:
                raise error
            if season == "pre-01":
                return dict(seasons=options if options is not None else ([] if missing else [dict(key="s1", value="测试赛季", is_current=True)]), heroes=[], **(pre_body or {}))
            if fail and battle_tid == mode:
                raise ApiError("unavailable")
            return dict(battle_tid="4" if mismatch else battle_tid,
                        player_info=dict(rating=3200, level="测试段位", lv=80),
                        overview=[dict(desc="赛季场次", value=100), dict(desc="获胜次数", value=0)])

        async def detail(*args):
            calls.append(("detail", args[-1]["match_id"]))
            if detail_failure:
                raise ApiError("detail failed")
            return dict(data=[dict(desc="振刀", value=3), dict(desc="击杀", value=2)])
        async def refresh(*args):
            calls.append(("refresh", args[-2]))
        fake = SimpleNamespace(_detail_query_mode=lambda: detail_mode, _session=session, _resolve_player=resolve,
                               _auth={}, _refresh_enabled=lambda: False, _count_setting=lambda key, default: default,
                               _fetch_recent_pool=pool, _home_data=home, config=config or {},
                               _note=lambda event, text: text, _error_text=str,
                               _body=AsyncMock(side_effect=lambda event, text, **kwargs: text),
                               _detail_cache=cache if cache is not None else {}, _fetch_detail=detail, _refresh=refresh)
        if season_request is not None:
            async def collect():
                return [text async for text in env["_season_report"](fake, None, "测试", mode, season_request)]
            result = SimpleNamespace(ok=True, texts=asyncio.run(collect()))
        else:
            result = asyncio.run(env["_run_read"](fake, "测试", mode, detailed, force_refresh=force_refresh, progress=progress, for_ai=for_ai))
        return result, calls

    def test_force_refresh_bypasses_selected_cache_and_requests_update(self):
        cache = {("example", "1"): dict(data=[dict(desc="振刀", value=9)]), ("other", "1"): dict(data=[])}
        _, calls = self.run_query(cache=cache, detail_mode="全部开启")
        self.assertNotIn(("detail", "1"), calls)
        result, calls = self.run_query(cache=cache, detail_mode="全部开启", force_refresh=True)
        self.assertIn(("detail", "1"), calls)
        self.assertTrue(any(kind == "refresh" for kind, _ in calls))
        self.assertEqual(cache[("example", "1")]["data"][0]["value"], 3)
        self.assertIn(("other", "1"), cache)
        self.assertIn("场均振刀：3.0", "\n".join(result.texts))

    def test_failed_force_refresh_does_not_reuse_stale_detail(self):
        cache = {("example", "1"): dict(data=[dict(desc="振刀", value=9)])}
        result, _ = self.run_query(cache=cache, detail_mode="全部开启", force_refresh=True, detail_failure=True)
        self.assertNotIn(("example", "1"), cache)
        self.assertIn("场均振刀：—（0/1场有数据）", "\n".join(result.texts))

    def test_actual_detail_loop_reports_progress(self):
        messages = []
        async def progress(text, **kwargs):
            messages.append(text)
        self.run_query(detail_mode="全部开启", pool_count=3, progress=progress)
        self.assertTrue(any("搜索玩家" in text for text in messages))
        self.assertTrue(any("正在获取详情：0/3" in text for text in messages))
        self.assertTrue(any("3/3" in text for text in messages))
        self.assertTrue(any("赛季目录和概览" in text for text in messages))

    def test_ai_evidence_uses_existing_query_result_without_extra_home_requests(self):
        config = dict(season_mappings=[dict(season_id="pre-01", season_name="全部", is_current=True)])
        ordinary, normal_calls = self.run_query(detail_mode="全部开启", config=config)
        analyzed, ai_calls = self.run_query(detail_mode="全部开启", config=config, for_ai=True)
        self.assertEqual(normal_calls, ai_calls)
        self.assertEqual(ordinary.analysis_data, {})
        self.assertEqual(analyzed.analysis_data["mode"], "天选双排")
        self.assertEqual(analyzed.analysis_data["recent_metrics"]["parry"]["mean"], 3)
        self.assertEqual(analyzed.analysis_data["season"]["name"], "全部赛季")
        self.assertIn("场均伤害", analyzed.report_text)

    def test_ai_mode_filter_retains_historical_overview_and_actual_pool_count(self):
        rows = [dict(match_id=str(i), battle_tid="12", damage=100) for i in range(30)]
        config = dict(season_mappings=[dict(season_id="pre-01", season_name="全部", is_current=True)])
        body = dict(overview=[dict(desc="总场次", value=103), dict(desc="最高伤害", value="3.5w")])
        ordinary, normal_calls = self.run_query(mode="5", pool_rows=rows, config=config, pre_body=body)
        result, calls = self.run_query(mode="5", pool_rows=rows, config=config, pre_body=body, for_ai=True)
        self.assertTrue(result.ok)
        self.assertEqual(normal_calls, calls)
        self.assertEqual(result.report_text, ordinary.report_text)
        self.assertIn("总场次：103", result.report_text)
        self.assertIn("最高伤害：3.5w", result.report_text)
        self.assertIn("最近30场对局中没有选中天人三排", result.report_text)
        self.assertEqual(result.analysis_data["query_context"]["fetched"], 30)
        self.assertEqual(result.analysis_data["query_context"]["requested"], 30)
        self.assertFalse(result.analysis_data["query_context"]["detail_enabled"])
        self.assertEqual(result.analysis_data["selected"], 0)
        self.assertEqual(result.analysis_data["valid_records"], 0)
        self.assertEqual(result.analysis_data["matches"], [])
        self.assertIsNone(result.analysis_data["recent_metrics"]["damage"]["mean"])
        self.assertFalse(any(kind == "detail" for kind, _ in calls))

    def test_report_dates_use_only_selected_mode_and_capture_query_start(self):
        from datetime import datetime, timezone
        def stamp(day):
            return datetime(2026, 10, day, tzinfo=timezone.utc).timestamp()
        rows = [dict(match_id="1", battle_tid="5", time=stamp(1)),
                dict(match_id="2", battle_tid="12", time=stamp(20)),
                dict(match_id="3", battle_tid="12", time=stamp(22))]
        for detailed in (False, True):
            with patch("time.time", return_value=stamp(23)):
                result, _ = self.run_query(detailed=detailed, pool_rows=rows)
            text = "\n".join(result.texts)
            self.assertIn("查询时间（北京时间）：2026-10-23 08:00:00", text)
            self.assertIn("2026-10-20 08:00 至 2026-10-22 08:00（2/2场有有效时间）", text)
            self.assertNotIn("2026-10-01", text)
        result, _ = self.run_query(season_request="全部")
        self.assertIn("查询时间（北京时间）", "\n".join(result.texts))
        self.assertIn("数据更新时间：未知", "\n".join(result.texts))

    def test_image_details_are_not_split_before_rendering(self):
        mappings = [dict(hero_id="03", hero_name="测试英雄" * 15)]
        image_result, _ = self.run_query(detailed=True, pool_count=10, config=dict(result_display_format="图片", hero_mappings=mappings))
        text_result, _ = self.run_query(detailed=True, pool_count=10, config=dict(result_display_format="文字", hero_mappings=mappings))
        self.assertEqual(len(image_result.texts), 1)
        self.assertGreater(len(text_result.texts), 1)
        self.assertIn("第10局", image_result.texts[0])
        self.assertEqual(image_result.texts[0].count("｜英雄："), 10)

    def test_summary_image_keeps_overview_and_recent_stats_together(self):
        from report_image import render_report_images
        from PIL import ImageDraw, Image
        from io import BytesIO
        body = dict(overview=[dict(desc=f"统计项{i}", value=i) for i in range(80)])
        mappings = [dict(season_id="pre-01", season_name="全部", is_current=True)]
        result, _ = self.run_query(config=dict(result_display_format="图片", season_mappings=mappings), pre_body=body)
        self.assertEqual(len(result.texts), 1)
        self.assertIn("统计项79：79", result.texts[0])
        self.assertIn("最近1场场均伤害：100.0", result.texts[0])
        captured = []
        original = ImageDraw.ImageDraw.text
        def capture(draw, xy, text, *args, **kwargs):
            captured.append(str(text))
            return original(draw, xy, text, *args, **kwargs)
        with patch.object(ImageDraw.ImageDraw, "text", capture):
            images = render_report_images(result.texts[0])
        self.assertEqual(len(images), 1)
        with Image.open(BytesIO(images[0])) as image:
            self.assertGreater(image.height, 1600)
            image.load()
        for expected in ("统计项79", "最近1场场均伤害", "100.0"):
            self.assertTrue(any(expected in item for item in captured), expected)

    def choose(self, options, request, mappings=None):
        tree = ast.parse(Path(__file__).resolve().parents[1].joinpath("main.py").read_text(encoding="utf-8-sig"))
        function = next(n for n in tree.body if getattr(n, "name", "") == "_choose_season")
        env = dict(time=time, merge_season_options=merge_season_options)
        exec(compile(ast.Module(body=[function], type_ignores=[]), "main.py", "exec"), env)
        return env["_choose_season"](options, request, mappings)

    def test_manual_season_sends_configured_id_missing_from_upstream(self):
        config = {"season_mappings": [dict(season_id="real-code", season_name="吞天赛季", is_current=True)]}
        result, calls = self.run_query(options=[dict(key="old", value="旧赛季", current=True)], config=config)
        self.assertIn(("12", "real-code"), calls)
        self.assertNotIn(("12", "old"), calls)
        self.assertIn("吞天赛季（WebUI配置）", "\n".join(result.texts))
        self.assertIn("指定赛季概览", result.report_text)
        self.assertNotIn("当前赛季概览", result.report_text)

    def test_all_report_paths_use_only_the_saved_scope_for_list_and_home(self):
        config = dict(season_mappings=[dict(season_id="saved-code", season_name="选中赛季", is_current=True)])
        for detailed, for_ai in ((False, False), (True, False), (False, True), (True, True)):
            with self.subTest(detailed=detailed, for_ai=for_ai):
                requests = []
                result, calls = self.run_query(config=config, detailed=detailed, for_ai=for_ai, pool_requests=requests)
                self.assertEqual(requests, [dict(battle_tid="12", season="saved-code")])
                self.assertTrue(calls)
                self.assertTrue(all(code == "saved-code" for _, code in calls))
                self.assertEqual(calls.count(("12", "saved-code")), 1)
                self.assertIn("对局赛季范围：选中赛季（WebUI配置）", result.report_text)
                self.assertNotIn("未能确认当前赛季", result.report_text)
                if for_ai:
                    ai = importlib.import_module("naraka_test_package.ai_analysis")
                    payload = ai.analysis_prompt(result.analysis_data)
                    self.assertEqual(result.analysis_data["query_context"]["recent_season"], "选中赛季（WebUI配置）")
                    self.assertIn("对局列表按选中赛季（WebUI配置）请求", payload)
        result, calls = self.run_query(config=config, season_request="当前")
        self.assertEqual(calls, [("12", "saved-code")])
        self.assertIn("选中赛季（WebUI配置）", "\n".join(result.texts))

    def test_selected_scope_home_failure_reloads_all_inputs_before_details(self):
        config = dict(season_mappings=[dict(season_id="unsupported", season_name="所选赛季", is_current=True)],
                      result_display_format="图片")
        rows = {"unsupported": [dict(match_id="old", battle_tid="12", damage=999)],
                "pre-01": [dict(match_id="new", battle_tid="12", damage=100)]}
        body = dict(player_info=dict(rating=3796), overview=[dict(desc="总场次", value=103)])
        for detailed in (False, True):
            requests = []
            with self.subTest(detailed=detailed):
                result, calls = self.run_query(config=config, home_error={"unsupported": ApiError("unavailable")},
                    pool_requests=requests, pool_rows=rows, detailed=detailed, detail_mode="全部开启", pre_body=body, for_ai=True)
                self.assertEqual([item["season"] for item in requests], ["unsupported", "pre-01"])
                self.assertEqual(calls[0], ("12", "unsupported"))
                self.assertEqual(calls.count(("12", "pre-01")), 1)
                self.assertNotIn(("detail", "old"), calls)
                self.assertIn(("detail", "new"), calls)
                self.assertIn("本次已自动切换为全部赛季", result.report_text)
                self.assertIn("对局赛季范围：全部赛季", result.report_text)
                self.assertNotIn("对局赛季范围：所选赛季", result.report_text)
                self.assertEqual(result.analysis_data["query_context"]["recent_season"], "全部赛季")
                self.assertEqual(result.analysis_data["recent_metrics"]["damage"]["mean"], 100)
                self.assertEqual(result.analysis_data["season"]["name"], "全部赛季")
                self.assertIn("本次已自动切换", result.analysis_data["season"]["notice"])
                if not detailed:
                    self.assertIn("全部赛季概览｜全部赛季", result.report_text)
                    self.assertIn("分数：3796", result.report_text)
                    self.assertFalse(result.analysis_data["season"]["error"])
                self.assertEqual(config["season_mappings"][0]["season_id"], "unsupported")
                self.assertTrue(config["season_mappings"][0]["is_current"])

    def test_selected_list_failure_retries_all_once_and_marks_the_report(self):
        config = dict(season_mappings=[dict(season_id="chosen", season_name="所选赛季", is_current=True)])
        for error in (ApiError("unavailable"), asyncio.TimeoutError()):
            with self.subTest(error=type(error).__name__):
                requests = []
                result, calls = self.run_query(config=config, pool_requests=requests,
                    pool_error={"chosen": error}, pre_body=dict(player_info=dict(rating=3796)), for_ai=True)
                self.assertEqual([item["season"] for item in requests], ["chosen", "pre-01"])
                self.assertTrue(all(season == "pre-01" for _, season in calls))
                self.assertIn("全部赛季概览｜全部赛季", result.report_text)
                self.assertIn("本次已自动切换为全部赛季", result.report_text)
                self.assertIn("3796", result.report_text)

    def test_selected_response_with_wrong_mode_falls_back_without_relabelling_it(self):
        config = dict(season_mappings=[dict(season_id="chosen", season_name="所选赛季", is_current=True)])
        requests = []
        body = dict(battle_tid="12", player_info=dict(rating=3796))
        result, calls = self.run_query(config=config, pool_requests=requests, mismatch=True, pre_body=body, for_ai=True)
        self.assertEqual([item["season"] for item in requests], ["chosen", "pre-01"])
        self.assertEqual(calls[:2], [("12", "chosen"), ("12", "pre-01")])
        self.assertIn("全部赛季概览｜全部赛季｜天选双排", result.report_text)
        self.assertEqual(result.analysis_data["season"]["player_stats"]["rating"], 3796)
        self.assertIn("本次已自动切换为全部赛季", result.report_text)
        self.assertNotIn("分数：3200", result.report_text)

    def test_failed_all_season_fallback_is_bounded_and_reported(self):
        config = dict(season_mappings=[dict(season_id="chosen", season_name="所选赛季", is_current=True)])
        requests = []
        result, calls = self.run_query(config=config, pool_requests=requests, pool_error=ApiError("unavailable"))
        self.assertEqual([item["season"] for item in requests], ["chosen", "pre-01"])
        self.assertEqual(calls, [])
        self.assertFalse(result.ok)
        self.assertIn("全部赛季查询也失败", result.reason)
        self.assertIn("本次已自动切换为全部赛季", result.reason)
        requests = []
        result, calls = self.run_query(config=config, pool_requests=requests, home_error=ApiError("unavailable"), for_ai=True)
        self.assertEqual([item["season"] for item in requests], ["chosen", "pre-01"])
        self.assertEqual(calls, [("12", "chosen"), ("12", "pre-01")])
        self.assertIn("全部赛季概览｜全部赛季", result.report_text)
        self.assertTrue(result.analysis_data["season"]["error"])
        self.assertTrue(all(value is None for value in result.analysis_data["season"]["player_stats"].values()))

    def test_login_and_rate_limit_stop_instead_of_requesting_fallback(self):
        config = dict(season_mappings=[dict(season_id="chosen", season_name="所选赛季", is_current=True)])
        errors = (ApiRateLimited("limited"), ApiLoginRequired("expired"), ApiError("小黑盒要求降低请求频率"))
        for error in errors:
            with self.subTest(error=type(error).__name__):
                requests = []
                with self.assertRaises(ApiError):
                    self.run_query(config=config, pool_requests=requests, pool_error=error)
                self.assertEqual([item["season"] for item in requests], ["chosen"])
                requests = []
                result, calls = self.run_query(config=config, pool_requests=requests, home_error=error, detail_mode="全部开启")
                self.assertEqual([item["season"] for item in requests], ["chosen"])
                self.assertEqual(calls, [("12", "chosen")])
                self.assertIn("已停止后续查询", result.report_text)
                self.assertNotIn("自动切换", result.report_text)
                explicit, calls = self.run_query(config=config, season_request="当前", home_error=error)
                self.assertEqual(calls, [("12", "chosen")])
                self.assertNotIn("自动切换", "\n".join(explicit.texts))
                requests = []
                explicit, calls = self.run_query(config=config, mode=None, season_request="当前", pool_error=error, pool_requests=requests)
                self.assertEqual([item["season"] for item in requests], ["chosen"])
                self.assertEqual(calls, [])

    def test_season_command_falls_back_for_default_and_explicit_seasons(self):
        config = dict(season_mappings=[dict(season_id="chosen", season_name="所选赛季", is_current=True)])
        body = dict(player_info=dict(rating=3796), overview=[dict(desc="总场次", value=103)])
        result, calls = self.run_query(config=config, season_request="当前", home_error={"chosen": ApiError("unavailable")}, pre_body=body)
        self.assertEqual(calls, [("12", "chosen"), ("12", "pre-01")])
        text = "\n".join(result.texts)
        self.assertIn("全部赛季", text)
        self.assertIn("本次已自动切换", text)
        self.assertIn("3796", text)
        result, calls = self.run_query(config=config, season_request="所选赛季", home_error={"chosen": ApiError("unavailable")}, pre_body=body)
        self.assertEqual(calls, [("12", "pre-01"), ("12", "chosen")])
        self.assertIn("本次已自动切换为全部赛季", "\n".join(result.texts))
        requests = []
        result, calls = self.run_query(config=config, mode=None, season_request="当前", pool_requests=requests,
            pool_error={"chosen": ApiError("unavailable")}, pre_body=body)
        self.assertEqual([item["season"] for item in requests], ["chosen", "pre-01"])
        self.assertEqual(calls, [("12", "pre-01")])
        self.assertIn("本次已自动切换", "\n".join(result.texts))

    def test_already_all_seasons_is_never_retried_as_a_fallback(self):
        config = dict(season_mappings=[dict(season_id="pre-01", season_name="全部", is_current=True)])
        requests = []
        with self.assertRaises(ApiError):
            self.run_query(config=config, pool_requests=requests, pool_error=ApiError("unavailable"))
        self.assertEqual([item["season"] for item in requests], ["pre-01"])
        result, calls = self.run_query(config=config, home_error=ApiError("unavailable"))
        self.assertEqual(calls, [("12", "pre-01")])
        self.assertNotIn("自动切换", result.report_text)

    def test_query_uses_one_saved_scope_even_when_settings_change_during_fetch(self):
        config = dict(season_mappings=[dict(season_id="first", season_name="先选赛季", is_current=True)])
        def select_next():
            config["season_mappings"] = [dict(season_id="second", season_name="后选赛季", is_current=True)]
        requests = []
        result, calls = self.run_query(config=config, on_pool=select_next, pool_requests=requests)
        self.assertEqual(requests[0]["season"], "first")
        self.assertTrue(all(code == "first" for _, code in calls))
        self.assertIn("先选赛季（WebUI配置）", result.report_text)
        self.assertNotIn("后选赛季", result.report_text)
        result, calls = self.run_query(config=config)
        self.assertTrue(all(code == "second" for _, code in calls))
        self.assertIn("后选赛季（WebUI配置）", result.report_text)

    def test_empty_selected_season_does_not_switch_to_all_seasons(self):
        config = dict(season_mappings=[dict(season_id="empty", season_name="空赛季", is_current=True)])
        for detailed in (False, True):
            requests = []
            result, calls = self.run_query(config=config, detailed=detailed, rows=False, pool_requests=requests)
            self.assertEqual(requests, [dict(battle_tid="12", season="empty")])
            self.assertNotIn(("12", "pre-01"), calls)
            if detailed:
                self.assertFalse(result.ok)
                self.assertIn("空赛季（WebUI配置）查询到对局 0 场", result.reason)
            else:
                self.assertTrue(result.ok)
                self.assertIn("选中对局：0", result.report_text)
                self.assertIn("指定赛季概览｜空赛季（WebUI配置）", result.report_text)

    def test_invalid_saved_scope_is_not_replaced_by_automatic_detection(self):
        config = dict(season_mappings=[dict(season_id=code, season_name=code, is_current=True) for code in ("a", "b")])
        requests = []
        with self.assertRaisesRegex(ValueError, "最多只能指定一个"):
            self.run_query(config=config, pool_requests=requests)
        self.assertEqual(requests, [])

    def test_pre01_displays_all_seasons_in_queries_and_image(self):
        from PIL import ImageDraw
        from report_image import render_report_images
        mappings = [dict(season_id="pre-01", season_name="全部", is_current=True)]
        options = [dict(key="pre-01", value="全部"), dict(key="qianji", value="千机赛季", current=True)]
        for request in ("当前", "全部", "pre-01"):
            self.assertEqual(self.choose(options, request, mappings), ("pre-01", "全部赛季"))
        self.assertEqual(self.choose(options, "全部"), ("pre-01", "全部赛季"))
        config = dict(season_mappings=mappings)
        body = dict(player_info=dict(rating=3456), overview=[dict(desc="赛季场次", value=42)])
        for request in (None, "当前", "全部", "pre-01"):
            result, calls = self.run_query(options=options, config=config, pre_body=body, season_request=request)
            text = "\n".join(result.texts)
            self.assertIn("全部赛季", text)
            self.assertNotIn("当前赛季", text)
            self.assertIn("3456", text)
            self.assertIn("42", text)
            self.assertEqual(calls.count(("12", "pre-01")), 1)
            self.assertTrue(all(code == "pre-01" for _, code in calls))
        captured = []
        original = ImageDraw.ImageDraw.text
        def capture(draw, xy, text, *args, **kwargs):
            captured.append(str(text))
            return original(draw, xy, text, *args, **kwargs)
        with patch.object(ImageDraw.ImageDraw, "text", capture):
            self.assertTrue(render_report_images(text))
        self.assertTrue(any("全部赛季" in item for item in captured))
        self.assertFalse(any("当前赛季" in item for item in captured))

    def test_manual_season_name_and_code_can_be_queried_explicitly(self):
        mappings = [dict(season_id="real-code", season_name="吞天赛季", is_current=False)]
        self.assertEqual(self.choose([], "吞天", mappings), ("real-code", "吞天赛季"))
        self.assertEqual(self.choose([], "real-code", mappings), ("real-code", "吞天赛季"))

    def test_multiple_manual_current_settings_are_rejected(self):
        mappings = [dict(season_id=code, season_name=code, is_current=True) for code in ("a", "b")]
        with self.assertRaisesRegex(ValueError, "最多只能指定一个"):
            self.choose([], "当前", mappings)

    def test_manual_display_name_overrides_api_name(self):
        mappings = [dict(season_id="real-code", season_name="自定义名称", is_current=False)]
        self.assertEqual(self.choose([dict(key="real-code", value="接口名称")], "real-code", mappings), ("real-code", "自定义名称"))

    def test_default_selection_and_last_entry_are_not_current(self):
        options = [dict(key="old", value="旧赛季", selected=True), dict(key="later", value="另一赛季")]
        with self.assertRaisesRegex(ValueError, "未唯一标明"):
            self.choose(options, "当前")

    def test_false_string_is_not_current(self):
        options = [dict(key="old", value="旧赛季", is_current="false"),
                   dict(key="new", value="新赛季", current="1")]
        self.assertEqual(self.choose(options, "当前"), ("new", "新赛季"))

    def test_conflicting_current_flags_fail_visibly(self):
        with self.assertRaisesRegex(ValueError, "未唯一标明"):
            self.choose([dict(key="a", value="A", current=True), dict(key="b", value="B", is_current=True)], "当前")

    def test_ended_qianji_not_mislabeled_current(self):
        options = [dict(key="qj", value="千机赛季", current=True)]
        with patch("time.time", return_value=1791504001):
            with self.assertRaisesRegex(ValueError, "已于2026-10-09结束"):
                self.choose(options, "当前")
            self.assertEqual(self.choose(options, "千机"), ("qj", "千机赛季"))

    def test_missing_name_error_lists_all_seasons(self):
        options = [dict(key=f"s{i}", value=f"赛季{i}") for i in range(25)]
        with self.assertRaisesRegex(ValueError, "赛季24｜s24"):
            self.choose(options, "吞天")

    def test_requested_name_after_eighth_option_can_be_selected(self):
        options = [dict(key=f"s{i}", value=f"赛季{i}") for i in range(25)]
        options.append(dict(key="verified-key", value="吞天赛季"))
        self.assertEqual(self.choose(options, "吞天"), ("verified-key", "吞天赛季"))

    def test_missing_current_keeps_recent_stats_without_requesting_guessed_id(self):
        result, calls = self.run_query(options=[dict(key="qj", value="千机赛季", selected=True)])
        text = "\n".join(result.texts)
        self.assertIn("未能确认当前赛季", text)
        self.assertIn("全部赛季概览", text)
        self.assertIn("最近1场场均伤害：100.0", text)
        self.assertEqual(calls.count(("12", "pre-01")), 1)
        self.assertTrue(all(code == "pre-01" for _, code in calls))

    def test_listing_command_shows_full_catalog_without_fetching_stats(self):
        options = [dict(key=f"s{i}", value=f"赛季{i}", selected="false") for i in range(40)]
        result, calls = self.run_query(options=options, season_request="列表")
        self.assertIn("赛季39｜ID：s39", "\n".join(result.texts))
        self.assertTrue(all(len(text) <= 1200 for text in result.texts))
        self.assertEqual(calls, [("12", "pre-01")])

    def test_current_mode_overview_and_reused_request(self):
        result, calls = self.run_query()
        text = "\n".join(result.texts)
        self.assertIn("当前赛季概览｜测试赛季｜天选双排", text)
        self.assertIn("段位：测试段位", text)
        self.assertIn("赛季场次：100", text)
        self.assertIn("获胜次数：0", text)
        self.assertIn("近期对局汇总", text)
        self.assertEqual(calls.count(("12", "s1")), 1)
        self.assertEqual(calls[1], ("12", "s1"))

    def test_explicit_mode_without_recent_matches(self):
        result, _ = self.run_query(rows=False)
        self.assertTrue(result.ok)
        self.assertIn("赛季场次：100", "\n".join(result.texts))
        self.assertIn("有效对局：0", "\n".join(result.texts))

    def test_missing_season_never_labels_all_seasons_as_current(self):
        result, calls = self.run_query(missing=True)
        text = "\n".join(result.texts)
        self.assertIn("全部赛季概览", text)
        self.assertIn("未能确认当前赛季", text)
        self.assertNotIn("当前赛季概览", text)
        self.assertEqual(calls.count(("12", "pre-01")), 1)
        self.assertTrue(all(code == "pre-01" for _, code in calls))

    def test_failed_season_keeps_recent_summary(self):
        result, _ = self.run_query(fail=True)
        text = "\n".join(result.texts)
        self.assertIn("当前模式赛季数据获取失败", text)
        self.assertIn("最近1场场均伤害：100.0", text)

    def test_non_ranked_mode_also_gets_own_season(self):
        result, calls = self.run_query(mode="6")
        self.assertIn(("6", "s1"), calls)
        self.assertIn("当前赛季概览｜测试赛季｜快速单排", "\n".join(result.texts))

    def test_wrong_mode_not_displayed(self):
        result, _ = self.run_query(mismatch=True)
        text = "\n".join(result.texts)
        self.assertIn("接口返回模式与所选模式不一致", text)
        self.assertNotIn("赛季场次：100", text)

    def test_long_dynamic_overview_preserves_all_fields(self):
        text = format_summary("测试", 0, "12", 0, [], {}, {}, season_name="测试赛季",
                              season_body=dict(overview=[dict(desc=f"指标{i}", value=i) for i in range(200)]))
        self.assertIn("指标199：199", text)
        self.assertIn("指标0：0", text)


if __name__ == "__main__":
    unittest.main()
