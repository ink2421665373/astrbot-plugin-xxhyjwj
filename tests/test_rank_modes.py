"""Mode-specific rank validation, without inventing a score conversion table."""
import copy
import importlib
import json
from pathlib import Path
import sys
from types import ModuleType
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
package = ModuleType("naraka_test_package")
package.__path__ = [str(ROOT)]
sys.modules.setdefault("naraka_test_package", package)
ai = importlib.import_module("naraka_test_package.ai_analysis")
from record_query import TIANXUAN_RANK_NAMES, normalize_rank_data, format_summary
from report_image import render_report_images


class RankModeTests(unittest.TestCase):
    def test_tianren_never_reuses_tianxuan_rank_names(self):
        for mode in ("4", "13", "5"):
            for name in TIANXUAN_RANK_NAMES:
                body = dict(player_info=dict(rating=3700, level=name + "III", lv=616))
                clean, note = normalize_rank_data(mode, body)
                self.assertIsNone(clean["player_info"]["level"])
                self.assertEqual(clean["player_info"]["rating"], 3700)
                self.assertEqual(clean["player_info"]["lv"], 616)
                self.assertIn("无法据此确认天人之战段位", note)
                self.assertEqual(body["player_info"]["level"], name + "III")

    def test_correct_api_names_and_historical_names_are_not_overwritten(self):
        for name in ("星月宗师", "无间泰斗", "大师二段", "宗师一段", "未定级"):
            body = dict(player_info=dict(rating=3796, level=name))
            clean, note = normalize_rank_data("5", body)
            self.assertEqual(clean["player_info"]["level"], name)
            self.assertEqual(note, "")

    def test_tianxuan_and_other_modes_keep_their_original_rank(self):
        for mode in ("5000000", "12", "5000001", "6"):
            body = dict(player_info=dict(rating=3796, level="蚀月III"))
            clean, note = normalize_rank_data(mode, body)
            self.assertEqual(clean, body)
            self.assertEqual(note, "")

    def test_numeric_code_or_rating_does_not_become_an_invented_rank(self):
        for level in (9, 0, "9", "3796", "3796.0"):
            clean, note = normalize_rank_data("5", dict(player_info=dict(rating=3796, level=level)))
            self.assertIsNone(clean["player_info"]["level"])
            self.assertIn("不按积分猜测段位", note)
        clean, note = normalize_rank_data("5", dict(player_info=dict(rating=3796)))
        self.assertNotIn("level", clean["player_info"])
        self.assertEqual(note, "")

    def test_overview_ranks_are_validated_without_removing_scores_or_mutating_source(self):
        body = dict(player_info=dict(rating=3796, level="蚀月III"), overview=[
            dict(desc="历史最高段位", value="无间修罗"), dict(desc="段位积分", value=3796),
            dict(desc="场均伤害", value=11357), dict(desc="天人段位", value="星月宗师")])
        original = copy.deepcopy(body)
        clean, note = normalize_rank_data("5", body)
        self.assertIsNone(clean["overview"][0]["value"])
        self.assertEqual(clean["overview"][1]["value"], 3796)
        self.assertEqual(clean["overview"][2]["value"], 11357)
        self.assertEqual(clean["overview"][3]["value"], "星月宗师")
        self.assertTrue(note)
        self.assertEqual(body, original)

    def test_summary_text_and_image_do_not_show_rejected_rank(self):
        from PIL import ImageDraw
        text = format_summary("示例", 30, "5", 0, [], {"5": "3796"}, {}, season_name="全部赛季",
            season_body=dict(player_info=dict(rating=3796, level="蚀月III", lv=616),
                             overview=[dict(desc="总场次", value=103)]))
        self.assertIn("段位：待确认", text)
        self.assertIn("分数：3796", text)
        self.assertIn("等级：616", text)
        self.assertIn("总场次：103", text)
        self.assertNotIn("蚀月", text)
        captured = []
        original = ImageDraw.ImageDraw.text
        def capture(draw, xy, value, *args, **kwargs):
            captured.append(str(value))
            return original(draw, xy, value, *args, **kwargs)
        with patch.object(ImageDraw.ImageDraw, "text", capture):
            images = render_report_images(text)
        self.assertEqual(len(images), 1)
        self.assertIn("待确认", "".join(captured))
        self.assertNotIn("蚀月", "".join(captured))

    def test_ai_receives_validated_rank_and_cannot_reinfer_the_wrong_mode(self):
        data = ai.build_analysis_data("5", 0, [], {}, season_name="全部赛季", season_body=dict(
            player_info=dict(rating=3796, level="蚀月III"),
            overview=[dict(desc="最高段位", value="坠日I"), dict(desc="场均伤害", value=11357)]))
        prompt = ai.analysis_prompt(data)
        payload = json.loads(prompt.split("\n")[-1])
        self.assertIsNone(payload["season"]["player_stats"]["level"])
        self.assertEqual(payload["season"]["player_stats"]["rating"], 3796)
        self.assertIsNone(payload["season"]["overview"][0]["value"])
        self.assertEqual(payload["season"]["overview"][1]["value"], 11357)
        self.assertIn("无法据此确认天人之战段位", payload["season"]["rank_note"])
        self.assertNotIn("蚀月", prompt)
        self.assertNotIn("坠日", prompt)
        self.assertIn("不得按天选分数", ai.RULES)

    def test_actual_record_and_season_commands_use_the_same_rank_validation(self):
        fixture = importlib.import_module("test_current_season").CurrentSeasonTests()
        config = dict(season_mappings=[dict(season_id="pre-01", season_name="全部", is_current=True)])
        body = dict(player_info=dict(rating=3796, level="蚀月III", lv=616),
                    overview=[dict(desc="总场次", value=103)])
        for mode in ("4", "13", "5"):
            result, _ = fixture.run_query(mode=mode, rows=False, config=config, pre_body=body, for_ai=True)
            self.assertIn("段位：待确认", result.report_text)
            self.assertNotIn("蚀月", result.report_text)
            self.assertIsNone(result.analysis_data["season"]["player_stats"]["level"])
            result, _ = fixture.run_query(mode=mode, config=config, pre_body=body, season_request="全部")
            text = "".join(result.texts)
            self.assertIn("段位：待确认", text)
            self.assertIn("分数：3796", text)
            self.assertNotIn("蚀月", text)

    def test_season_command_rejects_response_for_another_mode(self):
        fixture = importlib.import_module("test_current_season").CurrentSeasonTests()
        result, _ = fixture.run_query(mode="5", season_request="全部", pre_body=dict(
            battle_tid="12", player_info=dict(rating=3796, level="蚀月III")))
        text = "".join(result.texts)
        self.assertIn("接口返回模式与所选模式不一致", text)
        self.assertNotIn("蚀月", text)


if __name__ == "__main__":
    unittest.main()
