"""Import the minimal owned-asset export produced inside Unity Editor."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = "stackforge.unity-my-assets.v1"
MAX_EXPORT_BYTES = 8 * 1024 * 1024
MAX_ASSETS = 20_000


class UnityMyAssetsError(ValueError):
    """Raised when a Unity Editor export is missing or malformed."""


@dataclass(frozen=True, slots=True)
class UnityOwnedAsset:
    product_id: str
    display_name: str
    purchased_time: str
    tags: tuple[str, ...]
    hidden: bool


@dataclass(frozen=True, slots=True)
class UnityMyAssetsExport:
    generated_at_utc: str
    unity_version: str
    assets: tuple[UnityOwnedAsset, ...]


def default_export_path() -> Path:
    base = os.getenv("LOCALAPPDATA") or os.getenv("APPDATA")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "game-stack-planner" / "unity-my-assets.json"


def _short_text(value: Any, *, name: str, maximum: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise UnityMyAssetsError(f"{name} must be text.")
    result = value.strip()
    if len(result) > maximum:
        raise UnityMyAssetsError(f"{name} is too long.")
    return result


def load_unity_my_assets(
    path: str | Path | None = None,
) -> UnityMyAssetsExport:
    target = Path(path or default_export_path()).expanduser()
    try:
        size = target.stat().st_size
    except FileNotFoundError as exc:
        raise UnityMyAssetsError(
            "Unity My Assets export was not found. Run Tools > Stackforge > "
            "My Assets Sync inside Unity first."
        ) from exc
    except OSError as exc:
        raise UnityMyAssetsError("Unity My Assets export cannot be read.") from exc
    if size <= 0 or size > MAX_EXPORT_BYTES:
        raise UnityMyAssetsError("Unity My Assets export size is invalid.")
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise UnityMyAssetsError("Unity My Assets export is not valid JSON.") from exc
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
        raise UnityMyAssetsError("Unsupported Unity My Assets export schema.")
    raw_assets = raw.get("assets")
    if not isinstance(raw_assets, list) or len(raw_assets) > MAX_ASSETS:
        raise UnityMyAssetsError("Unity My Assets export has an invalid asset list.")

    assets: dict[str, UnityOwnedAsset] = {}
    for index, item in enumerate(raw_assets):
        if not isinstance(item, dict):
            raise UnityMyAssetsError(f"assets[{index}] must be an object.")
        product = item.get("productId")
        if isinstance(product, bool) or not isinstance(product, (int, str)):
            raise UnityMyAssetsError(f"assets[{index}].productId is invalid.")
        product_id = str(product).strip()
        if not product_id.isascii() or not product_id.isdigit():
            raise UnityMyAssetsError(f"assets[{index}].productId is invalid.")
        if int(product_id) <= 0 or len(product_id) > 19:
            raise UnityMyAssetsError(f"assets[{index}].productId is invalid.")
        display_name = _short_text(
            item.get("displayName"),
            name=f"assets[{index}].displayName",
            maximum=500,
        )
        if not display_name:
            display_name = f"Unity Asset Store product {product_id}"
        purchased_time = _short_text(
            item.get("purchasedTime"),
            name=f"assets[{index}].purchasedTime",
            maximum=100,
        )
        raw_tags = item.get("tags", [])
        if (
            not isinstance(raw_tags, list)
            or len(raw_tags) > 64
            or not all(isinstance(tag, str) for tag in raw_tags)
        ):
            raise UnityMyAssetsError(f"assets[{index}].tags is invalid.")
        tags = tuple(dict.fromkeys(
            value
            for tag in raw_tags
            if (value := _short_text(
                tag,
                name=f"assets[{index}].tags",
                maximum=100,
            ))
        ))
        hidden = item.get("hidden", False)
        if not isinstance(hidden, bool):
            raise UnityMyAssetsError(f"assets[{index}].hidden must be boolean.")
        assets[product_id] = UnityOwnedAsset(
            product_id=product_id,
            display_name=display_name,
            purchased_time=purchased_time,
            tags=tags,
            hidden=hidden,
        )

    return UnityMyAssetsExport(
        generated_at_utc=_short_text(
            raw.get("generatedAtUtc"),
            name="generatedAtUtc",
            maximum=100,
        ),
        unity_version=_short_text(
            raw.get("unityVersion"),
            name="unityVersion",
            maximum=100,
        ),
        assets=tuple(assets.values()),
    )


__all__ = [
    "MAX_ASSETS",
    "MAX_EXPORT_BYTES",
    "SCHEMA",
    "UnityMyAssetsError",
    "UnityMyAssetsExport",
    "UnityOwnedAsset",
    "default_export_path",
    "load_unity_my_assets",
]
