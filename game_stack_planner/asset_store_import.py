"""Open Unity's interactive import preview for one inspected cached package."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import threading
from datetime import timedelta
from uuid import uuid4

from .asset_store_download import (
    AssetStoreDownloadCoordinator, AssetStoreDownloadError,
    _now, _parse_time, _read_json, _write_json, _SAFE_JOB_ID,
)


class AssetStoreImportCoordinator:
    def __init__(self, downloader: AssetStoreDownloadCoordinator) -> None:
        self.downloader = downloader
        self.root = downloader.root
        self.command = self.root / "unity-import-command.json"
        self.jobs = self.root / "unity-import-jobs"
        self.lock = threading.Lock()

    def request(self, *, package: Path, project: Path, title: str) -> dict:
        with self.lock:
            bridge = self.downloader.bridge_status()
            if not bridge["online"]:
                raise AssetStoreDownloadError(409, "import_bridge_offline",
                    "インポート先のUnityプロジェクトを開いてください。ダウンロードは済んでいます。")
            heartbeat = _read_json(self.downloader.heartbeat_path) or {}
            if not heartbeat.get("supportsImportDialog"):
                raise AssetStoreDownloadError(409, "import_bridge_update_required",
                    "Unity側のFafnir更新・コンパイル完了を待ってから再試行してください。")
            connected = str(heartbeat.get("projectPath") or "")
            if not connected or os.path.normcase(str(Path(connected).resolve())) != os.path.normcase(str(project.resolve())):
                raise AssetStoreDownloadError(409, "import_project_not_connected",
                    "接続中のUnityとインポート先が異なります。選択したプロジェクトをUnityで開いてください。")
            if heartbeat.get("busy"):
                raise AssetStoreDownloadError(409, "import_bridge_busy", "Unityの処理完了を待って再試行してください。")
            pending = _read_json(self.command)
            expires = _parse_time((pending or {}).get("expiresAtUtc"))
            if pending and (expires is None or expires > _now()):
                raise AssetStoreDownloadError(409, "import_bridge_busy", "別のインポート要求を処理中です。")
            with package.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            job_id = uuid4().hex
            job = {
                "id": job_id, "state": "queued", "title": title,
                "projectPath": str(project.resolve()),
                "message": "Unityのインポート確認画面を開いています…",
                "updatedAtUtc": _now().isoformat(),
                "expiresAtUtc": (_now() + timedelta(minutes=2)).isoformat(),
            }
            _write_json(self.jobs / f"{job_id}.json", job)
            _write_json(self.command, {
                "schema": "fafnir.asset-store-import-command.v1", "jobId": job_id,
                "projectPath": job["projectPath"], "packagePath": str(package.resolve()),
                "sha256": digest, "expiresAtUtc": job["expiresAtUtc"],
            })
            return {"job": job}

    def get(self, job_id: str) -> dict:
        if not _SAFE_JOB_ID.fullmatch(job_id):
            raise AssetStoreDownloadError(400, "invalid_job_id", "Invalid import job ID.")
        job = _read_json(self.jobs / f"{job_id}.json")
        if not job:
            raise AssetStoreDownloadError(404, "import_job_not_found", "Import job not found.")
        expires = _parse_time(job.get("expiresAtUtc"))
        if job.get("state") == "queued" and expires and expires <= _now():
            job = {**job, "state": "expired", "message": "Unityへの要求が期限切れです。接続を確認して再試行してください。"}
        return {"job": job}
