from __future__ import annotations

import json
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from game_stack_planner.asset_store_details import (
    AssetStoreDetailsError,
    HttpDocument,
    parse_product_page,
    resolve_product_urls,
)
from game_stack_planner.asset_store_visuals import review_candidate_visuals
from game_stack_planner.compatibility import assess_candidate_compatibility
from game_stack_planner.local_asset_validation import validate_cached_asset
from game_stack_planner.models import Candidate, GameRequirement, ProjectSnapshot
from game_stack_planner.repository import StackRepository
from game_stack_planner.service import GameStackPlanner, _candidate_score
from game_stack_planner.unity_my_assets import UnityMyAssetsExport, UnityOwnedAsset
from game_stack_planner.unitypackage_inspection import (
    UnityPackageInspectionError,
    inspect_unitypackage,
)


PRODUCT_ID = "42001"
PRODUCT_URL = (
    "https://assetstore.unity.com/packages/tools/utilities/"
    "subsea-systems-42001"
)


def product_details_state() -> dict[str, object]:
    return {
        "data": {
            "ENTITY": {
                "Product": {
                    PRODUCT_ID: {
                        "id": PRODUCT_ID,
                        "name": "Subsea Systems",
                        "slug": "subsea-systems-42001",
                        "description": "<p>Inventory, save, and interaction tools.</p>",
                        "aiDescription": "",
                        "elevatorPitch": "Build an underwater horror game.",
                        "keyFeatures": "<ul><li>Inventory</li><li>Save</li></ul>",
                        "compatibilityInfo": "Requires Input System 1.7 or later.",
                        "publishNotes": "<p>Unity 6 support.</p>",
                        "publisher": {"type": "id", "id": ["ProductPublisher", "9"]},
                        "category": {"type": "id", "id": ["Category", "22"]},
                        "currentVersion": {"type": "id", "id": ["ProductVersion", "77"]},
                        "popularTags": [
                            {"type": "id", "id": ["ProductTag", "91"]}
                        ],
                        "dependencies": [
                            {"type": "id", "id": ["Product", "42002"]}
                        ],
                        "dependencyIds": ["42002"],
                        "supportedUnityVersions": ["2022.3.10", "6000.0.20"],
                        "srps": [
                            {"version": "2022.3.10f1", "types": ["standard", "lightweight"]},
                            {"version": "6000.0.20f1", "types": ["lightweight"]},
                        ],
                        "rating": {"average": 4.5, "count": 12},
                        "reviewCount": 13,
                        "downloadSize": "2048",
                        "assetCount": 42,
                        "originalPrice": {
                            "originalPrice": "20.00",
                            "finalPrice": "10.00",
                            "currency": "USD",
                            "isFree": False,
                            "entitlementType": "SEAT",
                            "discount": {"percentage": 50},
                        },
                        "packageType": "unitypackage",
                        "state": "published",
                        "customLicense": False,
                        "licenseText": "",
                        "firstPublishedDate": "2024-01-02T00:00:00Z",
                        "mainImage": {
                            "big": "//assetstorev1-prd-cdn.unity3d.com/key-image/main.jpg",
                        },
                        "images": [
                            {
                                "type": "screenshot",
                                "imageUrl": "//assetstorev1-prd-cdn.unity3d.com/package-screenshot/one.jpg",
                                "thumbnailUrl": "//assetstorev1-prd-cdn.unity3d.com/package-screenshot/one_thumb.jpg",
                            },
                            {
                                "type": "youtube",
                                "imageUrl": "https://www.youtube.com/embed/example",
                                "thumbnailUrl": "//assetstorev1-prd-cdn.unity3d.com/package-screenshot/video_thumb.png",
                            },
                        ],
                    },
                    "42002": {"id": "42002", "name": "Required Core"},
                },
                "ProductPublisher": {
                    "9": {
                        "id": "9",
                        "name": "Deep Studio",
                        "url": "https://publisher.example/",
                        "supportUrl": "https://publisher.example/support",
                    }
                },
                "Category": {
                    "22": {"id": "22", "name": "Utilities", "slug": "tools/utilities"}
                },
                "ProductVersion": {
                    "77": {
                        "id": "77",
                        "name": "2.1.0",
                        "publishedDate": "2026-08-01T00:00:00Z",
                    }
                },
                "ProductTag": {"91": {"id": "91", "name": "Inventory"}},
            }
        }
    }


