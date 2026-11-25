"""针对 tools.py（LLM 工具层）的单元测试。

覆盖点：
* 工具**不会**往群里发消息（不出现 event.send / _body / _note）
* ``enable_llm_tools=false`` 时所有工具都拒绝执行
* ``llm_tool_allow_unbind=false`` 时解绑工具拒绝执行
* ``naraka_unbind`` 在 ``data_mode=none`` 时直接拒绝
* 6 个工具都注册为异步生成器（@filter.llm_tool 要求）
* llm_tool_max_text_chars 截断生效
* 免登录模式（none）下查询类工具给出统一的 _NONE_MODE_HINT 文案
"""

import ast
import asyncio
import importlib
import inspect
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
package = ModuleType("naraka_test_package")
package.__path__ = [str(ROOT)]
sys.modules.setdefault("naraka_test_package", package)

TOOL_NAMES = {
    "tool_query_summary": "naraka_query_summary",
    "tool_query_detail": "naraka_query_detail",
    "tool_search_player": "naraka_search_player",
    "tool_season_stats": "naraka_season_stats",
    "tool_ai_analysis": "naraka_ai_analysis",
    "tool_unbind": "naraka_unbind",
}


def make_mixin(**config):
    """造一个只带 NarakaToolMixin 行为、不带 AstrBot 运行时的替身对象。"""

    class Harness:
        pass

    # 把 mixin 的方法绑到一个空壳上
    import astrbot_stub  # noqa: F401  (确保 stub 已注册)

    tools_mod = sys.modules["naraka_test_package.tools"]
    for name, fn in vars(tools_mod.NarakaToolMixin).items():
        if callable(fn) and not name.startswith("__"):
            setattr(Harness, name, fn)
    obj = Harness()
    obj.config = dict(config)
    obj.data_mode = "heybox"
    obj.logged_in = True
    obj._query_lock = asyncio.Lock()
    obj._ai_lock = asyncio.Lock()

    class _AsyncCM:
        """可重复使用的异步上下文管理器替身。"""

        def __init__(self, value=None):
            self.value = value if value is not None else SimpleNamespace()

        async def __aenter__(self):
            return self.value

        async def __aexit__(self, *exc):
            return None

    obj._AsyncCM = _AsyncCM
    obj._session = MagicMock(return_value=_AsyncCM())
    obj._binding = MagicMock()
    obj._binding.recover = AsyncMock()
    obj._binding.manual_unbind = AsyncMock(return_value=(True, ""))
    obj._allowed_group = MagicMock(return_value=True)
    obj._start_query_cooldown = MagicMock(return_value=0)
    obj._residual_interval_seconds = MagicMock(return_value=60)
    obj._auto_bind = MagicMock(return_value=True)
    obj._ranked_ids = MagicMock(return_value={4, 5, 13})
    obj.result_method = MagicMock(return_value="plain")
    obj._message_id = MagicMock(return_value=None)
    obj._send_original_images = AsyncMock()
    return obj


def collect(gen):
    async def _run():
        return [chunk async for chunk in gen]
    return asyncio.run(_run())


class LlmToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 需要先准备好 astrbot stub，否则 tools.py 无法导入
        stub_dir = ROOT / "tests"
        sys.path.insert(0, str(stub_dir))
        if "astrbot" not in sys.modules:
            import astrbot_stub  # noqa: F401
        cls.tools = importlib.import_module("naraka_test_package.tools")

    def event(self):
        return MagicMock()

    # ---------- 注册形态 ----------

    def test_all_tools_are_async_generators(self):
        for attr in TOOL_NAMES:
            fn = getattr(self.tools.NarakaToolMixin, attr)
            self.assertTrue(inspect.isasyncgenfunction(fn), f"{attr} 不是异步生成器")
            self.assertTrue(hasattr(fn, "__wrapped__") or callable(fn))

    def test_tool_decorator_metadata_present(self):
        for attr, expected_name in TOOL_NAMES.items():
            fn = getattr(self.tools.NarakaToolMixin, attr)
            # filter.llm_tool 会把 name 记录在 __llm_tool_name__ 或类似属性上
            joined = str([v for v in vars(fn).values() if isinstance(v, str)])
            src = inspect.getsource(fn)
            self.assertIn(expected_name, src + joined, f"{attr} 缺少工具名")

    def test_every_tool_docstring_has_args_section(self):
        """AstrBot 靠解析 Args 生成 schema，缺失会导致参数丢失。"""
        for attr in TOOL_NAMES:
            fn = getattr(self.tools.NarakaToolMixin, attr)
            doc = inspect.getdoc(fn) or ""
            self.assertIn("Args:", doc, f"{attr} 的 docstring 缺少 Args 段")
            self.assertIn("(string)", doc, f"{attr} 的参数未标注 (string)")

    def test_first_non_self_param_is_event(self):
        for attr in TOOL_NAMES:
            fn = getattr(self.tools.NarakaToolMixin, attr)
            params = list(inspect.signature(fn).parameters)
            self.assertEqual(params[0], "self")
            self.assertEqual(params[1], "event", f"{attr} 第二个参数必须是 event")

    # ---------- 发消息边界 ----------

    def test_tools_only_send_through_image_channel(self):
        """除 ``_tool_deliver_image`` 之外，工具绝不调用 event.send / _body / _note 等。

        用 AST 检查真实调用点，而不是扫源文本——docstring 里的说明会误伤子串匹配。
        唯一的发消息入口收敛在 ``_tool_deliver_image``（图片模式下的长图投递）。
        """
        tree = ast.parse(Path(ROOT / "tools.py").read_text(encoding="utf-8"))
        banned_calls = {"send", "plain_result", "chain_result", "image_result"}
        banned_self = {"_body", "_note", "_send_at", "_ack"}
        allowed = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_tool_deliver_image":
                allowed.update(id(sub) for sub in ast.walk(node))
        offenders = []
        for node in ast.walk(tree):
            if id(node) in allowed or not isinstance(node, ast.Call):
                continue
            fn = node.func
            if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
                if fn.value.id == "self" and fn.attr in banned_self:
                    offenders.append(f"self.{fn.attr}")
                if fn.value.id == "event" and fn.attr in banned_calls:
                    offenders.append(f"event.{fn.attr}")
        self.assertEqual(offenders, [], f"工具只允许经 _tool_deliver_image 发消息，发现：{offenders}")

    def test_only_image_channel_helper_sends(self):
        """确认发消息能力确实只存在于 _tool_deliver_image 内。"""
        tree = ast.parse(Path(ROOT / "tools.py").read_text(encoding="utf-8"))
        owners = []
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)]:
            for node in ast.walk(fn):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr in ("send", "chain_result")):
                    owners.append(fn.name)
        self.assertEqual(sorted(set(owners)), ["_tool_deliver_image"])

    # ---------- 开关 ----------

    def test_disabled_switch_blocks_tools(self):
        obj = make_mixin(enable_llm_tools=False)
        for attr in ("tool_query_summary", "tool_query_detail", "tool_search_player",
                     "tool_season_stats", "tool_ai_analysis"):
            fn = getattr(obj, attr)
            out = collect(fn(self.event(), "张三"))
            self.assertTrue(any("enable_llm_tools" in c for c in out), f"{attr} 未拦截关闭态")

    def test_unbind_requires_extra_switch(self):
        obj = make_mixin(enable_llm_tools=True, llm_tool_allow_unbind=False)
        out = collect(obj.tool_unbind(self.event(), "张三"))
        self.assertTrue(any("未开启" in c for c in out))

    def test_unbind_rejected_in_none_mode(self):
        obj = make_mixin(enable_llm_tools=True, llm_tool_allow_unbind=True)
        obj.data_mode = "none"
        out = collect(obj.tool_unbind(self.event(), "张三"))
        self.assertTrue(any("无需解绑" in c for c in out))

    def test_unbind_happy_path(self):
        obj = make_mixin(enable_llm_tools=True, llm_tool_allow_unbind=True)
        out = collect(obj.tool_unbind(self.event(), "张三"))
        self.assertTrue(any("已解绑" in c for c in out))

    # ---------- 免登录模式 ----------

    def test_none_mode_hint_for_read_tools(self):
        for attr in ("tool_query_summary", "tool_query_detail", "tool_season_stats",
                     "tool_ai_analysis"):
            obj = make_mixin(enable_llm_tools=True)
            obj.data_mode = "none"
            out = collect(getattr(obj, attr)(self.event(), "张三"))
            joined = "\n".join(out)
            self.assertIn("免登录", joined, f"{attr} 未给出免登录提示")

    # ---------- 截断 ----------

    def test_trim_by_max_chars(self):
        obj = make_mixin(enable_llm_tools=True, llm_tool_max_text_chars=500)
        text = "长" * 3000
        trimmed = obj._tool_trim(text)
        self.assertLessEqual(len(trimmed), 600)
        self.assertIn("截断", trimmed)

    def test_trim_keeps_short_text(self):
        obj = make_mixin(enable_llm_tools=True, llm_tool_max_text_chars=4000)
        self.assertEqual(obj._tool_trim("很短"), "很短")

    def test_trim_clamps_low_values(self):
        obj = make_mixin(enable_llm_tools=True, llm_tool_max_text_chars=10)
        self.assertEqual(obj._tool_max_chars(), 500)  # 下限保护

    def test_trim_handles_bad_config(self):
        obj = make_mixin(enable_llm_tools=True, llm_tool_max_text_chars="不是数字")
        self.assertEqual(obj._tool_max_chars(), 4000)

    # ---------- 错误处理 ----------

    def test_error_text_delegates_to_main(self):
        obj = make_mixin()
        tools_cls = sys.modules["naraka_test_package.tools"].NarakaToolMixin
        with patch.object(tools_cls, "_error_text",
                          staticmethod(lambda exc: f"已处理:{exc}"), create=True):
            self.assertEqual(obj._tool_error_text(ValueError("坏了")), "已处理:坏了")

    def test_error_text_degrades_when_no_main_impl(self):
        obj = make_mixin()
        err = ValueError("原始信息")
        with patch.object(
            sys.modules["naraka_test_package.tools"].NarakaToolMixin,
            "_error_text",
            None,
            create=True,
        ):
            self.assertEqual(obj._tool_error_text(err), "原始信息")

    # ---------- 冷却 ----------

    def test_cooldown_short_circuits(self):
        obj = make_mixin(enable_llm_tools=True)
        obj._start_query_cooldown = MagicMock(return_value=30)
        out = collect(obj.tool_query_summary(self.event(), "张三"))
        self.assertTrue(any("30 秒" in c for c in out))

    def test_group_whitelist_enforced(self):
        obj = make_mixin(enable_llm_tools=True)
        obj._allowed_group = MagicMock(return_value=False)
        out = collect(obj.tool_query_summary(self.event(), "张三"))
        self.assertTrue(any("白名单" in c for c in out))

    def test_empty_nickname_rejected(self):
        obj = make_mixin(enable_llm_tools=True)
        for attr in ("tool_query_summary", "tool_query_detail", "tool_search_player",
                     "tool_ai_analysis"):
            out = collect(getattr(obj, attr)(self.event(), "   "))
            self.assertTrue(any("昵称" in c for c in out), f"{attr} 未校验空昵称")


