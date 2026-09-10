from __future__ import annotations

import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from game_stack_planner.api import GameStackApplication, ApiError
from game_stack_planner.asset_store_download import AssetStoreDownloadCoordinator, AssetStoreDownloadError
from game_stack_planner.asset_store_import import AssetStoreImportCoordinator
from game_stack_planner.repository import StackRepository
from game_stack_planner.server import create_server


class AssetImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = StackRepository(self.root / "test.db")
        self.downloader = AssetStoreDownloadCoordinator(self.repo, bridge_root=self.root / "bridge")
        self.importer = AssetStoreImportCoordinator(self.downloader)
        self.project = self.root / "Project"
        self.project.mkdir()
        self.package = self.root / "Example.unitypackage"
        self.package.write_bytes(b"test-only cache")
        self.downloader.root.mkdir()
        self.heartbeat()

    def tearDown(self):
        self.repo.close()
        self.temp.cleanup()

    def heartbeat(self, **fields):
        value = {"schema": "fafnir.asset-store-download-bridge.v1",
                 "updatedAtUtc": datetime.now(timezone.utc).isoformat(),
                 "projectPath": str(self.project), "supportsImportDialog": True}
        value.update(fields)
        self.downloader.heartbeat_path.write_text(json.dumps(value))

    def request(self):
        return self.importer.request(package=self.package, project=self.project, title="Example")

    def test_request_binds_project_and_package_hash_without_importing(self):
        result = self.request()
        command = json.loads(self.importer.command.read_text())
        self.assertEqual(str(self.project.resolve()), command["projectPath"])
        self.assertEqual(hashlib.sha256(self.package.read_bytes()).hexdigest(), command["sha256"])
        self.assertEqual("queued", self.importer.get(result["job"]["id"])["job"]["state"])
        self.assertEqual([], list(self.project.iterdir()))
        with self.assertRaises(AssetStoreDownloadError) as caught:
            self.request()
        self.assertEqual("import_bridge_busy", caught.exception.code)

    def test_wrong_project_old_bridge_and_busy_cannot_queue(self):
        for fields, code in [({"projectPath": str(self.root / "Other")}, "import_project_not_connected"),
                             ({"supportsImportDialog": False}, "import_bridge_update_required"),
                             ({"busy": True}, "import_bridge_busy")]:
            with self.subTest(code=code):
                self.heartbeat(**fields)
                with self.assertRaises(AssetStoreDownloadError) as caught:
                    self.request()
                self.assertEqual(code, caught.exception.code)
                self.assertFalse(self.importer.command.exists())

    def test_cached_import_needs_no_asset_store_login(self):
        self.heartbeat(accountState="signed_out")
        self.assertEqual("queued", self.request()["job"]["state"])

    def test_expired_job_and_unity_cancel_remain_distinct(self):
        result = self.request()
        job = result["job"]
        job["expiresAtUtc"] = "2000-01-01T00:00:00Z"
        path = self.importer.jobs / (job["id"] + ".json")
        path.write_text(json.dumps(job))
        self.assertEqual("expired", self.importer.get(job["id"])["job"]["state"])
        job["state"] = "cancelled"
        path.write_text(json.dumps(job))
        self.assertEqual("cancelled", self.importer.get(job["id"])["job"]["state"])
        with self.assertRaises(AssetStoreDownloadError):
            self.importer.get("../outside")

    def test_import_api_requires_inspection_and_does_not_accept_arbitrary_package_path(self):
        app = GameStackApplication(repository=self.repo)
        app.asset_store_importer = self.importer
        with self.assertRaises(ApiError) as caught:
            app.request_asset_import({"package_path": str(self.package)})
        self.assertEqual("invalid_request", caught.exception.code)
        with patch.object(app, "validate_asset_candidate", return_value={
            "validation": {"overall_status": "incompatible"},
        }):
            with self.assertRaises(ApiError) as caught:
                app.request_asset_import({"candidate_id": "asset_store:1", "project_path": str(self.project)})
        self.assertEqual("asset_incompatible", caught.exception.code)
        self.assertFalse(self.importer.command.exists())

    def test_download_and_import_http_require_same_origin(self):
        app = GameStackApplication(repository=self.repo)
        server = create_server(app, port=0)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            for route, method in [("download/prepare", "prepare_asset_store_download"),
                                  ("download/start", "start_asset_store_download"),
                                  ("import", "request_asset_import")]:
                with patch.object(app, method, return_value={"accepted": True}) as called:
                    for origin, expected in [(None, 403), ("https://example.org", 403),
                                             (f"http://127.0.0.1:{server.server_port}", 200)]:
                        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
                        headers = {"Content-Type": "application/json"}
                        if origin: headers["Origin"] = origin
                        connection.request("POST", "/api/install/asset-store/" + route, "{}", headers)
                        response = connection.getresponse()
                        response.read()
                        self.assertEqual(expected, response.status)
                        connection.close()
                    self.assertEqual(1, called.call_count)
        finally:
            server.shutdown(); server.server_close(); worker.join()


if __name__ == "__main__":
    unittest.main()
