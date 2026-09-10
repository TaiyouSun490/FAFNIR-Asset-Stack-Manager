from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from game_stack_planner.asset_store_download import (
    AssetStoreDownloadCoordinator,
    AssetStoreDownloadError,
)
from game_stack_planner.models import Candidate
from game_stack_planner.repository import StackRepository


class AssetStoreDownloadCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = StackRepository(self.root / "catalog.db")
        self.coordinator = AssetStoreDownloadCoordinator(
            self.repository,
            bridge_root=self.root / "bridge",
        )
        self.repository.upsert_candidates((Candidate(
            id="asset_store:122249",
            source="asset_store",
            external_id="122249",
            title="Look Animator",
            url="https://assetstore.unity.com/packages/slug-122249",
            ownership="owned",
            version="2.0.2.4",
            metadata={
                "inventory_state": "confirmed_owned",
                "ownership_evidence": {
                    "kind": "unity_editor_my_assets",
                    "verified": True,
                },
                "asset_store_details": {
                    "download_size_bytes": 12_582_912,
                },
            },
        ),))

    def tearDown(self) -> None:
        self.repository.close()
        self.temporary.cleanup()

    def _write_online_heartbeat(self) -> None:
        self.coordinator.root.mkdir(parents=True, exist_ok=True)
        self.coordinator.heartbeat_path.write_text(json.dumps({
            "schema": "fafnir.asset-store-download-bridge.v1",
            "updatedAtUtc": datetime.now(timezone.utc).isoformat(),
            "unityVersion": "6000.3.15f1",
            "projectName": "Bridge Host",
        }), encoding="utf-8")

    def test_prepare_then_start_writes_only_bounded_product_command(self) -> None:
        prepared = self.coordinator.prepare(candidate_ids=["asset_store:122249"])
        plan = prepared["plan"]

        self.assertEqual([122249], plan["product_ids_to_download"])
        self.assertEqual("Look Animator", plan["items"][0]["title"])
        self.assertFalse(self.coordinator.command_path.exists())

        self._write_online_heartbeat()
        started = self.coordinator.start(
            plan_id=plan["id"],
            approval_nonce=prepared["approval_nonce"],
        )

        self.assertEqual("queued", started["job"]["state"])
        command = json.loads(self.coordinator.command_path.read_text(encoding="utf-8"))
        self.assertEqual({
            "createdAtUtc",
            "expiresAtUtc",
            "jobId",
            "productIds",
            "schema",
        }, set(command))
        self.assertEqual([122249], command["productIds"])
        self.assertNotIn("Look Animator", json.dumps(command))

        with self.assertRaises(AssetStoreDownloadError) as raised:
            self.coordinator.start(
                plan_id=plan["id"],
                approval_nonce=prepared["approval_nonce"],
            )
        self.assertEqual("download_plan_consumed", raised.exception.code)

    def test_start_fails_closed_while_unity_bridge_is_offline(self) -> None:
        prepared = self.coordinator.prepare(candidate_ids=["asset_store:122249"])

        with self.assertRaises(AssetStoreDownloadError) as raised:
            self.coordinator.start(
                plan_id=prepared["plan"]["id"],
                approval_nonce=prepared["approval_nonce"],
            )

        self.assertEqual("download_bridge_offline", raised.exception.code)
        self.assertFalse(self.coordinator.command_path.exists())

    def test_stale_cache_catalog_entry_is_downloaded_again(self) -> None:
        cache_id = "asset_store_cache:" + "a" * 64
        self.repository.upsert_candidates((Candidate(
            id=cache_id,
            source="asset_store_cache",
            external_id="a" * 64,
            title="Look Animator",
            url="",
            metadata={
                "asset_store_candidate_id": "asset_store:122249",
                "cache_relative_path": "Publisher/Tools/Look Animator.unitypackage",
                "cache_root_kind": "default",
            },
        ),))
        with patch.dict(os.environ, {"APPDATA": str(self.root)}):
            prepared = self.coordinator.prepare(candidate_ids=["asset_store:122249"])

        self.assertFalse(prepared["plan"]["items"][0]["cached"])
        self.assertEqual([122249], prepared["plan"]["product_ids_to_download"])

    def test_connected_legacy_bridge_needs_no_project_switch_or_login(self) -> None:
        self._write_online_heartbeat()
        status = self.coordinator.bridge_status()
        self.assertEqual("unknown", status["account_state"])
        self.assertEqual("connected", status["readiness"])
        self.assertEqual("start_download", status["next_action"])
        self.assertFalse(status["target_project_required"])

    def test_logout_and_busy_do_not_consume_approval_and_recovery_can_retry(self) -> None:
        for fields, code in (
            ({"accountState": "signed_out"}, "download_sign_in_required"),
            ({"accountState": "signed_in", "busy": True}, "download_bridge_busy"),
        ):
            with self.subTest(code=code):
                prepared = self.coordinator.prepare(candidate_ids=["asset_store:122249"])
                self._write_online_heartbeat()
                heartbeat = json.loads(self.coordinator.heartbeat_path.read_text())
                heartbeat.update(fields)
                self.coordinator.heartbeat_path.write_text(json.dumps(heartbeat))
                with self.assertRaises(AssetStoreDownloadError) as raised:
                    self.coordinator.start(plan_id=prepared["plan"]["id"],
                                           approval_nonce=prepared["approval_nonce"])
                self.assertEqual(code, raised.exception.code)
                self.assertFalse(self.coordinator.command_path.exists())
                self._write_online_heartbeat()
                result = self.coordinator.start(plan_id=prepared["plan"]["id"],
                                                approval_nonce=prepared["approval_nonce"])
                self.assertEqual("queued", result["job"]["state"])
                self.coordinator.command_path.unlink()

    def test_offline_bridge_does_not_request_login_based_on_stale_account(self) -> None:
        self._write_online_heartbeat()
        heartbeat = json.loads(self.coordinator.heartbeat_path.read_text())
        heartbeat.update(updatedAtUtc="2000-01-01T00:00:00Z", accountState="signed_out")
        self.coordinator.heartbeat_path.write_text(json.dumps(heartbeat))
        status = self.coordinator.bridge_status()
        self.assertEqual("offline", status["readiness"])
        self.assertEqual("open_bridge_project", status["next_action"])

    def test_authentication_failure_has_specific_recovery_but_generic_failure_does_not(self) -> None:
        prepared = self.coordinator.prepare(candidate_ids=["asset_store:122249"])
        plan_id = prepared["plan"]["id"]
        for error_code, action in (("unity_authentication_required", "sign_in_unity"),
                                   ("", "inspect_error")):
            with self.subTest(error_code=error_code):
                self.coordinator.status_path.write_text(json.dumps({
                    "jobId": plan_id, "state": "failed", "errorCode": error_code,
                }))
                result = self.coordinator.get(plan_id)
                self.assertEqual(error_code, result["job"]["error_code"])
                self.assertEqual(action, result["job"]["next_action"])

    def test_existing_cache_file_skips_download(self) -> None:
        cache_id = "asset_store_cache:" + "b" * 64
        relative = Path("Publisher/Tools/Look Animator.unitypackage")
        package = self.root / "Unity" / "Asset Store-5.x" / relative
        package.parent.mkdir(parents=True)
        package.write_bytes(b"cached")
        self.repository.upsert_candidates((Candidate(
            id=cache_id,
            source="asset_store_cache",
            external_id="b" * 64,
            title="Look Animator",
            url="",
            metadata={
                "asset_store_candidate_id": "asset_store:122249",
                "cache_relative_path": str(relative).replace("\\", "/"),
                "cache_root_kind": "default",
            },
        ),))
        with patch.dict(os.environ, {"APPDATA": str(self.root)}):
            prepared = self.coordinator.prepare(candidate_ids=["asset_store:122249"])

        self.assertTrue(prepared["plan"]["items"][0]["cached"])
        self.assertEqual([], prepared["plan"]["product_ids_to_download"])

    def test_unverified_ownership_and_unsafe_job_ids_are_rejected(self) -> None:
        self.repository.upsert_candidates((Candidate(
            id="asset_store:137246",
            source="asset_store",
            external_id="137246",
            title="Eyes Animator",
            url="https://assetstore.unity.com/packages/slug-137246",
            ownership="owned",
        ),))

        with self.assertRaises(AssetStoreDownloadError) as raised:
            self.coordinator.prepare(candidate_ids=["asset_store:137246"])
        self.assertEqual("verified_ownership_required", raised.exception.code)

        with self.assertRaises(AssetStoreDownloadError) as raised:
            self.coordinator.get("../unity-download-command")
        self.assertEqual("invalid_request", raised.exception.code)

    def test_completed_status_is_reported_with_product_progress(self) -> None:
        prepared = self.coordinator.prepare(candidate_ids=["asset_store:122249"])
        plan_id = prepared["plan"]["id"]
        self.coordinator.status_path.write_text(json.dumps({
            "schema": "fafnir.asset-store-download-status.v1",
            "jobId": plan_id,
            "state": "completed",
            "updatedAtUtc": datetime.now(timezone.utc).isoformat(),
            "message": "done",
            "products": [{
                "productId": 122249,
                "state": "completed",
                "progress": 1.0,
            }],
        }), encoding="utf-8")

        result = self.coordinator.get(plan_id)

        self.assertEqual("completed", result["job"]["state"])
        self.assertEqual(1.0, result["job"]["products"][0]["progress"])


if __name__ == "__main__":
    unittest.main()