class ToolReadWithBindTests(unittest.TestCase):
    """验证 _tool_read_with_bind 的阶段一/阶段二，不依赖 event。"""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / "tests"))
        import astrbot_stub  # noqa: F401
        cls.tools = importlib.import_module("naraka_test_package.tools")

    def make(self, **over):
        obj = make_mixin(enable_llm_tools=True)
        for k, v in over.items():
            setattr(obj, k, v)
        return obj

    def test_mode_parse_error_short_circuits(self):
        obj = self.make()
        with patch.object(self.tools, "resolve_mode", side_effect=ValueError("模式不认识")):
            res = asyncio.run(obj._tool_read_with_bind("张三", "怪模式", False))
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, "模式不认识")

    def test_not_logged_in(self):
        obj = self.make(logged_in=False)
        res = asyncio.run(obj._tool_read_with_bind("张三", "", False))
        self.assertFalse(res.ok)
        self.assertIn("尚未登录", res.reason)

    def test_phase1_success_no_bind(self):
        obj = self.make()
        ok_result = SimpleNamespace(ok=True, needs_bind=False, report_text="战绩ABC")
        obj._run_read = AsyncMock(return_value=ok_result)
        res = asyncio.run(obj._tool_read_with_bind("张三", "", False))
        self.assertTrue(res.ok)
        self.assertEqual(res.report_text, "战绩ABC")

    def test_phase2_bind_when_needed(self):
        obj = self.make()
        need = SimpleNamespace(ok=False, needs_bind=True, nickname="张三")
        success = SimpleNamespace(ok=True, needs_bind=False, report_text="绑定后战绩")
        obj._run_read = AsyncMock(side_effect=[need, success])
        ctx = obj._AsyncCM(SimpleNamespace(ok=True))
        obj._binding.bound = MagicMock(return_value=ctx)
        obj._binding.negative_hit = MagicMock(return_value=False)
        res = asyncio.run(obj._tool_read_with_bind("张三", "", False))
        self.assertTrue(res.ok)
        self.assertEqual(res.report_text, "绑定后战绩")

    def test_negative_cache_skips_bind(self):
        obj = self.make()
        need = SimpleNamespace(ok=False, needs_bind=True, nickname="张三")
        obj._run_read = AsyncMock(return_value=need)
        obj._binding.negative_hit = MagicMock(return_value=True)
        res = asyncio.run(obj._tool_read_with_bind("张三", "", False))
        self.assertFalse(res.ok)
        self.assertIn("暂时没有可查数据", res.reason)

    def test_auto_bind_disabled(self):
        obj = self.make()
        need = SimpleNamespace(ok=False, needs_bind=True, nickname="张三")
        obj._run_read = AsyncMock(return_value=need)
        obj._auto_bind = MagicMock(return_value=False)
        obj._binding.negative_hit = MagicMock(return_value=False)
        res = asyncio.run(obj._tool_read_with_bind("张三", "", False))
        self.assertFalse(res.ok)
        self.assertIn("没有找到", res.reason)

    def test_exception_in_run_read_becomes_reason(self):
        obj = self.make()
        obj._run_read = AsyncMock(side_effect=ValueError("上游炸了"))
        res = asyncio.run(obj._tool_read_with_bind("张三", "", False))
        self.assertFalse(res.ok)
        self.assertIn("上游炸了", res.reason)


