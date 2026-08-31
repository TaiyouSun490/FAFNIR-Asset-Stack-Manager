"""Read-only, non-extracting inspection of legacy Unity ``.unitypackage`` files."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath
import re
import struct
import tarfile
from typing import Any


MAX_MEMBERS = 100_000
MAX_TOTAL_UNCOMPRESSED_BYTES = 32 * 1024 * 1024 * 1024
MAX_PATHNAME_BYTES = 32 * 1024
MAX_ANALYZED_FILES = 2_000
MAX_ANALYZED_FILE_BYTES = 2 * 1024 * 1024
MAX_ANALYZED_TOTAL_BYTES = 32 * 1024 * 1024


class UnityPackageInspectionError(ValueError):
    """The archive cannot be inspected within Fafnir's safety limits."""


@dataclass(frozen=True, slots=True)
class UnityPackageInspection:
    summary: dict[str, Any]
    guids: tuple[str, ...]


_TEXT_EXTENSIONS = {
    ".asmdef", ".asmref", ".cginc", ".compute", ".cs", ".hlsl", ".json",
    ".md", ".shader", ".txt", ".xml", ".yaml", ".yml",
}
_NATIVE_EXTENSIONS = {".a", ".bundle", ".dll", ".dylib", ".framework", ".so"}
_CONTENT_GROUPS: tuple[tuple[str, set[str]], ...] = (
    ("scenes", {".unity"}),
    ("prefabs", {".prefab"}),
    ("scripts", {".cs"}),
    ("assemblies", {".asmdef", ".asmref"}),
    ("shaders", {".shader", ".compute", ".hlsl", ".cginc", ".shadergraph"}),
    ("materials", {".mat"}),
    ("audio", {".wav", ".mp3", ".ogg", ".aiff", ".aif"}),
    ("textures", {".png", ".jpg", ".jpeg", ".tga", ".psd", ".exr", ".hdr"}),
    ("models", {".fbx", ".obj", ".blend", ".dae"}),
    ("animations", {".anim", ".controller", ".overridecontroller", ".mask"}),
    ("fonts", {".ttf", ".otf"}),
    ("video", {".mp4", ".webm", ".mov"}),
    ("native_plugins", _NATIVE_EXTENSIONS),
)


def _safe_member_name(value: str) -> PurePosixPath:
    normalized = str(value or "").replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or normalized.startswith("/")
        or path.is_absolute()
        or ".." in path.parts
        or any("\x00" in part for part in path.parts)
    ):
        raise UnityPackageInspectionError("Unity package contains an unsafe path.")
    return path


def _read_member(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    *,
    maximum: int,
) -> bytes:
    if not member.isfile() or member.size < 0 or member.size > maximum:
        raise UnityPackageInspectionError("Unity package metadata file is too large.")
    stream = archive.extractfile(member)
    if stream is None:
        raise UnityPackageInspectionError("Unity package metadata cannot be read.")
    value = stream.read(maximum + 1)
    if len(value) > maximum:
        raise UnityPackageInspectionError("Unity package metadata file is too large.")
    return value


def _decode_text(value: bytes) -> str:
    try:
        return value.decode("utf-8-sig")
    except UnicodeDecodeError:
        return value.decode("utf-8", errors="replace")


def _string_list(value: Any, *, limit: int = 100) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item)[:300] for item in value[:limit] if str(item).strip()]


