from __future__ import annotations

import unittest

from game_stack_planner.requirements import derive_requirements, requirement_categories


class AssetStoreTaxonomyTests(unittest.TestCase):
    def test_visual_assets_are_a_baseline_for_asset_discovery(self):
        keys = {item.key for item in derive_requirements("小さなパズルゲーム")}
        self.assertIn("visual_assets", keys)

    def test_asset_specific_language_selects_store_friendly_requirements(self):
        keys = {
            item.key
            for item in derive_requirements(
                "東京の街を舞台に、キャラクターモーションと魔法VFXを使う3Dゲーム"
            )
        }
        self.assertTrue({
            "visual_assets",
            "animation",
            "vfx",
        }.issubset(keys))

    def test_manual_capture_taxonomy_exposes_asset_categories(self):
        keys = {item["key"] for item in requirement_categories()}
        self.assertTrue({
            "visual_assets",
            "character_art",
            "animation",
            "vfx",
            "shaders_materials",
            "editor_tools",
        }.issubset(keys))

    def test_horror_escape_brief_expands_into_a_buildable_feature_set(self):
        keys = {item.key for item in derive_requirements(
            "ただのホラーだしゅつゲーム"
        )}
        self.assertTrue({
            "character_controller",
            "camera",
            "interaction",
            "puzzle",
            "horror_atmosphere",
            "lighting",
            "audio",
        }.issubset(keys))


if __name__ == "__main__":
    unittest.main()
