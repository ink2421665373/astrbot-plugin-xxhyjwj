import ast
import asyncio
import base64
from io import BytesIO
import json
import logging
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image, ImageDraw, ImageFont
from report_image import FONT_PATH, WIDTH, _blocks, _wrap, render_report_images
from delivery import (
    WECHAT_IMAGE_BUDGET_BYTES,
    encode_jpeg,
    encode_jpeg_within_budget,
    resize_image,
    save_originals,
    slice_poster,
    split_message,
)


class ReportImageTests(unittest.TestCase):
    def test_details_keep_identity_and_all_matches_in_one_long_image(self):
        original = ImageDraw.ImageDraw.text
        captured = []
        def capture(draw, xy, text, *args, **kwargs):
            captured.append(str(text))
            return original(draw, xy, text, *args, **kwargs)
        report = ("昵称：示例玩家\nID：123\n模式：天选双排\n"
                  "第1局｜英雄：宁红夜｜时间：10-22 20:30\n"
                  "排名：2｜参赛人数：60\n击杀：6｜伤害：12345｜恢复：1200\n"
                  "振刀：0｜积分变化：+25\n武器（伤害占比）：长剑60%\n魂玉：测试魂玉\n"
                  "第2局｜英雄：迦南｜时间：10-22 20:00\n排名：12｜击杀：2｜伤害：4560\n" +
                  "".join(f"第{i}局｜英雄：迦南｜时间：10-22 19:00\n排名：{i}｜击杀：2｜伤害：{5000+i}\n武器（伤害占比）：长剑100%\n魂玉：末局魂玉{i}\n" for i in range(3, 11)))
        with patch.object(ImageDraw.ImageDraw, "text", capture):
            pages = render_report_images(report)
        self.assertEqual(len(pages), 1)
        with Image.open(BytesIO(pages[0])) as image:
            self.assertGreater(image.height, 1600)
            self.assertEqual(image.width, WIDTH)
            image.load()
        for expected in ("示例玩家", "ID：123", "宁红夜", "迦南", "12345", "振刀", "0", "+25", "魂玉：测试魂玉"):
            self.assertTrue(any(expected in text for text in captured), expected)
        self.assertNotIn("玩家信息见附页", captured)
        self.assertTrue(any("末局魂玉10" in text for text in captured))
        self.assertTrue(any("5010" in text for text in captured))

    def test_chinese_png_is_one_complete_long_image(self):
        pages = render_report_images("昵称：测试玩家\n" * 100)
        self.assertEqual(len(pages), 1)
        for page in pages:
            with Image.open(BytesIO(page)) as image:
                self.assertEqual(image.format, "PNG")
                self.assertEqual(image.width, WIDTH)
                self.assertGreater(image.height, 1600)
                image.load()

    def test_semantic_cards_preserve_dynamic_and_zero_metrics(self):
        blocks = _blocks("昵称：测试\nID：123\n排位：100 — 300\n当前赛季概览｜测试赛季\n自定义指标：0｜积分变化：-12\n接口提示文本\n武器（伤害占比）：长剑60%\n")
        metrics = [item for kind, data in blocks if kind == "metrics" for item in data]
        self.assertIn(("排位 · 单排", "100"), metrics)
        self.assertIn(("排位 · 双排", "—"), metrics)
        self.assertIn(("自定义指标", "0"), metrics)
        self.assertIn(("积分变化", "-12"), metrics)
        self.assertIn(("text", "接口提示文本"), blocks)
        self.assertIn(("text", "武器（伤害占比）：长剑60%"), blocks)

    def test_extreme_names_ids_and_metrics_remain_in_one_image(self):
        report = "昵称：" + "长名字" * 200 + "\nID：" + "1234567890" * 200 + "\n动态指标：" + "详细内容" * 300
        pages = render_report_images(report)
        self.assertEqual(len(pages), 1)
        for page in pages:
            with Image.open(BytesIO(page)) as image:
                self.assertGreater(image.height, 1600)

    def test_wrapping_keeps_long_names_and_ids(self):
        font = ImageFont.truetype(str(FONT_PATH), 28)
        text = "很长的中文玩家名字" * 20 + "0123456789" * 20
        lines = _wrap(text, font, 500)
        self.assertEqual("".join(lines), text)
        self.assertTrue(all(font.getlength(line) <= 500 for line in lines))

    def plugin(self, config):
        tree = ast.parse(Path(__file__).resolve().parents[1].joinpath("main.py").read_text(encoding="utf-8-sig"))
        source = next(n for n in tree.body if isinstance(n, ast.ClassDef) and any(getattr(f, "name", "") == "_body" for f in n.body))
        methods = [f for f in source.body if getattr(f, "name", "") in ("_body", "_reply", "_message_id", "_send_at", "result_method", "_send_text_fallback", "_send_original_images", "_image_send_mode", "_image_original_mode", "_image_jpeg_quality", "_image_split_height", "_image_pages", "_image_budget", "_image_render_width")]
        cls = ast.ClassDef(name="Plugin", bases=[], keywords=[], body=methods, decorator_list=[])
        components = SimpleNamespace(Reply=lambda **kw: ("reply", kw), Plain=lambda text: ("plain", text),
                                     Image=SimpleNamespace(fromBytes=lambda image: ("image", image)),
                                     File=lambda **kwargs: ("file", kwargs))
        env = dict(asyncio=asyncio, base64=base64, AstrMessageEvent=object, Comp=components,
                   render_report_images=lambda text: [b"png"], logger=logging.getLogger(__name__),
                   split_message=split_message, save_originals=save_originals, slice_poster=slice_poster,
                   encode_jpeg=encode_jpeg, encode_jpeg_within_budget=encode_jpeg_within_budget,
                   resize_image=resize_image,
                   WECHAT_IMAGE_BUDGET_BYTES=WECHAT_IMAGE_BUDGET_BYTES)
        exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])), "main.py", "exec"), env)
        plugin = env["Plugin"]()
        plugin.config = config
        return plugin, env

    def event(self):
        return SimpleNamespace(message_obj=SimpleNamespace(message_id="99", raw_message={"group_id": 123}),
                               chain_result=lambda chain: chain, plain_result=lambda text: ("text", text),
                               get_platform_name=lambda: "aiocqhttp", get_sender_id=lambda: "456",
                               bot=SimpleNamespace(send=AsyncMock()), send=AsyncMock())

    def test_dynamic_toggle_default_and_quote_image(self):
        plugin, env = self.plugin({})
        event = self.event()
        text_result = asyncio.run(plugin._body(event, "测试", first=True))
        self.assertEqual(text_result[1], ("plain", "测试"))
        plugin.config["result_display_format"] = "图片"
        image_result = asyncio.run(plugin._body(event, "测试", first=True))
        self.assertEqual(image_result, [("reply", {"id": "99"}), ("image", b"png")])
        plugin.config["result_display_format"] = "文字"
        self.assertEqual(asyncio.run(plugin._body(event, "测试", first=True)), text_result)

    def test_at_only_first_image_uses_onebot_base64(self):
        plugin, _ = self.plugin(dict(result_display_format="图片", result_reply_method="at"))
        event = self.event()
        self.assertIsNone(asyncio.run(plugin._body(event, "测试", first=True)))
        segments = event.bot.send.call_args.args[1]
        self.assertEqual(segments[0], dict(type="at", data=dict(qq="456")))
        self.assertEqual(segments[-1]["data"]["file"], "base64://cG5n")
        self.assertEqual(asyncio.run(plugin._body(event, "第二页")), [("image", b"png")])
        self.assertEqual(event.bot.send.call_count, 1)

    def test_generation_failure_falls_back_to_text(self):
        plugin, env = self.plugin(dict(result_display_format="图片", result_reply_method="plain"))
        def fail(text):
            raise RuntimeError("font missing")
        env["render_report_images"] = fail
        result = asyncio.run(plugin._body(self.event(), "原始完整结果"))
        self.assertIn("图片生成失败", result[1])
        self.assertIn("原始完整结果", result[1])

    def test_at_send_failure_preserves_image(self):
        plugin, _ = self.plugin(dict(result_display_format="图片", result_reply_method="at"))
        event = self.event()
        event.bot.send.side_effect = RuntimeError("offline")
        self.assertEqual(asyncio.run(plugin._body(event, "测试", first=True)), [("image", b"png")])

    def test_oversized_fallback_is_split_without_losing_text(self):
        plugin, env = self.plugin(dict(result_display_format="图片", result_reply_method="plain"))
        def fail(text):
            raise RuntimeError("failed")
        env["render_report_images"] = fail
        message = "单行超长内容" * 700 + "\n末尾完整保留\n"
        event = self.event()
        last = asyncio.run(plugin._body(event, message, first=True))
        chunks = [call.args[0][1] for call in event.send.call_args_list] + [last[1]]
        self.assertTrue(all(len(chunk) <= 1200 for chunk in chunks))
        self.assertEqual("".join(chunks), "图片生成失败，已改用文字显示。\n" + message)

    def test_original_attachment_uses_identical_png_after_image(self):
        plugin, _ = self.plugin(dict(result_display_format="图片", result_reply_method="plain", send_original_image_file=True))
        with tempfile.TemporaryDirectory() as directory:
            plugin._original_image_dir = Path(directory)
            event = self.event()
            captured = []
            async def send(chain):
                captured.append(chain)
                if chain[0][0] == "file":
                    self.assertEqual(Path(chain[0][1]["file"]).read_bytes(), b"png")
            event.send.side_effect = send
            self.assertIsNone(asyncio.run(plugin._body(event, "完整报告")))
            self.assertEqual(captured[0], [("image", b"png")])
            self.assertEqual(captured[1][0][0], "file")

    def test_at_fallback_mentions_only_once_and_keeps_all_chunks(self):
        plugin, env = self.plugin(dict(result_display_format="图片", result_reply_method="at"))
        def fail(text):
            raise RuntimeError("failed")
        env["render_report_images"] = fail
        event = self.event()
        report = "完整对局数据" * 800
        result = asyncio.run(plugin._body(event, report, first=True))
        self.assertEqual(event.bot.send.await_count, 1)
        first = event.bot.send.call_args.args[1][1]["data"]["text"][1:]
        remainder = [call.args[0][1] for call in event.send.call_args_list] + [result[1]]
        self.assertEqual(first + "".join(remainder), "图片生成失败，已改用文字显示。\n" + report)

    def test_attachment_failure_preserves_already_sent_image(self):
        plugin, _ = self.plugin(dict(result_display_format="图片", result_reply_method="plain", send_original_image_file=True))
        with tempfile.TemporaryDirectory() as directory:
            plugin._original_image_dir = Path(directory)
            event = self.event()
            async def send(chain):
                if chain[0][0] == "file":
                    raise RuntimeError("adapter rejects file")
            event.send.side_effect = send
            result = asyncio.run(plugin._body(event, "完整报告"))
            self.assertIn("图片结果已保留", result[1])
            self.assertEqual(event.send.call_args_list[0].args[0], [("image", b"png")])

    def test_webui_schema_matches_runtime_values(self):
        schema = json.loads(Path(__file__).resolve().parents[1].joinpath("_conf_schema.json").read_text(encoding="utf-8-sig"))
        setting = schema["result_display_format"]
        self.assertEqual(setting["options"], ["文字", "图片"])
        self.assertEqual(setting["default"], "文字")

    def wechat_event(self):
        event = self.event()
        event.get_platform_name = lambda: "wechat_agent"
        return event

    def test_auto_mode_sends_inline_image_on_qq(self):
        plugin, _ = self.plugin(dict(result_display_format="图片", result_reply_method="plain"))
        self.assertEqual(plugin._image_send_mode(self.event()), "内嵌图片")
        event = self.event()
        result = asyncio.run(plugin._body(event, "完整报告"))
        self.assertEqual(result, [("image", b"png")])
        event.send.assert_not_awaited()

    def test_auto_mode_sends_inline_image_on_wechat(self):
        plugin, _ = self.plugin(dict(result_display_format="图片", result_reply_method="plain"))
        self.assertEqual(plugin._image_send_mode(self.wechat_event()), "内嵌图片")
        # 默认走「原图直发」：微信不切页、不重编码
        self.assertTrue(plugin._image_original_mode(self.wechat_event()))
        self.assertEqual(plugin._image_split_height(self.wechat_event()), 0)
        self.assertEqual(plugin._image_split_height(self.event()), 0)
        self.assertFalse(plugin._image_original_mode(self.event()))
        event = self.wechat_event()
        result = asyncio.run(plugin._body(event, "完整报告"))
        self.assertEqual(result, [("image", b"png")])
        event.send.assert_not_awaited()

    def test_wechat_original_mode_keeps_single_full_height_jpeg(self):
        plugin, env = self.plugin(dict(result_display_format="图片", result_reply_method="plain",
                                       image_render_width=0))
        tall = Image.new("RGB", (1080, 2400), "#ffffff")
        ImageDraw.Draw(tall).text((80, 80), "长图", fill="#252722")
        buffer = BytesIO()
        tall.save(buffer, format="PNG")
        env["render_report_images"] = lambda text: [buffer.getvalue()]
        pages = plugin._image_pages(self.wechat_event(), [buffer.getvalue()])
        self.assertEqual(len(pages), 1)
        # 守护进程只对 JPEG 源逐字节原样转发，所以原图模式要先编码成 JPEG
        self.assertTrue(pages[0].startswith(b"\xff\xd8"))
        with Image.open(BytesIO(pages[0])) as image:
            self.assertEqual(image.size, (1080, 2400))

    def test_wechat_render_width_defaults_to_900(self):
        plugin, env = self.plugin(dict(result_display_format="图片", result_reply_method="plain"))
        self.assertEqual(plugin._image_render_width(self.wechat_event()), 900)
        tall = Image.new("RGB", (1080, 2400), "#ffffff")
        ImageDraw.Draw(tall).text((80, 80), "长图", fill="#252722")
        buffer = BytesIO()
        tall.save(buffer, format="PNG")
        pages = plugin._image_pages(self.wechat_event(), [buffer.getvalue()])
        self.assertEqual(len(pages), 1)
        self.assertTrue(pages[0].startswith(b"\xff\xd8"))
        with Image.open(BytesIO(pages[0])) as image:
            self.assertEqual(image.size, (900, 2000))

    def test_render_width_tolerates_bad_values(self):
        plugin, _ = self.plugin(dict(image_render_width="bad"))
        self.assertEqual(plugin._image_render_width(self.event()), 900)
        plugin, _ = self.plugin(dict(image_render_width=10))
        self.assertEqual(plugin._image_render_width(self.event()), 480)
        plugin, _ = self.plugin(dict(image_render_width=99999))
        self.assertEqual(plugin._image_render_width(self.event()), 2160)
        plugin, _ = self.plugin(dict(image_render_width=0))
        self.assertEqual(plugin._image_render_width(self.event()), 0)

    def test_resize_image_keeps_aspect_and_skips_bad_input(self):
        tall = Image.new("RGB", (1080, 2400), "#ffffff")
        buffer = BytesIO()
        tall.save(buffer, format="PNG")
        shrunk = resize_image(buffer.getvalue(), 900)
        with Image.open(BytesIO(shrunk)) as image:
            self.assertEqual(image.size, (900, 2000))
        self.assertIsNone(resize_image(buffer.getvalue(), 0))
        self.assertIsNone(resize_image(b"not an image", 900))
        wide = resize_image(buffer.getvalue(), 1440)
        with Image.open(BytesIO(wide)) as image:
            self.assertEqual(image.size, (1080, 2400))

    def test_wechat_jpeg_quality_is_configurable(self):
        plugin, _ = self.plugin(dict(image_wechat_jpeg_quality=70))
        self.assertEqual(plugin._image_jpeg_quality(), 70)
        plugin, _ = self.plugin(dict(image_wechat_jpeg_quality="bad"))
        self.assertEqual(plugin._image_jpeg_quality(), 92)
        plugin, _ = self.plugin(dict(image_wechat_jpeg_quality=200))
        self.assertEqual(plugin._image_jpeg_quality(), 100)

    def test_original_mode_off_restores_splitting(self):
        plugin, _ = self.plugin(dict(result_display_format="图片", result_reply_method="plain",
                                     image_wechat_original=False, image_split_height=900))
        self.assertFalse(plugin._image_original_mode(self.wechat_event()))
        self.assertEqual(plugin._image_split_height(self.wechat_event()), 900)

    def test_original_mode_string_false_disables(self):
        plugin, _ = self.plugin(dict(image_wechat_original="off"))
        self.assertFalse(plugin._image_original_mode(self.wechat_event()))

    def test_explicit_file_mode_still_sends_file_on_wechat(self):
        plugin, _ = self.plugin(dict(result_display_format="图片", result_reply_method="plain",
                                     image_send_mode="原图文件"))
        self.assertEqual(plugin._image_send_mode(self.wechat_event()), "原图文件")
        with tempfile.TemporaryDirectory() as directory:
            plugin._original_image_dir = Path(directory)
            event = self.wechat_event()
            result = asyncio.run(plugin._body(event, "完整报告"))
            self.assertIsNone(result)
            sent = [call.args[0] for call in event.send.await_args_list]
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0][0][0], "file")

    def test_wechat_inline_mode_still_splits(self):
        plugin, env = self.plugin(dict(result_display_format="图片", result_reply_method="plain",
                                       image_wechat_original=False,
                                       image_split_height=900, image_send_mode="内嵌图片"))
        self.assertEqual(plugin._image_split_height(self.wechat_event()), 900)
        tall = Image.new("RGB", (1080, 2400), "#f2efe7")
        ImageDraw.Draw(tall).text((80, 80), "长图", fill="#252722")
        buffer = BytesIO()
        tall.save(buffer, format="PNG")
        env["render_report_images"] = lambda text: [buffer.getvalue()]
        event = self.wechat_event()
        result = asyncio.run(plugin._body(event, "完整报告"))
        self.assertGreaterEqual(len(result), 3)
        for _, page in result:
            with Image.open(BytesIO(page)) as image:
                self.assertLessEqual(image.height, 900)
        qq = asyncio.run(plugin._body(self.event(), "完整报告"))
        self.assertEqual(len(qq), 1)

    def test_explicit_inline_mode_overrides_wechat_auto(self):
        plugin, _ = self.plugin(dict(result_display_format="图片", result_reply_method="plain", image_send_mode="内嵌图片"))
        event = self.wechat_event()
        self.assertEqual(asyncio.run(plugin._body(event, "完整报告")), [("image", b"png")])
        event.send.assert_not_awaited()

    def test_both_mode_keeps_image_then_file(self):
        plugin, _ = self.plugin(dict(result_display_format="图片", result_reply_method="plain", image_send_mode="两者都发"))
        with tempfile.TemporaryDirectory() as directory:
            plugin._original_image_dir = Path(directory)
            event = self.wechat_event()
            captured = []
            async def send(chain):
                captured.append(chain)
            event.send.side_effect = send
            self.assertIsNone(asyncio.run(plugin._body(event, "完整报告")))
            self.assertEqual(captured[0], [("image", b"png")])
            self.assertEqual(captured[1][0][0], "file")

    def test_legacy_boolean_still_means_both(self):
        plugin, _ = self.plugin(dict(send_original_image_file=True))
        self.assertEqual(plugin._image_send_mode(self.event()), "两者都发")
        self.assertEqual(plugin._image_send_mode(self.wechat_event()), "两者都发")
        plugin, _ = self.plugin(dict())
        self.assertEqual(plugin._image_send_mode(self.wechat_event()), "内嵌图片")
        self.assertEqual(plugin._image_send_mode(self.event()), "内嵌图片")

    def test_wechat_pages_are_jpeg_within_daemon_budget(self):
        """旧守护进程只原样转发 ≤65,520 字节的 JPEG，超了就缩图降质。"""
        plugin, env = self.plugin(dict(result_display_format="图片", result_reply_method="plain",
                                       image_wechat_original=False,
                                       image_render_width=0,
                                       image_send_mode="内嵌图片"))
        tall = Image.new("RGB", (1080, 2400), "#ffffff")
        draw = ImageDraw.Draw(tall)
        for y in range(40, 2400, 60):
            draw.rectangle((60, y, 1020, y + 28), fill="#1b2a4a")
        buffer = BytesIO()
        tall.save(buffer, format="PNG")
        env["render_report_images"] = lambda text: [buffer.getvalue()]
        result = asyncio.run(plugin._body(self.wechat_event(), "完整报告"))
        self.assertGreaterEqual(len(result), 3)
        budget = env["WECHAT_IMAGE_BUDGET_BYTES"]
        for _, page in result:
            self.assertLessEqual(len(page), budget)
            self.assertTrue(page.startswith(b"\xff\xd8"), "微信页必须是 JPEG")
            with Image.open(BytesIO(page)) as image:
                self.assertEqual(image.format, "JPEG")
                self.assertEqual(image.width, 1080)
        qq = asyncio.run(plugin._body(self.event(), "完整报告"))
        self.assertEqual(len(qq), 1)
        self.assertTrue(qq[0][1].startswith(b"\x89PNG"), "QQ 仍发原始 PNG")

    def test_jpeg_budget_encoder_shrinks_but_keeps_full_width(self):
        canvas = Image.new("RGB", (1080, 700), "#ffffff")
        ImageDraw.Draw(canvas).rectangle((40, 40, 1040, 120), fill="#123456")
        buffer = BytesIO()
        canvas.save(buffer, format="PNG")
        data = encode_jpeg_within_budget(buffer.getvalue(), 30000)
        self.assertIsNotNone(data)
        self.assertLessEqual(len(data), 30000)
        with Image.open(BytesIO(data)) as image:
            self.assertEqual(image.format, "JPEG")
            self.assertLessEqual(image.width, 1080)
        self.assertIsNone(encode_jpeg_within_budget(b"not an image", 30000))
        self.assertIsNone(encode_jpeg_within_budget(buffer.getvalue(), 0))

    def test_slice_poster_keeps_every_pixel_and_cuts_on_blank_rows(self):
        canvas = Image.new("RGB", (200, 1000), "#f2efe7")
        draw = ImageDraw.Draw(canvas)
        for y in range(0, 1000, 100):
            draw.rectangle((0, y, 200, y + 40), fill="#252722")
        buffer = BytesIO()
        canvas.save(buffer, format="PNG")
        pages = slice_poster(buffer.getvalue(), 300)
        self.assertGreaterEqual(len(pages), 4)
        total = 0
        for page in pages:
            with Image.open(BytesIO(page)) as image:
                self.assertLessEqual(image.height, 300)
                total += image.height
        self.assertEqual(total, 1000)
        self.assertEqual(slice_poster(buffer.getvalue(), 0), [buffer.getvalue()])


