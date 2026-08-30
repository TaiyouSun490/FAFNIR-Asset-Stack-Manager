"""Safe URL handling for user-selected Unity Asset Store product pages."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

_ALLOWED_HOSTS = {"assetstore.unity.com", "marketplace.unity.com"}
_PRODUCT_SLUG = re.compile(r"^.+-(?P<product_id>[1-9][0-9]*)$")
_ALLOWED_SUBPAGES = {"reviews"}


@dataclass(frozen=True, slots=True)
class AssetStoreProductUrl:
    product_id: str
    canonical_url: str


def normalize_asset_store_product_url(url: str) -> AssetStoreProductUrl:
    """Validate and canonicalize one public product URL without fetching it."""

    try:
        parsed = urlsplit(str(url or "").strip())
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid Unity Asset Store product URL.") from exc
    hostname = (parsed.hostname or "").rstrip(".").casefold()
    if (
        parsed.scheme.casefold() != "https"
        or hostname not in _ALLOWED_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        raise ValueError("Use an official HTTPS Unity Asset Store product URL.")

    path = parsed.path.replace("\\", "/")
    if "%2f" in path.casefold() or "%5c" in path.casefold():
        raise ValueError("Encoded path separators are not allowed.")
    parts = [part for part in path.split("/") if part]
    if parts and parts[-1].casefold() in _ALLOWED_SUBPAGES:
        parts.pop()
    if (
        len(parts) < 3
        or parts[0].casefold() != "packages"
        or any(part in {".", ".."} for part in parts)
    ):
        raise ValueError("Use a Unity Asset Store product page, not a search page.")

    match = _PRODUCT_SLUG.fullmatch(parts[-1])
    if match is None:
        raise ValueError("Unity Asset Store product ID is missing from the URL.")
    product_id = match.group("product_id")
    canonical_path = "/" + "/".join(parts)
    canonical_url = urlunsplit(
        ("https", "assetstore.unity.com", canonical_path, "", "")
    )
    return AssetStoreProductUrl(
        product_id=product_id,
        canonical_url=canonical_url,
    )


__all__ = ["AssetStoreProductUrl", "normalize_asset_store_product_url"]