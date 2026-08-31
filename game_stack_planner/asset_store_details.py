"""Bounded retrieval of public Unity Asset Store product metadata.

Ownership is established separately by the Unity Editor bridge.  This module
uses only public, credential-free sitemap and product pages to enrich an
already-known catalog record.  The two concerns intentionally stay separate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
import json
import re
import time
from typing import Any, Callable, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET


ASSET_STORE_HOST = "assetstore.unity.com"
SITEMAP_INDEX_URL = f"https://{ASSET_STORE_HOST}/sitemap.xml"
DETAIL_SCHEMA = "stackforge.asset-store-product-details.v1"
MAX_PRODUCT_PAGE_BYTES = 4 * 1024 * 1024
MAX_SITEMAP_BYTES = 8 * 1024 * 1024
MAX_SITEMAPS = 64
MAX_PRODUCTS_PER_PAGE = 20_000
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_FETCH_DELAY_SECONDS = 1.5


class AssetStoreDetailsError(RuntimeError):
    """A public Asset Store metadata request or parse failed safely."""


@dataclass(frozen=True, slots=True)
class HttpDocument:
    content: bytes
    final_url: str
    etag: str = ""
    last_modified: str = ""


FetchDocument = Callable[[str, int], HttpDocument]


class _ScriptCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._capture = False
        self._attributes: dict[str, str] = {}
        self._parts: list[str] = []
        self.scripts: list[tuple[dict[str, str], str]] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag.casefold() != "script" or self._capture:
            return
        self._capture = True
        self._attributes = {
            str(key).casefold(): str(value or "") for key, value in attrs
        }
        self._parts = []

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() != "script" or not self._capture:
            return
        self.scripts.append((self._attributes, "".join(self._parts)))
        self._capture = False
        self._attributes = {}
        self._parts = []


class _PlainTextParser(HTMLParser):
    _BLOCKS = {
        "br", "div", "li", "ol", "p", "section", "table", "td", "th", "tr",
        "ul",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        del attrs
        normalized = tag.casefold()
        if normalized in {"script", "style"}:
            self._ignored_depth += 1
        elif normalized in self._BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.casefold()
        if normalized in {"script", "style"} and self._ignored_depth:
            self._ignored_depth -= 1
        elif normalized in self._BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.parts.append(data)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bounded_string(value: Any, maximum: int) -> str:
    text = str(value or "").strip()
    return text[:maximum]


def html_to_text(value: Any, *, maximum: int) -> str:
    raw = _bounded_string(value, maximum * 4)
    if not raw:
        return ""
    parser = _PlainTextParser()
    try:
        parser.feed(raw)
        parser.close()
    except Exception as exc:  # HTMLParser errors are rare but source is remote.
        raise AssetStoreDetailsError("Asset Store text markup is invalid.") from exc
    lines = [
        re.sub(r"\s+", " ", line).strip()
        for line in "".join(parser.parts).splitlines()
    ]
    return "\n".join(line for line in lines if line)[:maximum]


def _validate_public_url(url: str) -> str:
    parsed = urlparse(str(url or ""))
    if parsed.scheme != "https" or (parsed.hostname or "").casefold() != ASSET_STORE_HOST:
        raise AssetStoreDetailsError("Asset Store metadata URL is not an official HTTPS page.")
    if parsed.username or parsed.password or parsed.port not in {None, 443}:
        raise AssetStoreDetailsError("Asset Store metadata URL contains unsafe authority data.")
    return parsed._replace(fragment="").geturl()


def fetch_document(
    url: str,
    maximum_bytes: int,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> HttpDocument:
    safe_url = _validate_public_url(url)
    request = Request(
        safe_url,
        headers={
            "Accept": "text/html,application/xml;q=0.9,*/*;q=0.1",
            "Accept-Language": "en-US,en;q=0.8",
            "User-Agent": "Stackforge/0.5 (+local Unity asset metadata sync)",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=max(1.0, float(timeout))) as response:
            final_url = _validate_public_url(response.geturl())
            declared = response.headers.get("Content-Length")
            if declared:
                try:
                    if int(declared) > maximum_bytes:
                        raise AssetStoreDetailsError("Asset Store response is too large.")
                except ValueError:
                    pass
            content = response.read(maximum_bytes + 1)
            if len(content) > maximum_bytes:
                raise AssetStoreDetailsError("Asset Store response is too large.")
            return HttpDocument(
                content=content,
                final_url=final_url,
                etag=_bounded_string(response.headers.get("ETag"), 500),
                last_modified=_bounded_string(
                    response.headers.get("Last-Modified"), 500
                ),
            )
    except AssetStoreDetailsError:
        raise
    except HTTPError as exc:
        raise AssetStoreDetailsError(
            f"Asset Store returned HTTP {exc.code}."
        ) from exc
    except (OSError, URLError, TimeoutError) as exc:
        raise AssetStoreDetailsError("Asset Store metadata request failed.") from exc


def _xml_locations(content: bytes, *, expected_root: str) -> list[str]:
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise AssetStoreDetailsError("Asset Store sitemap is invalid XML.") from exc
    local_name = root.tag.rsplit("}", 1)[-1]
    if local_name != expected_root:
        raise AssetStoreDetailsError("Asset Store sitemap has an unexpected root.")
    values: list[str] = []
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] != "loc" or not element.text:
            continue
        values.append(_validate_public_url(element.text.strip()))
    return values


def resolve_product_urls(
    product_ids: Iterable[str],
    *,
    fetch: FetchDocument = fetch_document,
) -> dict[str, str]:
    """Resolve numeric product IDs through Unity's advertised public sitemaps."""
    wanted = {
        str(value).strip() for value in product_ids
        if str(value).strip().isascii() and str(value).strip().isdigit()
    }
    if not wanted:
        return {}
    index = fetch(SITEMAP_INDEX_URL, MAX_SITEMAP_BYTES)
    sitemap_urls = _xml_locations(index.content, expected_root="sitemapindex")
    if len(sitemap_urls) > MAX_SITEMAPS:
        raise AssetStoreDetailsError("Asset Store sitemap index exceeds the safety limit.")

    resolved: dict[str, str] = {}
    for sitemap_url in sitemap_urls:
        document = fetch(sitemap_url, MAX_SITEMAP_BYTES)
        for candidate_url in _xml_locations(document.content, expected_root="urlset"):
            parsed = urlparse(candidate_url)
            if not parsed.path.startswith("/packages/"):
                continue
            match = re.search(r"-(\d+)/?$", parsed.path)
            if match and match.group(1) in wanted:
                resolved[match.group(1)] = candidate_url
        if wanted.issubset(resolved):
            break
    return resolved


