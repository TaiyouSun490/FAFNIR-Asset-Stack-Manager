from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from game_stack_planner.api import ApiError, GameStackApplication
from game_stack_planner.models import Candidate
from game_stack_planner.repository import StackRepository
from game_stack_planner.requirements import derive_requirements
from game_stack_planner.server import create_server
from game_stack_planner.service import GameStackPlanner
from game_stack_planner.sources import GitHubSource, OpenUpmSource, SourceResult
from game_stack_planner.unity_project import scan_unity_project


def make_unity_project(root: Path) -> Path:
    (root / "Packages").mkdir(parents=True)
    (root / "ProjectSettings").mkdir()
    (root / "Packages" / "manifest.json").write_text(
        json.dumps({
            "dependencies": {
                "com.unity.inputsystem": "1.11.2",
                "com.unity.render-pipelines.universal": "17.0.3",
                "com.example.git": "https://github.com/example/pkg.git",
            }
        }),
        encoding="utf-8",
    )
    (root / "Packages" / "packages-lock.json").write_text(
        json.dumps({
            "dependencies": {
                "com.unity.inputsystem": {
                    "version": "1.11.2",
                    "source": "registry",
                    "depth": 0,
                },
                "com.unity.render-pipelines.universal": {
                    "version": "17.0.3",
                    "source": "registry",
                    "depth": 0,
                },
                "com.example.git": {
                    "version": "https://github.com/example/pkg.git",
                    "source": "git",
                    "depth": 0,
                },
            }
        }),
        encoding="utf-8",
    )
    (root / "ProjectSettings" / "ProjectVersion.txt").write_text(
        "m_EditorVersion: 6000.0.32f1\n",
        encoding="utf-8",
    )
    (root / "ProjectSettings" / "ProjectSettings.asset").write_text(
        "PlayerSettings:\n"
        "  companyName: Test Studio\n"
        "  productName: Test Game\n"
        "  activeInputHandler: 1\n",
        encoding="utf-8",
    )
    return root


class FakeRemoteSource:
    def __init__(self, source: str) -> None:
        self.source = source

    def search(self, requirement, *, limit=5):
        candidate = Candidate(
            id=f"{self.source}:{requirement.key}",
            source=self.source,
            external_id=requirement.key,
            title=f"{requirement.title} Toolkit",
            url=f"https://example.com/{self.source}/{requirement.key}",
            description=requirement.query,
            categories=(requirement.key,),
            license="MIT",
            stars=1200 if self.source == "github" else 0,
            updated_at="2099-01-01T00:00:00Z",
        )
        return SourceResult(source=self.source, candidates=(candidate,), remaining=50)


class FakeFetcher:
    def __init__(self, payload):
        self.payload = payload
        self.urls = []

    def get(self, url, *, headers=None):
        self.urls.append(url)
        return self.payload, {"x-ratelimit-remaining": "42"}


class RequirementTests(unittest.TestCase):
    def test_japanese_roguelite_is_decomposed(self):
        values = derive_requirements(
            "4人協力型ローグライト。自動生成ダンジョンと装備収集、ボス戦。Steam Deck対応。"
        )
        keys = {item.key for item in values}
        self.assertTrue({
            "networking",
            "lobby_matchmaking",
            "procedural_generation",
            "combat",
            "inventory",
            "input",
            "steam",
        }.issubset(keys))
        self.assertIn("マルチ", next(
            item.title for item in values if item.key == "networking"
        ))

    def test_quest_adds_xr(self):
        keys = {item.key for item in derive_requirements("物理パズル", platform="quest")}
        self.assertIn("xr", keys)
        self.assertIn("input", keys)

    def test_underwater_horror_brief_keeps_specific_gameplay_requirements(self):
        values = derive_requirements(
            "閉鎖された海底研究施設の一人称ホラー。浸水した通路、非常灯、"
            "徘徊する異形、足音の変化、鍵と暗証番号、持ち物管理、"
            "チェックポイント保存が必要。"
        )
        keys = {item.key for item in values}
        self.assertTrue({
            "character_controller",
            "camera",
            "save_system",
            "inventory",
            "enemy_ai",
            "interaction",
            "puzzle",
            "horror_atmosphere",
            "lighting",
            "water_environment",
            "footstep_audio",
            "visual_assets",
            "character_art",
        }.issubset(keys))
        self.assertNotIn("combat", keys)


