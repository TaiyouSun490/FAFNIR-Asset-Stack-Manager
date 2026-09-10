from __future__ import annotations

from datetime import timedelta
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from game_stack_planner.bridge_setup import (
    BridgeSetupCoordinator, PACKAGE, PACKAGE_ROOT, RECEIPT, _fingerprint, _tree,
)
from game_stack_planner.install_service import InstallCoordinatorError
from game_stack_planner.repository import StackRepository
from game_stack_planner.asset_store_download import _now
from game_stack_planner.api import GameStackApplication, ApiError
from game_stack_planner.mcp_server import StackforgeMcpTools
from game_stack_planner.cli import build_parser


class BridgeSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "My Game 日本語"
        for directory in ("Assets", "Packages", "ProjectSettings"):
            (self.project / directory).mkdir(parents=True)
        (self.project / "ProjectSettings/ProjectVersion.txt").write_text(
            "m_EditorVersion: 6000.7.0a5\n", encoding="utf-8")
        (self.project / "Packages/manifest.json").write_text(
            '{"dependencies":{"com.example.existing":"1.2.3"}}\n', encoding="utf-8")
        self.bundle = self.root / "bundle"
        shutil.copytree(Path(__file__).resolve().parents[1] / "unity_package" / PACKAGE, self.bundle)
        self.bridge_root = self.root / "bridge"
        self.repo = StackRepository(self.root / "catalog.db")
        self.service = BridgeSetupCoordinator(self.repo, bundle=self.bundle, bridge_root=self.bridge_root)
        self.before = self.snapshot()

    def tearDown(self):
        self.repo.close()
        self.temp.cleanup()

    def snapshot(self):
        return {p.relative_to(self.project).as_posix(): p.read_bytes()
                for p in self.project.rglob("*") if p.is_file()}

    def prepare(self):
        return self.service.prepare(str(self.project))

    def apply(self, prepared=None):
        prepared = prepared or self.prepare()
        return self.service.execute(prepared["plan"]["id"], prepared["approval_nonce"])

    def heartbeat(self, **overrides):
        self.bridge_root.mkdir(exist_ok=True)
        value = {"schema": "fafnir.asset-store-download-bridge.v1",
                 "updatedAtUtc": _now().isoformat(), "projectPath": str(self.project),
                 "projectName": "My Game", "bridgeVersion": "0.5.1",
                 "bridgeContentHash": _fingerprint(_tree(self.bundle)),
                 "compilationState": "passed", "syncState": "succeeded", "syncCount": 0,
                 "accountState": "signed_in"}
        value.update(overrides)
        (self.bridge_root / "unity-download-bridge.json").write_text(json.dumps(value))

    def test_diagnosis_and_plan_do_not_change_project(self):
        self.assertEqual("not_installed", self.service.diagnose(str(self.project))["installation"])
        planned = self.prepare()["plan"]
        self.assertTrue(planned["requires_approval"])
        self.assertIn("com.example.existing", planned["manifest_diff"])
        self.assertEqual(self.before, self.snapshot())
        self.assertNotIn("before_manifest", planned)
        self.assertEqual("MIT", planned["license"])

    def test_install_and_rollback_restore_exact_bytes(self):
        applied = self.apply()
        self.assertEqual(_tree(self.bundle), _tree(self.project / PACKAGE_ROOT))
        self.assertFalse(applied["diagnosis"]["setup_verified"])
        self.assertEqual("applied_waiting_for_unity", applied["job"]["status"])
        job = self.service.rollback(applied["job"]["id"], applied["rollback_nonce"])
        self.assertEqual("rolled_back", job["job"]["status"])
        self.assertEqual(self.before, self.snapshot())
        self.assertFalse((self.project / PACKAGE_ROOT).exists())

    def test_noop_after_install(self):
        self.apply()
        state = self.snapshot()
        plan = self.prepare()
        self.assertFalse(plan["plan"]["requires_approval"])
        self.assertIsNone(plan["approval_nonce"])
        self.assertEqual(state, self.snapshot())

    def test_wrong_expired_and_reused_approval_cannot_write(self):
        plan = self.prepare()
        with self.assertRaises(InstallCoordinatorError):
            self.service.execute(plan["plan"]["id"], "wrong")
        with patch("game_stack_planner.bridge_setup._now", return_value=_now() + timedelta(hours=1)):
            with self.assertRaises(InstallCoordinatorError):
                self.apply(plan)
        self.assertEqual(self.before, self.snapshot())
        self.apply(plan)
        installed = self.snapshot()
        with self.assertRaises(InstallCoordinatorError):
            self.apply(plan)
        self.assertEqual(installed, self.snapshot())

    def test_manifest_changed_after_review_is_preserved(self):
        plan = self.prepare()
        manifest = self.project / "Packages/manifest.json"
        manifest.write_text('{"dependencies":{"user.change":"2.0.0"}}')
        expected = self.snapshot()
        with self.assertRaisesRegex(InstallCoordinatorError, "changed after review"):
            self.apply(plan)
        self.assertEqual(expected, self.snapshot())

    def test_changed_bundle_invalidates_approval(self):
        plan = self.prepare()
        (self.bundle / "README.md").write_text("new release")
        with self.assertRaises(InstallCoordinatorError):
            self.apply(plan)
        self.assertEqual(self.before, self.snapshot())

    def test_existing_matching_unmanaged_package_can_be_adopted(self):
        shutil.copytree(self.bundle, self.project / PACKAGE_ROOT)
        self.apply()
        self.assertTrue(self.service.diagnose(str(self.project))["managed_unmodified"])

    def test_managed_update_and_rollback(self):
        self.apply()
        before_update = self.snapshot()
        info = json.loads((self.bundle / "package.json").read_text())
        info["version"] = "0.5.2"
        (self.bundle / "package.json").write_text(json.dumps(info))
        result = self.apply()
        self.assertEqual("0.5.2", result["diagnosis"]["installed_version"])
        self.service.rollback(result["job"]["id"], result["rollback_nonce"])
        self.assertEqual(before_update, self.snapshot())

    def test_user_edits_are_not_overwritten_on_update(self):
        self.apply()
        (self.project / PACKAGE_ROOT / "README.md").write_text("my local edits")
        before = self.snapshot()
        with self.assertRaisesRegex(InstallCoordinatorError, "local edits"):
            self.prepare()
        self.assertEqual(before, self.snapshot())

    def test_external_dependency_is_not_silently_replaced(self):
        (self.project / "Packages/manifest.json").write_text(
            json.dumps({"dependencies": {PACKAGE: "file:/some/other/project"}}))
        with self.assertRaisesRegex(InstallCoordinatorError, "external bridge dependency"):
            self.prepare()

    def test_editor_lock_requires_closing_without_consuming_approval(self):
        plan = self.prepare()
        lock = self.project / "Temp/UnityLockfile"
        lock.parent.mkdir()
        lock.write_text("")
        with self.assertRaisesRegex(InstallCoordinatorError, "Close the target"):
            self.apply(plan)
        lock.unlink()
        self.apply(plan)

    def test_changed_file_blocks_rollback(self):
        result = self.apply()
        (self.project / "Packages/manifest.json").write_text('{"dependencies":{}}')
        before = self.snapshot()
        with self.assertRaises(InstallCoordinatorError):
            self.service.rollback(result["job"]["id"], result["rollback_nonce"])
        self.assertEqual(before, self.snapshot())

    def test_partial_write_failure_restores_only_our_changes(self):
        from game_stack_planner.bridge_setup import _write_atomic
        count = 0
        def fail_once(path, value):
            nonlocal count
            count += 1
            if count == 4:
                raise OSError("simulated disk error")
            return _write_atomic(path, value)
        with patch("game_stack_planner.bridge_setup._write_atomic", side_effect=fail_once):
            with self.assertRaises(OSError):
                self.apply()
        self.assertEqual(self.before, self.snapshot())

    def test_symlink_target_cannot_escape_project(self):
        outside = self.root / "outside"
        outside.mkdir()
        link = self.project / PACKAGE_ROOT
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("symlink creation unavailable")
        with self.assertRaises(InstallCoordinatorError):
            self.prepare()
        self.assertEqual([], list(outside.iterdir()))

    def test_live_evidence_is_required_not_just_files_or_signin(self):
        self.apply()
        self.heartbeat(compilationState="unknown")
        self.assertFalse(self.service.diagnose(str(self.project))["setup_verified"])
        self.heartbeat(syncState="not_run")
        self.assertFalse(self.service.diagnose(str(self.project))["setup_verified"])
        self.heartbeat(bridgeContentHash="old")
        self.assertFalse(self.service.diagnose(str(self.project))["setup_verified"])
        self.heartbeat()
        self.assertTrue(self.service.diagnose(str(self.project))["setup_verified"])

    def test_stale_foreign_and_future_heartbeats_cannot_verify_target(self):
        self.apply()
        for overrides, expected in (
                ({"updatedAtUtc": (_now() - timedelta(minutes=5)).isoformat()}, "offline"),
                ({"projectPath": str(self.root / "another")}, "other_project"),
                ({"updatedAtUtc": (_now() + timedelta(hours=1)).isoformat()}, "offline"),
                ({"projectPath": ""}, "unknown_project")):
            with self.subTest(overrides=overrides):
                self.heartbeat(**overrides)
                state = self.service.diagnose(str(self.project))
                self.assertEqual(expected, state["editor"])
                self.assertFalse(state["setup_verified"])

    def test_api_mcp_and_cli_use_same_boundary(self):
        app = GameStackApplication(repository=self.repo)
        app.unity_bridge = self.service
        tools = StackforgeMcpTools(app)
        self.assertEqual("not_installed", tools.diagnose_unity_bridge(str(self.project))["installation"])
        planned = tools.prepare_unity_bridge_install(str(self.project))
        applied = tools.apply_reviewed_unity_bridge_install(
            planned["plan"]["id"], planned["approval_nonce"])
        self.assertEqual(applied["job"]["id"],
                         tools.get_unity_bridge_install_status(applied["job"]["id"])["job"]["id"])
        tools.rollback_unity_bridge_install(applied["job"]["id"], applied["rollback_nonce"])
        self.assertEqual(self.before, self.snapshot())
        with self.assertRaises(ApiError):
            app.manage_unity_bridge("prepare", {"project_path": str(self.project), "shell": "no"})
        self.assertEqual("bridge-doctor", build_parser().parse_args(
            ["bridge-doctor", "--project", str(self.project)]).command)

    def test_unsupported_or_unknown_unity_is_rejected(self):
        version = self.project / "ProjectSettings/ProjectVersion.txt"
        for text in ("m_EditorVersion: 2021.3.0f1", "not a version"):
            version.write_text(text)
            before = self.snapshot()
            with self.assertRaises(InstallCoordinatorError):
                self.prepare()
            self.assertEqual(before, self.snapshot())

    def test_setup_lock_blocks_a_second_job(self):
        plan = self.prepare()
        lock = self.project / ".fafnir-bridge-setup.lock"
        lock.write_text("other job")
        before = self.snapshot()
        with self.assertRaisesRegex(InstallCoordinatorError, "project lock"):
            self.apply(plan)
        self.assertEqual(before, self.snapshot())

    def test_signed_out_is_not_verified(self):
        self.apply()
        self.heartbeat(accountState="signed_out")
        state = self.service.diagnose(str(self.project))
        self.assertFalse(state["setup_verified"])
        self.assertEqual("sign_in_unity", state["next_action"])

    def test_bridge_root_override_is_explicit_and_shared(self):
        from game_stack_planner.bridge_paths import default_bridge_root
        from game_stack_planner.unity_my_assets import default_export_path
        with patch.dict("os.environ", {"FAFNIR_BRIDGE_ROOT": str(self.bridge_root)}):
            self.assertEqual(self.bridge_root, default_bridge_root())
            self.assertEqual(self.bridge_root / "unity-my-assets.json", default_export_path())
        with patch.dict("os.environ", {"FAFNIR_BRIDGE_ROOT": "relative/path"}):
            with self.assertRaises(ValueError):
                default_bridge_root()


class BridgeProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_stdio_review_apply_diagnose_and_rollback(self):
        import os
        import sys
        from mcp.client import Client
        from mcp.client.stdio import StdioServerParameters

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "Game 日本語"
            for name in ("Assets", "Packages", "ProjectSettings"):
                (project / name).mkdir(parents=True)
            (project / "Packages/manifest.json").write_text('{"dependencies":{}}')
            (project / "ProjectSettings/ProjectVersion.txt").write_text("m_EditorVersion: 6000.7.0a5")
            env = {**os.environ, "FAFNIR_BRIDGE_ROOT": str(root / "isolated-bridge")}
            parameters = StdioServerParameters(command=sys.executable,
                args=["-m", "game_stack_planner", "--db", str(root / "catalog.db"), "mcp"], env=env)
            async with Client(parameters, read_timeout_seconds=30) as client:
                listed = {t.name: t for t in (await client.list_tools()).tools}
                self.assertTrue(listed["diagnose_unity_bridge"].annotations.read_only_hint)
                self.assertTrue(listed["apply_reviewed_unity_bridge_install"].annotations.destructive_hint)

                async def call(name, arguments):
                    result = await client.call_tool(name, arguments)
                    self.assertFalse(result.is_error, result.content)
                    return result.structured_content

                state = await call("diagnose_unity_bridge", {"project_path": str(project)})
                self.assertEqual("not_installed", state["installation"])
                prepared = await call("prepare_unity_bridge_install", {"project_path": str(project)})
                self.assertFalse((project / PACKAGE_ROOT).exists())
                applied = await call("apply_reviewed_unity_bridge_install",
                    {"plan_id": prepared["plan"]["id"], "approval_nonce": prepared["approval_nonce"]})
                self.assertFalse(applied["diagnosis"]["setup_verified"])
                self.assertTrue((project / PACKAGE_ROOT / "package.json").exists())
                await call("get_unity_bridge_install_status", {"job_id": applied["job"]["id"]})
                rolled = await call("rollback_unity_bridge_install",
                    {"job_id": applied["job"]["id"], "rollback_nonce": applied["rollback_nonce"]})
                self.assertEqual("rolled_back", rolled["job"]["status"])
                self.assertFalse((project / PACKAGE_ROOT).exists())
