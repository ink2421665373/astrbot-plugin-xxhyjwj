"""AstrBot plugin-view API; configuration remains in AstrBotConfig."""
import asyncio
import copy
import time

from .diagnostics import DiagnosticStore
from .mapping_config import (
    MAPPING_KEYS,
    collect_season_catalog,
    flatten_nickname_groups,
    group_nickname_rows,
    mapping_snapshot,
    validate_mappings,
    validate_nickname_mappings,
)


class MappingViewMixin:
    def _setup_mapping_view(self, context):
        self._mapping_view_lock = asyncio.Lock()
        self._diagnostics = DiagnosticStore()
        if not callable(getattr(context, "register_web_api", None)):
            return False
        try:
            from astrbot.api import web
        except ImportError:
            return False
        self._mapping_web = web
        context.register_web_api("/astrbot_plugin_naraka/mappings", self.view_get_mappings, ["GET"], "读取英雄和赛季映射")
        context.register_web_api("/astrbot_plugin_naraka/mappings/save", self.view_save_mappings, ["POST"], "保存英雄和赛季映射")
        context.register_web_api("/astrbot_plugin_naraka/mappings/seasons/sync", self.view_sync_seasons, ["POST"], "从接口同步完整赛季目录")
        context.register_web_api("/astrbot_plugin_naraka/mappings/login-status", self.view_login_status, ["GET"], "查看登录状态摘要，不返回凭证")
        context.register_web_api("/astrbot_plugin_naraka/diagnostics", self.view_diagnostics, ["GET"], "读取脱敏接口数据诊断")
        context.register_web_api("/astrbot_plugin_naraka/diagnostics/clear", self.view_clear_diagnostics, ["POST"], "清空内存中的接口诊断记录")
        context.register_web_api("/astrbot_plugin_naraka/diagnostics/cache/clear", self.view_clear_cache, ["POST"], "清空单局详情缓存")
        context.register_web_api("/astrbot_plugin_naraka/nicknames", self.view_get_nicknames, ["GET"], "读取昵称与角色ID对照")
        context.register_web_api("/astrbot_plugin_naraka/nicknames/save", self.view_save_nicknames, ["POST"], "保存昵称与角色ID对照")
        context.register_web_api("/astrbot_plugin_naraka/nicknames/resolve", self.view_resolve_nickname, ["POST"], "按昵称搜索并解析角色唯一标识")
        return True

    async def view_diagnostics(self):
        return self._mapping_web.json_response(dict(self._diagnostics.snapshot(), cache_entries=len(getattr(self, "_detail_cache", {}))))

    async def view_clear_cache(self):
        if self._query_lock.locked():
            return self._mapping_web.error_response("战绩查询正在执行，请等待结束后再清空缓存", status_code=409)
        cache = getattr(self, "_detail_cache", {})
        count = len(cache)
        cache.clear()
        return self._mapping_web.json_response(dict(cleared_count=count, cache_entries=0))

    async def view_clear_diagnostics(self):
        self._diagnostics.clear()
        return self._mapping_web.json_response(self._diagnostics.snapshot())

    async def _diagnosed_call(self, kind, mode, season, request):
        started = time.perf_counter()
        try:
            body = await request()
        except Exception as exc:
            self._record_diagnostic(kind, mode, season, elapsed_ms=(time.perf_counter() - started) * 1000, error=type(exc).__name__)
            raise
        self._record_diagnostic(kind, mode, season, body, elapsed_ms=(time.perf_counter() - started) * 1000)
        return body

    def _record_diagnostic(self, *args, **kwargs):
        try:
            self._diagnostics.append(*args, **kwargs)
        except Exception:
            # Observability must never interrupt a battle query or expose raw data.
            pass

    def _web_login_status(self):
        file_state = self._reload_web_auth()
        return dict(data_mode=self.data_mode, has_credentials=bool(self.logged_in),
                    credential_file_state=file_state, online_verified=False)

    async def view_get_nicknames(self):
        snapshot = dict(mapping_snapshot(self.config))
        return self._mapping_web.json_response(
            dict(snapshot, roles=group_nickname_rows(snapshot.get("nickname_mappings")),
                 login_status=self._web_login_status()))

    async def view_save_nicknames(self):
        web = self._mapping_web
        payload = await web.request.json(default=None)
        if not isinstance(payload, dict):
            return web.error_response("请求内容必须是对象", status_code=400)
        if "roles" in payload:
            # 面板按「一个角色多个昵称」提交；配置里仍保存成一行一个昵称。
            roles = payload.get("roles")
            if not isinstance(roles, list):
                return web.error_response("roles 必须是列表", status_code=400)
            for item in roles:
                if not isinstance(item, dict):
                    return web.error_response("roles 的每一项必须是对象", status_code=400)
                kept = [nick for nick in (item.get("nicknames") or []) if str(nick).strip()]
                if not kept:
                    return web.error_response(
                        f"角色 {str(item.get('role_id') or '（未知）')} 至少要保留一个昵称；"
                        "要移除整个角色，请直接删除该行",
                        status_code=400)
            entries = flatten_nickname_groups(roles)
        else:
            entries = payload.get("nickname_mappings")
        try:
            values = {"nickname_mappings": validate_nickname_mappings(entries)}
        except ValueError as exc:
            return web.error_response(str(exc), status_code=400)
        async with self._mapping_view_lock:
            if payload.get("revision") != mapping_snapshot(self.config)["revision"]:
                return web.error_response("配置已被其他页面修改，请重新载入后再编辑", status_code=409)
            before = copy.deepcopy(self.config.get("nickname_mappings", []))
            try:
                self.config.save_config(values)
            except Exception:
                self.config["nickname_mappings"] = before
                return web.error_response("配置保存失败，未应用本次修改，请检查 AstrBot 日志与磁盘权限", status_code=500)
            snapshot = dict(mapping_snapshot(self.config))
            return web.json_response(
                dict(snapshot, roles=group_nickname_rows(snapshot.get("nickname_mappings")),
                     saved=True))

    async def view_resolve_nickname(self):
        """面板里输入昵称 → 走免登录搜索拿 role_id（只读展示用，不写入配置）。"""
        web = self._mapping_web
        payload = await web.request.json(default=None)
        if not isinstance(payload, dict):
            return web.error_response("请求内容必须是对象", status_code=400)
        nickname = payload.get("nickname")
        if not isinstance(nickname, str) or not nickname.strip() or len(nickname) > 80:
            return web.error_response("请填写要解析的玩家昵称", status_code=400)
        nickname = nickname.strip()
        cooldown = self._start_query_cooldown()
        if cooldown:
            return web.error_response(f"请求过于频繁，请{cooldown}秒后再试", status_code=429)
        try:
            async with self._session() as session:
                rows = await self._public_search_rows(session, nickname, limit=20)
        except Exception as exc:
            return web.error_response(f"搜索失败：{self._error_text(exc)}", status_code=400)
        exact = [row for row in rows if str(row.get("role_name") or "").strip() == nickname]
        candidates = exact or rows
        if not candidates:
            return web.error_response(f"没有找到「{nickname}」，请核对昵称后重试。", status_code=404)
        if len(candidates) > 1 and not exact:
            preview = [{"role_id": str(row.get("role_id") or ""),
                        "role_name": str(row.get("role_name") or ""),
                        "server": str(row.get("server") or "")} for row in candidates[:10]]
            try:
                return web.error_response("匹配到多个玩家，请填写更精确的昵称", status_code=409, extra=preview)
            except TypeError:
                return web.error_response(
                    "匹配到多个玩家，请填写更精确的昵称：" + "；".join(
                        f"{item['role_name'] or '—'}（{item['role_id']}）" for item in preview),
                    status_code=409)
        row = candidates[0]
        role_id = str(row.get("role_id") or "")
        if not role_id:
            return web.error_response("搜索结果缺少角色ID", status_code=400)
        return web.json_response({
            "nickname": nickname,
            "role_id": role_id,
            "role_name": str(row.get("role_name") or nickname),
            "server": str(row.get("server") or "163"),
        })

    async def view_login_status(self):
        return self._mapping_web.json_response(self._web_login_status())

    async def _remember_season_catalog(self, options):
        async with self._mapping_view_lock:
            before = copy.deepcopy(self.config.get("season_mappings", []))
            merged, stats = collect_season_catalog(before, options)
            if merged != before:
                try:
                    self.config.save_config({"season_mappings": merged})
                except Exception:
                    self.config["season_mappings"] = before
                    raise ValueError("赛季列表已获取，但保存配置失败，请检查磁盘权限") from None
            return stats

    async def view_sync_seasons(self):
        web = self._mapping_web
        payload = await web.request.json(default=None)
        if not isinstance(payload, dict):
            return web.error_response("请求内容必须是对象", status_code=400)
        nickname = payload.get("nickname")
        mode = payload.get("mode", "12")
        if not isinstance(nickname, str) or not nickname.strip() or len(nickname) > 80:
            return web.error_response("请填写用于查询的真实玩家昵称", status_code=400)
        if mode not in ("5000000", "12", "5000001", "4", "13", "5"):
            return web.error_response("请选择有效的天选或天人模式", status_code=400)
        if payload.get("revision") != mapping_snapshot(self.config)["revision"]:
            return web.error_response("配置已更新，请先重新载入再同步", status_code=409)
        login_status = self._web_login_status()
        if login_status["data_mode"] == "none":
            return web.error_response("当前插件数据模式为 none（免登录），此模式不能同步赛季。请在插件配置改为 heybox 或 wechat，保存并重载；已有登录无需重复扫码。", status_code=400)
        if not login_status["has_credentials"]:
            reason = {"missing": "当前插件的登录文件不存在", "empty": "当前插件登录文件中没有凭证",
                      "invalid": "当前插件登录文件格式无法读取", "unreadable": "当前插件登录文件读取失败，请检查文件权限"}.get(login_status["credential_file_state"], "当前插件未读取到凭证")
            return web.error_response(f"{reason}。如果聊天中已提示登录成功，请先重载本插件并刷新登录状态，确认登录的是同一个 AstrBot 实例中的本插件；仍未登录时再发送 /永劫登录。无需提供登录凭证。", status_code=400)
        if self._query_lock.locked():
            return web.error_response("已有战绩查询或赛季同步正在执行，请稍后再试", status_code=409)
        cooldown = self._start_query_cooldown()
        if cooldown:
            return web.error_response(f"请求过于频繁，请{cooldown}秒后再试", status_code=429)
        try:
            async with self._query_lock:
                async with self._session() as session:
                    player, reason = await self._resolve_player(session, nickname.strip())
                    if player is None:
                        raise ValueError(reason or "未找到可直接查询的玩家，请核对昵称；同步不会自动绑定账号")
                    role_id = str(player.get("role_id") or "")
                    if not role_id:
                        raise ValueError("搜索结果缺少角色ID")
                    common = dict(role_id=role_id, server=str(player.get("server") or "163"),
                                  heybox_id=str(self._auth.get("uid") or self._auth.get("cookies", {}).get("heybox_id") or ""))
                    before = {item.get("season_id") for item in self.config.get("season_mappings", []) if isinstance(item, dict)}
                    body = await self._home_data(session, common, mode, "pre-01")
                    # _home_data also collects during ordinary bot queries.
                    # Calling again is idempotent and verifies persistence here.
                    stats = await self._remember_season_catalog(body.get("seasons"))
                    after = {item["season_id"] for item in self.config.get("season_mappings", [])}
                    stats["added_count"] = len(after - before)
            return web.json_response(dict(mapping_snapshot(self.config), **stats, synced=True))
        except Exception as exc:
            return web.error_response(f"赛季同步失败：{self._error_text(exc)}", status_code=400)

    async def view_get_mappings(self):
        return self._mapping_web.json_response(dict(mapping_snapshot(self.config), login_status=self._web_login_status()))

    async def view_save_mappings(self):
        web = self._mapping_web
        payload = await web.request.json(default=None)
        try:
            values = validate_mappings(payload)
        except ValueError as exc:
            return web.error_response(str(exc), status_code=400)
        async with self._mapping_view_lock:
            if payload.get("revision") != mapping_snapshot(self.config)["revision"]:
                return web.error_response("配置已被其他页面修改，请重新载入后再编辑", status_code=409)
            before = {key: copy.deepcopy(self.config.get(key, [])) for key in MAPPING_KEYS}
            try:
                # A single save updates only the two mapping fields. No tokens,
                # login state, reply settings or other plugin config are exposed.
                self.config.save_config(values)
            except Exception:
                self.config.update(before)
                return web.error_response("配置保存失败，未应用本次修改，请检查 AstrBot 日志与磁盘权限", status_code=500)
            return web.json_response(dict(mapping_snapshot(self.config), saved=True))
