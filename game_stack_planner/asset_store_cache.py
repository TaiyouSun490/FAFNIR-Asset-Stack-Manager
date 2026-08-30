"""Bounded, read-only discovery of Unity's local Asset Store package cache."""

from __future__ import annotations

import hashlib
import os
import stat
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from .models import Candidate

_CACHE_DIR_NAME = "Asset Store-5.x"
_DEFAULT_MAX_ITEMS = 5000
_DEFAULT_MAX_DEPTH = 8


class AssetStoreCacheError(ValueError):
    """Raised when an explicitly selected cache root cannot be scanned."""


@dataclass(frozen=True, slots=True)
class CacheRoot:
    kind: str
    path: Path


@dataclass(frozen=True, slots=True)
class CacheScanResult:
    candidates: tuple[Candidate, ...]
    roots: tuple[dict[str, object], ...]
    warnings: tuple[str, ...]
    truncated: bool

    def summary(self) -> dict[str, object]:
        return {
            "found": len(self.candidates),
            "roots": list(self.roots),
            "warnings": list(self.warnings),
            "truncated": self.truncated,
            "inventory_state": "locally_cached",
            "ownership_confirmed": False,
        }


def _normalized_identity(value: str) -> str:
    return unicodedata.normalize("NFKC", value).replace("\\", "/").casefold()


def _configured_cache_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if path.name.casefold() == _CACHE_DIR_NAME.casefold():
        return path
    child = path / _CACHE_DIR_NAME
    return child if child.is_dir() else path


def discover_cache_roots(
    explicit_root: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> tuple[CacheRoot, ...]:
    """Return documented/default roots without searching unrelated folders."""
    env = os.environ if environ is None else environ
    values: list[CacheRoot] = []
    if explicit_root is not None and str(explicit_root).strip():
        values.append(CacheRoot("custom", _configured_cache_path(explicit_root)))
    else:
        configured = str(env.get("ASSETSTORE_CACHE_PATH") or "").strip()
        if configured:
            values.append(CacheRoot("configured", _configured_cache_path(configured)))
        appdata = str(env.get("APPDATA") or "").strip()
        if appdata:
            values.append(CacheRoot("default", Path(appdata) / "Unity" / _CACHE_DIR_NAME))

    unique: list[CacheRoot] = []
    seen: set[str] = set()
    for root in values:
        try:
            identity = os.path.normcase(str(root.path.resolve(strict=False)))
        except OSError:
            identity = os.path.normcase(str(root.path.absolute()))
        if identity not in seen:
            seen.add(identity)
            unique.append(root)
    return tuple(unique)


def _is_reparse_point(path: Path) -> bool:
    try:
        details = path.lstat()
    except OSError:
        return True
    attributes = getattr(details, "st_file_attributes", 0)
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return path.is_symlink() or bool(flag and attributes & flag)


def _cache_candidate(path: Path, root: CacheRoot) -> Candidate:
    relative = path.relative_to(root.path)
    parts = relative.parts
    publisher = parts[0] if len(parts) > 1 else "Unknown publisher"
    store_categories = list(parts[1:-1])
    identity = _normalized_identity(str(relative))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    details = path.stat()
    modified = datetime.fromtimestamp(
        details.st_mtime,
        tz=timezone.utc,
    ).isoformat()
    tags = tuple(dict.fromkeys((publisher, *store_categories)))
    return Candidate(
        id=f"asset_store_cache:{digest}",
        source="asset_store_cache",
        external_id=digest,
        title=path.stem,
        url="",
        description=f"Unity Asset Store local cache package by {publisher}.",
        tags=tags,
        ownership="unknown",
        installed=False,
        updated_at=modified,
        metadata={
            "inventory_state": "locally_cached",
            "ownership_evidence": {
                "kind": "asset_store_cache",
                "verified": False,
            },
            "publisher": publisher,
            "store_categories": store_categories,
            "cache_relative_path": relative.as_posix(),
            "cache_root_kind": root.kind,
            "size_bytes": max(0, int(details.st_size)),
            "available": True,
        },
    )


def scan_asset_store_cache(
    explicit_root: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    max_items: int = _DEFAULT_MAX_ITEMS,
    max_depth: int = _DEFAULT_MAX_DEPTH,
) -> CacheScanResult:
    roots = discover_cache_roots(explicit_root, environ=environ)
    if explicit_root is not None and not roots:
        raise AssetStoreCacheError("Asset Store cache path is required.")
    bounded_items = max(1, min(int(max_items), _DEFAULT_MAX_ITEMS))
    bounded_depth = max(1, min(int(max_depth), _DEFAULT_MAX_DEPTH))
    warnings: list[str] = []
    candidates: dict[str, Candidate] = {}
    root_summaries: list[dict[str, object]] = []
    truncated = False

    for root in roots:
        if not root.path.is_dir():
            root_summaries.append({"kind": root.kind, "found": False, "packages": 0})
            if root.kind == "custom":
                raise AssetStoreCacheError("The selected Asset Store cache does not exist.")
            continue
        package_count = 0

        def onerror(error: OSError) -> None:
            warnings.append(f"{root.kind}: {type(error).__name__}")

        for directory, dirnames, filenames in os.walk(
            root.path,
            topdown=True,
            onerror=onerror,
            followlinks=False,
        ):
            current = Path(directory)
            depth = len(current.relative_to(root.path).parts)
            dirnames[:] = sorted(
                (
                    name for name in dirnames
                    if not _is_reparse_point(current / name)
                ),
                key=str.casefold,
            )
            if depth >= bounded_depth:
                if dirnames:
                    warnings.append(f"{root.kind}: depth limit reached")
                dirnames.clear()
            for filename in sorted(filenames, key=str.casefold):
                if not filename.casefold().endswith(".unitypackage"):
                    continue
                path = current / filename
                if _is_reparse_point(path):
                    continue
                try:
                    candidate = _cache_candidate(path, root)
                except (OSError, ValueError) as exc:
                    warnings.append(f"{root.kind}: {type(exc).__name__}")
                    continue
                package_count += 1
                candidates.setdefault(candidate.id, candidate)
                if len(candidates) >= bounded_items:
                    truncated = True
                    break
            if truncated:
                break
        root_summaries.append({
            "kind": root.kind,
            "found": True,
            "packages": package_count,
        })
        if truncated:
            break

    return CacheScanResult(
        candidates=tuple(sorted(candidates.values(), key=lambda item: item.title.casefold())),
        roots=tuple(root_summaries),
        warnings=tuple(dict.fromkeys(warnings))[:20],
        truncated=truncated,
    )


__all__ = [
    "AssetStoreCacheError",
    "CacheRoot",
    "CacheScanResult",
    "discover_cache_roots",
    "scan_asset_store_cache",
]