class ToolImageDeliveryTests(unittest.TestCase):
    """图片模式下，自然语言调用也要把同款长图发到会话。"""

    @classmethod
    def setUpClass(cls):
        stub_dir = ROOT / "tests"
        sys.path.insert(0, str(stub_dir))
        if "astrbot" not in sys.modules:
            import astrbot_stub  # noqa: F401
        cls.tools = importlib.import_module("naraka_test_package.tools")

    def image_event(self):
        event = MagicMock()
        event.send = AsyncMock()
        event.chain_result = MagicMock(side_effect=lambda chain: ("chain", chain))
        return event

    def test_image_mode_sends_chain_with_images(self):
        obj = make_mixin(enable_llm_tools=True, result_display_format="图片")
        event = self.image_event()
        with patch.object(self.tools, "render_report_images", return_value=[b"png"]):
            asyncio.run(obj._tool_deliver_image(event, "昵称：张三"))
        event.send.assert_awaited_once()
        chain = event.chain_result.call_args.args[0]
        self.assertEqual(chain, [("image", b"png")])

    def test_quote_mode_prepends_reply(self):
        obj = make_mixin(enable_llm_tools=True, result_display_format="图片")
        obj.result_method = MagicMock(return_value="quote")
        obj._message_id = MagicMock(return_value="99")
        event = self.image_event()
        with patch.object(self.tools, "render_report_images", return_value=[b"png"]):
            asyncio.run(obj._tool_deliver_image(event, "昵称：张三"))
        chain = event.chain_result.call_args.args[0]
        self.assertEqual(chain[0].__class__.__name__, "Reply")
        self.assertEqual(chain[1], ("image", b"png"))

    def test_text_mode_does_not_send(self):
        obj = make_mixin(enable_llm_tools=True, result_display_format="文字")
        event = self.image_event()
        asyncio.run(obj._tool_deliver_image(event, "昵称：张三"))
        event.send.assert_not_awaited()

    def test_switch_off_does_not_send(self):
        obj = make_mixin(enable_llm_tools=True, result_display_format="图片",
                         llm_tool_send_image=False)
        event = self.image_event()
        with patch.object(self.tools, "render_report_images", return_value=[b"png"]):
            asyncio.run(obj._tool_deliver_image(event, "昵称：张三"))
        event.send.assert_not_awaited()

    def test_string_false_disables(self):
        obj = make_mixin(enable_llm_tools=True, result_display_format="图片",
                         llm_tool_send_image="false")
        event = self.image_event()
        with patch.object(self.tools, "render_report_images", return_value=[b"png"]):
            asyncio.run(obj._tool_deliver_image(event, "昵称：张三"))
        event.send.assert_not_awaited()

    def test_render_failure_keeps_text_result(self):
        obj = make_mixin(enable_llm_tools=True, result_display_format="图片")
        event = self.image_event()
        with patch.object(self.tools, "render_report_images", side_effect=ValueError("字体缺了")):
            asyncio.run(obj._tool_deliver_image(event, "昵称：张三"))
        event.send.assert_not_awaited()

    def test_send_failure_is_swallowed(self):
        obj = make_mixin(enable_llm_tools=True, result_display_format="图片")
        event = self.image_event()
        event.send = AsyncMock(side_effect=RuntimeError("适配器炸了"))
        with patch.object(self.tools, "render_report_images", return_value=[b"png"]):
            asyncio.run(obj._tool_deliver_image(event, "昵称：张三"))

    def test_summary_tool_delivers_image_and_returns_text(self):
        obj = make_mixin(enable_llm_tools=True, result_display_format="图片")
        obj._tool_read_with_bind = AsyncMock(return_value=SimpleNamespace(
            ok=True, report_text="昵称：张三\n场均伤害：10000", texts=[], reason=""))
        event = self.image_event()
        with patch.object(self.tools, "render_report_images", return_value=[b"png"]):
            out = collect(obj.tool_query_summary(event, "张三"))
        self.assertIn("场均伤害", "\n".join(out))
        event.send.assert_awaited_once()

    def test_detail_tool_delivers_image(self):
        obj = make_mixin(enable_llm_tools=True, result_display_format="图片")
        obj._tool_read_with_bind = AsyncMock(return_value=SimpleNamespace(
            ok=True, report_text="第1局｜伤害 1000", texts=[], reason=""))
        event = self.image_event()
        with patch.object(self.tools, "render_report_images", return_value=[b"png"]):
            collect(obj.tool_query_detail(event, "张三"))
        event.send.assert_awaited_once()

    def test_failed_query_sends_no_image(self):
        obj = make_mixin(enable_llm_tools=True, result_display_format="图片")
        obj._tool_read_with_bind = AsyncMock(return_value=SimpleNamespace(
            ok=False, report_text="", texts=[], reason="没有找到「张三」"))
        event = self.image_event()
        with patch.object(self.tools, "render_report_images", return_value=[b"png"]):
            out = collect(obj.tool_query_summary(event, "张三"))
        self.assertIn("没有找到", "\n".join(out))
        event.send.assert_not_awaited()


