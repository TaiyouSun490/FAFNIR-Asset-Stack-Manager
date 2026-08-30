"""Bounded inspection of a local Unity project."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .models import Candidate, ProjectSnapshot

_MAX_METADATA_BYTES = 4 * 1024 * 1024
_PACKAGE_CATEGORIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("inputsystem", ("input",)),
    ("cinemachine", ("camera",)),
    ("netcode", ("networking",)),
    ("multiplayer", ("networking", "lobby_matchmaking")),
    ("lobby", ("lobby_matchmaking",)),
    ("relay", ("networking", "lobby_matchmaking")),
    ("navigation", ("enemy_ai",)),
    ("localization", ("localization",)),
    ("addressables", ("addressables",)),
    ("modules.ui", ("ui",)),
    ("ugui", ("ui",)),
    ("modules.audio", ("audio",)),
    ("render-pipelines", ("rendering",)),
    ("xr.", ("xr",)),
    ("modules.xr", ("xr",)),
    ("openxr", ("xr",)),
    ("timeline", ("cutscene",)),
    ("visualscripting", ("visual_scripting",)),
    ("entities", ("ecs",)),
    ("burst", ("performance",)),
    ("collections", ("performance",)),
    ("test-framework", ("testing",)),
)


class UnityProjectError(ValueError):
    pass


def _read_text(path: Path, *, required: bool = False) -> str:
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        if required:
            raise UnityProjectError(f"Required Unity file not found: {path}")
        return ""
    if size > _MAX_METADATA_BYTES:
        raise UnityProjectError(f"Unity metadata file is too large: {path}")
    try:
        return path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise UnityProjectError(f"Could not read Unity metadata: {path}") from exc


def _read_json(path: Path, *, required: bool = False) -> dict[str, Any]:
    text = _read_text(path, required=required)
    if not text:
        return {}
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise UnityProjectError(f"Invalid JSON in Unity metadata: {path}") from exc
    if not isinstance(value, dict):
        raise UnityProjectError(f"Expected an object in Unity metadata: {path}")
    return value


def _categories(name: str) -> tuple[str, ...]:
    lowered = name.casefold()
    values: list[str] = []
    for marker, categories in _PACKAGE_CATEGORIES:
        if marker in lowered:
            values.extend(categories)
    return tuple(dict.fromkeys(values))


def _package_url(name: str, declared: str, lock: dict[str, Any]) -> str:
    if declared.startswith(("http://", "https://", "git+", "ssh://")):
        return declared.removeprefix("git+")
    lock_url = str(lock.get("url") or "")
    if lock_url.startswith(("http://", "https://")):
        return lock_url
    if name.startswith("com.unity."):
        version = str(lock.get("version") or declared).split("#", 1)[0]
        return (
            "https://docs.unity3d.com/Packages/"
            f"{quote(name, safe='.')}"
            f"@{quote(version, safe='.-')}/manual/index.html"
        )
    return ""


def _project_setting(text: str, name: str) -> str | None:
    match = re.search(
        rf"(?m)^\s*{re.escape(name)}:\s*(.+?)\s*$",
        text,
    )
    if not match:
        return None
    value = match.group(1).strip()
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    return value or None


def scan_unity_project(path: str | Path) -> ProjectSnapshot:
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise UnityProjectError("Unity project directory does not exist.")
    manifest_path = root / "Packages" / "manifest.json"
    project_version_path = root / "ProjectSettings" / "ProjectVersion.txt"
    if not manifest_path.is_file() or not project_version_path.is_file():
        raise UnityProjectError(
            "Select a Unity project containing Packages/manifest.json and "
            "ProjectSettings/ProjectVersion.txt."
        )

    manifest = _read_json(manifest_path, required=True)
    package_lock = _read_json(root / "Packages" / "packages-lock.json")
    project_version_text = _read_text(project_version_path, required=True)
    project_settings_text = _read_text(
        root / "ProjectSettings" / "ProjectSettings.asset"
    )

    raw_dependencies = manifest.get("dependencies", {})
    if not isinstance(raw_dependencies, dict):
        raise UnityProjectError("Unity package manifest dependencies are invalid.")
    lock_dependencies = package_lock.get("dependencies", {})
    if not isinstance(lock_dependencies, dict):
        lock_dependencies = {}

    packages: list[Candidate] = []
    warnings: list[str] = []
    for name, declared_value in sorted(raw_dependencies.items()):
        declared = str(declared_value)
        lock_value = lock_dependencies.get(name, {})
        lock = lock_value if isinstance(lock_value, dict) else {}
        version = str(lock.get("version") or declared)
        source = str(lock.get("source") or "manifest")
        packages.append(
            Candidate(
                id=f"local:{name}",
                source="local",
                external_id=name,
                title=name,
                url=_package_url(name, declared, lock),
                description=f"Installed Unity package ({source}).",
                categories=_categories(name),
                tags=(name, source),
                ownership="installed",
                installed=True,
                license=None,
                version=version,
                metadata={
                    "declared": declared,
                    "source": source,
                    "depth": lock.get("depth"),
                    "dependencies": lock.get("dependencies", {}),
                },
            )
        )
        if source == "git" and not re.search(r"[#@][0-9a-f]{7,40}$", declared):
            warnings.append(
                f"{name}: Git dependency is not pinned to an immutable commit."
            )

    names = {item.external_id for item in packages}
    if "com.unity.render-pipelines.high-definition" in names:
        render_pipeline = "hdrp"
    elif "com.unity.render-pipelines.universal" in names:
        render_pipeline = "urp"
    else:
        render_pipeline = "built-in-or-custom"

    active_input = _project_setting(project_settings_text, "activeInputHandler")
    input_backend = {
        "0": "legacy",
        "1": "input-system",
        "2": "both",
    }.get(active_input or "", "input-system" if "com.unity.inputsystem" in names else "unknown")

    version_match = re.search(
        r"(?m)^m_EditorVersion:\s*(?P<version>[^\r\n]+)",
        project_version_text,
    )
    unity_version = (
        version_match.group("version").strip() if version_match else None
    )
    return ProjectSnapshot(
        path=str(root),
        name=root.name,
        unity_version=unity_version,
        render_pipeline=render_pipeline,
        input_backend=input_backend,
        packages=tuple(packages),
        product_name=_project_setting(project_settings_text, "productName"),
        company_name=_project_setting(project_settings_text, "companyName"),
        warnings=tuple(dict.fromkeys(warnings)),
    )


__all__ = ["UnityProjectError", "scan_unity_project"]
