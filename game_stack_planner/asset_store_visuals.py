"""Bounded, opt-in visual review for public Unity Asset Store products."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .asset_store_details import (
    ASSET_STORE_CDN_HOST,
    AssetStoreDetailsError,
    fetch_product_details,
)
from .models import Candidate


MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
_DETAIL_LIMITS = {"quick": 3, "detail": 6}
_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}


class AssetStoreVisualsError(RuntimeError):
    """An opt-in public image review could not be completed safely."""


@dataclass(frozen=True, slots=True)
class ReviewImage:
    url: str
    role: str
    mime_type: str
    content: bytes


ImageFetcher = Callable[[str], tuple[bytes, str]]


def _validate_image_url(value: str) -> str:
    parsed = urlparse(str(value or ""))
    try:
        port = parsed.port
    except ValueError as exc:
        raise AssetStoreVisualsError(
            "Product image URL has an invalid port."
        ) from exc
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").casefold() != ASSET_STORE_CDN_HOST
        or parsed.username
        or parsed.password
        or port not in {None, 443}
    ):
        raise AssetStoreVisualsError("Product image URL is not on Unity's public CDN.")
    return parsed._replace(fragment="").geturl()


def fetch_asset_store_image(url: str) -> tuple[bytes, str]:
    safe_url = _validate_image_url(url)
    request = Request(
        safe_url,
        headers={
            "Accept": "image/avif,image/webp,image/png,image/jpeg;q=0.9",
            "User-Agent": "Fafnir/0.5 (+opt-in Asset Store visual review)",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=30.0) as response:
            _validate_image_url(response.geturl())
            mime_type = str(response.headers.get_content_type()).casefold()
            if mime_type not in _MIME_TYPES:
                raise AssetStoreVisualsError("Asset Store returned an unsupported image type.")
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > MAX_IMAGE_BYTES:
                raise AssetStoreVisualsError("Asset Store image exceeds the size limit.")
            content = response.read(MAX_IMAGE_BYTES + 1)
            if not content or len(content) > MAX_IMAGE_BYTES:
                raise AssetStoreVisualsError("Asset Store image exceeds the size limit.")
            return content, mime_type
    except AssetStoreVisualsError:
        raise
    except (HTTPError, URLError, OSError, TimeoutError, ValueError) as exc:
        raise AssetStoreVisualsError("Asset Store image request failed.") from exc


def review_candidate_visuals(
    candidate: Candidate,
    *,
    detail: str = "quick",
    fetch_image: ImageFetcher = fetch_asset_store_image,
) -> tuple[dict[str, Any], tuple[ReviewImage, ...]]:
    normalized_detail = str(detail or "quick").casefold()
    if normalized_detail not in _DETAIL_LIMITS:
        raise AssetStoreVisualsError("detail must be 'quick' or 'detail'.")
    if candidate.source != "asset_store":
        raise AssetStoreVisualsError("Visual review requires an Asset Store candidate.")
    try:
        details, _ = fetch_product_details(candidate.external_id, candidate.url)
    except AssetStoreDetailsError as exc:
        raise AssetStoreVisualsError(str(exc)) from exc
    visuals = details.get("visuals")
    visuals = visuals if isinstance(visuals, dict) else {}
    selected: list[tuple[str, str]] = []
    main_url = str(visuals.get("main_image_url") or "")
    if main_url:
        selected.append((main_url, "main"))
    gallery = visuals.get("gallery")
    if isinstance(gallery, list):
        ordered_gallery = list(gallery)
        if normalized_detail == "quick":
            ordered_gallery.sort(
                key=lambda item: 0
                if isinstance(item, dict) and item.get("type") == "screenshot"
                else 1
            )
        for index, item in enumerate(ordered_gallery, start=1):
            if not isinstance(item, dict):
                continue
            url = str(item.get("image_url") or item.get("thumbnail_url") or "")
            if url and url not in {value[0] for value in selected}:
                selected.append((url, f"gallery_{index}"))
            if len(selected) >= _DETAIL_LIMITS[normalized_detail]:
                break

    images: list[ReviewImage] = []
    total = 0
    for url, role in selected[:_DETAIL_LIMITS[normalized_detail]]:
        content, mime_type = fetch_image(_validate_image_url(url))
        total += len(content)
        if total > MAX_TOTAL_BYTES:
            raise AssetStoreVisualsError("Visual review exceeds the total size limit.")
        images.append(ReviewImage(url, role, mime_type, content))
    if not images:
        raise AssetStoreVisualsError("No reviewable product images were published.")
    return ({
        "candidate_id": candidate.id,
        "title": candidate.title,
        "detail": normalized_detail,
        "image_count": len(images),
        "product_url": candidate.url,
        "decision_options": ["adopt", "hold_for_detail", "reject"],
        "guidance": (
            "Judge visible style and apparent fit only. Use textual product details, "
            "compatibility evidence, and package validation before final adoption."
        ),
        "images": [
            {"index": index, "role": item.role, "source_url": item.url}
            for index, item in enumerate(images, start=1)
        ],
    }, tuple(images))


__all__ = [
    "AssetStoreVisualsError",
    "ReviewImage",
    "fetch_asset_store_image",
    "review_candidate_visuals",
]
