from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from game_stack_planner.api import ApiError, GameStackApplication
from game_stack_planner.models import Candidate


OPENUPM_URL = "https://package.openupm.com"
PACKAGE_NAME = "com.example.inventory"
PACKAGE_VERSION = "1.2.3"


def _write_json(path: Path, value: dict[str, object]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def make_unity_project(
    root: Path,
    *,
    dependencies: dict[str, str] | None = None,
    lock_dependencies: dict[str, object] | None = None,
) -> Path:
    """Create only the bounded metadata needed by the installer."""
    packages = root / "Packages"
    settings = root / "ProjectSettings"
    packages.mkdir(parents=True)
    settings.mkdir()
    _write_json(
        packages / "manifest.json",
        {
            "dependencies": dependencies
            if dependencies is not None
            else {"com.unity.inputsystem": "1.11.2"},
            "scopedRegistries": [
                {
                    "name": "Studio registry",
                    "url": "https://packages.example.test",
                    "scopes": ["com.studio"],
                }
            ],
            "testMetadata": {"mustRemain": True},
        },
    )
    _write_json(
        packages / "packages-lock.json",
        {
            "dependencies": lock_dependencies
            if lock_dependencies is not None
            else {
                "com.unity.inputsystem": {
                    "version": "1.11.2",
                    "depth": 0,
                    "source": "registry",
                }
            }
        },
    )
    (settings / "ProjectVersion.txt").write_text(
        "m_EditorVersion: 6000.0.32f1\n",
        encoding="utf-8",
    )
    (settings / "ProjectSettings.asset").write_text(
        "PlayerSettings:\n  productName: Installer Contract Test\n",
        encoding="utf-8",
    )
    return root


def openupm_candidate(
    *,
    candidate_id: str = f"openupm:{PACKAGE_NAME}",
    package_name: str = PACKAGE_NAME,
    version: str | None = PACKAGE_VERSION,
    license_name: str | None = "MIT",
) -> Candidate:
    return Candidate(
        id=candidate_id,
        source="openupm",
        external_id=package_name,
        title="Inventory Toolkit",
        url=f"https://openupm.com/packages/{package_name}/",
        description="An installable Unity package.",
        categories=("inventory",),
        license=license_name,
        version=version,
    )


def github_candidate() -> Candidate:
    return Candidate(
        id="github:example/inventory",
        source="github",
        external_id="example/inventory",
        title="GitHub Inventory",
        url="https://github.com/example/inventory",
        description="Search result, not yet inspected as a Unity package.",
        license="MIT",
        metadata={"default_branch": "main"},
    )


def asset_store_candidate() -> Candidate:
    return Candidate(
        id="asset_store:12345",
        source="asset_store",
        external_id="12345",
        title="Asset Store Inventory",
        url=(
            "https://assetstore.unity.com/packages/tools/"
            "asset-store-inventory-12345"
        ),
        ownership="candidate",
    )


def cached_unitypackage_candidate() -> Candidate:
    return Candidate(
        id="asset_store_cache:" + "a" * 64,
        source="asset_store_cache",
        external_id="a" * 64,
        title="Cached Inventory",
        url="",
        ownership="unknown",
        metadata={
            "inventory_state": "locally_cached",
            "cache_relative_path": (
                "Example Publisher/Tools/Cached Inventory.unitypackage"
            ),
            "cache_root_kind": "default",
            "available": True,
            "ownership_evidence": {
                "kind": "asset_store_cache",
                "verified": False,
            },
        },
    )


def local_candidate() -> Candidate:
    return Candidate(
        id="local:com.unity.inputsystem",
        source="local",
        external_id="com.unity.inputsystem",
        title="com.unity.inputsystem",
        url="",
        ownership="installed",
        installed=True,
        version="1.11.2",
    )


class GameStackInstallationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = make_unity_project(self.root / "UnityProject")
        self.app = GameStackApplication(self.root / "catalog.db")

    def tearDown(self) -> None:
        self.app.close()
        self.temp.cleanup()

    @property
    def manifest_path(self) -> Path:
        return self.project / "Packages" / "manifest.json"

    @property
    def lock_path(self) -> Path:
        return self.project / "Packages" / "packages-lock.json"

    def seed(self, *candidates: Candidate) -> None:
        self.app.repository.upsert_candidates(candidates)

    def prepare(self, candidate_id: str) -> dict[str, object]:
        return self.app.prepare_install(
            {
                "candidate_id": candidate_id,
                "project_path": str(self.project),
            }
        )

    def test_prepare_openupm_exact_version_returns_reviewable_manifest_diff(self) -> None:
        candidate = openupm_candidate()
        self.seed(candidate)
        before = self.manifest_path.read_bytes()

        response = self.prepare(candidate.id)

        self.assertIn("plan", response)
        self.assertIn("approval_nonce", response)
        self.assertIsInstance(response["approval_nonce"], str)
        self.assertTrue(response["approval_nonce"])
        plan = response["plan"]
        self.assertIsInstance(plan, dict)
        required_keys = {
            "id",
            "status",
            "kind",
            "candidate",
            "project",
            "package",
            "manifest",
            "requires_approval",
            "risks",
            "verification",
            "rollback_limit",
        }
        self.assertTrue(required_keys.issubset(plan))
        self.assertEqual("ready", plan["status"])
        self.assertEqual("upm_registry_add", plan["kind"])
        self.assertTrue(plan["requires_approval"])
        self.assertEqual(candidate.id, plan["candidate"]["id"])
        self.assertEqual("openupm", plan["candidate"]["source"])
        self.assertEqual("MIT", plan["candidate"]["license"])
        self.assertEqual("6000.0.32f1", plan["project"]["unity_version"])
        self.assertEqual(PACKAGE_NAME, plan["package"]["name"])
        self.assertEqual(PACKAGE_VERSION, plan["package"]["version"])
        self.assertEqual(OPENUPM_URL, plan["package"]["registry"])
        self.assertEqual(PACKAGE_NAME, plan["package"]["scope"])
        before_hash = hashlib.sha256(before).hexdigest()
        self.assertEqual(before_hash, plan["manifest"]["before_sha256"])
        self.assertRegex(plan["manifest"]["after_sha256"], r"^[0-9a-f]{64}$")
        self.assertNotEqual(
            plan["manifest"]["before_sha256"],
            plan["manifest"]["after_sha256"],
        )
        diff = json.dumps(
            plan["manifest"]["diff"], ensure_ascii=False, sort_keys=True
        )
        self.assertIn(PACKAGE_NAME, diff)
        self.assertIn(PACKAGE_VERSION, diff)
        self.assertIn(OPENUPM_URL, diff)
        self.assertEqual(before, self.manifest_path.read_bytes())

    def test_execute_applies_only_manifest_and_preserves_existing_content(self) -> None:
        candidate = openupm_candidate()
        self.seed(candidate)
        lock_before = self.lock_path.read_bytes()
        prepared = self.prepare(candidate.id)

        response = self.app.execute_install(
            {
                "plan_id": prepared["plan"]["id"],
                "approval_nonce": prepared["approval_nonce"],
            }
        )

        self.assertIn("job", response)
        self.assertIn("rollback_nonce", response)
        self.assertTrue(response["rollback_nonce"])
        job = response["job"]
        self.assertEqual("applied_waiting_for_unity", job["status"])
        self.assertEqual(prepared["plan"]["id"], job["plan"]["id"])
        self.assertIn("result", job)
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(PACKAGE_VERSION, manifest["dependencies"][PACKAGE_NAME])
        self.assertEqual({"mustRemain": True}, manifest["testMetadata"])
        private_registry = next(
            registry
            for registry in manifest["scopedRegistries"]
            if registry["url"] == "https://packages.example.test"
        )
        self.assertEqual(["com.studio"], private_registry["scopes"])
        openupm_registry = next(
            registry
            for registry in manifest["scopedRegistries"]
            if registry["url"] == OPENUPM_URL
        )
        self.assertIn(PACKAGE_NAME, openupm_registry["scopes"])
        self.assertEqual(lock_before, self.lock_path.read_bytes())

        fetched = self.app.install_job(job["id"])
        self.assertEqual(job["id"], fetched["job"]["id"])
        self.assertEqual("applied_waiting_for_unity", fetched["job"]["status"])

    def test_execute_nonce_is_one_shot(self) -> None:
        candidate = openupm_candidate()
        self.seed(candidate)
        prepared = self.prepare(candidate.id)
        payload = {
            "plan_id": prepared["plan"]["id"],
            "approval_nonce": prepared["approval_nonce"],
        }
        first = self.app.execute_install(payload)
        manifest_after_first = self.manifest_path.read_bytes()

        with self.assertRaises(ApiError) as context:
            self.app.execute_install(payload)

        self.assertEqual(409, context.exception.status)
        self.assertIn(
            context.exception.code,
            {"install_plan_consumed", "approval_consumed", "install_conflict"},
        )
        self.assertEqual(manifest_after_first, self.manifest_path.read_bytes())
        self.assertEqual(
            first["job"]["id"],
            self.app.install_job(first["job"]["id"])["job"]["id"],
        )

    def test_execute_rejects_plan_when_manifest_changed_after_review(self) -> None:
        candidate = openupm_candidate()
        self.seed(candidate)
        prepared = self.prepare(candidate.id)
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        manifest["dependencies"]["com.example.user-change"] = "9.9.9"
        _write_json(self.manifest_path, manifest)
        changed = self.manifest_path.read_bytes()

        with self.assertRaises(ApiError) as context:
            self.app.execute_install(
                {
                    "plan_id": prepared["plan"]["id"],
                    "approval_nonce": prepared["approval_nonce"],
                }
            )

        self.assertEqual(409, context.exception.status)
        self.assertIn(
            context.exception.code,
            {"manifest_changed", "install_plan_stale", "install_conflict"},
        )
        self.assertEqual(changed, self.manifest_path.read_bytes())
        self.assertNotIn(
            PACKAGE_NAME,
            json.loads(changed.decode("utf-8"))["dependencies"],
        )

    def test_existing_exact_package_is_noop(self) -> None:
        self.project = make_unity_project(
            self.root / "AlreadyPresent",
            dependencies={
                "com.unity.inputsystem": "1.11.2",
                PACKAGE_NAME: PACKAGE_VERSION,
            },
        )
        candidate = openupm_candidate()
        self.seed(candidate)
        before = self.manifest_path.read_bytes()

        response = self.prepare(candidate.id)

        self.assertEqual("already_present", response["plan"]["status"])
        self.assertEqual("noop", response["plan"]["kind"])
        self.assertFalse(response["plan"]["requires_approval"])
        self.assertNotIn("approval_nonce", response)
        self.assertEqual(before, self.manifest_path.read_bytes())

    def test_existing_different_version_is_blocked(self) -> None:
        self.project = make_unity_project(
            self.root / "VersionConflict",
            dependencies={
                "com.unity.inputsystem": "1.11.2",
                PACKAGE_NAME: "1.0.0",
            },
        )
        candidate = openupm_candidate()
        self.seed(candidate)
        before = self.manifest_path.read_bytes()

        response = self.prepare(candidate.id)

        self.assertEqual("blocked", response["plan"]["status"])
        self.assertEqual("upm_registry_add", response["plan"]["kind"])
        self.assertFalse(response["plan"]["requires_approval"])
        self.assertNotIn("approval_nonce", response)
        self.assertTrue(response["plan"]["risks"])
        self.assertIn("1.0.0", json.dumps(response["plan"], ensure_ascii=False))
        self.assertEqual(before, self.manifest_path.read_bytes())

    def test_openupm_without_exact_version_is_blocked(self) -> None:
        candidates = (
            openupm_candidate(
                candidate_id="openupm:missing-version",
                package_name="com.example.missing",
                version=None,
            ),
            openupm_candidate(
                candidate_id="openupm:floating-version",
                package_name="com.example.floating",
                version="latest",
            ),
        )
        self.seed(*candidates)

        for candidate in candidates:
            with self.subTest(candidate=candidate.id):
                response = self.prepare(candidate.id)
                self.assertEqual("blocked", response["plan"]["status"])
                self.assertEqual("upm_registry_add", response["plan"]["kind"])
                self.assertFalse(response["plan"]["requires_approval"])
                self.assertNotIn("approval_nonce", response)
                self.assertTrue(response["plan"]["risks"])

    def test_github_result_requires_repository_inspection(self) -> None:
        candidate = github_candidate()
        self.seed(candidate)
        before = self.manifest_path.read_bytes()

        response = self.prepare(candidate.id)

        self.assertEqual("needs_inspection", response["plan"]["status"])
        self.assertEqual("inspect_repository", response["plan"]["kind"])
        self.assertFalse(response["plan"]["requires_approval"])
        self.assertNotIn("approval_nonce", response)
        self.assertEqual("github", response["plan"]["candidate"]["source"])
        self.assertTrue(response["plan"]["risks"])
        self.assertEqual(before, self.manifest_path.read_bytes())

    def test_asset_store_and_local_cache_are_manual_only(self) -> None:
        candidates = (asset_store_candidate(), cached_unitypackage_candidate())
        self.seed(*candidates)
        expected_kinds = {
            "asset_store": "manual_asset_store",
            "asset_store_cache": "manual_unitypackage",
        }
        before = self.manifest_path.read_bytes()

        for candidate in candidates:
            with self.subTest(source=candidate.source):
                response = self.prepare(candidate.id)
                self.assertEqual("manual", response["plan"]["status"])
                self.assertEqual(
                    expected_kinds[candidate.source], response["plan"]["kind"]
                )
                self.assertFalse(response["plan"]["requires_approval"])
                self.assertNotIn("approval_nonce", response)
                self.assertTrue(response["plan"]["risks"])
        self.assertEqual(before, self.manifest_path.read_bytes())

    def test_local_project_package_is_already_present(self) -> None:
        candidate = local_candidate()
        self.seed(candidate)

        response = self.prepare(candidate.id)

        self.assertEqual("already_present", response["plan"]["status"])
        self.assertEqual("noop", response["plan"]["kind"])
        self.assertFalse(response["plan"]["requires_approval"])
        self.assertNotIn("approval_nonce", response)

    def test_rollback_restores_exact_manifest_without_touching_lock(self) -> None:
        candidate = openupm_candidate()
        self.seed(candidate)
        manifest_before = self.manifest_path.read_bytes()
        lock_before = self.lock_path.read_bytes()
        prepared = self.prepare(candidate.id)
        executed = self.app.execute_install(
            {
                "plan_id": prepared["plan"]["id"],
                "approval_nonce": prepared["approval_nonce"],
            }
        )
        self.assertNotEqual(manifest_before, self.manifest_path.read_bytes())

        response = self.app.rollback_install(
            {
                "job_id": executed["job"]["id"],
                "rollback_nonce": executed["rollback_nonce"],
            }
        )

        self.assertEqual(executed["job"]["id"], response["job"]["id"])
        self.assertEqual("rolled_back", response["job"]["status"])
        self.assertEqual(manifest_before, self.manifest_path.read_bytes())
        self.assertEqual(lock_before, self.lock_path.read_bytes())

        with self.assertRaises(ApiError) as context:
            self.app.rollback_install(
                {
                    "job_id": executed["job"]["id"],
                    "rollback_nonce": executed["rollback_nonce"],
                }
            )
        self.assertEqual(409, context.exception.status)

    def test_rollback_refuses_to_overwrite_post_install_user_change(self) -> None:
        candidate = openupm_candidate()
        self.seed(candidate)
        prepared = self.prepare(candidate.id)
        executed = self.app.execute_install(
            {
                "plan_id": prepared["plan"]["id"],
                "approval_nonce": prepared["approval_nonce"],
            }
        )
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        manifest["dependencies"]["com.example.after-install"] = "2.0.0"
        _write_json(self.manifest_path, manifest)
        user_change = self.manifest_path.read_bytes()

        with self.assertRaises(ApiError) as context:
            self.app.rollback_install(
                {
                    "job_id": executed["job"]["id"],
                    "rollback_nonce": executed["rollback_nonce"],
                }
            )

        self.assertEqual(409, context.exception.status)
        self.assertIn(
            context.exception.code,
            {"manifest_changed", "rollback_conflict", "install_conflict"},
        )
        self.assertEqual(user_change, self.manifest_path.read_bytes())

    def test_install_endpoints_reject_unknown_input_fields(self) -> None:
        candidate = openupm_candidate()
        self.seed(candidate)
        with self.assertRaises(ApiError) as prepare_error:
            self.app.prepare_install(
                {
                    "candidate_id": candidate.id,
                    "project_path": str(self.project),
                    "command": "arbitrary input must not cross the boundary",
                }
            )
        self.assertEqual(400, prepare_error.exception.status)
        self.assertEqual("invalid_request", prepare_error.exception.code)

        prepared = self.prepare(candidate.id)
        with self.assertRaises(ApiError) as execute_error:
            self.app.execute_install(
                {
                    "plan_id": prepared["plan"]["id"],
                    "approval_nonce": prepared["approval_nonce"],
                    "force": True,
                }
            )
        self.assertEqual(400, execute_error.exception.status)
        self.assertEqual("invalid_request", execute_error.exception.code)

        executed = self.app.execute_install(
            {
                "plan_id": prepared["plan"]["id"],
                "approval_nonce": prepared["approval_nonce"],
            }
        )
        with self.assertRaises(ApiError) as rollback_error:
            self.app.rollback_install(
                {
                    "job_id": executed["job"]["id"],
                    "rollback_nonce": executed["rollback_nonce"],
                    "manifest_path": str(self.manifest_path),
                }
            )
        self.assertEqual(400, rollback_error.exception.status)
        self.assertEqual("invalid_request", rollback_error.exception.code)


if __name__ == "__main__":
    unittest.main()
