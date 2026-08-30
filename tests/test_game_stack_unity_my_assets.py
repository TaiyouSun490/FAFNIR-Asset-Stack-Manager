from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from game_stack_planner.api import GameStackApplication
from game_stack_planner.unity_my_assets import (
    SCHEMA,
    UnityMyAssetsError,
    load_unity_my_assets,
)


def _write_export(path: Path, *, assets: list[dict[str, object]]) -> None:
    path.write_text(
        json.dumps({
            "schema": SCHEMA,
            "generatedAtUtc": "2026-08-30T12:34:56Z",
            "unityVersion": "6000.4.3f1",
            "assets": assets,
        }, ensure_ascii=False),
        encoding="utf-8",
    )


class UnityMyAssetsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.export = self.root / "unity-my-assets.json"
        self.application = GameStackApplication(self.root / "catalog.db")

    def tearDown(self) -> None:
        self.application.close()
        self.temporary.cleanup()

    def test_sync_imports_owned_metadata_into_catalog_and_rag(self) -> None:
        _write_export(self.export, assets=[{
            "productId": 171146,
            "displayName": "100 Special Skills Effects Pack",
            "purchasedTime": "2021-11-15T14:01:58Z",
            "tags": ["VFX", "Skills"],
            "hidden": False,
        }])

        result = self.application.sync_unity_my_assets({
            "path": str(self.export),
        })

        self.assertEqual(1, result["sync"]["imported"])
        catalog = self.application.catalog(
            query="Special Skills",
            source="asset_store",
        )
        self.assertEqual(1, catalog["count"])
        item = catalog["items"][0]
        self.assertEqual("asset_store:171146", item["id"])
        self.assertEqual("owned", item["ownership"])
        self.assertEqual("confirmed_owned", item["inventory_state"])
        self.assertEqual(
            "unity_editor_my_assets",
            item["ownership_evidence"]["kind"],
        )
        self.assertTrue(item["ownership_evidence"]["verified"])
        rag = self.application.search_asset_rag(query="VFX Skills", limit=10)
        self.assertEqual(1, rag["count"])
        self.assertEqual("100 Special Skills Effects Pack", rag["items"][0]["user_alias"])

    def test_sync_is_idempotent_and_preserves_user_authored_rag(self) -> None:
        url = "https://assetstore.unity.com/packages/tools/example-tool-171146"
        self.application.save_owned_rag({
            "url": url,
            "title": "Store title",
            "user_alias": "自分用エフェクト",
            "notes": "ボス戦で使う",
            "categories": ["vfx"],
            "purchase_confirmation": True,
        })
        _write_export(self.export, assets=[{
            "productId": "171146",
            "displayName": "Updated official title",
            "purchasedTime": "2021-11-15T14:01:58Z",
            "tags": ["Effects"],
            "hidden": True,
        }])

        self.application.sync_unity_my_assets({"path": str(self.export)})
        self.application.sync_unity_my_assets({"path": str(self.export)})

        catalog = self.application.catalog(source="asset_store")
        self.assertEqual(1, catalog["count"])
        self.assertEqual("ボス戦で使う", catalog["items"][0]["description"])
        rag = self.application.search_asset_rag(query="ボス戦", limit=10)
        self.assertEqual(1, rag["count"])
        self.assertEqual("自分用エフェクト", rag["items"][0]["user_alias"])
        self.assertTrue(rag["items"][0]["ownership_evidence"]["verified"])

    def test_later_complete_sync_marks_missing_bridge_asset_stale(self) -> None:
        _write_export(self.export, assets=[{
            "productId": 171146,
            "displayName": "Temporary owned asset",
            "purchasedTime": "2021-11-15T14:01:58Z",
            "tags": ["Effects"],
            "hidden": False,
        }])
        self.application.sync_unity_my_assets({"path": str(self.export)})
        _write_export(self.export, assets=[])

        self.application.sync_unity_my_assets({"path": str(self.export)})

        item = self.application.catalog(source="asset_store")["items"][0]
        self.assertEqual("unknown", item["ownership"])
        self.assertEqual("not_in_latest_my_assets", item["inventory_state"])
        self.assertEqual(
            0,
            self.application.search_asset_rag(query="Temporary", limit=10)["count"],
        )

    def test_parser_rejects_unknown_schema_and_invalid_products(self) -> None:
        self.export.write_text('{"schema":"wrong","assets":[]}', encoding="utf-8")
        with self.assertRaises(UnityMyAssetsError):
            load_unity_my_assets(self.export)

        _write_export(self.export, assets=[{
            "productId": "../../secret",
            "displayName": "bad",
            "tags": [],
            "hidden": False,
        }])
        with self.assertRaises(UnityMyAssetsError):
            load_unity_my_assets(self.export)


class UnityBridgeStaticTests(unittest.TestCase):
    def test_bridge_is_editor_only_and_does_not_export_credentials(self) -> None:
        root = (
            Path(__file__).parents[1]
            / "unity_package"
            / "com.taiyousun.stackforge"
        )
        package = json.loads((root / "package.json").read_text(encoding="utf-8"))
        assembly = json.loads(
            (root / "Editor" / "Stackforge.Editor.asmdef").read_text(encoding="utf-8")
        )
        source = (
            root / "Editor" / "StackforgeMyAssetsWindow.cs"
        ).read_text(encoding="utf-8")

        self.assertEqual("com.taiyousun.stackforge", package["name"])
        self.assertEqual(["Editor"], assembly["includePlatforms"])
        self.assertIn("IAssetStoreRestAPI", source)
        self.assertIn("stackforge.unity-my-assets.v1", source)
        self.assertIn('method.Name == "UpdateStatus"', source)
        self.assertIn("values[0].ParameterType", source)
        self.assertNotIn('RequireType("PageFilterStatus")', source)
        self.assertIn(
            "?view=catalog&scope=owned_assets&sync=my-assets",
            source,
        )
        for forbidden in (
            "accessToken",
            "Authorization",
            "cookie",
            "GetAuthToken",
            "Purchases.json",
            "AssetInventory",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_owned_stat_deep_links_and_syncs_into_owned_catalog(self) -> None:
        static = Path(__file__).parents[1] / "game_stack_planner" / "static"
        html = (static / "index.html").read_text(encoding="utf-8")
        javascript = (static / "app.js").read_text(encoding="utf-8")

        self.assertIn(
            '/?view=catalog&amp;scope=owned_assets',
            html,
        )
        self.assertIn('initialRoute.get("sync") === "my-assets"', javascript)
        self.assertIn('showView(initialView', javascript)
        self.assertIn('return "Unity My Assets"', javascript)


if __name__ == "__main__":
    unittest.main()
