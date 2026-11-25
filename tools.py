"""LLM 工具：把战绩查询能力暴露成 AstrBot 的 llm_tool，让模型用自然语言调用。

设计要点：
* 所有工具都是**只读**（除可选的解绑），不依赖 ``self.context``，只复用已有的查询核心
  ``_run_read`` / ``_public_search_rows`` / ``_season_report``。
* 工具把结果文本 yield 回去，由 AstrBot 作为 tool result 回传给 LLM，LLM 再组织成人话回答。
* 当 ``result_display_format`` 为「图片」且 ``llm_tool_send_image`` 开启时，查询类工具还会
  先按同一份正文渲染长图并 ``event.send`` 到会话，做到「指令」与「自然语言」两种入口都出图；
  发图失败只记日志，不影响 yield 给模型的文本结果。
* 为避免对上游（小黑盒）造成压力，工具复用 ``_start_query_cooldown`` / ``_query_lock`` /
  ``_ai_lock`` 等原有保护，并新增 ``llm_tool_max_text_chars`` 限制回传文本长度。
"""

from __future__ import annotations

import asyncio
import functools
import re
from collections.abc import AsyncGenerator
from types import SimpleNamespace

import aiohttp
import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter

from .ai_analysis import AnalysisError
from .heybox_api import DEFAULT_SERVER, HeyboxError
from .record_query import resolve_mode
from .report_image import render_report_images

# 免登录模式（none）下战绩查询退化为角色名片的说明，供多个工具复用
_NONE_MODE_HINT = (
    "当前插件为「免登录（无绑定）」模式，只能走免登录接口查询角色名片，"
    "无法查询近期战绩、赛季数据，也无法做战绩评价。请在插件配置里把 data_mode 切换为 heybox 或 wechat 并登录。"
)

# 长图渲染/发送可能抛出的异常：只兜这些，避免把真 bug 一起吞掉
_TOOL_IMAGE_ERRORS = (
    OSError, ValueError, TypeError, KeyError, IndexError, RuntimeError, AttributeError,
    HeyboxError, aiohttp.ClientError,
)

# ==================== 其它插件误注入提示的清理 ====================
# 背景：剑三插件（astrbot_plugin_mingjian）的 on_llm_request 钩子只要看到「战绩」这类通用
# 数据词，就会把「（内部提示：这是剑三数据查询，必须调用 mingjian_query…）」拼到用户 prompt
# 上，并在 event 上留下一个 query_retry_request，用于「模型没调工具就强制补调」。
# 永劫消息因此被当成剑三查询处理，模型开始胡说。这里在它之后跑一次，把注入的痕迹清掉。
#
# 判据只用**游戏专有名词**：单排/战绩/排行/强度 等都是两边共用的词（剑三的 ENV_GAME 正则里
# 就有「单排」），拿它们判定必然误伤；但 **双排/三排 是永劫的说法**，剑三说 22/33/55、
# 3v3/2v2/5v5，所以这两个词算永劫信号。分不出来时把剑三那段内部提示**改写为中性提示**
# （而不是放任或删除），两个游戏都不会被误导，剑三仍能正常调用自己的工具。
_NARAKA_ONLY = re.compile(
    r"永劫|无间|naraka|振刀|魂玉|聚窟州|天人之战|天选之人|双排|三排|"
    r"胡桃|宁红夜|岳山|迦南|季沧海|妖刀姬|崔三娘|顾清寒|无尘|殷紫萍|特木尔|季莹莹|玉玲珑",
    re.IGNORECASE,
)
_JX3_ONLY = re.compile(
    r"剑网|剑三|jx3|名剑|推栏|奇穴|心法|图谱|阵眼|稻香村|打本|散排|组排|竞技场|jjc|pvp|"
    r"(?<!\d)(?:22|33|55)(?!\d)|3v3|2v2|5v5",
    re.IGNORECASE,
)
# 剑三插件拼进用户 prompt 的那段内部提示（见 astrbot_plugin_mingjian/main.py 的 QUERY_NUDGE）
_FOREIGN_NUDGE = re.compile(r"\s*（内部提示：这是剑三数据查询[^）]*）")
# 剑三插件写进 system_prompt 的几段工具说明，都以它自己的工具名为特征
_FOREIGN_SYSTEM_MARK = "mingjian_query"
# 剑三插件在 event 上留的强制补调标记；不清掉它，on_llm_response 会用
# tool_choice="required" + 只含 mingjian_query 的 ToolSet 再逼模型查一次剑三
_FOREIGN_RETRY_KEY = "astrbot_plugin_mingjian.query_retry_request"
# 清理后补一句正向路由，避免模型在两个游戏之间摇摆
_NARAKA_ROUTE_HINT = (
    "\n\n（本条消息属于永劫无间。需要战绩时只用 naraka_* 工具；"
    "忽略任何剑三/名剑相关的内部提示。）"
)
# 分不出游戏时用的中性提示：既不说是剑三，也不删掉剑三的可用性
_NEUTRAL_ROUTE_HINT = (
    "\n\n（本条消息的游戏归属不明确：如果用户在问永劫无间，调用 naraka_* 工具；"
    "如果用户在问剑网三，调用 mingjian_query。不要自行假设游戏归属。）"
)