def _assembly_definition(path: str, text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    return {
        "path": path[:1000],
        "name": str(value.get("name") or "")[:300],
        "references": _string_list(value.get("references")),
        "include_platforms": _string_list(value.get("includePlatforms")),
        "exclude_platforms": _string_list(value.get("excludePlatforms")),
        "define_constraints": _string_list(value.get("defineConstraints")),
        "auto_referenced": value.get("autoReferenced"),
    }


def _package_manifest(path: str, text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict) or not str(value.get("name") or "").strip():
        return None
    dependencies = value.get("dependencies")
    return {
        "path": path[:1000],
        "name": str(value.get("name") or "")[:300],
        "display_name": str(value.get("displayName") or "")[:500],
        "version": str(value.get("version") or "")[:100],
        "unity": str(value.get("unity") or "")[:100],
        "dependencies": (
            {
                str(key)[:300]: str(item)[:100]
                for key, item in list(dependencies.items())[:100]
            }
            if isinstance(dependencies, dict)
            else {}
        ),
    }


def _binary_architecture(value: bytes) -> str:
    if len(value) >= 64 and value[:2] == b"MZ":
        offset = struct.unpack_from("<I", value, 0x3C)[0]
        if offset + 6 <= len(value) and value[offset:offset + 4] == b"PE\x00\x00":
            machine = struct.unpack_from("<H", value, offset + 4)[0]
            return {
                0x014C: "x86",
                0x8664: "x86_64",
                0x01C0: "arm",
                0xAA64: "arm64",
            }.get(machine, f"pe:0x{machine:04x}")
    if len(value) >= 20 and value[:4] == b"\x7fELF":
        endian = "<" if value[5:6] == b"\x01" else ">"
        machine = struct.unpack_from(endian + "H", value, 18)[0]
        return {
            3: "x86",
            40: "arm",
            62: "x86_64",
            183: "arm64",
        }.get(machine, f"elf:{machine}")
    return "unknown"


def _platform_hints(path: str) -> set[str]:
    normalized = "/" + path.replace("\\", "/").casefold() + "/"
    result: set[str] = set()
    markers = {
        "windows": ("/windows/", "/win32/", "/win64/", "/x86/", "/x86_64/"),
        "macos": ("/macos/", "/osx/", ".bundle/", ".dylib/"),
        "linux": ("/linux/", ".so/"),
        "android": ("/android/", "/armeabi", "/arm64-v8a/"),
        "ios": ("/ios/", "/iphone/", ".framework/", ".a/"),
        "webgl": ("/webgl/",),
    }
    for platform, values in markers.items():
        if any(marker in normalized for marker in values):
            result.add(platform)
    return result


def inspect_unitypackage(path: str | Path) -> UnityPackageInspection:
    target = Path(path).expanduser().resolve()
    if not target.is_file() or target.suffix.casefold() != ".unitypackage":
        raise UnityPackageInspectionError("Select an existing .unitypackage file.")

    pathname_members: dict[str, tarfile.TarInfo] = {}
    asset_members: dict[str, tarfile.TarInfo] = {}
    guids: set[str] = set()
    member_count = 0
    total_size = 0
    rejected_types = 0
    truncated = False

    try:
        archive = tarfile.open(target, mode="r:*")
    except (OSError, tarfile.TarError) as exc:
        raise UnityPackageInspectionError("Unity package is not a readable tar archive.") from exc

    with archive:
        try:
            for member in archive:
                member_count += 1
                if member_count > MAX_MEMBERS:
                    truncated = True
                    break
                member_path = _safe_member_name(member.name)
                if member.issym() or member.islnk() or member.isdev():
                    rejected_types += 1
                    continue
                if member.size < 0:
                    raise UnityPackageInspectionError("Unity package contains an invalid size.")
                total_size += int(member.size)
                if total_size > MAX_TOTAL_UNCOMPRESSED_BYTES:
                    raise UnityPackageInspectionError(
                        "Unity package exceeds the uncompressed safety limit."
                    )
                if len(member_path.parts) < 2 or not member.isfile():
                    continue
                guid = member_path.parts[0]
                if not re.fullmatch(r"[0-9a-fA-F]{32}", guid):
                    continue
                guids.add(guid.casefold())
                leaf = member_path.parts[-1].casefold()
                if leaf == "pathname":
                    pathname_members[guid.casefold()] = member
                elif leaf == "asset":
                    asset_members[guid.casefold()] = member

            logical_paths: dict[str, str] = {}
            for guid, member in pathname_members.items():
                raw = _read_member(archive, member, maximum=MAX_PATHNAME_BYTES)
                # Asset Store packages produced by older Unity releases append
                # a literal ``\n00`` record terminator to pathname payloads.
                # Treat it as archive framing, not as part of the file suffix.
                logical = _decode_text(raw).replace("\\", "/").rstrip("\x00")
                logical = re.sub(r"\r?\n00\Z", "", logical).strip("\x00\r\n ")
                if not logical:
                    continue
                safe = _safe_member_name(logical)
                logical_paths[guid] = safe.as_posix()

            extensions: Counter[str] = Counter()
            groups: Counter[str] = Counter()
            assembly_definitions: list[dict[str, Any]] = []
            package_manifests: list[dict[str, Any]] = []
            native_plugins: list[dict[str, Any]] = []
            platforms: set[str] = set()
            markers = {
                "input_system": False,
                "legacy_input": False,
                "urp": False,
                "hdrp": False,
                "addressables": False,
                "cinemachine": False,
                "unity_editor_outside_editor_folder": False,
            }
            analyzed_files = 0
            analyzed_bytes = 0
            for guid, logical_path in sorted(logical_paths.items(), key=lambda item: item[1].casefold()):
                suffix = Path(logical_path).suffix.casefold()
                extensions[suffix or "(none)"] += 1
                for group, values in _CONTENT_GROUPS:
                    if suffix in values:
                        groups[group] += 1
                platforms.update(_platform_hints(logical_path))
                member = asset_members.get(guid)
                if member is None or suffix not in _TEXT_EXTENSIONS | _NATIVE_EXTENSIONS:
                    continue
                if analyzed_files >= MAX_ANALYZED_FILES:
                    truncated = True
                    continue
                if member.size > MAX_ANALYZED_FILE_BYTES:
                    continue
                if analyzed_bytes + member.size > MAX_ANALYZED_TOTAL_BYTES:
                    truncated = True
                    continue
                content = _read_member(
                    archive, member, maximum=MAX_ANALYZED_FILE_BYTES
                )
                analyzed_files += 1
                analyzed_bytes += len(content)
                if suffix in _NATIVE_EXTENSIONS:
                    native_plugins.append({
                        "path": logical_path[:1000],
                        "architecture": _binary_architecture(content[:4096]),
                        "platform_hints": sorted(_platform_hints(logical_path)),
                    })
                    continue
                text = _decode_text(content)
                lowered = text.casefold()
                markers["input_system"] |= (
                    "unityengine.inputsystem" in lowered
                    or "com.unity.inputsystem" in lowered
                )
                markers["legacy_input"] |= bool(
                    re.search(r"\binput\.(getaxis|getbutton|getkey|mouseposition)\b", lowered)
                )
                markers["urp"] |= (
                    "rendering.universal" in lowered
                    or "com.unity.render-pipelines.universal" in lowered
                )
                markers["hdrp"] |= (
                    "rendering.highdefinition" in lowered
                    or "com.unity.render-pipelines.high-definition" in lowered
                )
                markers["addressables"] |= "unityengine.addressableassets" in lowered
                markers["cinemachine"] |= "cinemachine" in lowered
                if suffix == ".cs" and "unityeditor" in lowered:
                    parts = {part.casefold() for part in PurePosixPath(logical_path).parts}
                    markers["unity_editor_outside_editor_folder"] |= "editor" not in parts
                if suffix == ".asmdef":
                    definition = _assembly_definition(logical_path, text)
                    if definition is not None:
                        assembly_definitions.append(definition)
                if PurePosixPath(logical_path).name.casefold() == "package.json":
                    manifest = _package_manifest(logical_path, text)
                    if manifest is not None:
                        package_manifests.append(manifest)
        except (OSError, EOFError, tarfile.TarError) as exc:
            raise UnityPackageInspectionError("Unity package archive is damaged.") from exc

    risks: list[str] = []
    if rejected_types:
        risks.append("リンクまたは特殊ファイルを無視しました")
    if truncated:
        risks.append("安全上限に達したため検査結果は部分的です")
    if native_plugins:
        risks.append("ネイティブプラグインの対象OS・CPUを確認してください")
    if markers["unity_editor_outside_editor_folder"]:
        risks.append("Editor API参照コードがEditorフォルダー外にあります")
    version = next(
        (
            str(item.get("version") or "")
            for item in package_manifests
            if str(item.get("version") or "")
        ),
        "",
    )
    summary: dict[str, Any] = {
        "schema": "stackforge.unitypackage-inspection.v1",
        "state": "partial" if truncated else "inspected",
        "archive_size_bytes": int(target.stat().st_size),
        "uncompressed_size_bytes": total_size,
        "archive_members": member_count,
        "asset_guids": len(guids),
        "logical_files": len(logical_paths),
        "version": version or None,
        "extensions": dict(extensions.most_common(100)),
        "content": dict(groups),
        "assembly_definitions": assembly_definitions[:200],
        "package_manifests": package_manifests[:20],
        "native_plugins": native_plugins[:200],
        "platform_hints": sorted(platforms),
        "code_markers": markers,
        "risks": risks,
    }
    return UnityPackageInspection(
        summary=summary,
        guids=tuple(sorted(guids)),
    )


__all__ = [
    "UnityPackageInspection",
    "UnityPackageInspectionError",
    "inspect_unitypackage",
]
