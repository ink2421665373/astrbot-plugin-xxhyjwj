"""Recent averages disclose each metric's actual data coverage."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from record_query import format_summary
from report_image import render_report_images
from PIL import ImageDraw


class SummarySampleTests(unittest.TestCase):
    def report(self, records, selected=10):
        return format_summary("样本测试", 30, "12", selected, records, {}, {},
                              season_error="赛季数据未获取")

    def test_each_metric_uses_its_own_sample_count_and_keeps_zero(self):
        records = [dict(damage=100, parry=0, kills=0),
                   dict(damage=300, parry=4, kills=None),
                   dict(damage=None, parry=None, kills=6)]
        text = self.report(records)
        self.assertIn("最近10场场均伤害：200.0（2/10场有数据）", text)
        self.assertIn("最近10场场均振刀：2.0（2/10场有数据）", text)
        self.assertIn("最近10场场均击败：3.0（2/10场有数据）", text)

    def test_missing_values_do_not_become_zero(self):
        text = self.report([dict(parry=0), {}, {}], selected=3)
        self.assertIn("场均振刀：0.0（1/3场有数据）", text)
        self.assertIn("场均伤害：—（0/3场有数据）", text)
        self.assertIn("场均击败：—（0/3场有数据）", text)
        self.assertIn("场均伤害：—（0/0场有数据）", self.report([], selected=0))

    def test_image_preserves_numbers_and_sample_coverage(self):
        records = [dict(damage=100, parry=0, kills=1)] * 8 + [{}, {}]
        text = self.report(records)
        captured = []
        original = ImageDraw.ImageDraw.text
        def capture(draw, xy, value, *args, **kwargs):
            captured.append(str(value))
            return original(draw, xy, value, *args, **kwargs)
        with patch.object(ImageDraw.ImageDraw, "text", capture):
            images = render_report_images(text)
        self.assertEqual(len(images), 1)
        combined = "".join(captured)
        for value in ("100.0", "0.0", "1.0", "8/10场有数据"):
            self.assertIn(value, combined)
        self.assertEqual(captured.count("8/10场有数据"), 3)


if __name__ == "__main__":
    unittest.main()