def product_html() -> str:
    state = json.dumps(product_details_state(), ensure_ascii=False)
    return (
        "<html><head></head><body><script type='text/javascript'>"
        "var __component__={};__component__.ReactDOMrender("
        + state
        + ",document.getElementById('app'));</script></body></html>"
    )


def parsed_details() -> dict[str, object]:
    return parse_product_page(
        product_html(),
        product_id=PRODUCT_ID,
        page_url=PRODUCT_URL,
        fetched_at="2026-08-31T00:00:00+00:00",
    )


def write_unitypackage(path: Path, *, unsafe: bool = False) -> tuple[str, ...]:
    files = {
        "Assets/Subsea/Subsea.asmdef": json.dumps({
            "name": "Subsea.Runtime",
            "references": ["Unity.InputSystem"],
        }).encode(),
        "Assets/Subsea/package.json": json.dumps({
            "name": "com.deep.subsea",
            "version": "2.0.0",
            "dependencies": {"com.unity.inputsystem": "1.7.0"},
        }).encode(),
        "Assets/Subsea/Runtime/Controller.cs": (
            b"using UnityEngine.InputSystem; using UnityEngine.Rendering.Universal;"
        ),
        "Assets/Plugins/x86_64/native.dll": b"MZ" + b"\0" * 200,
    }
    guids: list[str] = []
    with tarfile.open(path, "w:gz") as archive:
        if unsafe:
            info = tarfile.TarInfo("../escape/pathname")
            info.size = 4
            archive.addfile(info, io.BytesIO(b"evil"))
            return ()
        for index, (logical, content) in enumerate(files.items(), start=1):
            guid = f"{index:032x}"
            guids.append(guid)
            # Legacy Asset Store exports use this literal record terminator.
            pathname = (logical + "\n00").encode()
            path_info = tarfile.TarInfo(f"{guid}/pathname")
            path_info.size = len(pathname)
            archive.addfile(path_info, io.BytesIO(pathname))
            asset_info = tarfile.TarInfo(f"{guid}/asset")
            asset_info.size = len(content)
            archive.addfile(asset_info, io.BytesIO(content))
    return tuple(guids)


class AssetStoreProductPageTests(unittest.TestCase):
    def test_parses_semantic_structured_compatibility_price_and_rating_fields(self) -> None:
        details = parsed_details()
        self.assertEqual("Subsea Systems", details["name"])
        self.assertEqual("Deep Studio", details["publisher"]["name"])
        self.assertEqual("Utilities", details["category"]["name"])
        self.assertEqual("2.1.0", details["latest_version"])
        self.assertEqual("2022.3.10", details["original_unity_version"])
        self.assertEqual(
            ["built_in", "urp"],
            details["render_pipeline_compatibility"][0]["compatible_pipelines"],
        )
        self.assertEqual("urp", details["render_pipeline_summary"])
        self.assertEqual("10.00", details["price"]["current"])
        self.assertEqual(50, details["price"]["discount_percent"])
        self.assertEqual(4.5, details["rating"]["average"])
        self.assertEqual(13, details["rating"]["review_count"])
        self.assertEqual(
            [{"product_id": "42002", "title": "Required Core"}],
            details["dependencies"],
        )
        self.assertNotIn("<", details["description"])
        self.assertEqual(3, details["visuals"]["image_count"])
        self.assertEqual(
            "https://assetstorev1-prd-cdn.unity3d.com/key-image/main.jpg",
            details["visuals"]["main_image_url"],
        )
        self.assertEqual(
            "https://assetstorev1-prd-cdn.unity3d.com/package-screenshot/video_thumb.png",
            details["visuals"]["gallery"][1]["image_url"],
        )

    def test_visual_review_is_opt_in_bounded_and_returns_actual_images(self) -> None:
        candidate = Candidate(
            id=f"asset_store:{PRODUCT_ID}",
            source="asset_store",
            external_id=PRODUCT_ID,
            title="Subsea Systems",
            url=PRODUCT_URL,
        )
        requested: list[str] = []

        def fetch_image(url: str) -> tuple[bytes, str]:
            requested.append(url)
            return b"image-bytes", "image/jpeg"

        with patch(
            "game_stack_planner.asset_store_visuals.fetch_product_details",
            return_value=(parsed_details(), HttpDocument(b"", PRODUCT_URL)),
        ):
            metadata, images = review_candidate_visuals(
                candidate,
                detail="quick",
                fetch_image=fetch_image,
            )

        self.assertEqual("quick", metadata["detail"])
        self.assertEqual(3, metadata["image_count"])
        self.assertEqual(3, len(images))
        self.assertEqual(3, len(requested))
        self.assertTrue(all(item.content == b"image-bytes" for item in images))

    def test_rejects_page_for_a_different_product(self) -> None:
        with self.assertRaises(AssetStoreDetailsError):
            parse_product_page(
                product_html(),
                product_id="99999",
                page_url=PRODUCT_URL,
            )

    def test_resolves_only_requested_product_urls_from_official_sitemaps(self) -> None:
        documents = {
            "https://assetstore.unity.com/sitemap.xml": b"""
                <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
                  <sitemap><loc>https://assetstore.unity.com/products.xml</loc></sitemap>
                </sitemapindex>
            """,
            "https://assetstore.unity.com/products.xml": f"""
                <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
                  <url><loc>{PRODUCT_URL}</loc></url>
                  <url><loc>https://assetstore.unity.com/packages/3d/other-99</loc></url>
                </urlset>
            """.encode(),
        }

        def fetch(url: str, maximum: int) -> HttpDocument:
            self.assertLessEqual(len(documents[url]), maximum)
            return HttpDocument(documents[url], url)

        self.assertEqual(
            {PRODUCT_ID: PRODUCT_URL},
            resolve_product_urls((PRODUCT_ID,), fetch=fetch),
        )


class AssetStoreDetailRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = StackRepository(
            Path(self.temporary.name) / "catalog.sqlite3"
        )
        self.repository.import_unity_my_assets(
            UnityMyAssetsExport(
                generated_at_utc="2026-08-31T00:00:00Z",
                unity_version="6000.0.32f1",
                assets=(
                    UnityOwnedAsset(
                        product_id=PRODUCT_ID,
                        display_name="Subsea Systems",
                        purchased_time="2026-08-30T00:00:00Z",
                        tags=("horror",),
                        hidden=False,
                    ),
                ),
            ),
            export_path="test.json",
        )

    def tearDown(self) -> None:
        self.repository.close()
        self.temporary.cleanup()

    def test_job_is_resumable_and_updates_candidate_and_rag_document(self) -> None:
        self.assertEqual(1, self.repository.enqueue_asset_store_detail_sync())
        self.repository.set_asset_store_product_url(
            f"asset_store:{PRODUCT_ID}", PRODUCT_URL
        )
        claimed = self.repository.claim_asset_store_detail_job()
        self.assertIsNotNone(claimed)
        updated = self.repository.complete_asset_store_detail_job(
            claimed.id,
            parsed_details(),
            etag="test-etag",
        )
        self.assertEqual(PRODUCT_URL, updated.url)
        self.assertEqual("2.1.0", updated.version)
        self.assertEqual("2022.3.10", updated.unity_version)
        self.assertEqual("urp", updated.render_pipeline)
        self.assertEqual("10.00", updated.metadata["asset_store_details"]["price"]["current"])
        status = self.repository.asset_store_detail_status()
        self.assertEqual(1, status["enriched"])
        self.assertEqual(1.0, status["coverage"])
        self.assertEqual(0, status["due"])
        row = self.repository._connection.execute(
            "SELECT search_text FROM asset_rag_documents WHERE candidate_id = ?",
            (updated.id,),
        ).fetchone()
        self.assertIn("underwater horror", str(row["search_text"]))
        self.assertIn("Requires Input System", str(row["search_text"]))

    def test_my_assets_resync_preserves_resolved_product_url_and_details(self) -> None:
        self.repository.enqueue_asset_store_detail_sync()
        self.repository.set_asset_store_product_url(
            f"asset_store:{PRODUCT_ID}", PRODUCT_URL
        )
        claimed = self.repository.claim_asset_store_detail_job()
        self.repository.complete_asset_store_detail_job(claimed.id, parsed_details())
        self.repository.import_unity_my_assets(
            UnityMyAssetsExport(
                generated_at_utc="2026-09-01T00:00:00Z",
                unity_version="6000.0.32f1",
                assets=(
                    UnityOwnedAsset(
                        product_id=PRODUCT_ID,
                        display_name="Subsea Systems",
                        purchased_time="2026-08-30T00:00:00Z",
                        tags=("horror", "save"),
                        hidden=False,
                    ),
                ),
            ),
            export_path="test.json",
        )
        item = self.repository.get_candidate(f"asset_store:{PRODUCT_ID}")
        self.assertEqual(PRODUCT_URL, item.url)
        self.assertIn("asset_store_details", item.metadata)

    def test_detail_job_lease_serializes_workers_across_process_connections(self) -> None:
        self.repository.enqueue_asset_store_detail_sync()
        second = StackRepository(self.repository.path)
        claimed = None
        try:
            claimed = self.repository.claim_asset_store_detail_job()
            self.assertIsNotNone(claimed)
            self.assertIsNone(second.claim_asset_store_detail_job())
        finally:
            if claimed is not None:
                self.repository.fail_asset_store_detail_job(
                    claimed.id,
                    "test release",
                    retry_after_seconds=1,
                )
            second.close()

    def test_automatic_enqueue_preserves_error_backoff(self) -> None:
        self.repository.enqueue_asset_store_detail_sync()
        claimed = self.repository.claim_asset_store_detail_job()
        self.assertIsNotNone(claimed)
        self.repository.fail_asset_store_detail_job(
            claimed.id,
            "temporary network failure",
            retry_after_seconds=86_400,
        )
        self.assertEqual(0, self.repository.enqueue_asset_store_detail_sync())
        self.assertEqual(0, self.repository.asset_store_detail_status()["due"])


