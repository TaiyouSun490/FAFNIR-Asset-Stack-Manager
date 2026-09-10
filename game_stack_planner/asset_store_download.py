"""Approval-gated handoff to Unity's authenticated Asset Store downloader."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from .local_asset_validation import (
    LocalAssetValidationError,
    resolve_cached_package_path,
)
from .repository import StackRepository
from .bridge_paths import default_bridge_root


_APPROVAL_TTL = timedelta(minutes=10)
_BRIDGE_HEARTBEAT_TTL = timedelta(seconds=20)
_MAX_PRODUCTS = 20
_MAX_FILE_BYTES = 1024 * 1024
_SAFE_JOB_ID = re.compile(r"^[0-9a-f]{32}$")


class AssetStoreDownloadError(RuntimeError):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        if not path.is_file() or path.stat().st_size > _MAX_FILE_BYTES:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def _approval_hash(plan_id: str, nonce: str, product_ids: Iterable[int]) -> str:
    binding = "\0".join((
        "asset-store-download",
        plan_id,
        ",".join(str(value) for value in product_ids),
        nonce,
    ))
    return hashlib.sha256(binding.encode("utf-8")).hexdigest()


class AssetStoreDownloadCoordinator:
    """Validate owned candidates and queue a bounded request for Unity Editor."""

    def __init__(
        self,
        repository: StackRepository,
        *,
        bridge_root: str | Path | None = None,
    ) -> None:
        self.repository = repository
        self.root = Path(bridge_root or default_bridge_root()).expanduser()
        self.plan_root = self.root / "asset-store-download-plans"
        self.command_path = self.root / "unity-download-command.json"
        self.status_path = self.root / "unity-download-status.json"
        self.heartbeat_path = self.root / "unity-download-bridge.json"

    def bridge_status(self) -> dict[str, Any]:
        heartbeat = _read_json(self.heartbeat_path)
        updated_at = _parse_time((heartbeat or {}).get("updatedAtUtc"))
        online = bool(
            heartbeat
            and heartbeat.get("schema") == "fafnir.asset-store-download-bridge.v1"
            and updated_at is not None
            and _now() - updated_at <= _BRIDGE_HEARTBEAT_TTL
        )
        return {
            "online": online,
            "unity_version": str((heartbeat or {}).get("unityVersion") or ""),
            "project_name": str((heartbeat or {}).get("projectName") or ""),
            "updated_at_utc": (
                updated_at.isoformat() if updated_at is not None else None
            ),
            "transport": "local_file_queue",
            "account_state": str((heartbeat or {}).get("accountState") or "unknown"),
            "readiness": (
                "offline" if not online else
                "sign_in_required" if (heartbeat or {}).get("accountState") == "signed_out" else
                "busy" if (heartbeat or {}).get("busy") is True else
                "connected"
            ),
            "download_scope": "global_cache",
            "target_project_required": False,
            "next_action": (
                "open_bridge_project" if not online else
                "sign_in_unity" if (heartbeat or {}).get("accountState") == "signed_out" else
                "wait" if (heartbeat or {}).get("busy") is True else
                "start_download"
            ),
        }

    def prepare(self, *, candidate_ids: list[str]) -> dict[str, Any]:
        if not isinstance(candidate_ids, list):
            raise AssetStoreDownloadError(
                400, "invalid_request", "candidate_ids must be a list."
            )
        normalized: list[str] = []
        for raw in candidate_ids:
            value = str(raw or "").strip()
            if not value or len(value) > 300:
                raise AssetStoreDownloadError(
                    400, "invalid_request", "Each candidate ID must be non-empty text."
                )
            if value not in normalized:
                normalized.append(value)
        if not normalized or len(normalized) > _MAX_PRODUCTS:
            raise AssetStoreDownloadError(
                400,
                "invalid_request",
                f"Select between 1 and {_MAX_PRODUCTS} candidates.",
            )

        self.repository.link_asset_store_cache_candidates()
        items: list[dict[str, Any]] = []
        product_ids: list[int] = []
        for candidate_id in normalized:
            candidate = self.repository.get_candidate(candidate_id)
            if candidate is None or candidate.source != "asset_store":
                raise AssetStoreDownloadError(
                    404,
                    "candidate_not_found",
                    f"{candidate_id} is not a saved Asset Store candidate.",
                )
            evidence = candidate.metadata.get("ownership_evidence")
            verified = isinstance(evidence, dict) and evidence.get("verified") is True
            if candidate.ownership not in {"owned", "installed"} or not verified:
                raise AssetStoreDownloadError(
                    422,
                    "verified_ownership_required",
                    f"{candidate_id} was not verified by the logged-in Unity Editor account.",
                )
            try:
                product_id = int(candidate.external_id)
            except (TypeError, ValueError) as exc:
                raise AssetStoreDownloadError(
                    422, "invalid_product_id", f"{candidate_id} has no valid product ID."
                ) from exc
            if product_id <= 0:
                raise AssetStoreDownloadError(
                    422, "invalid_product_id", f"{candidate_id} has no valid product ID."
                )

            details = candidate.metadata.get("asset_store_details")
            details = details if isinstance(details, dict) else {}
            cache_id = str(candidate.metadata.get("cache_candidate_id") or "")
            cache_candidate = (
                self.repository.get_candidate(cache_id) if cache_id else None
            )
            cached = False
            if cache_candidate is not None:
                try:
                    resolve_cached_package_path(cache_candidate)
                except LocalAssetValidationError:
                    cached = False
                else:
                    cached = True
            item = {
                "candidate_id": candidate.id,
                "product_id": product_id,
                "title": candidate.title,
                "version": str(candidate.version or details.get("latest_version") or ""),
                "download_size_bytes": int(details.get("download_size_bytes") or 0),
                "cached": cached,
            }
            items.append(item)
            if not cached:
                product_ids.append(product_id)

        plan_id = uuid4().hex
        nonce = secrets.token_urlsafe(32) if product_ids else None
        expires_at = _now() + _APPROVAL_TTL if nonce else None
        stored = {
            "schema": "fafnir.asset-store-download-plan.v1",
            "id": plan_id,
            "state": "awaiting_approval" if nonce else "already_downloaded",
            "createdAtUtc": _now().isoformat(),
            "expiresAtUtc": expires_at.isoformat() if expires_at else None,
            "items": items,
            "productIds": product_ids,
            "approvalHash": (
                _approval_hash(plan_id, nonce, product_ids) if nonce else None
            ),
        }
        _write_json(self.plan_root / f"{plan_id}.json", stored)
        response: dict[str, Any] = {
            "plan": self._public_plan(stored),
            "bridge": self.bridge_status(),
        }
        if nonce:
            response["approval_nonce"] = nonce
        return response

    def start(self, *, plan_id: str, approval_nonce: str) -> dict[str, Any]:
        if not _SAFE_JOB_ID.fullmatch(str(plan_id or "")) or not approval_nonce:
            raise AssetStoreDownloadError(
                400, "invalid_request", "plan_id and approval_nonce are required."
            )
        path = self.plan_root / f"{plan_id}.json"
        plan = _read_json(path)
        if plan is None or plan.get("schema") != "fafnir.asset-store-download-plan.v1":
            raise AssetStoreDownloadError(
                404, "download_plan_not_found", "Asset Store download plan was not found."
            )
        if plan.get("state") != "awaiting_approval":
            raise AssetStoreDownloadError(
                409,
                "download_plan_consumed",
                "Download approval is invalid, expired, or already consumed.",
            )
        expires_at = _parse_time(plan.get("expiresAtUtc"))
        product_ids = [int(value) for value in plan.get("productIds", [])]
        expected = _approval_hash(plan_id, approval_nonce, product_ids)
        if (
            expires_at is None
            or expires_at < _now()
            or not secrets.compare_digest(str(plan.get("approvalHash") or ""), expected)
        ):
            raise AssetStoreDownloadError(
                409,
                "download_plan_consumed",
                "Download approval is invalid, expired, or already consumed.",
            )
        bridge = self.bridge_status()
        if not bridge["online"]:
            raise AssetStoreDownloadError(
                409,
                "download_bridge_offline",
                "Open any Unity project that already contains the Fafnir bridge. "
                "Downloads use Unity's shared cache; the destination project is not required.",
            )
        if bridge["readiness"] == "sign_in_required":
            raise AssetStoreDownloadError(
                409, "download_sign_in_required",
                "Restore the account session in Unity Hub and the open Unity Editor. "
                "The destination project is not required for downloading.",
            )
        if bridge["readiness"] == "busy":
            raise AssetStoreDownloadError(
                409, "download_bridge_busy",
                "The connected Unity Editor is compiling or processing a download. Retry when idle.",
            )
        existing = _read_json(self.command_path)
        if existing and str(existing.get("jobId") or "") != plan_id:
            raise AssetStoreDownloadError(
                409,
                "download_bridge_busy",
                "Unity download bridge already has a queued request.",
            )

        command = {
            "schema": "fafnir.asset-store-download-command.v1",
            "jobId": plan_id,
            "createdAtUtc": _now().isoformat(),
            "expiresAtUtc": expires_at.isoformat(),
            "productIds": product_ids,
        }
        _write_json(self.command_path, command)
        _write_json(self.status_path, {
            "schema": "fafnir.asset-store-download-status.v1",
            "jobId": plan_id,
            "state": "queued",
            "updatedAtUtc": _now().isoformat(),
            "message": "Waiting for the Unity Editor download bridge.",
            "products": [],
        })
        plan["state"] = "queued"
        plan["approvalHash"] = None
        plan["consumedAtUtc"] = _now().isoformat()
        _write_json(path, plan)
        return {
            "job": self.get(plan_id)["job"],
            "bridge": self.bridge_status(),
        }

    def get(self, job_id: str) -> dict[str, Any]:
        if not _SAFE_JOB_ID.fullmatch(str(job_id or "")):
            raise AssetStoreDownloadError(
                400, "invalid_request", "job_id is required."
            )
        plan = _read_json(self.plan_root / f"{job_id}.json")
        if plan is None:
            raise AssetStoreDownloadError(
                404, "download_job_not_found", "Asset Store download job was not found."
            )
        status = _read_json(self.status_path)
        if status is None or str(status.get("jobId") or "") != job_id:
            status = {
                "jobId": job_id,
                "state": str(plan.get("state") or "unknown"),
                "message": "",
                "products": [],
                "updatedAtUtc": plan.get("createdAtUtc"),
            }
        return {
            "job": {
                "id": job_id,
                "state": str(status.get("state") or "unknown"),
                "message": str(status.get("message") or ""),
                "error_code": str(status.get("errorCode") or ""),
                "next_action": (
                    "sign_in_unity" if status.get("errorCode") == "unity_authentication_required"
                    else "inspect_error" if status.get("state") == "failed"
                    else "none" if status.get("state") == "completed"
                    else "wait"
                ),
                "products": status.get("products") if isinstance(status.get("products"), list) else [],
                "items": self._public_plan(plan)["items"],
                "updated_at_utc": status.get("updatedAtUtc"),
            },
            "bridge": self.bridge_status(),
        }

    @staticmethod
    def _public_plan(plan: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": str(plan.get("id") or ""),
            "state": str(plan.get("state") or "unknown"),
            "items": plan.get("items") if isinstance(plan.get("items"), list) else [],
            "product_ids_to_download": [
                int(value) for value in plan.get("productIds", [])
            ],
            "expires_at_utc": plan.get("expiresAtUtc"),
            "requires_approval": bool(plan.get("productIds")),
            "effect": (
                "Queue only verified-owned products for Unity Editor's authenticated "
                "Asset Store downloader. This downloads to Unity's cache and does not import."
            ),
        }


__all__ = [
    "AssetStoreDownloadCoordinator",
    "AssetStoreDownloadError",
    "default_bridge_root",
]
