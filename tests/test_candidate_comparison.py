import http.client
import json
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from mcp.server.mcpserver.utilities.types import Image
from game_stack_planner.api import ApiError, GameStackApplication
from game_stack_planner.asset_store_visuals import AssetStoreVisualsError
from game_stack_planner.mcp_server import StackforgeMcpTools
from game_stack_planner.models import Candidate
from game_stack_planner.repository import StackRepository
from game_stack_planner.server import create_server


class CandidateComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = StackRepository(Path(self.temp.name) / "catalog.db")
        self.app = GameStackApplication(repository=self.repo)
        self.tools = StackforgeMcpTools(self.app)
        self.ids = [f"asset_store:{i}" for i in range(1, 51)]
        self.repo.upsert_candidates([Candidate(
            id=candidate_id, source="asset_store", external_id=str(i), title=f"Sky {i}",
            url=f"https://assetstore.unity.com/packages/package/{i}", ownership="owned", version="1.2",
            metadata={"private_path":"C:/private", "approval_nonce":"secret", "ownership_evidence":{"verified":True, "private_path":"C:/private-evidence"},
                      "asset_store_details":{"download_size_bytes":136000000,
                          "visuals":{"main_image_url":f"https://assetstorev1-prd-cdn.unity3d.com/key-image/{i}.jpg",
                                     "gallery":[{"image_url":"https://localhost/private.png"}]}}},
        ) for i, candidate_id in enumerate(self.ids, 1)])

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def test_many_candidates_paginate_without_silently_limiting_shortlist(self):
        first = self.app.compare_asset_candidates({"candidate_ids":self.ids})
        self.assertEqual(50, first["total"])
        self.assertEqual(6, first["next_offset"])
        self.assertEqual(self.ids[:6], [c["id"] for c in first["items"]])
        last = self.app.compare_asset_candidates({"candidate_ids":self.ids, "offset":48})
        self.assertEqual(self.ids[48:], [c["id"] for c in last["items"]])
        self.assertIsNone(last["next_offset"])
        self.assertFalse(first["selection_is_approval"])
        encoded = json.dumps(first)
        self.assertNotIn("C:/private", encoded)
        self.assertNotIn("secret", encoded)
        self.assertNotIn("localhost", encoded)
        for c in first["items"]:
            self.assertEqual(c["id"], c["images"][0]["candidate_id"])

    def test_invalid_lists_boundaries_and_unknown_candidates(self):
        for body in [{"candidate_ids":[]}, {"candidate_ids":[1]}, {"candidate_ids":[self.ids[0]] * 2},
                     {"candidate_ids":self.ids,"limit":0}, {"candidate_ids":self.ids,"limit":7},
                     {"candidate_ids":self.ids,"offset":-1}, {"candidate_ids":self.ids,"offset":True},
                     {"candidate_ids":["asset_store:missing"]}, {"candidate_ids":self.ids,"approve":True}]:
            with self.subTest(body=body), self.assertRaises(ApiError):
                self.app.compare_asset_candidates(body)
        self.assertEqual([], self.app.compare_asset_candidates({"candidate_ids":self.ids,"offset":50})["items"])

    def test_mcp_identity_immediately_precedes_actual_image_and_never_downloads(self):
        with patch("game_stack_planner.mcp_server.fetch_asset_store_image", return_value=(b"png", "image/png")), \
             patch.object(self.app, "prepare_asset_store_download") as prepare:
            content = self.tools.compare_asset_store_candidates(self.ids, limit=3)
        self.assertEqual(7, len(content))
        self.assertEqual(50, json.loads(content[0])["total"])
        for index in range(3):
            self.assertEqual(self.ids[index], json.loads(content[1+index*2])["id"])
            self.assertIsInstance(content[2+index*2], Image)
        prepare.assert_not_called()

    def test_images_off_is_strict_and_partial_image_failure_keeps_other_candidates(self):
        with patch("game_stack_planner.mcp_server.fetch_asset_store_image") as fetch, \
             patch.object(self.app, "asset_product_preview") as preview:
            content = self.tools.compare_asset_store_candidates(self.ids, include_images=False)
            fetch.assert_not_called(); preview.assert_not_called()
        self.assertEqual(4, len(content))
        with patch("game_stack_planner.mcp_server.fetch_asset_store_image", side_effect=[
            AssetStoreVisualsError("unavailable"), (b"png", "image/png")]):
            content = self.tools.compare_asset_store_candidates(self.ids, limit=2)
        self.assertEqual("unavailable", json.loads(content[1])["image_status"])
        self.assertEqual(self.ids[1], json.loads(content[2])["id"])
        self.assertIsInstance(content[3], Image)

    def test_image_budget_and_missing_images_do_not_become_false_compatibility_findings(self):
        with patch("game_stack_planner.mcp_server.fetch_asset_store_image", return_value=(b"x" * (5*1024*1024), "image/png")) as fetch:
            content = self.tools.compare_asset_store_candidates(self.ids, limit=6)
        self.assertEqual(4, fetch.call_count)
        self.assertEqual(4, sum(isinstance(c, Image) for c in content))
        self.assertIn("Page image budget", content[-1])
        candidate = replace(self.repo.get_candidate(self.ids[0]), metadata={"asset_store_details":{"visuals":None}})
        self.repo.upsert_candidates([candidate])
        with patch.object(self.app, "asset_product_preview", side_effect=ApiError(422, "missing", "No image")):
            content = self.tools.compare_asset_store_candidates([self.ids[0]])
        self.assertEqual("unavailable", json.loads(content[1])["image_status"])
        self.assertNotIn("incompatible", content[1])

    def test_http_route_serves_read_only_cards_and_blocks_foreign_origins(self):
        server = create_server(self.app, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            for origin, status in [(f"http://127.0.0.1:{server.server_port}",200),("https://evil.invalid",403)]:
                client = http.client.HTTPConnection("127.0.0.1", server.server_port)
                client.request("POST", "/api/asset-store/compare", json.dumps({"candidate_ids":self.ids[:3]}),
                               {"Content-Type":"application/json","Origin":origin})
                response = client.getresponse(); self.assertEqual(status, response.status); response.read(); client.close()
        finally:
            server.shutdown(); server.server_close(); thread.join()