class CompatibilityTests(unittest.TestCase):
    def candidate(self) -> Candidate:
        return Candidate(
            id=f"asset_store:{PRODUCT_ID}",
            source="asset_store",
            external_id=PRODUCT_ID,
            title="Subsea Systems",
            url=PRODUCT_URL,
            description="Inventory save interaction",
            categories=("inventory",),
            ownership="owned",
            metadata={"asset_store_details": parsed_details()},
        )

    @staticmethod
    def project(version: str, pipeline: str) -> ProjectSnapshot:
        return ProjectSnapshot(
            path="C:/Project",
            name="Project",
            unity_version=version,
            render_pipeline=pipeline,
            input_backend="input-system",
            packages=(),
        )

    def test_matches_same_unity_stream_and_pipeline(self) -> None:
        result = assess_candidate_compatibility(
            self.candidate(),
            project=self.project("2022.3.20f1", "urp"),
            platform="pc",
        )
        self.assertEqual("compatible", result["status"])
        self.assertEqual("2022.3.10f1", result["matched_unity_version"])

    def test_rejects_pipeline_explicitly_absent_from_matching_row(self) -> None:
        result = assess_candidate_compatibility(
            self.candidate(),
            project=self.project("6000.0.32f1", "hdrp"),
            platform="pc",
        )
        self.assertEqual("incompatible", result["status"])

    def test_rejects_project_older_than_original_unity_version(self) -> None:
        result = assess_candidate_compatibility(
            self.candidate(),
            project=self.project("2021.3.40f1", "urp"),
            platform="pc",
        )
        self.assertEqual("incompatible", result["status"])

    def test_incompatible_asset_is_filtered_before_recommendation_selection(self) -> None:
        requirement = GameRequirement(
            key="inventory",
            title="Inventory",
            priority="required",
            query="inventory save",
            rationale="test",
        )
        score = _candidate_score(
            requirement,
            self.candidate(),
            project=self.project("6000.0.32f1", "hdrp"),
            platform="pc",
        )
        self.assertEqual(0.0, score["score"])
        self.assertEqual("incompatible", score["candidate"]["compatibility"]["status"])

    def test_planner_structurally_filters_before_rag_and_uses_rag_relevance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = StackRepository(Path(directory) / "catalog.db")
            try:
                compatible = self.candidate()
                incompatible_details = parsed_details()
                incompatible_details["original_unity_version"] = "6000.0.0"
                incompatible = Candidate(
                    id="asset_store:99999",
                    source="asset_store",
                    external_id="99999",
                    title="Unrelated newer product",
                    url="https://assetstore.unity.com/packages/tools/99999",
                    ownership="owned",
                    metadata={"asset_store_details": incompatible_details},
                )
                repository.upsert_candidates((compatible, incompatible))
                planner = GameStackPlanner(repository)
                project = self.project("2022.3.20f1", "urp")
                seen_eligible: list[frozenset[str]] = []

                def fake_rag(**kwargs):
                    ids = frozenset(kwargs["candidate_ids"])
                    seen_eligible.append(ids)
                    return {
                        "items": [{
                            "candidate_id": compatible.id,
                            "score": 0.86,
                            "rank_score": 0.9,
                        }],
                        "retrieval_mode": "hybrid_dense",
                    }

                with patch.object(planner, "scan_project", return_value=project), \
                        patch.object(repository, "search_asset_rag", side_effect=fake_rag):
                    result = planner.recommend(
                        prompt="一人称ホラーゲーム",
                        project_path="C:/Project",
                        platform="pc",
                        budget="owned_first",
                        remote=False,
                    )

                self.assertTrue(seen_eligible)
                self.assertTrue(all(compatible.id in ids for ids in seen_eligible))
                self.assertTrue(all(incompatible.id not in ids for ids in seen_eligible))
                returned = [
                    item
                    for items in result["recommendations"].values()
                    for item in items
                    if item["candidate"]["id"] == compatible.id
                ]
                self.assertTrue(returned)
                self.assertEqual("hybrid_dense", returned[0]["candidate"]["retrieval"]["mode"])
            finally:
                repository.close()