def _json_object(script: str, marker: str) -> dict[str, Any] | None:
    position = script.find(marker)
    if position < 0:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(script[position + len(marker):])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AssetStoreDetailsError("Asset Store product state is invalid JSON.") from exc
    return value if isinstance(value, dict) else None


def _resolve_entity(
    entities: Mapping[str, Any],
    value: Any,
) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    reference = value.get("id")
    if value.get("type") != "id" or not isinstance(reference, list) or len(reference) != 2:
        return value
    group = entities.get(str(reference[0]))
    resolved = group.get(str(reference[1])) if isinstance(group, dict) else None
    return resolved if isinstance(resolved, dict) else None


def _strings(value: Any, *, limit: int = 64, maximum: int = 200) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value[:limit]:
        text = _bounded_string(item, maximum)
        if text and text not in result:
            result.append(text)
    return result


def _positive_int(value: Any) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, result)


def _finite_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and result not in {float("inf"), -float("inf")} else None


def _unity_key(value: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", value)
    return tuple(int(item) for item in numbers[:4]) or (10**9,)


def _render_pipeline_matrix(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    aliases = {
        "standard": "built_in",
        "lightweight": "urp",
        "universal": "urp",
        "hd": "hdrp",
        "high_definition": "hdrp",
        "custom": "custom",
    }
    rows: list[dict[str, Any]] = []
    for raw in value[:64]:
        if not isinstance(raw, dict):
            continue
        version = _bounded_string(raw.get("version"), 100)
        raw_types = raw.get("types")
        if not version or not isinstance(raw_types, list):
            continue
        compatible = sorted({
            aliases.get(str(item).casefold(), str(item).casefold()[:40])
            for item in raw_types[:16]
            if str(item).strip()
        })
        rows.append({
            "unity_version": version,
            "compatible_pipelines": compatible,
        })
    return sorted(rows, key=lambda row: _unity_key(row["unity_version"]))


def _collapse_render_pipeline(matrix: list[dict[str, Any]]) -> str | None:
    if not matrix:
        return None
    sets = [set(row["compatible_pipelines"]) for row in matrix]
    common = set.intersection(*sets) if sets else set()
    known = {"built_in", "urp", "hdrp"}
    if known.issubset(common):
        return "all"
    exact = common & known
    if len(exact) == 1:
        return next(iter(exact)).replace("built_in", "built-in")
    return None


def parse_product_page(
    html: str,
    *,
    product_id: str,
    page_url: str,
    fetched_at: str | None = None,
) -> dict[str, Any]:
    expected_id = str(product_id).strip()
    if not expected_id.isascii() or not expected_id.isdigit():
        raise AssetStoreDetailsError("Asset Store product ID is invalid.")
    safe_page_url = _validate_public_url(page_url)
    collector = _ScriptCollector()
    try:
        collector.feed(html)
        collector.close()
    except Exception as exc:
        raise AssetStoreDetailsError("Asset Store product page is invalid HTML.") from exc

    state: dict[str, Any] | None = None
    marker = "__component__.ReactDOMrender("
    for _, script in collector.scripts:
        if marker in script:
            state = _json_object(script, marker)
            break
    if state is None:
        raise AssetStoreDetailsError("Asset Store product metadata was not found.")

    data = state.get("data")
    entities = data.get("ENTITY") if isinstance(data, dict) else None
    products = entities.get("Product") if isinstance(entities, dict) else None
    product = products.get(expected_id) if isinstance(products, dict) else None
    if not isinstance(product, dict):
        raise AssetStoreDetailsError("Asset Store page does not match the product ID.")

    version = _resolve_entity(entities, product.get("currentVersion")) or {}
    publisher = _resolve_entity(entities, product.get("publisher")) or {}
    category = _resolve_entity(entities, product.get("category")) or {}
    supported_versions = _strings(product.get("supportedUnityVersions"), maximum=100)
    matrix = _render_pipeline_matrix(product.get("srps"))

    tags: list[str] = []
    raw_tags = product.get("popularTags")
    if isinstance(raw_tags, list):
        for reference in raw_tags[:64]:
            tag = _resolve_entity(entities, reference) or {}
            name = _bounded_string(tag.get("name"), 100)
            if name and name not in tags:
                tags.append(name)

    dependencies: list[dict[str, str]] = []
    raw_dependencies = product.get("dependencies")
    if isinstance(raw_dependencies, list):
        for reference in raw_dependencies[:100]:
            dependency = _resolve_entity(entities, reference) or {}
            dependency_id = _bounded_string(dependency.get("id"), 30)
            name = _bounded_string(dependency.get("name"), 500)
            if dependency_id or name:
                dependencies.append({"product_id": dependency_id, "title": name})

    offer = product.get("originalPrice")
    offer = offer if isinstance(offer, dict) else {}
    discount = offer.get("discount")
    discount = discount if isinstance(discount, dict) else {}
    price = {
        "currency": _bounded_string(offer.get("currency"), 10),
        "current": _bounded_string(offer.get("finalPrice"), 40),
        "original": _bounded_string(offer.get("originalPrice"), 40),
        "is_free": bool(offer.get("isFree", False)),
        "discount_percent": _positive_int(discount.get("percentage")),
        "entitlement_type": _bounded_string(offer.get("entitlementType"), 40),
    }
    rating = product.get("rating")
    rating = rating if isinstance(rating, dict) else {}
    state_value = _bounded_string(product.get("state"), 80).casefold()

    original_unity_version = (
        min(supported_versions, key=_unity_key) if supported_versions else None
    )
    result = {
        "schema": DETAIL_SCHEMA,
        "source": "unity_asset_store_public_product_page",
        "source_url": safe_page_url,
        "fetched_at": fetched_at or _now(),
        "product_id": expected_id,
        "name": _bounded_string(product.get("name"), 500),
        "slug": _bounded_string(product.get("slug"), 500),
        "description": html_to_text(product.get("description"), maximum=30_000),
        "ai_description": html_to_text(product.get("aiDescription"), maximum=10_000),
        "elevator_pitch": html_to_text(product.get("elevatorPitch"), maximum=5_000),
        "key_features": html_to_text(product.get("keyFeatures"), maximum=15_000),
        "compatibility_info": html_to_text(
            product.get("compatibilityInfo"), maximum=10_000
        ),
        "release_notes": html_to_text(product.get("publishNotes"), maximum=20_000),
        "publisher": {
            "id": _bounded_string(publisher.get("id"), 40),
            "name": _bounded_string(publisher.get("name"), 500),
            "website_url": _bounded_string(publisher.get("url"), 2048),
            "support_url": _bounded_string(publisher.get("supportUrl"), 2048),
        },
        "category": {
            "id": _bounded_string(category.get("id"), 40),
            "name": _bounded_string(category.get("name"), 300),
            "slug": _bounded_string(category.get("slug"), 500),
        },
        "latest_version": _bounded_string(version.get("name"), 100),
        "latest_release_date": _bounded_string(version.get("publishedDate"), 100),
        "first_published_date": _bounded_string(product.get("firstPublishedDate"), 100),
        "original_unity_version": original_unity_version,
        "supported_unity_versions": supported_versions,
        "render_pipeline_compatibility": matrix,
        "render_pipeline_summary": _collapse_render_pipeline(matrix),
        "platforms": [],
        "dependencies": dependencies,
        "dependency_ids": _strings(product.get("dependencyIds"), limit=100, maximum=40),
        "tags": tags,
        "package_type": _bounded_string(product.get("packageType"), 80),
        "product_state": state_value or "unknown",
        "custom_license": bool(product.get("customLicense", False)),
        "license_text": html_to_text(product.get("licenseText"), maximum=10_000),
        "download_size_bytes": _positive_int(product.get("downloadSize")),
        "asset_count": _positive_int(product.get("assetCount")),
        "price": price,
        "rating": {
            "average": _finite_float(rating.get("average")),
            "count": _positive_int(rating.get("count")),
            "review_count": _positive_int(product.get("reviewCount")),
        },
    }
    return result


def fetch_product_details(
    product_id: str,
    page_url: str,
    *,
    fetch: FetchDocument = fetch_document,
) -> tuple[dict[str, Any], HttpDocument]:
    document = fetch(page_url, MAX_PRODUCT_PAGE_BYTES)
    try:
        html = document.content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AssetStoreDetailsError("Asset Store product page is not UTF-8.") from exc
    details = parse_product_page(
        html,
        product_id=product_id,
        page_url=document.final_url,
    )
    return details, document


def polite_delay(seconds: float = DEFAULT_FETCH_DELAY_SECONDS) -> None:
    time.sleep(max(0.0, min(float(seconds), 60.0)))


__all__ = [
    "ASSET_STORE_HOST",
    "AssetStoreDetailsError",
    "DEFAULT_FETCH_DELAY_SECONDS",
    "DETAIL_SCHEMA",
    "FetchDocument",
    "HttpDocument",
    "fetch_document",
    "fetch_product_details",
    "html_to_text",
    "parse_product_page",
    "polite_delay",
    "resolve_product_urls",
]
