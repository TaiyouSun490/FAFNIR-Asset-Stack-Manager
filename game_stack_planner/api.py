"""Validation boundary shared by the CLI and local web server."""

from __future__ import annotations

from hashlib import sha256
import os
from pathlib import Path
import subprocess
import sys
import threading
from typing import Any
from urllib.parse import urlparse

from . import __version__
from .asset_store import normalize_asset_store_product_url
from .asset_store_cache import AssetStoreCacheError
from .asset_store_import import AssetStoreImportCoordinator
from .asset_store_download import (
    AssetStoreDownloadCoordinator,
    AssetStoreDownloadError,
)
from .asset_store_details import (
    AssetStoreDetailsError,
    DEFAULT_FETCH_DELAY_SECONDS,
    fetch_product_details,
    polite_delay,
    resolve_product_urls,
)
from .install_service import InstallCoordinator, InstallCoordinatorError
from .bridge_setup import BridgeSetupCoordinator
from .candidate_comparison import comparison_card
from .local_asset_validation import (
    LocalAssetValidationError,
    resolve_cached_package_path,
    validate_cached_asset,
)
from .repository import (
    RagIndexBusyError,
    RagIndexCancelledError,
    RagIndexNotReadyError,
    StackRepository,
)
from .requirements import requirement_categories
from .scopes import SEARCH_SCOPES, candidate_scope, candidate_view
from .service import GameStackPlanner
from .text_embeddings import TextEmbeddingError, TransformersTextEmbeddingBackend
from .unity_project import UnityProjectError
from .unity_my_assets import (
    UnityMyAssetsError,
    default_export_path,
    load_unity_my_assets,
)

_PLATFORMS = {"pc", "mobile", "webgl", "vr", "quest"}
_BUDGETS = {"free", "mixed", "owned_first"}
_SOURCES = {"local", "github", "openupm", "asset_store", "asset_store_cache"}
_SCOPES = set(SEARCH_SCOPES)
_OWNERSHIP = {"candidate", "owned", "installed", "unknown"}
_MY_ASSETS_WATCH_INTERVAL_SECONDS = 2.0

_REQUIREMENT_CATEGORIES = requirement_categories()
_REQUIREMENT_CATEGORY_KEYS = {
    item["key"] for item in _REQUIREMENT_CATEGORIES
}

_REQUIREMENT_CATEGORY_TITLES = {
    item["key"]: item["title"]
    for item in _REQUIREMENT_CATEGORIES
}

class ApiError(ValueError):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.details = details or {}


def _text(
    value: Any,
    *,
    name: str,
    required: bool = False,
    maximum: int,
) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ApiError(400, "invalid_request", f"{name} must be text.")
    result = value.strip()
    if required and not result:
        raise ApiError(400, "invalid_request", f"{name} is required.")
    if len(result) > maximum:
        raise ApiError(
            400,
            "invalid_request",
            f"{name} is too long.",
            {"maximum": maximum},
        )
    return result


def _only_fields(payload: dict[str, Any], allowed: set[str]) -> None:
    unknown = sorted(str(key) for key in payload if key not in allowed)
    if unknown:
        raise ApiError(
            400,
            "invalid_request",
            "Request contains unsupported fields.",
            {"unsupported": unknown, "allowed": sorted(allowed)},
        )