class ProjectTests(unittest.TestCase):
    def test_scan_detects_unity_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = scan_unity_project(make_unity_project(Path(directory)))
        self.assertEqual(snapshot.unity_version, "6000.0.32f1")
        self.assertEqual(snapshot.render_pipeline, "urp")
        self.assertEqual(snapshot.input_backend, "input-system")
        self.assertEqual(snapshot.product_name, "Test Game")
        self.assertEqual(len(snapshot.packages), 3)
        self.assertTrue(any("not pinned" in warning for warning in snapshot.warnings))


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.requirement = derive_requirements("オンライン協力")[0]

    def test_github_response_mapping(self):
        fetcher = FakeFetcher({
            "items": [{
                "full_name": "studio/unity-net",
                "html_url": "https://github.com/studio/unity-net",
                "description": "Networking",
                "topics": ["unity"],
                "license": {"spdx_id": "MIT"},
                "stargazers_count": 900,
                "pushed_at": "2026-01-01T00:00:00Z",
            }]
        })
        result = GitHubSource(fetcher=fetcher).search(self.requirement)
        self.assertEqual(result.candidates[0].license, "MIT")
        self.assertEqual(result.candidates[0].stars, 900)
        self.assertIn("api.github.com/search/repositories", fetcher.urls[0])

    def test_openupm_response_mapping(self):
        fetcher = FakeFetcher({
            "objects": [{
                "package": {
                    "name": "com.studio.net",
                    "description": "Networking",
                    "version": "1.2.3",
                    "license": "MIT",
                    "links": {"homepage": "https://example.com/pkg"},
                },
                "score": {"final": 0.8},
            }]
        })
        result = OpenUpmSource(fetcher=fetcher).search(self.requirement)
        self.assertEqual(result.candidates[0].version, "1.2.3")
        self.assertEqual(result.candidates[0].source, "openupm")