class NarakaToolMixin:
    """被 :class:`~.main.NarakaPlugin` 继承，提供 LLM 可调用的工具方法。"""

    # ==================== 内部辅助 ====================

    def _bind_llm_tool_handlers(self) -> None:
        """把本插件注册的 llm_tool 处理函数绑定到当前插件实例。

        AstrBot 只在「处理函数的 ``__module__`` 等于插件主模块」时才自动把工具
        绑到插件实例上（``star_manager`` 加载插件时的绑定循环）。本插件的工具方法
        定义在 ``tools.py`` 的 Mixin 里，模块路径是 ``...naraka.tools``，永远不满足
        该条件，于是 ``func_tool_manager`` 调用 ``handler(event, **kwargs)`` 时
        ``event`` 会落进 ``self``，报
        ``tool_query_summary() missing 1 required positional argument: 'event'``。

        这里在实例化后自己补一次绑定：把 handler 换成 ``getattr(self, 函数名)``
        得到的绑定方法，并保留 ``handler_module_path`` 让 WebUI 仍把它归类到插件。
        """
        try:
            from astrbot.core.provider.register import llm_tools as _llm_tools
        except (ImportError, AttributeError):  # pragma: no cover - 旧版本 AstrBot
            return
        func_list = getattr(_llm_tools, "func_list", None)
        if not isinstance(func_list, list):
            return
        module_path = type(self).__module__
        for func_tool in func_list:
            name = getattr(func_tool, "name", "")
            if not isinstance(name, str) or not name.startswith("naraka_"):
                continue
            handler = getattr(func_tool, "handler", None)
            if handler is None:
                continue
            raw = handler.func if isinstance(handler, functools.partial) else handler
            method = getattr(self, getattr(raw, "__name__", ""), None)
            if not callable(method):
                continue
            func_tool.handler = method
            func_tool.handler_module_path = module_path
            logger.debug(f"[naraka] 已绑定 llm tool 处理函数：{name} -> {module_path}")

    def _tool_enabled(self) -> bool:
        """总开关：``enable_llm_tools``（默认开启）。"""
        return bool(self.config.get("enable_llm_tools", True))

    def _tool_max_chars(self) -> int:
        value = self.config.get("llm_tool_max_text_chars", 4000)
        try:
            return max(500, int(value))
        except (TypeError, ValueError):
            return 4000

    def _tool_trim(self, text: str) -> str:
        limit = self._tool_max_chars()
        text = (text or "").strip()
        if len(text) <= limit:
            return text
        return text[:limit] + f"\n（结果过长，已截断到 {limit} 字；如需要更完整的数据，请使用对应的文字指令。）"

    def _tool_send_image_enabled(self) -> bool:
        """自然语言调用是否也把长图发到会话：``llm_tool_send_image``（默认开启）。"""
        value = self.config.get("llm_tool_send_image", True)
        if isinstance(value, str):
            return value.strip().lower() not in ("", "false", "0", "no", "off")
        return bool(value)

    def _tool_image_mode(self) -> bool:
        """当前显示格式是否要求出图（与 ``_body`` 判据保持一致）。"""
        return str(self.config.get("result_display_format", "文字") or "文字").strip() in ("图片", "image")

    # ==================== 清理其它插件的误注入提示 ====================

    def _strip_hints_enabled(self) -> bool:
        """``llm_tool_strip_foreign_hints``（默认开启）。"""
        value = self.config.get("llm_tool_strip_foreign_hints", True)
        if isinstance(value, str):
            return value.strip().lower() not in ("", "false", "0", "no", "off")
        return bool(value)

    def _hint_guard_ready(self) -> bool:
        """清理动作的总门控：智能工具总开关 + 本功能开关。"""
        return self._tool_enabled() and self._strip_hints_enabled()

    def _nickname_hit(self, text: str) -> bool:
        """消息里是否出现面板登记过的永劫昵称。

        ``nickname_mappings`` 是用户亲手为永劫登记的名字，命中即说明这条消息属于永劫，
        比模式词（单排/双排/三排 两个插件都在用）可靠得多。
        """
        from .mapping_config import nickname_key

        entries = self.config.get("nickname_mappings", [])
        if not isinstance(entries, list):
            return False
        haystack = nickname_key(text)
        if not haystack:
            return False
        for row in entries:
            if not isinstance(row, dict):
                continue
            key = nickname_key(row.get("nickname", ""))
            if key and key in haystack:
                return True
        return False

    def _game_of_message(self, text: str) -> str:
        """判定消息属于哪个游戏：``jx3`` / ``naraka`` / ``""``（分不出）。"""
        if _JX3_ONLY.search(text):
            return "jx3"
        if _NARAKA_ONLY.search(text) or self._nickname_hit(text):
            return "naraka"
        return ""

    async def _strip_foreign_hints(self, event: AstrMessageEvent, request) -> None:
        """把其它插件（当前是剑三插件）误注入到本次请求里的提示清掉。

        三种情形：
        * 消息明确属于剑三 → 一个字都不动；
        * 消息明确属于永劫 → 删掉注入文本、相关 system_prompt 段落与强制补调标记；
        * 分不出游戏（只有「单排」「战绩」这类共享词）→ 把那段内部提示**改写为中性提示**，
          并同样清掉强制补调标记，两个游戏都不会被误导。
        """
        if not self._hint_guard_ready():
            return
        try:
            text = str(event.get_message_str() or "").strip()
            if not text:
                return
            game = self._game_of_message(text)
            if game == "jx3":
                return
            prompt = request.prompt or ""
            system = request.system_prompt or ""
            if game == "naraka":
                new_prompt = _FOREIGN_NUDGE.sub("", prompt).rstrip()
                kept = [seg for seg in system.split("\n\n") if _FOREIGN_SYSTEM_MARK not in seg]
                new_system = "\n\n".join(kept).rstrip()
                if new_prompt == prompt.rstrip() and new_system == system.rstrip():
                    return  # 没有注入痕迹：一字不改
                request.prompt = new_prompt
                request.system_prompt = new_system + _NARAKA_ROUTE_HINT
                event.set_extra(_FOREIGN_RETRY_KEY, None)
                logger.info(
                    f"[naraka] 已清理其它插件误注入的剑三提示 session={event.unified_msg_origin} 消息={text[:40]}"
                )
                return
            # 分不出游戏：不删别人的提示（可能真是剑三在问），只把它换成中性的说法
            if not _FOREIGN_NUDGE.search(prompt) and _FOREIGN_SYSTEM_MARK not in system:
                return
            request.prompt = _FOREIGN_NUDGE.sub(_NEUTRAL_ROUTE_HINT, prompt).rstrip()
            if _FOREIGN_SYSTEM_MARK in system:
                kept = [seg for seg in system.split("\n\n") if _FOREIGN_SYSTEM_MARK not in seg]
                request.system_prompt = "\n\n".join(kept).rstrip() + _NEUTRAL_ROUTE_HINT
            event.set_extra(_FOREIGN_RETRY_KEY, None)
            logger.info(
                f"[naraka] 消息游戏归属不明，已把剑三注入的内部提示改为中性提示 session={event.unified_msg_origin} 消息={text[:40]}"
            )
        except (AttributeError, TypeError, ValueError, KeyError) as exc:
            logger.warning(f"[naraka] 清理误注入提示时出错，已跳过：{type(exc).__name__}: {exc}")


    async def _tool_deliver_image(self, event: AstrMessageEvent, text: str) -> None:
        """图片模式下把同一份正文渲染成长图并发到会话。

        只在「显示格式=图片」且开关开启时动作；渲染或发送失败只记日志，
        保证 yield 给模型的文本结果不受影响（不重复发文字，避免和 tool result 打架）。
        """
        text = (text or "").strip()
        if not text or not self._tool_send_image_enabled() or not self._tool_image_mode():
            return
        try:
            images = await asyncio.to_thread(render_report_images, text)
        except _TOOL_IMAGE_ERRORS as exc:  # 渲染失败：保留纯文本结果
            logger.warning(f"[naraka][tool] 长图渲染失败，已保留文字结果：{type(exc).__name__}")
            return
        if not images:
            return
        try:
            chain = []
            if self.result_method() == "quote":
                message_id = self._message_id(event)
                if message_id:
                    chain.append(Comp.Reply(id=message_id))
            chain.extend(Comp.Image.fromBytes(image) for image in images)
            await event.send(event.chain_result(chain))
        except _TOOL_IMAGE_ERRORS as exc:  # 发送失败：文本结果已经可用
            logger.warning(f"[naraka][tool] 长图发送失败，已保留文字结果：{type(exc).__name__}")
            return
        if self.config.get("send_original_image_file", False) is True:
            await self._send_original_images(event, images)

    @staticmethod
    def _tool_error_text(exc: Exception) -> str:
        """复用主类的错误文案；主类没有该实现时退化成原始信息。"""
        impl = getattr(NarakaToolMixin, "_error_text", None)
        if impl is None:
            return str(exc) or type(exc).__name__
        try:
            return impl(exc)
        except (AttributeError, TypeError):
            return str(exc) or type(exc).__name__

    async def _tool_residual_recover(self) -> None:
        """和命令通道一样，在查询前补一次残留解绑。失败只记日志。"""
        try:
            async with self._session() as session:
                await self._binding.recover(session, self._residual_interval_seconds())
        except (TimeoutError, HeyboxError, aiohttp.ClientError, ValueError) as exc:
            logger.warning(f"[naraka][tool] 残留恢复失败: {type(exc).__name__}")

    async def _tool_read_with_bind(self, player: str, mode: str, detailed: bool,
                                   force_refresh: bool = False) -> object:
        """只读查询 + 自动绑定兜底，返回 ``_ReadResult``。

        逻辑与命令版 ``_query_records`` 的阶段一/阶段二等价，但**不发送任何消息**，
        只在最后返回一个结果对象供调用方取 ``report_text`` / ``reason``。
        """
        report = None
        if isinstance(mode, str) and mode.strip():
            try:
                resolve_mode(mode)
            except ValueError as exc:
                return self._fail_result(str(exc))

        if self.data_mode == "none":
            return self._fail_result(_NONE_MODE_HINT)
        if not self.logged_in:
            return self._fail_result("尚未登录小黑盒，需要管理员私聊发送 /永劫登录。")

        await self._tool_residual_recover()

        # ---- 阶段一：只读 ----
        async with self._query_lock:
            try:
                report = await self._run_read(player, mode, detailed,
                                              force_refresh=force_refresh, for_ai=False)
            except (TimeoutError, HeyboxError, aiohttp.ClientError, ValueError) as exc:
                return self._fail_result(self._tool_error_text(exc))
        if report.ok or not report.needs_bind:
            return report

        # ---- 阶段二：绑定 → 再读 → 解绑 ----
        nickname = report.nickname or player
        if not self._auto_bind():
            return self._fail_result(f"没有找到「{nickname}」，请用 永劫搜索 核对昵称。")
        if self._binding.negative_hit(nickname.strip().lower()):
            return self._fail_result(f"「{nickname}」暂时没有可查数据，请稍后再试，或用 永劫搜索 核对昵称。")

        again = None
        async with self._session() as session, self._binding.bound(session, nickname, DEFAULT_SERVER) as outcome:
            if not outcome.ok:
                return self._fail_result(
                    self._bind_failure_text(nickname, outcome, forced=False))
            try:
                async with self._query_lock:
                    again = await self._run_read(player, mode, detailed,
                                                 force_refresh=force_refresh, for_ai=False)
            except (TimeoutError, HeyboxError, aiohttp.ClientError, ValueError) as exc:
                again = self._fail_result(self._tool_error_text(exc))
            if not again.ok and getattr(again, "needs_bind", False):
                    self._binding.mark_negative(nickname.strip().lower())
        return again or report

    def _fail_result(self, reason: str) -> object:
        """构造一个失败的 ``_ReadResult``。

        优先复用主类里的 ``_ReadResult``；万一拿不到（例如在测试替身里、
        或 main 尚未完成导入），退化成一个等价的轻量对象，避免把整个
        main 及其第三方依赖（qrcode 等）拖进来。
        """
        try:
            from .main import _ReadResult  # 局部导入：tools 被 main 引用

            return _ReadResult(ok=False, reason=reason)
        except ImportError:
            return SimpleNamespace(ok=False, reason=reason, texts=[], needs_bind=False,
                                   nickname="", report_text="", analysis_data={})

    # ==================== 具体工具 ====================

    @filter.llm_tool(name="naraka_query_summary")
    async def tool_query_summary(self, event: AstrMessageEvent, player: str, mode: str = "") -> AsyncGenerator:
        """查询《永劫无间》端游玩家近期战绩概览（场均伤害、击败、振刀、恢复、存活等）。

        用于回答"某人最近打得怎么样""场均伤害多少"这类问题。
        注意：必须传玩家昵称（nickname），本工具不支持按角色ID查询。

        Args:
            player(string): 玩家昵称，例如"张三"；不支持角色ID
            mode(string): 可选，模式筛选，如"天人""天选""单排""双排""三排"；留空自动选择最新一场排位对局的模式
        """
        if not self._tool_enabled():
            yield "战绩工具已关闭（enable_llm_tools=false）。"
            return
        if not player or not str(player).strip():
            yield "需要提供玩家昵称。"
            return
        player = str(player).strip()
        mode = str(mode or "").strip()
        if not self._allowed_group(event):
            yield "当前群不在白名单内，无法使用战绩工具。"
            return
        cooldown = self._start_query_cooldown()
        if cooldown:
            yield f"查询过于频繁，请 {cooldown} 秒后再试。"
            return
        if self.data_mode == "none":
            yield _NONE_MODE_HINT
            return
        result = await self._tool_read_with_bind(player, mode, detailed=False)
        if getattr(result, "ok", False):
            full = getattr(result, "report_text", "") or "\n".join(getattr(result, "texts", []) or [])
            await self._tool_deliver_image(event, full)
            yield self._tool_trim(full)
        else:
            yield getattr(result, "reason", "") or "查询失败"

    @filter.llm_tool(name="naraka_query_detail")
    async def tool_query_detail(self, event: AstrMessageEvent, player: str, mode: str = "") -> AsyncGenerator:
        """查询《永劫无间》端游玩家近期逐场战绩（每局的英雄、伤害、击败、排名等明细）。

        用于回答"某某一局具体数据""最近几场分别怎么样"这类问题。

        Args:
            player(string): 玩家昵称；不支持角色ID
            mode(string): 可选，模式筛选（天人/天选/单排/双排/三排）；留空自动选择最新排位模式
        """
        if not self._tool_enabled():
            yield "战绩工具已关闭（enable_llm_tools=false）。"
            return
        if not player or not str(player).strip():
            yield "需要提供玩家昵称。"
            return
        player = str(player).strip()
        mode = str(mode or "").strip()
        if not self._allowed_group(event):
            yield "当前群不在白名单内，无法使用战绩工具。"
            return
        cooldown = self._start_query_cooldown()
        if cooldown:
            yield f"查询过于频繁，请 {cooldown} 秒后再试。"
            return
        if self.data_mode == "none":
            yield _NONE_MODE_HINT
            return
        result = await self._tool_read_with_bind(player, mode, detailed=True)
        if getattr(result, "ok", False):
            full = getattr(result, "report_text", "") or "\n".join(getattr(result, "texts", []) or [])
            await self._tool_deliver_image(event, full)
            yield self._tool_trim(full)
        else:
            yield getattr(result, "reason", "") or "查询失败"

    @filter.llm_tool(name="naraka_search_player")
    async def tool_search_player(self, event: AstrMessageEvent, nickname: str) -> AsyncGenerator:
        """搜索《永劫无间》玩家（免登录，不需要登录也能用）。

        用于确认昵称对应的玩家是否存在、以及精确昵称是什么。搜不到时用它核对昵称。

        Args:
            nickname(string): 要搜索的玩家昵称
        """
        if not self._tool_enabled():
            yield "战绩工具已关闭（enable_llm_tools=false）。"
            return
        if not nickname or not str(nickname).strip():
            yield "需要提供玩家昵称。"
            return
        nickname = str(nickname).strip()
        if not self._allowed_group(event):
            yield "当前群不在白名单内，无法使用战绩工具。"
            return
        cooldown = self._start_query_cooldown()
        if cooldown:
            yield f"查询过于频繁，请 {cooldown} 秒后再试。"
            return
        try:
            async with self._session() as session:
                rows = await self._public_search_rows(session, nickname, limit=10)
        except (TimeoutError, HeyboxError, aiohttp.ClientError, ValueError) as exc:
            yield f"查询失败：{self._tool_error_text(exc)}"
            return
        if not rows:
            yield f"没有找到「{nickname}」。"
            return
        lines = [f"搜索「{nickname}」："]
        for row in rows[:10]:
            lines.append(self._public_row_line(row))
        lines.append(f"查近期对局：战绩查询 {nickname}")
        yield self._tool_trim("\n".join(lines))

    @filter.llm_tool(name="naraka_season_stats")
    async def tool_season_stats(self, event: AstrMessageEvent, player: str,
                                mode: str = "", season: str = "当前") -> AsyncGenerator:
        """查询《永劫无间》玩家的赛季总览数据（段位、评分、赛季场均等）。

        用于回答"某某这个赛季什么段位""赛季数据怎么样"这类问题。

        Args:
            player(string): 玩家昵称；不支持角色ID
            mode(string): 可选，天人/天选 + 单排/双排/三排；赛季总览只支持这些排位模式
            season(string): 可选，"当前"=配置的当前赛季，"全部"=全部赛季，"列表"=列出可用赛季，或直接填赛季名
        """
        if not self._tool_enabled():
            yield "战绩工具已关闭（enable_llm_tools=false）。"
            return
        if not player or not str(player).strip():
            yield "需要提供玩家昵称。"
            return
        player = str(player).strip()
        mode = str(mode or "").strip()
        season = str(season or "当前").strip()
        if not self._allowed_group(event):
            yield "当前群不在白名单内，无法使用战绩工具。"
            return
        if self.data_mode == "none":
            yield _NONE_MODE_HINT
            return
        if not self.logged_in:
            yield "尚未登录小黑盒，需要管理员私聊发送 /永劫登录。"
            return
        mode_id = None
        if mode:
            try:
                mode_id = resolve_mode(mode)
            except ValueError as exc:
                yield str(exc)
                return
            if mode_id not in self._ranked_ids():
                yield "赛季总览只支持天人和天选的单排、双排、三排。"
                return
        cooldown = self._start_query_cooldown()
        if cooldown:
            yield f"查询过于频繁，请 {cooldown} 秒后再试。"
            return
        if self._query_lock.locked():
            yield "已有一份战绩正在查询，请稍后再试。"
            return
        chunks: list[str] = []
        async with self._query_lock:
            async for message in self._season_report(event, player, mode_id, season):
                text = getattr(getattr(message, "chain", None), "get_plain_text", None)
                chunks.append(text() if callable(text) else str(getattr(message, "text", "")))
        full = "\n".join(chunk for chunk in chunks if chunk)
        await self._tool_deliver_image(event, full)
        yield self._tool_trim(full)

    @filter.llm_tool(name="naraka_ai_analysis")
    async def tool_ai_analysis(self, event: AstrMessageEvent, player: str, mode: str = "") -> AsyncGenerator:
        """让 AI 对《永劫无间》玩家近期战绩做一段评价（优点、可改进点、可执行建议）。

        用于回答"评价一下某某的战绩""某某打得有什么问题"这类问题。会用已配置的模型+人格生成评价。

        Args:
            player(string): 玩家昵称；不支持角色ID
            mode(string): 可选，天人/天选 + 单排/双排/三排；留空自动选择最新排位模式
        """
        if not self._tool_enabled():
            yield "战绩工具已关闭（enable_llm_tools=false）。"
            return
        if self.config.get("enable_ai_analysis", True) is not True:
            yield "AI战绩分析已关闭，请管理员在插件配置中启用。"
            return
        if not player or not str(player).strip():
            yield "需要提供玩家昵称。"
            return
        player = str(player).strip()
        mode = str(mode or "").strip()
        if not self._allowed_group(event):
            yield "当前群不在白名单内，无法使用战绩工具。"
            return
        if self.data_mode == "none":
            yield _NONE_MODE_HINT
            return
        if not self.logged_in:
            yield "尚未登录小黑盒，需要管理员私聊发送 /永劫登录。"
            return
        cooldown = self._start_query_cooldown()
        if cooldown:
            yield f"查询过于频繁，请 {cooldown} 秒后再试。"
            return
        if self._ai_lock.locked():
            yield "已有AI分析正在进行，请稍后再试。"
            return
        async with self._ai_lock:
            result = await self._tool_read_with_bind(player, mode, detailed=False, force_refresh=False)
            if not getattr(result, "ok", False):
                yield getattr(result, "reason", "") or "查询失败"
                return
            # 重新取一次带 for_ai 的分析数据
            async with self._query_lock:
                try:
                    ai_result = await self._run_read(player, mode, False, force_refresh=False, for_ai=True)
                except (TimeoutError, HeyboxError, aiohttp.ClientError, ValueError) as exc:
                    yield self._tool_error_text(exc)
                    return
            if not getattr(ai_result, "ok", False):
                yield getattr(ai_result, "reason", "") or "查询失败"
                return
            data = getattr(ai_result, "analysis_data", {}) or {}
            try:
                analysis = await self._evaluate_records(event, data)
            except (AnalysisError, TimeoutError, HeyboxError,
                    aiohttp.ClientError, ValueError) as exc:
                yield f"AI分析未完成：{self._tool_error_text(exc)}"
                return
            # 和命令通道保持一致：原战绩全文 + AI战绩分析
            full = (getattr(ai_result, "report_text", "") or "") + "\n\nAI战绩分析\n" + analysis
            await self._tool_deliver_image(event, full)
            yield self._tool_trim(full)

    @filter.llm_tool(name="naraka_unbind")
    async def tool_unbind(self, event: AstrMessageEvent, nickname: str) -> AsyncGenerator:
        """手动解绑《永劫无间》玩家绑定（清除该昵称在小黑盒账号上的绑定）。

        会写操作，有副作用：仅在确实需要清理绑定残留时使用。

        Args:
            nickname(string): 要解绑的玩家昵称
        """
        if not self._tool_enabled() or not self.config.get("llm_tool_allow_unbind", False):
            yield "解绑工具未开启（需要 enable_llm_tools=true 且 llm_tool_allow_unbind=true）。"
            return
        if not nickname or not str(nickname).strip():
            yield "需要提供玩家昵称。"
            return
        nickname = str(nickname).strip()
        if not self._allowed_group(event):
            yield "当前群不在白名单内，无法使用战绩工具。"
            return
        if self.data_mode == "none":
            yield "当前为无绑定模式，无需解绑。"
            return
        if not self.logged_in:
            yield "尚未登录小黑盒，无法解绑。"
            return
        async with self._session() as session:
            ok, reason = await self._binding.manual_unbind(session, nickname, DEFAULT_SERVER)
        logger.info(f"[naraka][tool] 解绑 nickname={nickname!r} ok={ok}")
        yield f"已解绑 {nickname}" if ok else f"{nickname} 解绑失败：{reason}"

    def _ranked_ids(self):
        from .record_query import RANKED_IDS

        return RANKED_IDS


__all__ = ["NarakaToolMixin"]
