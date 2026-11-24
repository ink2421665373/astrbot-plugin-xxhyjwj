import asyncio
import ast
from contextlib import asynccontextmanager
import importlib
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
package = ModuleType("naraka_test_package")
package.__path__ = [str(ROOT)]
sys.modules.setdefault("naraka_test_package", package)
ai = importlib.import_module("naraka_test_package.ai_analysis")
from delivery import QueryProgress


class AIAnalysisTests(unittest.TestCase):
    def data(self, count=3):
        records = [dict(hero_id="1000003", damage=100, parry=0, kills=2,
                        role_id="ROLE_SECRET", cookies="COOKIE_SECRET", weapons=["长剑60%"]) for _ in range(count)]
        records.append(dict(hero_id="1000003", parry=None, damage=None, kills=None))
        return ai.build_analysis_data("12", count + 2, records, {"1000003":"宁红夜"},
            season_name="全部赛季", season_body=dict(player_info=dict(rating=3796, name="PRIVATE_NAME"),
                overview=[dict(desc="场均伤害", value=11357)]), queried_at=1792717200)

    def view(self, **settings):
        view = ai.AIAnalysisMixin()
        view.config = dict(ai_provider_id="chat-model", ai_persona_id="coach", **settings)
        view._ai_lock = asyncio.Lock()
        view._number_setting = lambda key, default, *limits: view.config.get(key, default)
        manager = SimpleNamespace(get_persona=AsyncMock(return_value=SimpleNamespace(system_prompt="用教练的语气回答")),
            get_persona_v3_by_id=lambda name: dict(prompt="默认人格") if name == "default" else None,
            resolve_selected_persona=AsyncMock(return_value=("group-persona", dict(prompt="群聊人格"), None, False)))
        context = SimpleNamespace(persona_manager=manager,
            conversation_manager=SimpleNamespace(get_curr_conversation_id=AsyncMock(return_value="cid"),
                get_conversation=AsyncMock(return_value=SimpleNamespace(persona_id="conversation-persona", history="PRIVATE_HISTORY"))),
            get_all_providers=lambda: [SimpleNamespace(meta=lambda: SimpleNamespace(id="chat-model"))],
            get_current_chat_provider_id=AsyncMock(return_value="session-model"),
            llm_generate=AsyncMock(return_value=SimpleNamespace(completion_text="有证据的战绩评价")))
        view._ai_context = context
        return view

    def event(self):
        return SimpleNamespace(unified_msg_origin="aiocqhttp:GroupMessage:EXAMPLE", get_platform_name=lambda: "aiocqhttp")

    def report(self):
        return SimpleNamespace(nickname="示例玩家", role_id="ROLE_SECRET", report_text="原始完整战绩",
                               texts=["原始完整战绩"], analysis_data=self.data())

    def test_evidence_keeps_zero_and_coverage_without_credentials(self):
        data = self.data()
        data["cookies"] = "TOP_SECRET"
        data["season"]["cookies"] = "NESTED_SECRET"
        data["matches"][0]["token"] = "MATCH_SECRET"
        self.assertEqual(data["recent_metrics"]["parry"], dict(mean=0, available=3, selected=5))
        self.assertEqual(data["season"]["player_stats"]["rating"], 3796)
        text = ai.analysis_prompt(data)
        self.assertNotIn("SECRET", text)
        self.assertNotIn("PRIVATE_NAME", text)
        self.assertIn("全部赛季", text)
        parsed = json.loads(text.split("\n")[-1])
        self.assertIsNone(parsed["data_updated_at"])
        self.assertEqual(parsed["recent_metrics"]["parry"]["available"], 3)

    def test_prompt_budget_removes_details_without_changing_aggregates(self):
        data = self.data(50)
        prompt = ai.analysis_prompt(data, max_matches=50, max_chars=4500)
        parsed = json.loads(prompt.split("\n")[-1])
        self.assertLess(len(parsed["matches"]), 50)
        self.assertEqual(parsed["match_details_provided"], len(parsed["matches"]))
        self.assertEqual(parsed["recent_metrics"]["damage"]["available"], 50)
        self.assertEqual(len(data["matches"]), 50)

    def test_selected_model_and_persona_are_passed_without_tools_or_history(self):
        view = self.view()
        result = asyncio.run(view._evaluate_records(self.event(), self.data()))
        self.assertEqual(result, "有证据的战绩评价")
        kwargs = view._ai_context.llm_generate.call_args.kwargs
        self.assertEqual(kwargs["chat_provider_id"], "chat-model")
        self.assertIn("用教练的语气回答", kwargs["system_prompt"])
        self.assertIn("null和缺失不是0", kwargs["system_prompt"])
        self.assertIn("pre-01", kwargs["system_prompt"])
        self.assertEqual(kwargs["contexts"], [])
        self.assertIsNone(kwargs["tools"])
        view._ai_context.conversation_manager.get_conversation.assert_not_awaited()
        self.assertNotIn("PRIVATE_HISTORY", str(kwargs))

    def test_empty_selection_follows_current_session_model_and_persona(self):
        view = self.view()
        view.config.update(ai_provider_id="", ai_persona_id="")
        asyncio.run(view._evaluate_records(self.event(), self.data()))
        kwargs = view._ai_context.llm_generate.call_args.kwargs
        self.assertEqual(kwargs["chat_provider_id"], "session-model")
        self.assertIn("群聊人格", kwargs["system_prompt"])
        self.assertNotIn("PRIVATE_HISTORY", str(kwargs))
        view._ai_context.persona_manager.resolve_selected_persona.assert_awaited_once_with(
            umo=self.event().unified_msg_origin, conversation_persona_id="conversation-persona", platform_name="aiocqhttp")

    def test_none_persona_is_respected(self):
        view = self.view()
        view.config["ai_persona_id"] = "[%None]"
        asyncio.run(view._evaluate_records(self.event(), self.data()))
        view._ai_context.persona_manager.get_persona.assert_not_awaited()
        self.assertNotIn("用教练的语气回答", view._ai_context.llm_generate.call_args.kwargs["system_prompt"])

    def test_removed_model_or_persona_does_not_fall_back_silently(self):
        view = self.view()
        view.config["ai_provider_id"] = "removed"
        with self.assertRaisesRegex(ai.AnalysisError, "重新选择"):
            asyncio.run(view._evaluate_records(self.event(), self.data()))
        view._ai_context.llm_generate.assert_not_awaited()
        view.config["ai_provider_id"] = "chat-model"
        view._ai_context.persona_manager.get_persona.side_effect = ValueError("PRIVATE_CONFIG")
        with self.assertRaises(ai.AnalysisError) as result:
            asyncio.run(view._evaluate_records(self.event(), self.data()))
        self.assertNotIn("PRIVATE_CONFIG", str(result.exception))
        view._ai_context.llm_generate.assert_not_awaited()

    def test_missing_data_never_calls_llm(self):
        view = self.view()
        with self.assertRaisesRegex(ai.AnalysisError, "未取得可分析"):
            asyncio.run(view._evaluate_records(self.event(), dict(valid_records=0)))
        view._ai_context.llm_generate.assert_not_awaited()

    def test_season_overview_can_be_analyzed_without_recent_matches(self):
        view = self.view()
        data = ai.build_analysis_data("12", 0, [], {}, season_name="全部赛季", season_body=dict(
            overview=[dict(desc="总场次", value=100), dict(desc="场均伤害", value=11357)]))
        self.assertEqual(asyncio.run(view._evaluate_records(self.event(), data)), "有证据的战绩评价")
        prompt = json.loads(view._ai_context.llm_generate.call_args.kwargs["prompt"].split("\n")[-1])
        self.assertEqual(prompt["valid_records"], 0)
        self.assertIsNone(prompt["recent_metrics"]["damage"]["mean"])
        self.assertEqual(prompt["season"]["name"], "全部赛季")
        self.assertIn("仅依据全部赛季概览", prompt["analysis_scope"])

    def test_empty_recent_window_keeps_overview_in_text_and_single_image(self):
        from record_query import format_summary
        from report_image import render_report_images
        from PIL import ImageDraw
        body = dict(overview=[dict(desc="总场次", value=103), dict(desc="最高伤害", value="3.5w")])
        data = ai.build_analysis_data("5", 0, [], {}, season_name="全部赛季", season_body=body,
                                     fetched=30, requested=30, detail_enabled=True)
        original_report = format_summary("示例玩家", 30, "5", 0, [], {}, {}, season_name="全部赛季", season_body=body)
        for display in ("文字", "图片"):
            view = self.view(result_display_format=display)
            report = SimpleNamespace(report_text=original_report, texts=[original_report], analysis_data=data)
            asyncio.run(view._prepare_ai_report(self.event(), report))
            text = "".join(report.texts)
            for expected in ("总场次：103", "最高伤害：3.5w", "查询最近场次：30", "选中对局：0",
                             "未选中天人三排对局", "仅依据全部赛季概览", "有证据的战绩评价"):
                self.assertIn(expected, text)
            kwargs = view._ai_context.llm_generate.call_args.kwargs
            payload = json.loads(kwargs["prompt"].split("\n")[-1])
            self.assertEqual(payload["query_context"], dict(fetched=30, requested=30, detail_enabled=True, stopped=False, mode_counts={}, recent_season=""))
            self.assertEqual(payload["selected"], 0)
            self.assertEqual(payload["season"]["overview"][0]["value"], 103)
            self.assertIn("不得把字段缺失", kwargs["system_prompt"])
            self.assertIn("用教练的语气回答", kwargs["system_prompt"])
            if display == "图片":
                captured = []
                original = ImageDraw.ImageDraw.text
                def capture(draw, xy, value, *args, **kwargs):
                    captured.append(str(value))
                    return original(draw, xy, value, *args, **kwargs)
                with patch.object(ImageDraw.ImageDraw, "text", capture):
                    images = render_report_images(text)
                self.assertEqual(len(images), 1)
                for expected in ("103", "3.5w", "仅依据全部赛季概览", "未选中天人三排对局"):
                    self.assertIn(expected, "".join(captured))

    def test_scope_distinguishes_empty_pool_failed_details_and_list_only(self):
        empty = ai.build_analysis_data("5", 0, [], {}, fetched=0, requested=30)
        missing_mode = ai.build_analysis_data("5", 0, [], {}, fetched=7, requested=30)
        failed = ai.build_analysis_data("5", 2, [], {}, fetched=30, failed=1, stopped=True)
        listed = ai.build_analysis_data("5", 1, [dict(damage=100)], {}, fetched=30, detail_enabled=False)
        self.assertIn("列表返回0场", ai.analysis_scope(empty))
        self.assertNotIn("最近30场", ai.analysis_scope(empty))
        self.assertIn("最近7场", ai.analysis_scope(missing_mode))
        self.assertIn("选中2场", ai.analysis_scope(failed))
        self.assertIn("获取失败1场", ai.analysis_scope(failed))
        self.assertIn("后续查询已停止", ai.analysis_scope(failed))
        self.assertNotIn("未选中", ai.analysis_scope(failed))
        self.assertIn("未请求单局详情", ai.analysis_scope(listed))
        self.assertIsNone(listed["recent_metrics"]["parry"]["mean"])

    def test_failure_retains_evidence_scope_and_does_not_blame_player(self):
        view = self.view()
        report = self.report()
        report.analysis_data = ai.build_analysis_data("5", 0, [], {}, fetched=30, season_error="未取得赛季概览")
        asyncio.run(view._prepare_ai_report(self.event(), report))
        text = "".join(report.texts)
        self.assertIn("原始完整战绩", text)
        self.assertIn("未选中天人三排对局", text)
        self.assertIn("AI分析未完成", text)
        self.assertIn("不代表玩家没有历史战绩", text)
        view._ai_context.llm_generate.assert_not_awaited()

    def test_timeout_and_provider_error_preserve_original_report(self):
        for error in (asyncio.TimeoutError(), RuntimeError("API_KEY_SECRET")):
            view = self.view()
            view._ai_context.llm_generate.side_effect = error
            report = self.report()
            asyncio.run(view._prepare_ai_report(self.event(), report))
            text = "".join(report.texts)
            self.assertIn("原始完整战绩", text)
            self.assertIn("AI分析未完成", text)
            self.assertNotIn("API_KEY_SECRET", text)
            self.assertFalse(view._ai_lock.locked())

    def test_blank_model_result_is_handled(self):
        view = self.view()
        view._ai_context.llm_generate.return_value = SimpleNamespace(completion_text=" ")
        with self.assertRaisesRegex(ai.AnalysisError, "没有返回"):
            asyncio.run(view._evaluate_records(self.event(), self.data()))

    def test_busy_analysis_does_not_call_another_model(self):
        view = self.view()
        async def run():
            async with view._ai_lock:
                with self.assertRaisesRegex(ai.AnalysisError, "正在进行"):
                    await view._evaluate_records(self.event(), self.data())
        asyncio.run(run())
        view._ai_context.llm_generate.assert_not_awaited()

    def test_analysis_honors_text_and_single_long_image_modes(self):
        from report_image import render_report_images, _blocks
        view = self.view(result_display_format="图片")
        report = self.report()
        view._ai_context.llm_generate.return_value = SimpleNamespace(completion_text="优点：伤害贡献稳定。\n第1局只是文字说明，不是新的对局记录。\n建议：保持合理的进攻距离。")
        asyncio.run(view._prepare_ai_report(self.event(), report))
        self.assertEqual(len(report.texts), 1)
        self.assertIn("AI战绩分析", report.texts[0])
        self.assertIn("原始完整战绩", report.texts[0])
        self.assertEqual(len(render_report_images(report.texts[0])), 1)
        self.assertFalse(any(kind == "match" for kind, _ in _blocks(report.texts[0])))
        view = self.view()
        view._ai_context.llm_generate.return_value = SimpleNamespace(completion_text="完整评价" * 1000)
        report = self.report()
        asyncio.run(view._prepare_ai_report(self.event(), report))
        self.assertTrue(all(len(text) <= 1200 for text in report.texts))
        self.assertIn("原始完整战绩", "".join(report.texts))

    def test_native_astrbot_selectors_are_declared(self):
        data = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(data["ai_provider_id"]["_special"], "select_provider")
        self.assertEqual(data["ai_persona_id"]["_special"], "select_persona")
        self.assertFalse(data["ai_auto_analysis"]["default"])

    def test_analysis_command_suppresses_a_second_default_chat_response(self):
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8-sig"))
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "NarakaPlugin")
        method = next(node for node in cls.body if getattr(node, "name", "") == "on_message")
        method.decorator_list = []
        env = dict(AstrMessageEvent=object, re=__import__("re"))
        exec(compile(ast.Module(body=[method], type_ignores=[]), "main.py", "exec"), env)
        async def query(*args, **kwargs):
            yield "结果"
        for command, auto, expected in (("战绩分析 玩家", False, 1), ("战绩查询 玩家", True, 1), ("战绩查询 玩家", False, 0)):
            from unittest.mock import Mock
            event = SimpleNamespace(message_str=command, should_call_llm=Mock())
            fake = SimpleNamespace(config=dict(ai_auto_analysis=auto), _allowed_group=lambda event: True, _query_records=query)
            async def collect():
                return [text async for text in env["on_message"](fake, event)]
            self.assertEqual(asyncio.run(collect()), ["结果"])
            self.assertEqual(event.should_call_llm.call_count, expected)
            if expected:
                event.should_call_llm.assert_called_once_with(False)

    def test_appended_ai_is_separate_from_each_match_in_one_image(self):
        from record_query import format_details
        from report_image import render_report_images, _blocks
        from PIL import ImageDraw
        records = [dict(hero_id="1000003", damage=12345), dict(hero_id="1000004", damage=9876)]
        report = self.report()
        report.report_text = format_details("示例玩家", 30, "12", 2, records, {"1000003":"宁红夜", "1000004":"迦南"})
        view = self.view(result_display_format="图片")
        asyncio.run(view._prepare_ai_report(self.event(), report))
        self.assertEqual(sum(kind == "match" for kind, _ in _blocks(report.texts[0])), 2)
        captured = []
        original = ImageDraw.ImageDraw.text
        def capture(draw, xy, text, *args, **kwargs):
            captured.append(str(text))
            return original(draw, xy, text, *args, **kwargs)
        with patch.object(ImageDraw.ImageDraw, "text", capture):
            pages = render_report_images(report.texts[0])
        self.assertEqual(len(pages), 1)
        for value in ("宁红夜", "迦南", "有证据的战绩评价", "9876"):
            self.assertTrue(any(value in text for text in captured), value)
        self.assertTrue(any("AI评价仅供参考" in text for text in captured))

    def test_auto_analysis_runs_after_unbinding_and_queries_only_once(self):
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8-sig"))
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "NarakaPlugin")
        method = next(node for node in cls.body if getattr(node, "name", "") == "_query_records")
        env = dict(AstrMessageEvent=object, QueryProgress=QueryProgress, asyncio=asyncio, DEFAULT_SERVER="163",
            looks_like_role_id=lambda value: False, _ReadResult=lambda **kwargs: SimpleNamespace(**kwargs),
            HeyboxError=ValueError, aiohttp=SimpleNamespace(ClientError=ValueError), _must_stop_after_error=lambda exc: False)
        exec(compile(ast.Module(body=[method], type_ignores=[]), "main.py", "exec"), env)
        active = [False]
        @asynccontextmanager
        async def session():
            yield None
        @asynccontextmanager
        async def bound(*args):
            active[0] = True
            try:
                yield SimpleNamespace(ok=True, unbind_ok=True)
            finally:
                active[0] = False
        async def analyze(event, report, **kwargs):
            self.assertFalse(active[0], "AI call must happen after unbinding")
            report.texts = ["带AI分析的结果"]
            return report
        fake = SimpleNamespace(_allowed_group=lambda event: True, _ack=AsyncMock(return_value=None),
            data_mode="heybox", logged_in=True, _query_lock=asyncio.Lock(), _start_query_cooldown=lambda: 0,
            config=dict(enable_query_progress=False, ai_auto_analysis=True), _number_setting=lambda key, default, *args: default,
            _session=session, _binding=SimpleNamespace(recover=AsyncMock(), negative_hit=lambda key: False, bound=bound),
            _auto_bind=lambda: True, _residual_interval_seconds=lambda: 0,
            _run_read=AsyncMock(return_value=SimpleNamespace(ok=True, texts=["原战绩"])),
            _prepare_ai_report=AsyncMock(side_effect=analyze), _body=AsyncMock(side_effect=lambda event,text,**kwargs:text),
            _note=lambda event,text:text)
        async def collect(force_bind, analysis):
            return [value async for value in env["_query_records"](fake, self.event(), "玩家", "", force_bind=force_bind, analysis=analysis)]
        for force_bind, analysis, auto in ((True, False, True), (False, True, False), (False, False, False)):
            fake.config["ai_auto_analysis"] = auto
            fake._run_read.reset_mock()
            fake._prepare_ai_report.reset_mock()
            fake._run_read.return_value = SimpleNamespace(ok=True, texts=["原战绩"])
            expected = "带AI分析的结果" if analysis or auto else "原战绩"
            self.assertEqual(asyncio.run(collect(force_bind, analysis)), [expected])
            fake._run_read.assert_awaited_once()
            self.assertEqual(fake._prepare_ai_report.await_count, int(analysis or auto))
            self.assertEqual(fake._run_read.call_args.kwargs["for_ai"], analysis or auto)
        fake.config["enable_ai_analysis"] = False
        fake._run_read.reset_mock()
        self.assertIn("已关闭", asyncio.run(collect(False, True))[0])
        fake._run_read.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