class UnityPackageInspectionTests(unittest.TestCase):
    def test_inspects_manifests_code_markers_and_native_plugins_without_extracting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "Subsea Systems.unitypackage"
            write_unitypackage(package)
            inspection = inspect_unitypackage(package)
        summary = inspection.summary
        self.assertEqual("2.0.0", summary["version"])
        self.assertEqual("Subsea.Runtime", summary["assembly_definitions"][0]["name"])
        self.assertTrue(summary["code_markers"]["input_system"])
        self.assertTrue(summary["code_markers"]["urp"])
        self.assertEqual(1, summary["content"]["native_plugins"])
        self.assertIn("windows", summary["platform_hints"])

    def test_rejects_unsafe_archive_member_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "unsafe.unitypackage"
            write_unitypackage(package, unsafe=True)
            with self.assertRaises(UnityPackageInspectionError):
                inspect_unitypackage(package)

    def test_project_validation_detects_imported_guids_and_missing_upm_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache_root = root / "Asset Store-5.x"
            package = cache_root / "Deep Studio" / "Tools" / "Subsea Systems.unitypackage"
            package.parent.mkdir(parents=True)
            guids = write_unitypackage(package)
            project_root = root / "Project"
            (project_root / "Assets").mkdir(parents=True)
            (project_root / "Packages").mkdir()
            (project_root / "ProjectSettings").mkdir()
            (project_root / "Assets" / "Subsea.meta").write_text(
                f"fileFormatVersion: 2\nguid: {guids[0]}\n",
                encoding="utf-8",
            )
            cache_candidate = Candidate(
                id="asset_store_cache:test",
                source="asset_store_cache",
                external_id="test",
                title="Subsea Systems",
                url="",
                metadata={
                    "cache_relative_path": (
                        "Deep Studio/Tools/Subsea Systems.unitypackage"
                    ),
                    "cache_root_kind": "custom",
                },
            )
            product = Candidate(
                id=f"asset_store:{PRODUCT_ID}",
                source="asset_store",
                external_id=PRODUCT_ID,
                title="Subsea Systems",
                url=PRODUCT_URL,
                metadata={"asset_store_details": parsed_details()},
            )
            project = ProjectSnapshot(
                path=str(project_root),
                name="Project",
                unity_version="2022.3.20f1",
                render_pipeline="urp",
                input_backend="input-system",
                packages=(),
            )
            validation = validate_cached_asset(
                product,
                cache_candidate,
                project,
                platform="pc",
                explicit_cache_root=cache_root,
            )
        self.assertEqual("partial", validation["import_detection"]["state"])
        self.assertEqual(1, validation["import_detection"]["matching_guids"])
        self.assertEqual(
            "com.unity.inputsystem",
            validation["static_checks"]["missing_packages"][0]["name"],
        )
        self.assertEqual("not_run", validation["compile_test"]["state"])


if __name__ == "__main__":
    unittest.main()
