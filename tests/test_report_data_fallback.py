"""Regression for stale season catalogs combined with an empty selected mode."""
import ast
import asyncio
from contextlib import asynccontextmanager
import importlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from record_query import match_mode_counts, recent_rows, select_matches
from report_image import render_report_images


class ReportDataFallbackTests(unittest.TestCase):
    def fixture(self):
        return importlib.import_module("test_current_season").CurrentSeasonTests()

    def test_screenshot_case_retains_all_season_data_and_discloses_recent_modes(self):
        fixture = self.fixture()
        rows = [dict(match_id=str(i), battle_tid="12") for i in range(30)]
        body = dict(battle_tid="5000001", player_info=dict(rating=3796, level="蚀月III", lv=616),
                    overview=[dict(desc="总场次", value=103), dict(desc="场均伤害", value=11357)])
        options = [dict(key="qianji", value="千机赛季", current=True)]
        requests = []
        with patch("time.time", return_value=1793055600):
            result, calls = fixture.run_query(mode="天选三排", pool_rows=rows, pre_body=body, options=options,
                                             pool_requests=requests, for_ai=True)
        text = result.report_text
        for value in ("当前赛季概览｜千机赛季｜天选三排", "分数：3200", "选中对局：0",
                      "本次列表模式分布为 天选双排30场"):
            self.assertIn(value, text)
        self.assertNotIn("未能确认当前赛季", text)
        self.assertNotIn("赛季未知", text)
        self.assertEqual(requests, [dict(battle_tid="5000001", season="pre-01")])
        self.assertEqual(calls.count(("5000001", "pre-01")), 1)
        self.assertIn(("5000001", "qianji"), calls)
        self.assertEqual(result.analysis_data["season"]["name"], "千机赛季")
        self.assertEqual(result.analysis_data["season"]["error"], "")
        self.assertEqual(result.analysis_data["season"]["notice"], "")
        self.assertEqual(result.analysis_data["query_context"]["mode_counts"], {"天选双排": 30})
        ai = importlib.import_module("naraka_test_package.ai_analysis")
        payload = json.loads(ai.analysis_prompt(result.analysis_data).split("\n")[-1])
        self.assertTrue(ai.has_season_data(result.analysis_data))
        self.assertIn("仅依据千机赛季概览", payload["analysis_scope"])
        self.assertEqual(payload["selected"], 0)

    def test_fallback_image_keeps_stats_and_labels_without_claiming_current_score(self):
        from PIL import ImageDraw
        result, _ = self.fixture().run_query(mode="5000001", rows=False, missing=True,
            pre_body=dict(player_info=dict(rating=3796), overview=[dict(desc="场均伤害", value=11357)]),
            config=dict(result_display_format="图片"))
        captured = []
        original = ImageDraw.ImageDraw.text
        def capture(draw, xy, value, *args, **kwargs):
            captured.append(str(value))
            return original(draw, xy, value, *args, **kwargs)
        with patch.object(ImageDraw.ImageDraw, "text", capture):
            pages = render_report_images(result.texts[0])
        self.assertEqual(len(pages), 1)
        drawn = "".join(captured)
        for expected in ("全部赛季概览", "11357", "3796"):
            self.assertIn(expected, drawn)
        self.assertNotIn("当前赛季概览", drawn)
        self.assertNotIn("当前分数", drawn)

    def test_all_explicit_mode_aliases_are_sent_to_the_list_request(self):
        for mode, expected in (("天选单排", "5000000"), ("双排", "12"), ("三排", "5000001"),
                               ("天人单排", "4"), ("天人双排", "13"), ("天人三排", "5")):
            requests = []
            result, _ = self.fixture().run_query(mode=mode, pool_requests=requests, respect_requested_mode=True)
            self.assertEqual(requests, [dict(battle_tid=expected, season="pre-01")])
            self.assertIn("选中对局：1", result.report_text)

    def test_auto_mode_keeps_legacy_request_but_selects_from_actual_returned_rows(self):
        requests = []
        result, _ = self.fixture().run_query(mode="", pool_requests=requests,
            pool_rows=[dict(match_id="1", battle_tid="5", damage=100)])
        self.assertEqual(requests, [dict(battle_tid="12", season="pre-01")])
        self.assertIn("当前模式：天人三排", result.report_text)
        self.assertIn("选中对局：1", result.report_text)

    def test_fetch_recent_pool_passes_the_chosen_mode_and_season_on_every_page(self):
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8-sig"))
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "NarakaPlugin")
        method = next(node for node in cls.body if getattr(node, "name", "") == "_fetch_recent_pool")
        env = dict(recent_rows=recent_rows)
        exec(compile(ast.Module(body=[method], type_ignores=[]), "main.py", "exec"), env)
        @asynccontextmanager
        async def paced():
            yield
        rows = [dict(match_id=str(i), battle_tid="5000001", time=i) for i in range(31)]
        client = SimpleNamespace(match_list=AsyncMock(side_effect=[dict(match_list=rows[:30]), dict(match_list=rows[30:])]))
        fake = SimpleNamespace(_api=client, _paced=paced, _pagination_mode=lambda: "offset")
        result = asyncio.run(env["_fetch_recent_pool"](fake, None, dict(role_id="example", server="163"), 31, battle_tid="5000001", season="saved-code"))
        self.assertEqual(len(result), 31)
        self.assertEqual([call.kwargs["battle_tid"] for call in client.match_list.call_args_list], ["5000001", "5000001"])
        self.assertEqual([call.kwargs["season"] for call in client.match_list.call_args_list], ["saved-code", "saved-code"])
        self.assertEqual([call.kwargs["offset"] for call in client.match_list.call_args_list], [0, 30])

    def test_mixed_response_is_filtered_and_unknown_modes_are_not_guessed(self):
        rows = [dict(battle_tid="12"), dict(battle_tid="12"), dict(battle_tid="9999"), dict(battle_tid="not-a-mode")]
        self.assertEqual(match_mode_counts(rows), {"天选双排": 2, "未识别模式（编码9999）": 1, "未标明模式": 1})
        mode, selected = select_matches(rows, "三排")
        self.assertEqual(mode, "5000001")
        self.assertEqual(selected, [])

    def test_network_failure_is_not_relabelled_as_available_all_season_data(self):
        module = importlib.import_module("test_current_season")
        result, calls = module.CurrentSeasonTests().run_query(home_error=module.ApiError("offline"), for_ai=True)
        self.assertNotIn("全部赛季概览", result.report_text)
        self.assertNotIn("以下展示接口返回的全部赛季数据", result.report_text)
        self.assertTrue(result.analysis_data["season"]["error"])
        self.assertEqual(calls, [("12", "pre-01")])

    def test_selected_scope_fallback_notice_and_stats_reach_image_and_model(self):
        from PIL import ImageDraw
        module = importlib.import_module("test_current_season")
        config = dict(season_mappings=[dict(season_id="selected", season_name="所选赛季", is_current=True)],
                      result_display_format="图片")
        for detailed in (False, True):
            result, _ = self.fixture().run_query(config=config, detailed=detailed, for_ai=True,
                home_error={"selected": module.ApiError("unavailable")},
                pre_body=dict(player_info=dict(rating=3796), overview=[dict(desc="场均伤害", value=11357)]))
            ai = importlib.import_module("naraka_test_package.ai_analysis")
            payload = json.loads(ai.analysis_prompt(result.analysis_data).split("\n")[-1])
            self.assertEqual(payload["query_context"]["recent_season"], "全部赛季")
            self.assertEqual(payload["season"]["name"], "全部赛季")
            self.assertIn("本次已自动切换为全部赛季", payload["analysis_scope"])
            captured = []
            original = ImageDraw.ImageDraw.text
            def capture(draw, xy, value, *args, **kwargs):
                captured.append(str(value))
                return original(draw, xy, value, *args, **kwargs)
            with patch.object(ImageDraw.ImageDraw, "text", capture):
                pages = render_report_images(result.report_text + "\nAI战绩分析\n本次评价依据全部赛季数据。")
            self.assertEqual(len(pages), 1)
            drawn = "".join(captured)
            self.assertIn("本次已自动切换为全部赛季", drawn)
            self.assertIn("对局赛季范围：全部赛季", drawn)
            self.assertIn("AI战绩分析", drawn)
            if detailed:
                self.assertLess(result.report_text.index("本次已自动切换"), result.report_text.index("第1局"))
            else:
                self.assertIn("11357", drawn)
                self.assertIn("全部赛季概览", drawn)

    def test_auto_current_stays_strict_but_saved_manual_scope_wins(self):
        fixture = self.fixture()
        options = [dict(key="qianji", value="千机赛季", current=True)]
        with patch("time.time", return_value=1793055600):
            with self.assertRaises(ValueError):
                fixture.choose(options, "当前")
            self.assertEqual(fixture.choose(options, "当前", [dict(season_id="qianji", season_name="千机赛季", is_current=True)]),
                             ("qianji", "千机赛季（WebUI配置）"))
        self.assertEqual(fixture.choose(options, "当前", [dict(season_id="pre-01", season_name="全部", is_current=True)]),
                         ("pre-01", "全部赛季"))

    def test_manual_ended_season_keeps_its_stats_scope_and_single_image(self):
        from PIL import ImageDraw
        options = [dict(key="qianji", value="千机赛季", current=True)]
        config = dict(season_mappings=[dict(season_id="qianji", season_name="千机赛季", is_current=True)],
                      result_display_format="图片")
        with patch("time.time", return_value=1793055600):
            result, calls = self.fixture().run_query(options=options, config=config, for_ai=True)
            explicit, explicit_calls = self.fixture().run_query(options=options, config=config, season_request="当前")
        self.assertIn(("12", "qianji"), calls)
        self.assertIn(("12", "qianji"), explicit_calls)
        self.assertIn("千机赛季（WebUI配置）", "\n".join(explicit.texts))
        self.assertIn("指定赛季概览｜千机赛季（WebUI配置）", result.report_text)
        self.assertIn("赛季场次：100", result.report_text)
        self.assertNotIn("当前赛季概览", result.report_text)
        self.assertNotIn("以下展示接口返回的全部赛季数据", result.report_text)
        ai = importlib.import_module("naraka_test_package.ai_analysis")
        payload = json.loads(ai.analysis_prompt(result.analysis_data).split("\n")[-1])
        self.assertEqual(payload["season"]["name"], "千机赛季（WebUI配置）")
        self.assertFalse(payload["season"]["error"])
        self.assertFalse(payload["season"]["notice"])
        self.assertEqual(payload["season"]["player_stats"]["rating"], 3200)
        captured = []
        original = ImageDraw.ImageDraw.text
        def capture(draw, xy, value, *args, **kwargs):
            captured.append(str(value))
            return original(draw, xy, value, *args, **kwargs)
        with patch.object(ImageDraw.ImageDraw, "text", capture):
            pages = render_report_images(result.report_text + "\nAI战绩分析\n仅评价指定赛季和本次近期样本。")
        self.assertEqual(len(pages), 1)
        drawn = "".join(captured)
        for value in ("指定赛季概览", "千机赛季（WebUI配置）", "3200", "100", "近期对局汇总", "AI战绩分析"):
            self.assertIn(value, drawn)
        self.assertNotIn("当前赛季概览", drawn)


if __name__ == "__main__":
    unittest.main()
