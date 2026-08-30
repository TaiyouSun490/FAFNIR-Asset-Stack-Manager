from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from game_stack_planner.models import Candidate
from game_stack_planner.repository import StackRepository
from game_stack_planner.service import GameStackPlanner


class RelevanceGateTests(unittest.TestCase):
    def test_installed_bonus_never_fills_an_unrelated_requirement(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = StackRepository(Path(directory) / "catalog.db")
            repository.upsert_candidates((Candidate(
                id="local:unrelated",
                source="local",
                external_id="unrelated",
                title="Unrelated Installed Utility",
                url="",
                ownership="installed",
                installed=True,
                categories=("enemy_ai",),
            ),))
            planner = GameStackPlanner(repository)
            result = planner.recommend(
                prompt="アイテム収集",
                remote=False,
                budget="owned_first",
            )
            self.assertEqual(result["recommendations"]["inventory"], [])
            self.assertIn("inventory", result["plans"][0]["missing"])
            repository.close()

    def test_generic_controller_word_does_not_recommend_vr_hands_for_input(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = StackRepository(Path(directory) / "catalog.db")
            repository.upsert_candidates((Candidate(
                id="asset_store:123",
                source="asset_store",
                external_id="123",
                title="Avatar Hand Controller for Leap Motion",
                url="https://assetstore.unity.com/packages/package/123",
                ownership="owned",
            ),))
            result = GameStackPlanner(repository).recommend(
                prompt="ただのホラーだしゅつゲーム",
                remote=False,
                budget="owned_first",
            )
            selected_ids = {
                item["candidate"]["id"]
                for plan in result["plans"]
                for item in plan["selected"]
            }
            self.assertNotIn("asset_store:123", selected_ids)
            self.assertFalse(any(
                item["candidate"]["id"] == "asset_store:123"
                for items in result["recommendations"].values()
                for item in items
            ))
            repository.close()

    def test_planner_searches_beyond_the_first_500_owned_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = StackRepository(Path(directory) / "catalog.db")
            unrelated = tuple(
                Candidate(
                    id=f"asset_store:{index}",
                    source="asset_store",
                    external_id=str(index),
                    title=f"A Decorative Prop {index:04d}",
                    url=f"https://assetstore.unity.com/packages/package/{index}",
                    ownership="owned",
                )
                for index in range(1, 521)
            )
            relevant = Candidate(
                id="asset_store:9999",
                source="asset_store",
                external_id="9999",
                title="ZZZ Easy Save Serializer",
                url="https://assetstore.unity.com/packages/package/9999",
                ownership="owned",
            )
            repository.upsert_candidates((*unrelated, relevant))
            result = GameStackPlanner(repository).recommend(
                prompt="セーブ対応ゲーム",
                remote=False,
                budget="owned_first",
            )
            self.assertIn(
                "asset_store:9999",
                {
                    item["candidate"]["id"]
                    for item in result["recommendations"]["save_system"]
                },
            )
            repository.close()


if __name__ == "__main__":
    unittest.main()
