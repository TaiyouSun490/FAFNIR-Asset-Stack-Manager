from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from game_stack_planner.llm_recommender import LlmStackRecommender
from game_stack_planner.models import Candidate
from game_stack_planner.repository import StackRepository
from game_stack_planner.service import GameStackPlanner


class LlmStackRecommendationTests(unittest.TestCase):
    def test_llm_can_only_select_retrieved_ids_and_adds_concrete_usage(self):
        calls: list[dict[str, object]] = []

        def fetcher(url, payload, headers, timeout):
            calls.append(payload)
            value = {
                "summary": "病院を主舞台にした最小構成です。",
                "selections": [
                    {
                        "candidate_id": "asset_store:10",
                        "requirement_key": "horror_atmosphere",
                        "role": "主舞台",
                        "use_case": "探索する廃病院の背景と小物に使う。",
                        "integration": "照明アセットと同じURP設定に揃える。",
                        "confidence": "high",
                    },
                    {
                        "candidate_id": "invented:not-real",
                        "requirement_key": "horror_atmosphere",
                        "role": "捏造候補",
                        "use_case": "使用しない。",
                        "integration": "なし",
                        "confidence": "low",
                    },
                ],
                "gaps": [],
                "warnings": [],
            }
            return {"output": [{"content": [{
                "type": "output_text",
                "text": json.dumps(value, ensure_ascii=False),
            }]}]}

        with tempfile.TemporaryDirectory() as directory:
            repository = StackRepository(Path(directory) / "catalog.db")
            repository.upsert_candidates((Candidate(
                id="asset_store:10",
                source="asset_store",
                external_id="10",
                title="Abandoned Hospital Horror Environment",
                url="https://assetstore.unity.com/packages/package/10",
                ownership="owned",
            ),))
            llm = LlmStackRecommender(api_key="test", fetcher=fetcher)
            result = GameStackPlanner(repository, llm=llm).recommend(
                prompt="ホラー脱出ゲーム",
                remote=False,
                budget="owned_first",
                use_llm=True,
            )
            self.assertEqual(result["recommended_plan_id"], "ai_recommended")
            self.assertEqual(result["llm"]["status"], "used")
            ai_plan = result["plans"][0]
            self.assertEqual(
                [item["candidate"]["id"] for item in ai_plan["selected"]],
                ["asset_store:10"],
            )
            self.assertEqual(
                ai_plan["selected"][0]["usage"][0]["role"],
                "主舞台",
            )
            sent = json.loads(calls[0]["input"])
            self.assertNotIn("url", sent["candidate_groups"][0])
            self.assertNotIn(
                "url",
                next(
                    candidate
                    for group in sent["candidate_groups"]
                    for candidate in group["candidates"]
                ),
            )
            repository.close()

    def test_no_api_key_does_not_send_catalog_data(self):
        llm = LlmStackRecommender(api_key="")
        result = llm.recommend(
            enabled=True,
            prompt="test",
            platform="pc",
            budget="mixed",
            project=None,
            requirements=(),
            recommendations={},
        )
        self.assertEqual(result["status"], "not_configured")
        self.assertFalse(result["data_sent"])


if __name__ == "__main__":
    unittest.main()