class GameStackApplication:
    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        repository: StackRepository | None = None,
        planner: GameStackPlanner | None = None,
    ) -> None:
        self._owns_default_repository = repository is None
        self.repository = repository or StackRepository(
            db_path,
            embedding_backend=TransformersTextEmbeddingBackend(),
        )
        self.planner = planner or GameStackPlanner(self.repository)
        self.installer = InstallCoordinator(self.repository)
        self.asset_store_downloader = AssetStoreDownloadCoordinator(self.repository)
        self.asset_store_importer = AssetStoreImportCoordinator(self.asset_store_downloader)
        self.unity_bridge = BridgeSetupCoordinator(self.repository)
        self._automatic_rag_indexing = False
        self._rag_thread_lock = threading.Lock()
        self._rag_cancel = threading.Event()
        self._rag_thread: threading.Thread | None = None
        self._rag_reschedule_requested = False
        self._detached_rag_indexing = False
        self._rag_process: subprocess.Popen[bytes] | None = None
        self._rag_launch_error = ""
        self._automatic_my_assets_sync = False
        self._my_assets_sync_lock = threading.Lock()
        self._my_assets_watch_stop = threading.Event()
        self._my_assets_watch_thread: threading.Thread | None = None
        self._my_assets_watch_path: Path | None = None
        self._my_assets_seen_signature: tuple[int, int] | None = None
        self._my_assets_sync_error = ""
        self._automatic_asset_store_details = False
        self._asset_store_detail_process: subprocess.Popen[bytes] | None = None
        self._asset_store_detail_launch_error = ""

    def close(self) -> None:
        self._my_assets_watch_stop.set()
        watcher = self._my_assets_watch_thread
        if watcher is not None and watcher.is_alive():
            watcher.join(timeout=2.0)
        self._rag_cancel.set()
        with self._rag_thread_lock:
            thread = self._rag_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        # A daemon worker may still be inside a provider download, which cannot
        # be interrupted safely. Keep its SQLite connection valid until process
        # shutdown instead of closing it underneath the worker.
        if thread is not None and thread.is_alive():
            self.repository.cancel_current_rag_index()
        else:
            self.repository.close()

    def enable_automatic_rag_indexing(self, *, detached: bool = False) -> bool:
        """Start resumable indexing on ordinary long-running UI/MCP startup."""
        self._automatic_rag_indexing = True
        self._detached_rag_indexing = bool(
            detached and self._owns_default_repository
        )
        return self._schedule_rag_index()

    def enable_automatic_maintenance(
        self,
        *,
        sync_my_assets: bool = True,
    ) -> bool:
        """Enable startup/file-change ownership sync and detached dense indexing."""
        self._automatic_rag_indexing = True
        self._detached_rag_indexing = self._owns_default_repository
        self._automatic_asset_store_details = self._owns_default_repository
        if sync_my_assets:
            self._automatic_my_assets_sync = True
            self._my_assets_watch_path = default_export_path()
            self._sync_default_my_assets_if_changed()
            self._start_my_assets_watcher()
        detail_started = self._schedule_asset_store_detail_sync()
        return detail_started or self._schedule_rag_index()

    @staticmethod
    def _asset_store_detail_ttl_days() -> int:
        raw = (
            os.getenv("FAFNIR_ASSET_STORE_DETAIL_TTL_DAYS")
            or os.getenv("STACKFORGE_ASSET_STORE_DETAIL_TTL_DAYS")
            or "30"
        )
        try:
            return max(1, min(int(raw), 3650))
        except ValueError:
            return 30

    def _schedule_asset_store_detail_sync(self) -> bool:
        if not self._automatic_asset_store_details:
            return False
        self.repository.enqueue_asset_store_detail_sync(
            stale_after_days=self._asset_store_detail_ttl_days(),
        )
        status = self.repository.asset_store_detail_status()
        if status["due"] == 0:
            return False
        process = self._asset_store_detail_process
        if process is not None and process.poll() is None:
            return False
        command = [
            sys.executable,
            "-m",
            "game_stack_planner",
            "--db",
            str(self.repository.path.resolve()),
            "--json",
            "asset-details-sync",
            "--limit",
            "0",
            "--reindex",
        ]
        log_path = self.repository.path.parent / "asset-store-details.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        environment = os.environ.copy()
        environment.setdefault("PYTHONUNBUFFERED", "1")
        creationflags = 0
        popen_options: dict[str, Any] = {}
        if os.name == "nt":
            creationflags = (
                subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NO_WINDOW
            )
        else:
            popen_options["start_new_session"] = True
        try:
            with log_path.open("ab", buffering=0) as log:
                self._asset_store_detail_process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    close_fds=True,
                    creationflags=creationflags,
                    env=environment,
                    **popen_options,
                )
        except OSError as exc:
            self._asset_store_detail_launch_error = (
                f"could not start Asset Store detail worker: {exc}"
            )[:2000]
            return False
        self._asset_store_detail_launch_error = ""
        return True

    def _schedule_rag_index(self) -> bool:
        if not self._automatic_rag_indexing:
            return False
        if self._detached_rag_indexing:
            return self._start_detached_rag_index_worker(
                force=False,
                batch_size=32,
            )
        return self._start_rag_index_worker(force=False, batch_size=32)

    def request_rag_index(
        self,
        *,
        force: bool = False,
        batch_size: int = 32,
    ) -> dict[str, Any]:
        """Queue an explicit non-blocking index refresh and return its state."""
        try:
            bounded_batch = max(1, min(int(batch_size), 128))
        except (TypeError, ValueError) as exc:
            raise ApiError(400, "invalid_request", "batch_size must be an integer") from exc
        if self._detached_rag_indexing:
            started = self._start_detached_rag_index_worker(
                force=bool(force),
                batch_size=bounded_batch,
            )
        else:
            started = self._start_rag_index_worker(
                force=bool(force),
                batch_size=bounded_batch,
            )
        return {
            "accepted": started,
            "rag_index": self.repository.rag_index_status(),
        }

    def _start_detached_rag_index_worker(
        self,
        *,
        force: bool,
        batch_size: int,
    ) -> bool:
        status = self.repository.rag_index_status()
        if (status["ready"] and not force) or status["documents"] == 0:
            return False
        if status["missing_dependencies"]:
            return False
        if status["state"] in {"preparing_model", "building"}:
            return False
        with self._rag_thread_lock:
            if self._rag_process is not None and self._rag_process.poll() is None:
                return False
            command = [
                sys.executable,
                "-m",
                "game_stack_planner",
                "--db",
                str(self.repository.path.resolve()),
                "--json",
                "rag-index",
                "--batch-size",
                str(max(1, min(int(batch_size), 128))),
            ]
            if force:
                command.append("--force")
            log_path = self.repository.path.parent / "rag-index.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            environment = os.environ.copy()
            environment.setdefault("PYTHONUNBUFFERED", "1")
            environment.setdefault("TOKENIZERS_PARALLELISM", "false")
            creationflags = 0
            popen_options: dict[str, Any] = {}
            if os.name == "nt":
                creationflags = (
                    subprocess.CREATE_NEW_PROCESS_GROUP
                    | subprocess.DETACHED_PROCESS
                    | subprocess.CREATE_NO_WINDOW
                )
            else:
                popen_options["start_new_session"] = True
            try:
                with log_path.open("ab", buffering=0) as log:
                    self._rag_process = subprocess.Popen(
                        command,
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        close_fds=True,
                        creationflags=creationflags,
                        env=environment,
                        **popen_options,
                    )
            except OSError as exc:
                self._rag_launch_error = (
                    f"could not start detached RAG index worker: {exc}"
                )[:2000]
                return False
            self._rag_launch_error = ""
            return True

    def _start_rag_index_worker(self, *, force: bool, batch_size: int) -> bool:
        status = self.repository.rag_index_status()
        if (status["ready"] and not force) or status["documents"] == 0:
            return False
        if status["missing_dependencies"]:
            return False
        with self._rag_thread_lock:
            if self._rag_thread is not None and self._rag_thread.is_alive():
                if self._automatic_rag_indexing and not force:
                    self._rag_reschedule_requested = True
                return False
            self._rag_cancel.clear()
            self._rag_thread = threading.Thread(
                target=self._run_background_rag_index,
                kwargs={"force": force, "batch_size": batch_size},
                name="stackforge-rag-index",
                daemon=True,
            )
            self._rag_thread.start()
        return True

    def _run_background_rag_index(self, *, force: bool, batch_size: int) -> None:
        try:
            self.repository.reindex_asset_rag_embeddings(
                force=force,
                batch_size=batch_size,
                should_cancel=self._rag_cancel.is_set,
            )
        except (RagIndexCancelledError, RagIndexBusyError):
            return
        except Exception:
            # The repository persists a bounded error for status/MCP reporting.
            return
        finally:
            with self._rag_thread_lock:
                retry = self._rag_reschedule_requested
                self._rag_reschedule_requested = False
                if self._rag_thread is threading.current_thread():
                    self._rag_thread = None
            if retry and not self._rag_cancel.is_set():
                self._schedule_rag_index()

    def status(self) -> dict[str, Any]:
        rag_index = self.repository.rag_index_status()
        with self._rag_thread_lock:
            local_worker_active = bool(
                self._rag_thread is not None and self._rag_thread.is_alive()
            )
            detached_worker_active = bool(
                self._rag_process is not None
                and self._rag_process.poll() is None
            )
        worker_active = bool(
            local_worker_active
            or detached_worker_active
            or rag_index["state"] in {"preparing_model", "building"}
        )
        externally_managed = bool(
            rag_index["state"] in {"preparing_model", "building"}
            and not local_worker_active
            and not detached_worker_active
        )
        ownership_sync = self.repository.source_sync_state(
            "unity_editor_my_assets"
        )
        detail_status = self.repository.asset_store_detail_status()
        detail_process = self._asset_store_detail_process
        detail_worker_active = bool(
            detail_process is not None and detail_process.poll() is None
        )
        return {
            "ready": True,
            "version": __version__,
            "catalog": self.repository.summary(),
            "capabilities": {
                "unity_project_scan": True,
                "unity_bridge_project_setup": True,
                "github_search": True,
                "openupm_search": True,
                "asset_store_official_links": True,
                "asset_store_manual_pins": True,
                "asset_store_local_cache_scan": True,
                "asset_store_cache_content_inspection": True,
                "asset_store_project_validation": True,
                "unity_staging_compile_validation": True,
                "asset_store_owned_rag": True,
                "asset_store_dense_rag": rag_index["ready"],
                "asset_store_dense_rag_configured": (
                    self.repository.embedding_backend is not None
                ),
                "asset_store_rag_user_authored_only": False,
                "unity_editor_my_assets_sync": True,
                "unity_editor_my_assets_auto_import": True,
                "mcp_server": True,
                "asset_store_content_rag": True,
                "asset_store_automated_fetch": True,
                "asset_store_structured_compatibility": True,
                "asset_store_price_and_rating": True,
                "install_planning": True,
                "openupm_exact_version_install": True,
                "github_install_requires_inspection": True,
                "asset_store_purchase_automated": True,
                "asset_store_download_automated": True,
                "unitypackage_preview_import": True,
            },
            "limits": {
                "prompt_characters": 8000,
                "request_bytes": 1024 * 1024,
                "remote_requirements": 14,
            },
            "rag_index": {
                **rag_index,
                "automatic": self._automatic_rag_indexing or externally_managed,
                "worker_active": worker_active,
                "execution_mode": (
                    "external"
                    if externally_managed
                    else (
                        "detached"
                        if self._detached_rag_indexing
                        else "in_process"
                    )
                ),
                "launch_error": self._rag_launch_error,
            },
            "ownership_sync": {
                "automatic": self._automatic_my_assets_sync,
                "path": str(
                    self._my_assets_watch_path or default_export_path()
                ),
                "error": self._my_assets_sync_error,
                "last_sync": ownership_sync,
            },
            "asset_store_details": {
                **detail_status,
                "automatic": self._automatic_asset_store_details,
                "worker_active": detail_worker_active,
                "ttl_days": self._asset_store_detail_ttl_days(),
                "launch_error": self._asset_store_detail_launch_error,
            },
            "asset_store_download_bridge": (
                self.asset_store_downloader.bridge_status()
            ),
            "requirement_categories": list(_REQUIREMENT_CATEGORIES),
            "search_scopes": list(SEARCH_SCOPES),
        }

    def scan(self, payload: dict[str, Any]) -> dict[str, Any]:
        path = _text(
            payload.get("path"), name="path", required=True, maximum=2048
        )
        try:
            snapshot = self.planner.scan_project(path)
        except UnityProjectError as exc:
            raise ApiError(422, "invalid_unity_project", str(exc)) from exc
        return {"project": snapshot.to_dict(), "catalog": self.repository.summary()}

    def scan_cache(self, payload: dict[str, Any]) -> dict[str, Any]:
        _only_fields(payload, {"path", "inspect"})
        path = _text(payload.get("path"), name="path", maximum=2048)
        inspect_packages = payload.get("inspect", False)
        if not isinstance(inspect_packages, bool):
            raise ApiError(400, "invalid_request", "inspect must be boolean.")
        try:
            result = self.planner.scan_asset_store_cache(
                path or None,
                inspect_packages=inspect_packages,
            )
        except AssetStoreCacheError as exc:
            raise ApiError(422, "invalid_asset_store_cache", str(exc)) from exc
        return {
            "scan": result.summary(),
            "catalog": self.repository.summary(),
        }

    def sync_unity_my_assets(self, payload: dict[str, Any]) -> dict[str, Any]:
        _only_fields(payload, {"path"})
        path = _text(payload.get("path"), name="path", maximum=2048)
        target = Path(path).expanduser() if path else default_export_path()
        with self._my_assets_sync_lock:
            try:
                export = load_unity_my_assets(target)
                revision = sha256(target.read_bytes()).hexdigest()
            except UnityMyAssetsError as exc:
                raise ApiError(
                    422,
                    "invalid_unity_my_assets_export",
                    str(exc),
                ) from exc
            except OSError as exc:
                raise ApiError(
                    422,
                    "invalid_unity_my_assets_export",
                    "Unity My Assets export cannot be read.",
                ) from exc
            previous = self.repository.source_sync_state(
                "unity_editor_my_assets"
            )
            changed = previous is None or previous["revision"] != revision
            imported = 0
            if changed:
                imported = self.repository.import_unity_my_assets(
                    export,
                    export_path=str(target),
                )
                self.repository.record_source_sync(
                    source="unity_editor_my_assets",
                    revision=revision,
                    item_count=len(export.assets),
                    source_generated_at=export.generated_at_utc,
                )
            try:
                stat = target.stat()
                self._my_assets_seen_signature = (
                    int(stat.st_mtime_ns),
                    int(stat.st_size),
                )
            except OSError:
                self._my_assets_seen_signature = None
            self._my_assets_sync_error = ""
        detail_started = self._schedule_asset_store_detail_sync()
        if not detail_started:
            self._schedule_rag_index()
        return {
            "sync": {
                "imported": imported,
                "total": len(export.assets),
                "changed": changed,
                "generated_at_utc": export.generated_at_utc,
                "unity_version": export.unity_version,
                "source": "unity_editor_my_assets",
                "ownership_confirmed": True,
            },
            "catalog": self.repository.summary(),
        }

    @staticmethod
    def _is_resolved_asset_store_product_url(url: str, product_id: str) -> bool:
        parsed = urlparse(str(url or ""))
        return bool(
            parsed.scheme == "https"
            and (parsed.hostname or "").casefold() == "assetstore.unity.com"
            and parsed.path.startswith("/packages/")
            and parsed.path.rstrip("/").endswith("-" + str(product_id))
        )

    def sync_asset_store_details(
        self,
        *,
        force: bool = False,
        limit: int = 50,
        delay_seconds: float = DEFAULT_FETCH_DELAY_SECONDS,
    ) -> dict[str, Any]:
        """Synchronously process a resumable batch of public product details."""
        try:
            bounded_limit = max(0, min(int(limit), 20_000))
            bounded_delay = max(0.0, min(float(delay_seconds), 60.0))
        except (TypeError, ValueError) as exc:
            raise ApiError(400, "invalid_request", "Invalid detail sync limits.") from exc
        queued = self.repository.enqueue_asset_store_detail_sync(
            stale_after_days=self._asset_store_detail_ttl_days(),
            force=bool(force),
        )
        due = self.repository.due_asset_store_detail_candidates(limit=20_000)
        unresolved = [
            item for item in due
            if not self._is_resolved_asset_store_product_url(
                item.url, item.external_id
            )
        ]
        resolved_count = 0
        resolution_error = ""
        if unresolved:
            try:
                resolved = resolve_product_urls(
                    item.external_id for item in unresolved
                )
            except AssetStoreDetailsError as exc:
                resolved = {}
                resolution_error = str(exc)[:2000]
            for candidate in unresolved:
                source_url = resolved.get(candidate.external_id)
                if source_url:
                    self.repository.set_asset_store_product_url(
                        candidate.id, source_url
                    )
                    resolved_count += 1
                elif not resolution_error:
                    self.repository.fail_asset_store_detail_job(
                        candidate.id,
                        "Product URL was not found in Unity's public sitemap.",
                        retry_after_seconds=(
                            self._asset_store_detail_ttl_days() * 86_400
                        ),
                    )

        attempted = 0
        completed = 0
        errors = 0
        maximum = 20_000 if bounded_limit == 0 else bounded_limit
        while attempted < maximum:
            candidate = self.repository.claim_asset_store_detail_job()
            if candidate is None:
                break
            if not self._is_resolved_asset_store_product_url(
                candidate.url, candidate.external_id
            ):
                self.repository.fail_asset_store_detail_job(
                    candidate.id,
                    resolution_error
                    or "Product URL has not been resolved from Unity's public sitemap.",
                    retry_after_seconds=(
                        300
                        if resolution_error
                        else self._asset_store_detail_ttl_days() * 86_400
                    ),
                )
                errors += 1
                attempted += 1
                continue
            try:
                details, document = fetch_product_details(
                    candidate.external_id,
                    candidate.url,
                )
                self.repository.complete_asset_store_detail_job(
                    candidate.id,
                    details,
                    etag=document.etag,
                    last_modified=document.last_modified,
                )
                completed += 1
            except (AssetStoreDetailsError, OSError, RuntimeError, ValueError) as exc:
                self.repository.fail_asset_store_detail_job(
                    candidate.id, str(exc)
                )
                errors += 1
            attempted += 1
            if attempted < maximum and bounded_delay:
                polite_delay(bounded_delay)
        linked_cache_packages = self.repository.link_asset_store_cache_candidates()
        status = self.repository.asset_store_detail_status()
        return {
            "queued": queued,
            "resolved_urls": resolved_count,
            "attempted": attempted,
            "completed": completed,
            "errors": errors,
            "linked_cache_packages": linked_cache_packages,
            "resolution_error": resolution_error,
            "status": status,
            "catalog": self.repository.summary(),
        }

    def request_asset_store_detail_sync(
        self,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        queued = self.repository.enqueue_asset_store_detail_sync(
            stale_after_days=self._asset_store_detail_ttl_days(),
            force=bool(force),
        )
        started = self._schedule_asset_store_detail_sync()
        return {
            "accepted": bool(started),
            "queued": queued,
            "asset_store_details": self.repository.asset_store_detail_status(),
        }

    def validate_asset_candidate(self, payload: dict[str, Any]) -> dict[str, Any]:
        _only_fields(
            payload,
            {"candidate_id", "project_path", "platform", "cache_path", "compile"},
        )
        candidate_id = _text(
            payload.get("candidate_id"),
            name="candidate_id",
            required=True,
            maximum=300,
        )
        project_path = _text(
            payload.get("project_path"),
            name="project_path",
            required=True,
            maximum=2048,
        )
        platform = _text(
            payload.get("platform", "pc"),
            name="platform",
            maximum=30,
        ).casefold()
        cache_path = _text(
            payload.get("cache_path"), name="cache_path", maximum=2048
        )
        compile_test = payload.get("compile", False)
        if platform not in _PLATFORMS or not isinstance(compile_test, bool):
            raise ApiError(400, "invalid_request", "Invalid validation options.")
        candidate = self.repository.get_candidate(candidate_id)
        if candidate is None or candidate.source != "asset_store":
            raise ApiError(
                404,
                "candidate_not_found",
                "Select a saved Asset Store product candidate.",
            )
        self.repository.link_asset_store_cache_candidates()
        candidate = self.repository.get_candidate(candidate_id)
        cache_candidate_id = str(
            candidate.metadata.get("cache_candidate_id") or ""
        )
        cache_candidate = self.repository.get_candidate(cache_candidate_id)
        if cache_candidate is None or cache_candidate.source != "asset_store_cache":
            raise ApiError(
                422,
                "cached_package_not_linked",
                "No uniquely matching downloaded .unitypackage is linked. Rescan the cache first.",
            )
        try:
            project = self.planner.scan_project(project_path)
            validation = validate_cached_asset(
                candidate,
                cache_candidate,
                project,
                platform=platform,
                explicit_cache_root=cache_path or None,
                compile_test=compile_test,
            )
        except (LocalAssetValidationError, UnityProjectError) as exc:
            raise ApiError(422, "asset_validation_failed", str(exc)) from exc
        updated = self.repository.record_candidate_local_validation(
            candidate.id, validation
        )
        return {
            "candidate": candidate_view(updated),
            "validation": validation,
            "catalog": self.repository.summary(),
        }

    def _sync_default_my_assets_if_changed(self) -> None:
        target = self._my_assets_watch_path or default_export_path()
        if not target.is_file():
            return
        try:
            self.sync_unity_my_assets({"path": str(target)})
        except ApiError as exc:
            self._my_assets_sync_error = f"{exc.code}: {exc}"[:2000]

    def _start_my_assets_watcher(self) -> None:
        if (
            self._my_assets_watch_thread is not None
            and self._my_assets_watch_thread.is_alive()
        ):
            return
        self._my_assets_watch_stop.clear()
        self._my_assets_watch_thread = threading.Thread(
            target=self._watch_default_my_assets,
            name="stackforge-my-assets-watch",
            daemon=True,
        )
        self._my_assets_watch_thread.start()

    def _watch_default_my_assets(self) -> None:
        target = self._my_assets_watch_path or default_export_path()
        while not self._my_assets_watch_stop.wait(
            _MY_ASSETS_WATCH_INTERVAL_SECONDS
        ):
            try:
                stat = target.stat()
                signature = (int(stat.st_mtime_ns), int(stat.st_size))
            except OSError:
                continue
            if signature == self._my_assets_seen_signature:
                continue
            self._sync_default_my_assets_if_changed()

    def recommend(self, payload: dict[str, Any]) -> dict[str, Any]:
        prompt = _text(
            payload.get("prompt"), name="prompt", required=True, maximum=8000
        )
        project_path = _text(
            payload.get("project_path"),
            name="project_path",
            maximum=2048,
        )
        platform = _text(
            payload.get("platform", "pc"), name="platform", maximum=20
        ).casefold()
        budget = _text(
            payload.get("budget", "mixed"), name="budget", maximum=30
        ).casefold()
        if platform not in _PLATFORMS:
            raise ApiError(400, "invalid_request", "Unsupported target platform.")
        if budget not in _BUDGETS:
            raise ApiError(400, "invalid_request", "Unsupported budget mode.")
        remote_value = payload.get("remote", True)
        if not isinstance(remote_value, bool):
            raise ApiError(400, "invalid_request", "remote must be a boolean.")
        try:
            return self.planner.recommend(
                prompt=prompt,
                project_path=project_path or None,
                platform=platform,
                budget=budget,
                remote=remote_value,
            )
        except UnityProjectError as exc:
            raise ApiError(422, "invalid_unity_project", str(exc)) from exc
        except ValueError as exc:
            raise ApiError(400, "invalid_request", str(exc)) from exc

    def manage_unity_bridge(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        fields = {
            "diagnose": ("project_path",), "prepare": ("project_path",),
            "apply": ("plan_id", "approval_nonce"), "get": ("job_id",),
            "rollback": ("job_id", "rollback_nonce"),
        }
        if action not in fields:
            raise ApiError(400, "invalid_request", "Unknown bridge setup action.")
        _only_fields(payload, set(fields[action]))
        values = {key: _text(payload.get(key), name=key, required=True,
                            maximum=2048 if key == "project_path" else 256)
                  for key in fields[action]}
        method = getattr(self.unity_bridge, "execute" if action == "apply" else action)
        try:
            return method(**values)
        except InstallCoordinatorError as exc:
            raise ApiError(exc.status, exc.code, str(exc)) from exc
        except (OSError, ValueError) as exc:
            raise ApiError(422, "bridge_setup_error", str(exc)) from exc

    def prepare_install(self, payload: dict[str, Any]) -> dict[str, Any]:
        _only_fields(payload, {"candidate_id", "project_path"})
        candidate_id = _text(
            payload.get("candidate_id"),
            name="candidate_id",
            required=True,
            maximum=500,
        )
        project_path = _text(
            payload.get("project_path"),
            name="project_path",
            required=True,
            maximum=2048,
        )
        try:
            return self.installer.prepare(
                candidate_id=candidate_id,
                project_path=project_path,
            )
        except InstallCoordinatorError as exc:
            raise ApiError(exc.status, exc.code, str(exc)) from exc

    def prepare_asset_store_download(
        self, payload: dict[str, Any]
    ) -> dict[str, Any]:
        _only_fields(payload, {"candidate_ids"})
        candidate_ids = payload.get("candidate_ids")
        if not isinstance(candidate_ids, list) or not all(
            isinstance(value, str) for value in candidate_ids
        ):
            raise ApiError(
                400,
                "invalid_request",
                "candidate_ids must be a list of candidate ID strings.",
            )
        try:
            return self.asset_store_downloader.prepare(
                candidate_ids=candidate_ids
            )
        except AssetStoreDownloadError as exc:
            raise ApiError(exc.status, exc.code, str(exc)) from exc

    def start_asset_store_download(
        self, payload: dict[str, Any]
    ) -> dict[str, Any]:
        _only_fields(payload, {"plan_id", "approval_nonce"})
        plan_id = _text(
            payload.get("plan_id"),
            name="plan_id",
            required=True,
            maximum=100,
        )
        approval_nonce = _text(
            payload.get("approval_nonce"),
            name="approval_nonce",
            required=True,
            maximum=256,
        )
        try:
            return self.asset_store_downloader.start(
                plan_id=plan_id,
                approval_nonce=approval_nonce,
            )
        except AssetStoreDownloadError as exc:
            raise ApiError(exc.status, exc.code, str(exc)) from exc

    def asset_store_download_job(self, job_id: str) -> dict[str, Any]:
        safe_id = _text(
            job_id,
            name="job_id",
            required=True,
            maximum=100,
        )
        try:
            result = self.asset_store_downloader.get(safe_id)
        except AssetStoreDownloadError as exc:
            raise ApiError(exc.status, exc.code, str(exc)) from exc
        if result["job"]["state"] == "completed":
            try:
                scan = self.planner.scan_asset_store_cache(
                    None, inspect_packages=False
                )
                result["cache_scan"] = scan.summary()
            except AssetStoreCacheError as exc:
                result["cache_scan_error"] = str(exc)
        return result

    def compare_asset_candidates(self, payload: dict[str, Any]) -> dict[str, Any]:
        _only_fields(payload, {"candidate_ids", "offset", "limit"})
        ids = payload.get("candidate_ids")
        if (not isinstance(ids, list) or not 1 <= len(ids) <= 5000
                or any(not isinstance(i, str) or not i or len(i) > 300 for i in ids)
                or len(set(ids)) != len(ids)):
            raise ApiError(400, "invalid_request", "Provide 1–5000 distinct saved candidate IDs.")
        offset, limit = payload.get("offset", 0), payload.get("limit", 6)
        if type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= 6:
            raise ApiError(400, "invalid_request", "offset must be non-negative; limit must be 1–6.")
        cards = []
        for candidate_id in ids[offset:offset + limit]:
            candidate = self.repository.get_candidate(candidate_id)
            if candidate is None or candidate.source != "asset_store":
                raise ApiError(404, "candidate_not_found", "Comparison requires saved Asset Store candidates.")
            cards.append(comparison_card(candidate))
        return {
            "items": cards, "total": len(ids), "offset": offset,
            "next_offset": offset + limit if offset + limit < len(ids) else None,
            "selection_is_approval": False,
            "guidance": "Compare images and explain role, suitability and unknowns. Selection is not download, import or scene adoption approval.",
        }

    def asset_product_preview(self, payload: dict[str, Any]) -> dict[str, Any]:
        _only_fields(payload, {"candidate_id"})
        candidate_id = _text(payload.get("candidate_id"), name="candidate_id", required=True, maximum=300)
        candidate = self.repository.get_candidate(candidate_id)
        if candidate is None or candidate.source != "asset_store":
            raise ApiError(404, "candidate_not_found", "Asset Store商品を選択してください。")
        value = candidate_view(candidate)
        if not candidate.metadata.get("asset_store_details", {}).get("visuals"):
            try:
                details, _ = fetch_product_details(candidate.external_id, candidate.url)
            except AssetStoreDetailsError as exc:
                raise ApiError(422, "product_preview_unavailable", str(exc)) from exc
            value = candidate_view(self.repository.complete_asset_store_detail_job(candidate.id, details))
        return {"candidate": value}

    def request_asset_import(self, payload: dict[str, Any]) -> dict[str, Any]:
        _only_fields(payload, {"candidate_id", "project_path", "platform", "cache_path"})
        checked = self.validate_asset_candidate({**payload, "compile": False})
        validation = checked["validation"]
        if validation["overall_status"] == "incompatible":
            raise ApiError(422, "asset_incompatible", "互換性検査で問題が見つかりました。検査結果を確認してください。", validation)
        candidate = self.repository.get_candidate(checked["candidate"]["id"])
        cache = self.repository.get_candidate(validation["cache_candidate_id"])
        try:
            package = resolve_cached_package_path(cache, explicit_cache_root=payload.get("cache_path") or None)
            return self.asset_store_importer.request(package=package,
                project=Path(payload["project_path"]), title=candidate.title)
        except AssetStoreDownloadError as exc:
            raise ApiError(exc.status, exc.code, str(exc)) from exc
        except LocalAssetValidationError as exc:
            raise ApiError(422, "cached_package_missing", str(exc)) from exc

    def asset_import_job(self, job_id: str) -> dict[str, Any]:
        try:
            return self.asset_store_importer.get(job_id)
        except AssetStoreDownloadError as exc:
            raise ApiError(exc.status, exc.code, str(exc)) from exc

    def execute_install(self, payload: dict[str, Any]) -> dict[str, Any]:
        _only_fields(payload, {"plan_id", "approval_nonce"})
        plan_id = _text(
            payload.get("plan_id"),
            name="plan_id",
            required=True,
            maximum=100,
        )
        approval_nonce = _text(
            payload.get("approval_nonce"),
            name="approval_nonce",
            required=True,
            maximum=256,
        )
        try:
            return self.installer.execute(
                plan_id=plan_id,
                approval_nonce=approval_nonce,
            )
        except InstallCoordinatorError as exc:
            raise ApiError(exc.status, exc.code, str(exc)) from exc

    def install_job(self, job_id: str) -> dict[str, Any]:
        safe_id = _text(
            job_id,
            name="job_id",
            required=True,
            maximum=100,
        )
        try:
            return self.installer.get(safe_id)
        except InstallCoordinatorError as exc:
            raise ApiError(exc.status, exc.code, str(exc)) from exc

    def rollback_install(self, payload: dict[str, Any]) -> dict[str, Any]:
        _only_fields(payload, {"job_id", "rollback_nonce"})
        job_id = _text(
            payload.get("job_id"),
            name="job_id",
            required=True,
            maximum=100,
        )
        rollback_nonce = _text(
            payload.get("rollback_nonce"),
            name="rollback_nonce",
            required=True,
            maximum=256,
        )
        try:
            return self.installer.rollback(
                job_id=job_id,
                rollback_nonce=rollback_nonce,
            )
        except InstallCoordinatorError as exc:
            raise ApiError(exc.status, exc.code, str(exc)) from exc

    def catalog(
        self,
        *,
        query: str = "",
        source: str = "",
        ownership: str = "",
        scope: str = "",
        limit: int = 100,
    ) -> dict[str, Any]:
        safe_query = _text(query, name="query", maximum=300)
        safe_source = _text(source, name="source", maximum=30).casefold()
        safe_ownership = _text(
            ownership, name="ownership", maximum=30
        ).casefold()
        safe_scope = _text(scope, name="scope", maximum=40).casefold()
        if safe_source and safe_source not in _SOURCES:
            raise ApiError(400, "invalid_request", "Unsupported source filter.")
        if safe_ownership and safe_ownership not in _OWNERSHIP:
            raise ApiError(400, "invalid_request", "Unsupported ownership filter.")
        if safe_scope and safe_scope not in _SCOPES:
            raise ApiError(400, "invalid_request", "Unsupported search scope.")
        try:
            safe_limit = max(1, min(int(limit), 500))
        except (TypeError, ValueError) as exc:
            raise ApiError(400, "invalid_request", "limit must be an integer.") from exc
        items = self.repository.list_candidates(
            query=safe_query,
            source=safe_source or None,
            ownership=safe_ownership or None,
            limit=5000 if safe_scope else safe_limit,
        )
        if safe_scope:
            items = [
                item for item in items
                if candidate_scope(item) == safe_scope
            ][:safe_limit]
        return {
            "items": [candidate_view(item) for item in items],
            "count": len(items),
            "summary": self.repository.summary(),
        }

    def save_manual(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = _text(
            payload.get("url"), name="url", required=True, maximum=2048
        )
        title = _text(
            payload.get("title"), name="title", required=True, maximum=500
        )
        notes = _text(payload.get("notes"), name="notes", maximum=4000)
        ownership = _text(
            payload.get("ownership", "candidate"),
            name="ownership",
            maximum=30,
        ).casefold()
        try:
            product_url = normalize_asset_store_product_url(url)
        except ValueError as exc:
            raise ApiError(
                400,
                "invalid_asset_store_url",
                str(exc),
            ) from exc
        url = product_url.canonical_url
        if ownership not in _OWNERSHIP:
            raise ApiError(400, "invalid_request", "Unsupported ownership value.")
        raw_categories = payload.get("categories", [])
        if not isinstance(raw_categories, list) or len(raw_categories) > 20:
            raise ApiError(400, "invalid_request", "categories must be a short list.")
        categories = tuple(dict.fromkeys(
            _text(item, name="category", maximum=80).casefold()
            for item in raw_categories
            if str(item).strip()
        ))
        unsupported = [
            item for item in categories
            if item not in _REQUIREMENT_CATEGORY_KEYS
        ]
        if unsupported:
            raise ApiError(
                400,
                "invalid_request",
                "Unsupported feature category.",
                {
                    "unsupported": unsupported,
                    "supported": sorted(_REQUIREMENT_CATEGORY_KEYS),
                },
            )
        item = self.repository.save_manual_asset(
            url=url,
            title=title,
            notes=notes,
            ownership=ownership,
            categories=categories,
        )
        self.repository.enqueue_asset_store_detail_candidate(item.id)
        self._schedule_asset_store_detail_sync()
        return {"item": candidate_view(item), "catalog": self.repository.summary()}


    def save_owned_rag(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Register a purchase assertion and only user-authored RAG fields."""
        _only_fields(payload, {
            "url",
            "title",
            "user_alias",
            "notes",
            "categories",
            "purchase_confirmation",
        })
        if payload.get("purchase_confirmation") is not True:
            raise ApiError(
                400,
                "purchase_confirmation_required",
                "Explicit self-asserted purchase confirmation is required.",
            )
        url = _text(
            payload.get("url"), name="url", required=True, maximum=2048
        )
        title = _text(
            payload.get("title"), name="title", required=True, maximum=500
        )
        user_alias = _text(
            payload.get("user_alias"), name="user_alias", maximum=200
        )
        notes = _text(payload.get("notes"), name="notes", maximum=4000)
        raw_categories = payload.get("categories", [])
        if (
            not isinstance(raw_categories, list)
            or len(raw_categories) > 20
            or not all(isinstance(item, str) for item in raw_categories)
        ):
            raise ApiError(
                400,
                "invalid_request",
                "categories must be a short text list.",
            )
        categories = tuple(dict.fromkeys(
            _text(item, name="category", maximum=80).casefold()
            for item in raw_categories
            if item.strip()
        ))
        unsupported = [
            item for item in categories
            if item not in _REQUIREMENT_CATEGORY_KEYS
        ]
        if unsupported:
            raise ApiError(
                400,
                "invalid_request",
                "Unsupported feature category.",
                {
                    "unsupported": unsupported,
                    "supported": sorted(_REQUIREMENT_CATEGORY_KEYS),
                },
            )
        if not user_alias and not notes and not categories:
            raise ApiError(
                400,
                "rag_context_required",
                "Add a user-authored alias, note, or feature category.",
            )
        try:
            item = self.repository.save_owned_asset_rag(
                url=url,
                title=title,
                user_alias=user_alias,
                notes=notes,
                categories=categories,
                category_titles=tuple(
                    _REQUIREMENT_CATEGORY_TITLES[item]
                    for item in categories
                ),
            )
        except ValueError as exc:
            raise ApiError(400, "invalid_asset_store_url", str(exc)) from exc
        self.repository.enqueue_asset_store_detail_candidate(item.id)
        detail_started = self._schedule_asset_store_detail_sync()
        if not detail_started:
            self._schedule_rag_index()
        return {
            "item": candidate_view(item),
            "catalog": self.repository.summary(),
        }

    def search_asset_rag(
        self,
        *,
        query: str = "",
        limit: int = 20,
    ) -> dict[str, Any]:
        safe_query = _text(query, name="query", maximum=300)
        try:
            safe_limit = max(1, min(int(limit), 100))
        except (TypeError, ValueError) as exc:
            raise ApiError(400, "invalid_request", "limit must be an integer.") from exc
        try:
            result = self.repository.search_asset_rag(
                query=safe_query,
                limit=safe_limit,
            )
            if result.get("retrieval_mode") == "lexical_fallback":
                self._schedule_rag_index()
            return result
        except RagIndexNotReadyError as exc:
            self._schedule_rag_index()
            raise ApiError(
                503,
                "rag_index_not_ready",
                str(exc),
                {"rag_index": exc.status},
            ) from exc
        except (TextEmbeddingError, RuntimeError, ValueError) as exc:
            raise ApiError(
                503,
                "embedding_unavailable",
                str(exc),
                {"rag_index": self.repository.rag_index_status()},
            ) from exc

    def reindex_asset_rag(
        self,
        *,
        force: bool = False,
        batch_size: int = 32,
    ) -> dict[str, Any]:
        try:
            return self.repository.reindex_asset_rag_embeddings(
                force=bool(force),
                batch_size=batch_size,
            )
        except RagIndexBusyError as exc:
            raise ApiError(
                409,
                "rag_index_busy",
                str(exc),
                {"rag_index": self.repository.rag_index_status()},
            ) from exc
        except (RuntimeError, ValueError) as exc:
            raise ApiError(503, "embedding_unavailable", str(exc)) from exc

__all__ = ["ApiError", "GameStackApplication"]
