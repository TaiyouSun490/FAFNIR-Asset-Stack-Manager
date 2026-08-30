"""Validation boundary shared by the CLI and local web server."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import __version__
from .asset_store import normalize_asset_store_product_url
from .asset_store_cache import AssetStoreCacheError
from .install_service import InstallCoordinator, InstallCoordinatorError
from .repository import StackRepository
from .requirements import requirement_categories
from .scopes import SEARCH_SCOPES, candidate_scope, candidate_view
from .service import GameStackPlanner
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
        self.repository = repository or StackRepository(db_path)
        self.planner = planner or GameStackPlanner(self.repository)
        self.installer = InstallCoordinator(self.repository)

    def close(self) -> None:
        self.repository.close()

    def status(self) -> dict[str, Any]:
        return {
            "ready": True,
            "version": __version__,
            "catalog": self.repository.summary(),
            "capabilities": {
                "unity_project_scan": True,
                "github_search": True,
                "openupm_search": True,
                "asset_store_official_links": True,
                "asset_store_manual_pins": True,
                "asset_store_local_cache_scan": True,
                "asset_store_owned_rag": True,
                "asset_store_rag_user_authored_only": False,
                "unity_editor_my_assets_sync": True,
                "mcp_server": True,
                "asset_store_content_rag": False,
                "asset_store_automated_fetch": False,
                "install_planning": True,
                "openupm_exact_version_install": True,
                "github_install_requires_inspection": True,
                "asset_store_purchase_automated": True,
                "asset_store_download_automated": False,
                "unitypackage_preview_import": False,
            },
            "limits": {
                "prompt_characters": 8000,
                "request_bytes": 1024 * 1024,
                "remote_requirements": 14,
            },
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
        path = _text(payload.get("path"), name="path", maximum=2048)
        try:
            result = self.planner.scan_asset_store_cache(path or None)
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
        try:
            export = load_unity_my_assets(target)
        except UnityMyAssetsError as exc:
            raise ApiError(422, "invalid_unity_my_assets_export", str(exc)) from exc
        imported = self.repository.import_unity_my_assets(
            export,
            export_path=str(target),
        )
        return {
            "sync": {
                "imported": imported,
                "generated_at_utc": export.generated_at_utc,
                "unity_version": export.unity_version,
                "source": "unity_editor_my_assets",
                "ownership_confirmed": True,
            },
            "catalog": self.repository.summary(),
        }

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
        items = self.repository.search_asset_rag(
            query=safe_query,
            limit=safe_limit,
        )
        return {"items": items, "count": len(items)}

__all__ = ["ApiError", "GameStackApplication"]
