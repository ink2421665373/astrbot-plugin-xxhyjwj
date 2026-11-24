"""Query time is separate from source update time and selected-match dates."""
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from record_query import (format_query_metadata, format_sample_period, format_summary,
                          format_details, make_record, _timestamp_ms)
from report_image import render_report_images
from PIL import Image, ImageDraw


def epoch(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp()


class ReportTimeTests(unittest.TestCase):
    def test_query_time_is_beijing_and_not_a_data_update_claim(self):
        lines = format_query_metadata(epoch(2026, 10, 22, 16, 5))
        self.assertEqual(lines[0], "查询时间（北京时间）：2026-10-23 00:05:00")
        self.assertEqual(lines[1], "数据更新时间：未知（不以查询时间代替）")

    def test_seconds_milliseconds_and_invalid_dates(self):
        stamp = epoch(2026, 10, 22)
        for value in (stamp, str(stamp), stamp * 1000, str(stamp * 1000)):
            self.assertEqual(_timestamp_ms(value), stamp * 1000)
        for value in (None, True, 0, -1, 600, "15min", "1700000000k", "—", float("inf"), float("nan"), 10**20, 10**500):
            with self.subTest(value=value):
                self.assertIsNone(_timestamp_ms(value))
        record = make_record(dict(time=stamp), dict(data=[dict(desc="存活时间", value=600)], time="bad"))
        self.assertEqual(record["time"], stamp * 1000)

    def test_sample_range_uses_selected_rows_including_failed_details(self):
        rows = [dict(time=epoch(2026, 10, 22, 17)), dict(time=epoch(2026, 10, 20, 16) * 1000)]
        records = [dict(time=epoch(2026, 10, 22, 17) * 1000)]
        result = format_sample_period(records, 2, rows)
        self.assertIn("2026-10-21 00:00 至 2026-10-23 01:00", result)
        self.assertIn("2/2场有有效时间", result)
        self.assertNotIn("仅覆盖", result)

    def test_partial_and_missing_dates_are_explicit(self):
        rows = [dict(time=epoch(2026, 10, 22)), dict(time=None), dict(time=600)]
        result = format_sample_period([], 3, rows)
        self.assertIn("1/3场有有效时间，范围仅覆盖有时间的对局", result)
        self.assertIn("未知（0/2场有有效时间）", format_sample_period([], 2, [{}, {}]))
        self.assertIn("无选中对局", format_sample_period([], 0, []))

    def test_year_boundary_and_per_match_times_use_beijing(self):
        rows = [dict(time=epoch(2025, 12, 31, 15)), dict(time=epoch(2026, 1, 1))]
        result = format_sample_period([], 2, rows)
        self.assertIn("2025-12-31 23:00 至 2026-01-01 08:00", result)
        details = format_details("测试", 1, "12", 1, [dict(time=epoch(2026, 10, 22, 16) * 1000)], {})
        self.assertIn("时间：10-23 00:00", details)

    def test_text_and_single_long_image_keep_time_metadata(self):
        queried_at = epoch(2026, 10, 23, 1, 5)
        rows = [dict(time=epoch(2026, 10, 20)), dict(time=epoch(2026, 10, 22))]
        records = [dict(time=row["time"] * 1000, hero_id="1000003", damage=12345) for row in rows]
        reports = [format_summary("时间预览", 30, "12", 2, records, {}, {}, queried_at=queried_at, samples=rows),
                   format_details("时间预览", 30, "12", 2, records, {}, queried_at=queried_at, samples=rows)]
        for report in reports:
            captured = []
            original = ImageDraw.ImageDraw.text
            def capture(draw, xy, text, *args, **kwargs):
                captured.append(str(text))
                return original(draw, xy, text, *args, **kwargs)
            with patch.object(ImageDraw.ImageDraw, "text", capture):
                pages = render_report_images(report)
            self.assertEqual(len(pages), 1)
            with Image.open(BytesIO(pages[0])) as image:
                image.load()
            combined = "".join(captured)
            for expected in ("2026-10-23 09:05:00", "数据更新时间：未知", "2026-10-20 08:00 至 2026-10-22 08:00", "2/2场有有效时间"):
                self.assertIn(expected, report)
                self.assertIn(expected, combined)


if __name__ == "__main__":
    unittest.main()
