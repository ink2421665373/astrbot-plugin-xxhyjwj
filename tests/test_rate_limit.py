"""Offline upstream rate-limit checks, with no authenticated requests."""
import ast
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.utils import format_datetime
import importlib
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
package = ModuleType("naraka_test_package")
package.__path__ = [str(ROOT)]
sys.modules.setdefault("naraka_test_package", package)
api = importlib.import_module("naraka_test_package.heybox_api")
BindingManager = importlib.import_module("naraka_test_package.binding").BindingManager
from delivery import QueryProgress


class RateLimitTests(unittest.TestCase):
    def harness(self, **config):
        self.clock = [100.0]
        self.sleeps = []
        async def sleep(seconds):
            self.sleeps.append(seconds)
            self.clock[0] += seconds
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8-sig"))
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "NarakaPlugin")
        names = {"_number_setting", "_interval_b", "_interval_c", "_detail_concurrency", "_paced",
                 "_start_query_cooldown",
                 "_error_text", "_resolve_player", "_refresh", "_query_records"}
        cls = ast.ClassDef(name="Harness", bases=[], keywords=[], decorator_list=[],
                           body=[node for node in cls.body if getattr(node, "name", "") in names])
        functions = [node for node in tree.body if getattr(node, "name", "") in ("_is_rate_limited", "_must_stop_after_error")]
        env = dict(asynccontextmanager=asynccontextmanager,
            asyncio=SimpleNamespace(sleep=sleep, TimeoutError=asyncio.TimeoutError),
            time=SimpleNamespace(monotonic=lambda: self.clock[0]), logger=Mock(),
            HeyboxError=api.HeyboxError, HeyboxRateLimited=api.HeyboxRateLimited,
            HeyboxLoginRequired=api.HeyboxLoginRequired, aiohttp=api.aiohttp,
            QueryProgress=QueryProgress, AstrMessageEvent=object, DEFAULT_SERVER="163",
            looks_like_role_id=lambda text: False, nickname_lookup=lambda config, name: None,
            _ReadResult=lambda **kwargs: SimpleNamespace(**kwargs))
        exec(compile(ast.fix_missing_locations(ast.Module(body=functions + [cls], type_ignores=[])), "main.py", "exec"), env)
        view = env["Harness"]()
        view.config = dict(enable_query_progress=False, **config)
        view._request_lock = asyncio.Lock()
        view._query_lock = asyncio.Lock()
        view._last_request_at = view._last_detail_request_at = view._last_query_started_at = None
        view._rate_limited_until = 0
        view._api = SimpleNamespace(update=AsyncMock())
        view._update_retry_interval = lambda: 3
        view._public_search_rows = AsyncMock(return_value=[])
        view._search = AsyncMock(return_value=[])
        view._login_search_fallback = lambda: True
        view.logged_in = True
        view.data_mode = "heybox"
        view._allowed_group = lambda event: True
        view._ack = AsyncMock(return_value=None)
        view._note = lambda event, text: text
        view._residual_interval_seconds = lambda: 3600
        view._auto_bind = lambda: True
        view._binding = SimpleNamespace(recover=AsyncMock(), bound=Mock(), negative_hit=lambda key: False, mark_negative=Mock())
        @asynccontextmanager
        async def session():
            yield None
        view._session = session
        return view

    def test_http_429_with_non_json_body_keeps_endpoint_and_retry_after(self):
        response = SimpleNamespace(status=429, headers={"Retry-After": "120"}, text=AsyncMock(return_value="<html>slow down</html>"))
        @asynccontextmanager
        async def request(*args, **kwargs):
            yield response
        session = SimpleNamespace(request=request)
        with self.assertRaises(api.HeyboxRateLimited) as result:
            asyncio.run(api.HeyboxClient().raw(session, "GET", "/game/yjwj/home/data"))
        self.assertEqual(result.exception.path, "/game/yjwj/home/data")
        self.assertEqual(result.exception.retry_after, 120)

    def test_business_limit_envelopes_are_consistent_for_both_search_channels(self):
        for payload in (dict(status="429"), dict(status="error", message="请求过于频繁"),
                        dict(status="error", msg="too many requests"), dict(status="error", msg="接口限流"),
                        dict(status="error", msg="请求频率过高，请降低请求频率")):
            with self.assertRaises(api.HeyboxRateLimited):
                api.check_envelope(payload, "/game/yjwj/search")
            client = api.HeyboxClient()
            client.raw = AsyncMock(return_value=payload)
            with self.assertRaises(api.HeyboxRateLimited) as result:
                asyncio.run(client.call_lenient(None, "GET", "/game/player_search/do"))
            self.assertEqual(result.exception.path, "/game/player_search/do")
        self.assertEqual(api.check_envelope(dict(result={})), {})
        with self.assertRaises(api.HeyboxLoginRequired):
            api.check_envelope(dict(status="login", msg="请登录"))

    def test_retry_after_accepts_http_date_and_rejects_invalid_values(self):
        stamp = datetime(2026, 10, 26, tzinfo=timezone.utc).timestamp()
        header = format_datetime(datetime.fromtimestamp(stamp + 90, timezone.utc), usegmt=True)
        with patch.object(api.time, "time", return_value=stamp):
            self.assertEqual(api._retry_after_seconds(header), 90)
        for value in (None, "nan", "inf", -1, True, "not-a-date"):
            self.assertIsNone(api._retry_after_seconds(value))

    def test_rate_limit_pauses_all_requests_even_when_fixed_intervals_are_zero(self):
        view = self.harness(request_interval_seconds=0, query_interval_seconds=0)
        network = AsyncMock(side_effect=api.HeyboxRateLimited("请求过于频繁", path="/game/yjwj/update"))
        async def run():
            with self.assertRaises(api.HeyboxRateLimited) as result:
                async with view._paced():
                    await network()
            self.assertEqual(result.exception.retry_after, 60)
            self.assertIn("插件已暂停上游请求", view._error_text(result.exception))
            until = view._rate_limited_until
            self.clock[0] += 12
            self.assertEqual(view._start_query_cooldown(), 48)
            with self.assertRaises(api.HeyboxRateLimited):
                async with view._paced():
                    await network()
            self.assertEqual(view._rate_limited_until, until)
            self.clock[0] = until
            network.side_effect = None
            self.assertEqual(view._start_query_cooldown(), 0)
            async with view._paced():
                await network()
        asyncio.run(run())
        self.assertEqual(network.await_count, 2)
        self.assertEqual(self.sleeps, [])

    def test_server_retry_after_takes_precedence_over_shorter_local_pause(self):
        view = self.harness(rate_limit_cooldown_seconds=60)
        async def run():
            with self.assertRaises(api.HeyboxRateLimited):
                async with view._paced():
                    raise api.HeyboxRateLimited(retry_after=180)
        asyncio.run(run())
        self.assertEqual(view._start_query_cooldown(), 180)

    def test_normal_spacing_and_extra_detail_spacing_take_the_maximum(self):
        view = self.harness(request_interval_seconds=2, detail_interval_seconds=4)
        async def run():
            async with view._paced(detail=True):
                pass
            async with view._paced():
                pass
            async with view._paced(detail=True):
                pass
        asyncio.run(run())
        self.assertEqual(self.sleeps, [2, 2])

    def test_limit_in_public_search_never_triggers_login_search_or_binding(self):
        view = self.harness()
        async def public(*args, **kwargs):
            async with view._paced():
                raise api.HeyboxRateLimited(path="/game/player_search/do")
        view._public_search_rows = AsyncMock(side_effect=public)
        async def read(*args, **kwargs):
            return await view._resolve_player(None, "示例")
        view._run_read = read
        async def run():
            return [text async for text in view._query_records(None, "示例", "")]
        output = asyncio.run(run())
        self.assertEqual(len(output), 1)
        self.assertIn("小黑盒已限流", output[0])
        view._search.assert_not_awaited()
        view._binding.bound.assert_not_called()
        view._binding.mark_negative.assert_not_called()

    def test_limit_in_login_search_does_not_turn_into_missing_player(self):
        view = self.harness()
        view._search.side_effect = api.HeyboxRateLimited()
        with self.assertRaises(api.HeyboxRateLimited):
            asyncio.run(view._resolve_player(None, "示例"))

    def test_search_timeout_is_not_treated_as_player_absence(self):
        view = self.harness()
        view._public_search_rows.side_effect = asyncio.TimeoutError()
        with self.assertRaises(asyncio.TimeoutError):
            asyncio.run(view._resolve_player(None, "示例"))
        view._search.assert_not_awaited()
        view._public_search_rows.side_effect = None
        view._search.side_effect = api.HeyboxNetworkError("offline")
        with self.assertRaises(api.HeyboxNetworkError):
            asyncio.run(view._resolve_player(None, "示例"))

    def test_update_limit_stops_initial_request_and_polling(self):
        for poll in (False, True):
            view = self.harness()
            error = api.HeyboxRateLimited(path="/game/yjwj/update")
            view._api.update.side_effect = [dict(state="waiting"), error] if poll else error
            with self.assertRaises(api.HeyboxRateLimited):
                asyncio.run(view._refresh(None, "example", "163"))
            self.assertEqual(view._api.update.await_count, 2 if poll else 1)
            self.assertEqual(view._start_query_cooldown(), 60)

    def test_limited_unbind_stops_retries_and_records_residual(self):
        with tempfile.TemporaryDirectory() as directory:
            fake_api = SimpleNamespace(unbind_game=AsyncMock(side_effect=api.HeyboxRateLimited()))
            manager = BindingManager(fake_api, Path(directory) / "state.json", unbind_attempts=3, unbind_interval=0)
            ok, _ = asyncio.run(manager.manual_unbind(None, "示例"))
            self.assertFalse(ok)
            fake_api.unbind_game.assert_awaited_once()
            self.assertEqual([item.nickname for item in manager.residuals()], ["示例"])

    def test_limited_bind_preserves_cleanup_record_without_more_network_requests(self):
        view = self.harness()
        with tempfile.TemporaryDirectory() as directory:
            fake_api = SimpleNamespace(bind_game=AsyncMock(side_effect=api.HeyboxRateLimited()), unbind_game=AsyncMock())
            manager = BindingManager(fake_api, Path(directory) / "state.json", pace=view._paced)
            async def run():
                async with manager.bound(None, "示例") as outcome:
                    self.assertFalse(outcome.ok)
                self.assertFalse(outcome.unbind_ok)
            asyncio.run(run())
            fake_api.bind_game.assert_awaited_once()
            fake_api.unbind_game.assert_not_awaited()
            self.assertEqual([item.nickname for item in manager.residuals()], ["示例"])
            self.assertFalse(manager.negative_hit("示例"))

    def test_bound_query_limit_is_not_negative_cached_and_cleanup_still_runs(self):
        view = self.harness()
        cleaned = []
        @asynccontextmanager
        async def bound(*args):
            try:
                yield SimpleNamespace(ok=True, unbind_ok=True)
            finally:
                cleaned.append(True)
        view._binding.bound = bound
        view._run_read = AsyncMock(side_effect=api.HeyboxRateLimited(retry_after=60))
        async def run():
            return [text async for text in view._query_records(None, "示例", "", force_bind=True)]
        result = asyncio.run(run())
        self.assertIn("小黑盒已限流", result[0])
        view._binding.mark_negative.assert_not_called()
        self.assertEqual(cleaned, [True])


if __name__ == "__main__":
    unittest.main()
