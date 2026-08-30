from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from game_stack_planner.api import GameStackApplication
from game_stack_planner.asset_store_cache import (
    discover_cache_roots,
    scan_asset_store_cache,
)
from game_stack_planner.models import Candidate
from game_stack_planner.native_messaging import (
    NativeMessagingError,
    handle_message,
)
from game_stack_planner.repository import StackRepository
from game_stack_planner.service import GameStackPlanner


def cache_root(parent: Path) -> Path:
    root = parent / "Asset Store-5.x"
    package = root / "Studio Name" / "ToolsEditor" / "Inventory Builder.unitypackage"
    package.parent.mkdir(parents=True)
    package.write_bytes(b"not opened by scanner")
    (package.parent / "ignore.txt").write_text("ignored", encoding="utf-8")
    return root


class AssetStoreCacheTests(unittest.TestCase):
    def test_scans_only_local_package_metadata_without_claiming_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            root = cache_root(Path(directory))
            result = scan_asset_store_cache(root)
        self.assertEqual(1, len(result.candidates))
        item = result.candidates[0]
        self.assertEqual("asset_store_cache", item.source)
        self.assertEqual("Inventory Builder", item.title)
        self.assertEqual("unknown", item.ownership)
        self.assertEqual("locally_cached", item.metadata["inventory_state"])
        self.assertFalse(item.metadata["ownership_evidence"]["verified"])
        self.assertEqual("Studio Name", item.metadata["publisher"])
        self.assertNotIn(str(root.parent), json.dumps(item.to_dict()))

    def test_configured_parent_resolves_asset_store_cache_child(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            expected = cache_root(parent)
            roots = discover_cache_roots(environ={
                "ASSETSTORE_CACHE_PATH": str(parent),
                "APPDATA": "",
            })
        self.assertEqual(expected, roots[0].path)
        self.assertEqual("configured", roots[0].kind)

    def test_application_imports_cache_into_owned_local_search_lane(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = cache_root(base)
            repository = StackRepository(base / "catalog.db")
            application = GameStackApplication(repository=repository)
            result = application.scan_cache({"path": str(root)})
            catalog = application.catalog(scope="owned_assets")
            self.assertEqual(1, result["scan"]["found"])
            self.assertFalse(result["scan"]["ownership_confirmed"])
            self.assertEqual(1, catalog["count"])
            self.assertEqual("locally_cached", catalog["items"][0]["inventory_state"])
            application.close()


class FederatedScopeTests(unittest.TestCase):
    def test_recommendation_keeps_three_sources_in_independent_lanes(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = StackRepository(Path(directory) / "catalog.db")
            repository.save_manual_asset(
                url="https://assetstore.unity.com/packages/tools/owned-101",
                title="Owned Inventory",
                ownership="owned",
                categories=("inventory",),
            )
            repository.save_manual_asset(
                url="https://assetstore.unity.com/packages/tools/market-102",
                title="Marketplace Inventory",
                categories=("inventory",),
            )
            repository.upsert_candidates((Candidate(
                id="github:inventory",
                source="github",
                external_id="studio/inventory",
                title="Open Inventory",
                url="https://github.com/studio/inventory",
                categories=("inventory",),
                license="MIT",
                stars=500,
            ),))
            result = GameStackPlanner(repository).recommend(
                prompt="アイテム収集ゲーム",
                remote=False,
            )
            groups = result["recommendation_groups"]["inventory"]
            self.assertEqual("asset_store:101", groups["owned_assets"][0]["candidate"]["id"])
            self.assertEqual("asset_store:102", groups["asset_store_market"][0]["candidate"]["id"])
            self.assertEqual("github:inventory", groups["community"][0]["candidate"]["id"])
            repository.close()

    def test_catalog_scope_filters_do_not_leak(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = StackRepository(Path(directory) / "catalog.db")
            application = GameStackApplication(repository=repository)
            application.save_manual({
                "url": "https://assetstore.unity.com/packages/tools/owned-201",
                "title": "Owned",
                "ownership": "owned",
                "categories": [],
            })
            application.save_manual({
                "url": "https://assetstore.unity.com/packages/tools/market-202",
                "title": "Market",
                "ownership": "candidate",
                "categories": [],
            })
            repository.upsert_candidates((Candidate(
                id="github:test",
                source="github",
                external_id="test/repo",
                title="Repo",
                url="https://github.com/test/repo",
            ),))
            expected = {
                "owned_assets": {"asset_store:201"},
                "asset_store_market": {"asset_store:202"},
                "community": {"github:test"},
            }
            for scope, ids in expected.items():
                with self.subTest(scope=scope):
                    result = application.catalog(scope=scope)
                    self.assertEqual(ids, {item["id"] for item in result["items"]})
            owned = application.catalog(scope="owned_assets")["items"][0]
            self.assertEqual("user_asserted", owned["ownership_evidence"]["kind"])
            self.assertFalse(owned["ownership_evidence"]["verified"])
            application.close()


class StackforgeNativeMessagingTests(unittest.TestCase):
    def test_native_host_saves_only_candidate_state(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = StackRepository(Path(directory) / "catalog.db")
            application = GameStackApplication(repository=repository)
            response = handle_message({
                "action": "save_asset_store_candidate",
                "schema_version": 1,
                "url": "https://assetstore.unity.com/packages/tools/test-301?ref=browser",
                "title": "Test Asset",
                "notes": "selected by user",
                "categories": ["inventory"],
            }, application)
            self.assertTrue(response["ok"])
            self.assertEqual("candidate", response["item"]["ownership"])
            self.assertEqual("asset_store_market", response["item"]["scope"])
            application.close()

    def test_native_host_rejects_unknown_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            application = GameStackApplication(Path(directory) / "catalog.db")
            with self.assertRaises(NativeMessagingError):
                handle_message({
                    "action": "get_stackforge_status",
                    "schema_version": 1,
                    "cookie": "must-not-be-accepted",
                }, application)
            application.close()


class AssetStoreExtensionStaticTests(unittest.TestCase):
    def test_extension_has_minimal_permissions_and_no_page_scraper(self):
        root = Path(__file__).parents[1] / "browser_extension" / "unity_asset_store"
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        worker = (root / "service_worker.js").read_text(encoding="utf-8")
        self.assertEqual({"activeTab", "nativeMessaging"}, set(manifest["permissions"]))
        self.assertNotIn("host_permissions", manifest)
        self.assertNotIn("scripting", manifest["permissions"])
        self.assertNotIn("executeScript", worker)
        self.assertNotIn("fetch(", worker)
        self.assertIn('const NATIVE_HOST = "jp.game_stack_planner";', worker)
        self.assertIn("購入未確認候補として保存", (root / "popup.html").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