class RepositoryAndServiceTests(unittest.TestCase):
    def test_repository_preserves_owned_status(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = StackRepository(Path(directory) / "catalog.db")
            item = repository.save_manual_asset(
                url="https://assetstore.unity.com/packages/tools/test-1",
                title="Test",
                ownership="owned",
            )
            repository.upsert_candidates((Candidate(
                id=item.id,
                source="asset_store",
                external_id=item.external_id,
                title="Updated",
                url=item.url,
            ),))
            saved = repository.list_candidates(source="asset_store")[0]
            self.assertEqual(saved.ownership, "owned")
            self.assertEqual(repository.summary()["total"], 1)
            repository.close()

    def test_planner_builds_three_plans_and_safe_store_links(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = StackRepository(Path(directory) / "catalog.db")
            planner = GameStackPlanner(
                repository,
                github=FakeRemoteSource("github"),
                openupm=FakeRemoteSource("openupm"),
            )
            result = planner.recommend(
                prompt="協力型ローグライト",
                platform="pc",
                budget="mixed",
            )
            self.assertEqual(len(result["plans"]), 3)
            self.assertTrue(result["recommendations"]["networking"])
            self.assertTrue(result["policy"]["asset_store_automated_fetch"])
            self.assertTrue(all(
                item["url"].startswith("https://assetstore.unity.com/?")
                for item in result["asset_store_searches"]
            ))
            repository.close()

    def test_manual_asset_category_is_reused_in_offline_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = StackRepository(Path(directory) / "catalog.db")
            repository.save_manual_asset(
                url="https://assetstore.unity.com/packages/tools/networking/test-1",
                title="Owned Networking Toolkit",
                ownership="owned",
                categories=("networking",),
            )
            planner = GameStackPlanner(repository)
            result = planner.recommend(
                prompt="オンライン協力ゲーム",
                platform="pc",
                budget="owned_first",
                remote=False,
            )
            networking = result["recommendations"]["networking"]
            self.assertEqual(networking[0]["candidate"]["source"], "asset_store")
            self.assertIn("必要機能のカテゴリと一致", networking[0]["reasons"])
            repository.close()

    def test_budget_selects_the_recommended_plan_order(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = StackRepository(Path(directory) / "catalog.db")
            planner = GameStackPlanner(repository)
            expected = {
                "owned_first": "installed_first",
                "free": "open_source",
                "mixed": "low_risk",
            }
            for budget, plan_id in expected.items():
                with self.subTest(budget=budget):
                    result = planner.recommend(
                        prompt="セーブ対応ゲーム",
                        budget=budget,
                        remote=False,
                    )
                    self.assertEqual(result["recommended_plan_id"], plan_id)
                    self.assertEqual(result["plans"][0]["id"], plan_id)
            repository.close()

    def test_current_project_does_not_inherit_installed_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_project = make_unity_project(root / "first")
            second_project = make_unity_project(root / "second")
            (second_project / "Packages" / "manifest.json").write_text(
                json.dumps({"dependencies": {}}),
                encoding="utf-8",
            )
            (second_project / "Packages" / "packages-lock.json").write_text(
                json.dumps({"dependencies": {}}),
                encoding="utf-8",
            )
            repository = StackRepository(root / "catalog.db")
            planner = GameStackPlanner(repository)
            planner.scan_project(str(first_project))
            result = planner.recommend(
                prompt="ゲームパッド対応",
                project_path=str(second_project),
                remote=False,
            )
            input_candidates = result["recommendations"]["input"]
            inherited = next(
                item for item in input_candidates
                if item["candidate"]["external_id"] == "com.unity.inputsystem"
            )
            self.assertFalse(inherited["installed_in_project"])
            self.assertFalse(inherited["candidate"]["installed"])
            self.assertEqual(inherited["candidate"]["ownership"], "unknown")
            self.assertNotIn("現在のプロジェクトに導入済み", inherited["reasons"])
            repository.close()

    def test_api_rejects_non_store_manual_url(self):
        with tempfile.TemporaryDirectory() as directory:
            app = GameStackApplication(Path(directory) / "catalog.db")
            rejected_urls = (
                "https://evil.example/products/1",
                "https://assetstore.unity.com/",
                "https://assetstore.unity.com/?q=networking",
                "https://assetstore.unity.com/tools/networking",
                "https://assetstore.unity.com.evil.example/packages/tool-1",
                "http://assetstore.unity.com/packages/tool-1",
            )
            for url in rejected_urls:
                with self.subTest(url=url), self.assertRaises(ApiError):
                    app.save_manual({"url": url, "title": "No"})
            app.close()

    def test_asset_store_product_url_is_canonical_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            app = GameStackApplication(Path(directory) / "catalog.db")
            first = app.save_manual({
                "url": (
                    "https://assetstore.unity.com/packages/tools/network/"
                    "network-kit-12345?utm_source=test#details"
                ),
                "title": "Network Kit",
                "categories": ["networking"],
            })
            second = app.save_manual({
                "url": (
                    "https://marketplace.unity.com/packages/tools/network/"
                    "network-kit-12345/reviews?tracking=1"
                ),
                "title": "Network Kit",
                "ownership": "owned",
                "categories": ["lobby_matchmaking"],
            })
            self.assertEqual(first["item"]["id"], "asset_store:12345")
            self.assertEqual(second["item"]["id"], "asset_store:12345")
            self.assertEqual(
                second["item"]["url"],
                (
                    "https://assetstore.unity.com/packages/tools/network/"
                    "network-kit-12345"
                ),
            )
            saved = app.catalog(source="asset_store")["items"]
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0]["ownership"], "owned")
            self.assertEqual(
                saved[0]["categories"],
                ("networking", "lobby_matchmaking"),
            )
            app.close()

    def test_api_exposes_and_validates_manual_feature_categories(self):
        with tempfile.TemporaryDirectory() as directory:
            app = GameStackApplication(Path(directory) / "catalog.db")
            status = app.status()
            self.assertIn(
                {"key": "networking", "title": "マルチプレイ通信"},
                status["requirement_categories"],
            )
            saved = app.save_manual({
                "url": "https://assetstore.unity.com/packages/tools/networking/test-1",
                "title": "Pinned",
                "categories": ["networking", "networking"],
            })
            self.assertEqual(saved["item"]["categories"], ("networking",))
            with self.assertRaises(ApiError) as context:
                app.save_manual({
                    "url": "https://assetstore.unity.com/packages/tools/test-2",
                    "title": "Invalid category",
                    "categories": ["not_a_feature"],
                })
            self.assertEqual(context.exception.code, "invalid_request")
            app.close()


class WebServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        static = root / "static"
        static.mkdir()
        (static / "index.html").write_text("<h1>ok</h1>", encoding="utf-8")
        self.app = GameStackApplication(root / "catalog.db")
        self.server = create_server(self.app, port=0, static_root=static)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.app.close()
        self.temp.cleanup()

    def test_status_and_static(self):
        with urlopen(f"{self.base}/api/status") as response:
            payload = json.loads(response.read())
            self.assertTrue(payload["ready"])
            self.assertIn("Content-Security-Policy", response.headers)
        with urlopen(f"{self.base}/") as response:
            self.assertIn(b"<h1>ok</h1>", response.read())

    def test_post_origin_and_manual_pin(self):
        body = json.dumps({
            "url": "https://assetstore.unity.com/packages/tools/test-1",
            "title": "Pinned",
        }).encode()
        request = Request(
            f"{self.base}/api/catalog/manual",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Origin": self.base,
            },
        )
        with urlopen(request) as response:
            self.assertEqual(json.loads(response.read())["item"]["title"], "Pinned")

        forbidden = Request(
            f"{self.base}/api/catalog/manual",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Origin": "https://evil.example",
            },
        )
        with self.assertRaises(HTTPError) as context:
            urlopen(forbidden)
        self.assertEqual(context.exception.code, 403)


if __name__ == "__main__":
    unittest.main()