class ToolBindingTests(unittest.TestCase):
    """回归：工具方法必须绑定到插件实例，否则调用时 event 会落进 self。

    AstrBot 只在「handler 的 ``__module__`` 等于插件主模块」时才自动绑定，
    而本插件的工具方法在 ``tools.py`` 的 Mixin 里，因此必须由插件自己补绑定。
    """

    def tool_fn(self, name="tool_query_summary"):
        return vars(sys.modules["naraka_test_package.tools"].NarakaToolMixin)[name]

    def register_modules(self, entries):
        register = ModuleType("astrbot.core.provider.register")
        register.llm_tools = SimpleNamespace(func_list=entries)
        provider = ModuleType("astrbot.core.provider")
        provider.register = register
        core = ModuleType("astrbot.core")
        core.provider = provider
        return {
            "astrbot.core": core,
            "astrbot.core.provider": provider,
            "astrbot.core.provider.register": register,
        }

    def entry(self, name="naraka_query_summary", handler=None, module_path=None):
        return SimpleNamespace(
            name=name,
            handler=self.tool_fn() if handler is None else handler,
            handler_module_path=module_path,
        )

    def test_unbound_handler_produces_the_reported_type_error(self):
        raw = self.tool_fn()  # 未经绑定的函数，等价于 llm_tools 里注册的那个
        with self.assertRaises(TypeError) as ctx:
            raw(SimpleNamespace(), player="lzn", mode="单排")
        self.assertIn("event", str(ctx.exception))

    def test_binding_makes_handler_a_bound_method(self):
        obj = make_mixin(enable_llm_tools=True)
        tool = self.entry()
        with patch.dict(sys.modules, self.register_modules([tool])):
            obj._bind_llm_tool_handlers()
        self.assertTrue(inspect.ismethod(tool.handler))
        self.assertIs(tool.handler.__self__, obj)
        self.assertEqual(tool.handler_module_path, type(obj).__module__)

    def test_bound_handler_accepts_event_positionally(self):
        obj = make_mixin(enable_llm_tools=True)
        obj._query_lock = asyncio.Lock()
        obj._run_read = AsyncMock(return_value=SimpleNamespace(
            ok=True, report_text="战绩正文", texts=["战绩正文"], reason="", needs_bind=False,
            nickname="lzn", analysis_data={}))
        tool = self.entry()
        with patch.dict(sys.modules, self.register_modules([tool])):
            obj._bind_llm_tool_handlers()
        event = SimpleNamespace(get_group_id=lambda: "g", get_sender_id=lambda: "u")
        result = tool.handler(event, player="lzn", mode="")
        self.assertTrue(inspect.isasyncgen(result))

        async def drain():
            return [chunk async for chunk in result]
        chunks = asyncio.run(drain())
        self.assertTrue(chunks)
        self.assertIn("战绩", chunks[-1])

    def test_partial_handler_is_rebased_on_instance(self):
        obj = make_mixin(enable_llm_tools=True)
        import functools as ft
        tool = self.entry(handler=ft.partial(self.tool_fn()))
        with patch.dict(sys.modules, self.register_modules([tool])):
            obj._bind_llm_tool_handlers()
        self.assertTrue(inspect.ismethod(tool.handler))
        self.assertIs(tool.handler.__self__, obj)

    def test_other_plugins_tools_are_left_alone(self):
        obj = make_mixin(enable_llm_tools=True)
        foreign = self.entry(name="mingjian_query", handler=lambda event: None)
        handler_before = foreign.handler
        with patch.dict(sys.modules, self.register_modules([foreign])):
            obj._bind_llm_tool_handlers()
        self.assertIs(foreign.handler, handler_before)
        self.assertIsNone(foreign.handler_module_path)

    def test_tool_without_matching_method_is_skipped(self):
        obj = make_mixin(enable_llm_tools=True)
        stranger = self.entry(name="naraka_not_a_method", handler=lambda event: None)
        handler_before = stranger.handler
        with patch.dict(sys.modules, self.register_modules([stranger])):
            obj._bind_llm_tool_handlers()
        self.assertIs(stranger.handler, handler_before)

    def test_missing_register_module_is_tolerated(self):
        obj = make_mixin(enable_llm_tools=True)
        with patch.dict(sys.modules, {"astrbot.core.provider.register": None}):
            obj._bind_llm_tool_handlers()  # 不抛异常即可

    def test_main_calls_the_binding_hook_in_init(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8-sig")
        body = source.split("def __init__(self, context: Context, config: AstrBotConfig):")[1]
        body = body.split("def data_mode")[0]
        self.assertIn("self._bind_llm_tool_handlers()", body)


class ForeignHintGuardTests(unittest.TestCase):
    """清理其它插件（剑三）误注入提示的判定与动作。"""

    NUDGE = (
        "\n\n（内部提示：这是剑三数据查询，必须调用 mingjian_query 工具，禁止用 search_meme_wiki、"
        "astr_kb_search、web_search 等工具确认游戏术语，也不要直接文字回答。"
        "若一次问了两件数据，请分别调用两次 mingjian_query。）"
    )
    HINT = "（剑三名剑数据）查排行时调用 mingjian_query。"

    def make(self, **config):
        obj = make_mixin(**config)
        obj._requests = []
        return obj

    @staticmethod
    def make_event(text, extras=None):
        store = dict(extras or {})

        class Event:
            unified_msg_origin = "default:GroupMessage:1"

            def get_message_str(self):
                return text

            def set_extra(self, key, value):
                store[key] = value

            def get_extra(self, key, default=None):
                return store.get(key, default)

            def extras(self):
                return dict(store)

        return Event()

    @staticmethod
    def make_request(prompt, system=""):
        return SimpleNamespace(prompt=prompt, system_prompt=system)

    def run_guard(self, obj, event, request):
        asyncio.run(obj._strip_foreign_hints(event, request))

    def test_naraka_message_gets_foreign_hint_stripped(self):
        obj = self.make(enable_llm_tools=True)
        event = self.make_event("查一下永劫的lzn战绩")
        request = self.make_request("查一下永劫的lzn战绩" + self.NUDGE, "你是助手。\n\n" + self.HINT)
        self.run_guard(obj, event, request)
        self.assertNotIn("剑三数据查询", request.prompt)
        self.assertEqual(request.prompt, "查一下永劫的lzn战绩")
        self.assertNotIn("mingjian_query", request.system_prompt)
        self.assertIn("永劫无间", request.system_prompt)

    def test_retry_marker_is_cleared(self):
        obj = self.make(enable_llm_tools=True)
        event = self.make_event("永劫战绩 君墨", {"astrbot_plugin_mingjian.query_retry_request": {"actions": ("战绩",)}})
        request = self.make_request("永劫战绩 君墨" + self.NUDGE)
        self.run_guard(obj, event, request)
        self.assertIsNone(event.get_extra("astrbot_plugin_mingjian.query_retry_request"))

    def test_jx3_message_is_never_touched(self):
        obj = self.make(enable_llm_tools=True)
        event = self.make_event("查一下梦江南的33战绩")
        request = self.make_request("查一下梦江南的33战绩" + self.NUDGE, self.HINT)
        self.run_guard(obj, event, request)
        self.assertIn("剑三数据查询", request.prompt)
        self.assertIn("mingjian_query", request.system_prompt)

    def test_three_queue_counts_as_naraka(self):
        # 剑三说 33/3v3，只有永劫说「三排/双排」→ 这是永劫信号
        obj = self.make(enable_llm_tools=True, nickname_mappings=[])
        event = self.make_event("查一下李知恩的三排战绩")
        request = self.make_request("查一下李知恩的三排战绩" + self.NUDGE)
        self.run_guard(obj, event, request)
        self.assertEqual(request.prompt, "查一下李知恩的三排战绩")
        self.assertNotIn("剑三数据查询", request.prompt)
        self.assertIn("永劫无间", request.system_prompt)

    def test_jx3_arena_numbers_are_jx3_signal(self):
        obj = self.make(enable_llm_tools=True, nickname_mappings=[])
        self.assertEqual(obj._game_of_message("查下这周 33 的排行"), "jx3")
        self.assertEqual(obj._game_of_message("查下这周3v3排行"), "jx3")
        self.assertEqual(obj._game_of_message("查下一场双排战绩"), "naraka")

    def test_jx3_wins_when_both_sides_mentioned(self):
        obj = self.make(enable_llm_tools=True)
        text = "永劫和剑三哪个好玩，顺便查下名剑战绩"
        event = self.make_event(text)
        request = self.make_request(text + self.NUDGE)
        self.run_guard(obj, event, request)
        self.assertIn("剑三数据查询", request.prompt)

    def test_shared_words_are_neutralized(self):
        obj = self.make(enable_llm_tools=True, nickname_mappings=[])
        # 只有共享词（单排/战绩）：不删剑三的提示，但改写成中性说法，并清掉强制补调
        event = self.make_event(
            "查一下某服单排战绩",
            {"astrbot_plugin_mingjian.query_retry_request": {"actions": ("战绩",)}},
        )
        request = self.make_request("查一下某服单排战绩" + self.NUDGE, self.HINT)
        self.run_guard(obj, event, request)
        self.assertNotIn("剑三数据查询", request.prompt)
        self.assertIn("游戏归属不明确", request.prompt)
        self.assertIn("mingjian_query", request.prompt)      # 剑三仍然可用
        self.assertNotIn("（剑三名剑数据）", request.system_prompt)   # 原来那段工具说明被拿掉
        self.assertIsNone(event.get_extra("astrbot_plugin_mingjian.query_retry_request"))

    def test_registered_nickname_counts_as_naraka_evidence(self):
        obj = self.make(enable_llm_tools=True, nickname_mappings=[{"nickname": "君墨", "role_id": "slil000037188700210163"}])
        event = self.make_event("查一下君墨单排战绩")
        request = self.make_request("查一下君墨单排战绩" + self.NUDGE)
        self.run_guard(obj, event, request)
        self.assertNotIn("剑三数据查询", request.prompt)

    def test_switch_off_leaves_request_untouched(self):
        obj = self.make(enable_llm_tools=True, llm_tool_strip_foreign_hints=False)
        event = self.make_event("永劫战绩")
        request = self.make_request("永劫战绩" + self.NUDGE)
        self.run_guard(obj, event, request)
        self.assertIn("剑三数据查询", request.prompt)

    def test_master_switch_off_leaves_request_untouched(self):
        obj = self.make(enable_llm_tools=False)
        event = self.make_event("永劫战绩")
        request = self.make_request("永劫战绩" + self.NUDGE)
        self.run_guard(obj, event, request)
        self.assertIn("剑三数据查询", request.prompt)

    def test_clean_request_is_not_modified(self):
        obj = self.make(enable_llm_tools=True)
        event = self.make_event("永劫战绩")
        request = self.make_request("永劫战绩", "你是助手。")
        self.run_guard(obj, event, request)
        self.assertEqual(request.prompt, "永劫战绩")
        self.assertEqual(request.system_prompt, "你是助手。")

    def test_broken_request_does_not_raise(self):
        obj = self.make(enable_llm_tools=True)
        event = self.make_event("永劫战绩")
        self.run_guard(obj, event, SimpleNamespace())  # 缺 prompt/system_prompt 属性

    def test_game_detection_uses_exclusive_words(self):
        obj = self.make(enable_llm_tools=True, nickname_mappings=[])
        self.assertEqual(obj._game_of_message("永劫 天人三排 战绩"), "naraka")
        self.assertEqual(obj._game_of_message("振刀怎么练"), "naraka")
        self.assertEqual(obj._game_of_message("剑三名名剑大会 33 排行".replace("名名", "名")), "jx3")
        # 只有共享词（单排/环境/战绩）时一律判不出，宁可不清理也不误伤剑三
        self.assertEqual(obj._game_of_message("查下本周单排环境"), "")
        self.assertEqual(obj._game_of_message("查一下某服单排战绩"), "")

    def test_hook_is_registered_in_main_module(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8-sig")
        self.assertIn("@filter.on_llm_request(priority=-100)", source)
        self.assertIn("await self._strip_foreign_hints(event, request)", source)

    def test_config_exposes_strip_switch(self):
        import json
        data = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8-sig"))
        entry = data.get("llm_tool_strip_foreign_hints")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.get("type"), "bool")
        self.assertTrue(entry.get("default"))


if __name__ == "__main__":
    unittest.main()
