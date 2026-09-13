from __future__ import annotations

import json
import tempfile
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp.client import Client
from mcp.client.stdio import StdioServerParameters
from mcp.server.mcpserver.utilities.types import Image

from game_stack_planner.api import GameStackApplication
from game_stack_planner.asset_store_visuals import ReviewImage
from game_stack_planner.mcp_server import StackforgeMcpTools, build_mcp_server
from game_stack_planner.models import Candidate
from game_stack_planner.repository import StackRepository


class StackforgeMcpBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "catalog.db"
        self.repository = StackRepository(self.db)
        self.app = GameStackApplication(repository=self.repository)
        self.tools = StackforgeMcpTools(self.app)

    def tearDown(self) -> None:
        self.app.close()
        self.temp.cleanup()

    def test_retrieval_returns_evidence_for_llm_judgment_without_local_metadata(self):
        self.repository.upsert_candidates((
            Candidate(
                id="asset_store:10",
                source="asset_store",
                external_id="10",
                title="Abandoned Hospital Horror Environment",
                url="https://assetstore.unity.com/packages/package/10",
                ownership="owned",
                metadata={"private_local_path": "C:/secret/cache"},
            ),
            Candidate(
                id="asset_store:11",
                source="asset_store",
                external_id="11",
                title="Avatar Hand Controller for Leap Motion",
                url="https://assetstore.unity.com/packages/package/11",
                ownership="owned",
            ),
        ))
        result = self.tools.retrieve_game_stack_evidence(
            game_brief="ただのホラーだしゅつゲーム",
            budget="owned_first",
            remote=False,
        )
        candidates = [
            item["candidate"]
            for group in result["candidate_groups"]
            for item in group["candidates"]
        ]
        ids = {item["id"] for item in candidates}
        self.assertIn("asset_store:10", ids)
        self.assertNotIn("asset_store:11", ids)
        self.assertTrue(all("metadata" not in item for item in candidates))
        self.assertIn("Never invent candidate IDs", result["llm_guidance"])

    def test_catalog_defaults_to_owned_assets(self):
        self.repository.upsert_candidates((
            Candidate(
                id="asset_store:20",
                source="asset_store",
                external_id="20",
                title="Owned Horror Props",
                url="https://assetstore.unity.com/packages/package/20",
                ownership="owned",
            ),
            Candidate(
                id="github:example/repo",
                source="github",
                external_id="example/repo",
                title="Horror repo",
                url="https://github.com/example/repo",
            ),
        ))
        result = self.tools.search_catalog(query="Horror")
        self.assertEqual(
            [item["id"] for item in result["items"]],
            ["asset_store:20"],
        )

    def test_reindex_tool_queues_work_and_returns_state_immediately(self):
        result = self.tools.reindex_owned_asset_rag()

        self.assertFalse(result["accepted"])
        self.assertEqual("disabled", result["rag_index"]["state"])

    def test_rag_search_returns_labeled_owned_only_fallback(self):
        self.app.save_owned_rag({
            "url": "https://assetstore.unity.com/packages/package/99101",
            "title": "Private store title",
            "user_alias": "Hospital environment",
            "notes": "horror rooms",
            "categories": ["visual_assets"],
            "purchase_confirmation": True,
        })

        result = self.tools.search_owned_asset_rag("怖い医療施設")

        self.assertEqual("lexical_fallback", result["retrieval_mode"])
        self.assertTrue(result["degraded"])
        self.assertEqual(1, result["count"])
        self.assertEqual("asset_store:99101", result["items"][0]["candidate_id"])
        self.assertEqual("disabled", result["index"]["state"])

    def test_visual_review_returns_metadata_then_actual_image_content(self):
        self.repository.upsert_candidates((Candidate(
            id="asset_store:30",
            source="asset_store",
            external_id="30",
            title="Visual Environment",
            url="https://assetstore.unity.com/packages/visual-environment-30",
        ),))
        review = ({
            "candidate_id": "asset_store:30",
            "detail": "quick",
            "image_count": 1,
        }, (ReviewImage(
            url="https://assetstorev1-prd-cdn.unity3d.com/key-image/test.jpg",
            role="main",
            mime_type="image/jpeg",
            content=b"jpeg-bytes",
        ),))

        with patch(
            "game_stack_planner.mcp_server.review_candidate_visuals",
            return_value=review,
        ):
            result = self.tools.review_asset_store_candidate_visuals(
                "asset_store:30", "quick"
            )

        self.assertIn('"image_count": 1', result[0])
        self.assertIsInstance(result[1], Image)
        self.assertEqual(b"jpeg-bytes", result[1].data)


class StackforgeMcpProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_server_lists_read_and_approval_gated_write_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            server, app = build_mcp_server(str(Path(directory) / "catalog.db"))
            try:
                async with Client(server) as client:
                    result = await client.list_tools()
                    tools = {item.name: item for item in result.tools}
                    self.assertIn("retrieve_game_stack_evidence", tools)
                    self.assertIn("search_unity_assets", tools)
                    self.assertIn("reindex_owned_asset_rag", tools)
                    self.assertIn("prepare_candidate_install", tools)
                    self.assertIn("apply_reviewed_install", tools)
                    self.assertIn("fafnir_status", tools)
                    self.assertIn("stackforge_status", tools)
                    self.assertIn("get_fafnir_install_status", tools)
                    self.assertIn("prepare_owned_asset_download", tools)
                    self.assertIn("start_reviewed_asset_download", tools)
                    self.assertIn("get_asset_store_download_status", tools)
                    self.assertIn("review_asset_store_candidate_visuals", tools)
                    self.assertIn("compare_asset_store_candidates", tools)
                    self.assertTrue(tools["compare_asset_store_candidates"].annotations.read_only_hint)
                    self.assertTrue(
                        tools["search_unity_assets"].annotations.read_only_hint
                    )
                    self.assertTrue(
                        tools["apply_reviewed_install"].annotations.destructive_hint
                    )
                    self.assertTrue(
                        tools["reindex_owned_asset_rag"].annotations.open_world_hint
                    )
                    self.assertFalse(
                        tools["prepare_owned_asset_download"].annotations.read_only_hint
                    )
                    self.assertTrue(
                        tools["start_reviewed_asset_download"].annotations.open_world_hint
                    )

                    called = await client.call_tool("fafnir_status", {})
                    self.assertFalse(called.is_error)
                    self.assertEqual(
                        called.structured_content["catalog"]["total"], 0
                    )
                    self.assertEqual(
                        called.structured_content["rag_index"]["state"], "empty"
                    )
                    app.repository.upsert_candidates([Candidate(
                        id="asset_store:123", source="asset_store", external_id="123", title="Fixture sky",
                        url="https://assetstore.unity.com/packages/fixture-123",
                        metadata={"asset_store_details":{"visuals":{"main_image_url":
                            "https://assetstorev1-prd-cdn.unity3d.com/key-image/fixture.png"}}},
                    )])
                    with patch("game_stack_planner.mcp_server.fetch_asset_store_image", return_value=(b"png", "image/png")):
                        compared = await client.call_tool("compare_asset_store_candidates", {"candidate_ids":["asset_store:123"]})
                    self.assertFalse(compared.is_error)
                    self.assertEqual(["text", "text", "image"], [block.type for block in compared.content])
                    self.assertEqual("asset_store:123", json.loads(compared.content[1].text)["id"])
            finally:
                app.close()

    async def test_stdio_cli_entrypoint_serves_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            parameters = StdioServerParameters(
                command=sys.executable,
                args=[
                    "-m",
                    "game_stack_planner",
                    "--db",
                    str(Path(directory) / "catalog.db"),
                    "mcp",
                ],
            )
            async with Client(parameters, read_timeout_seconds=30) as client:
                result = await client.list_tools()
                self.assertIn(
                    "retrieve_game_stack_evidence",
                    {item.name for item in result.tools},
                )
                called = await client.call_tool("stackforge_status", {})
                self.assertFalse(called.is_error)


if __name__ == "__main__":
    unittest.main()