class CrispRenderingTests(unittest.TestCase):
    """微信把内嵌图压到 64KB：默认给字描边 + 纯白底，压完还认得出。"""

    def setUp(self):
        import poster_image
        self.poster = poster_image
        self.addCleanup(poster_image.configure, stroke=1, plain_paper=True)

    def test_configure_toggles_stroke_and_paper(self):
        self.poster.configure(stroke=0, plain_paper=False)
        self.assertEqual(self.poster._STROKE, 0)
        self.assertFalse(self.poster._PLAIN_PAPER)
        self.poster.configure(stroke=1, plain_paper=True)
        self.assertEqual(self.poster._STROKE, 1)
        self.assertTrue(self.poster._PLAIN_PAPER)

    def test_stroke_is_clamped(self):
        self.poster.configure(stroke=9, plain_paper=True)
        self.assertEqual(self.poster._STROKE, 3)

    def test_crisp_mode_draws_on_white_paper(self):
        self.poster.configure(stroke=1, plain_paper=True)
        page = render_report_images("昵称：示例玩家\nID：123\n当前赛季概览\n排位 · 单排：3000 排位 · 双排：3200 排位 · 三排：5326")[0]
        with Image.open(BytesIO(page)) as image:
            # 左上角页面背景应为纯白（水墨纸纹被关掉）
            self.assertEqual(image.convert("RGB").getpixel((2, 2)), (255, 255, 255))

    def test_plain_paper_switch_keeps_texture(self):
        self.poster.configure(stroke=0, plain_paper=False)
        page = render_report_images("昵称：示例玩家\nID：123\n当前赛季概览\n排位 · 单排：3000")[0]
        with Image.open(BytesIO(page)) as image:
            self.assertNotEqual(image.convert("RGB").getpixel((2, 2)), (255, 255, 255))


if __name__ == "__main__":
    unittest.main()
