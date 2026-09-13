"""Read-only, paginated comparison cards; never procurement or user approval."""

from __future__ import annotations

from typing import Any

from .asset_store_visuals import AssetStoreVisualsError, _validate_image_url
from .models import Candidate
from .scopes import candidate_view


def comparison_card(candidate: Candidate) -> dict[str, Any]:
    value = candidate_view(candidate)
    details = candidate.metadata.get("asset_store_details")
    details = details if isinstance(details, dict) else {}
    visuals = details.get("visuals")
    visuals = visuals if isinstance(visuals, dict) else {}
    gallery = visuals.get("gallery")
    gallery = gallery if isinstance(gallery, list) else []
    references = [(visuals.get("main_image_url"), "main")]
    references.extend(
        (item.get("image_url") or item.get("thumbnail_url"), "gallery")
        for item in gallery if isinstance(item, dict)
    )
    images = []
    seen = set()
    for url, role in references:
        if not url:
            continue
        try:
            url = _validate_image_url(url)
        except AssetStoreVisualsError:
            continue
        if url not in seen:
            images.append({"candidate_id": candidate.id, "source_url": url, "role": role})
            seen.add(url)
        if len(images) == 12:
            break
    # Do not expose raw metadata, cache paths, credentials or approval nonces.
    return {
        **{key: value.get(key) for key in (
            "id", "source", "title", "url", "description", "ownership",
            "version", "unity_version", "render_pipeline", "license", "installed",
        )},
        "ownership_evidence": {
            "kind": str(value["ownership_evidence"].get("kind") or "none"),
            "verified": value["ownership_evidence"].get("verified") is True,
        },
        "download_size_bytes": details.get("download_size_bytes"),
        "render_pipeline_compatibility": details.get("render_pipeline_compatibility") or [],
        "images": images,
        "image_status": "available" if images else "not_loaded_or_unavailable",
        "caveat": "商品紹介画像です。実パッケージ・互換性・性能・配置結果の検証ではありません。",
    }
