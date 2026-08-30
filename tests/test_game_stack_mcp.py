from __future__ import annotations

import tempfile
import sys
import unittest
from pathlib import Path

from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

from game_stack_planner.api import GameStackApplication
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
                    self.assertIn("prepare_candidate_install", tools)
                    self.assertIn("apply_reviewed_install", tools)
                    self.assertTrue(
                        tools["search_unity_assets"].annotations.read_only_hint
                    )
                    self.assertTrue(
                        tools["apply_reviewed_install"].annotations.destructive_hint
                    )

                    called = await client.call_tool("stackforge_status", {})
                    self.assertFalse(called.is_error)
                    self.assertEqual(
                        called.structured_content["catalog"]["total"], 0
                    )
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
