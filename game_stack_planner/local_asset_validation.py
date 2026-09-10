"""Project-specific validation for cached legacy Asset Store packages."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any

from .asset_store_cache import discover_cache_roots
from .compatibility import assess_candidate_compatibility
from .models import Candidate, ProjectSnapshot
from .unitypackage_inspection import (
    UnityPackageInspection,
    UnityPackageInspectionError,
    inspect_unitypackage,
)


MAX_PROJECT_META_FILES = 250_000
MAX_META_BYTES = 1024 * 1024
MAX_COMPILER_DIAGNOSTICS = 100


class LocalAssetValidationError(ValueError):
    """A candidate cannot be validated against the selected local project."""


def resolve_cached_package_path(
    cache_candidate: Candidate,
    *,
    explicit_cache_root: str | Path | None = None,
) -> Path:
    relative_text = str(cache_candidate.metadata.get("cache_relative_path") or "")
    relative = Path(relative_text)
    if not relative_text or relative.is_absolute() or ".." in relative.parts:
        raise LocalAssetValidationError("Cached package has an invalid relative path.")
    roots = discover_cache_roots(explicit_cache_root)
    expected_kind = str(cache_candidate.metadata.get("cache_root_kind") or "")
    for root in roots:
        if explicit_cache_root is None and expected_kind and root.kind != expected_kind:
            continue
        base = root.path.resolve(strict=False)
        target = (base / relative).resolve(strict=False)
        if base not in target.parents or not target.is_file():
            continue
        return target
    raise LocalAssetValidationError(
        "Cached .unitypackage could not be resolved. Rescan the cache or provide its root."
    )


def _project_asset_guids(project_root: Path) -> tuple[set[str], bool]:
    assets = project_root / "Assets"
    if not assets.is_dir():
        return set(), False
    result: set[str] = set()
    count = 0
    truncated = False
    for directory, dirnames, filenames in os.walk(assets, followlinks=False):
        current = Path(directory)
        dirnames[:] = sorted(
            name for name in dirnames
            if not (current / name).is_symlink()
        )
        for filename in sorted(filenames, key=str.casefold):
            if not filename.casefold().endswith(".meta"):
                continue
            count += 1
            if count > MAX_PROJECT_META_FILES:
                truncated = True
                return result, truncated
            path = current / filename
            try:
                if path.is_symlink() or path.stat().st_size > MAX_META_BYTES:
                    continue
                with path.open("rb") as stream:
                    head = stream.read(4096)
            except OSError:
                continue
            match = re.search(rb"(?m)^guid:\s*([0-9a-fA-F]{32})\s*$", head)
            if match:
                result.add(match.group(1).decode("ascii").casefold())
    return result, truncated


def _import_detection(
    inspection: UnityPackageInspection,
    project_root: Path,
) -> dict[str, Any]:
    project_guids, truncated = _project_asset_guids(project_root)
    package_guids = set(inspection.guids)
    matching = package_guids & project_guids
    ratio = 0.0 if not package_guids else len(matching) / len(package_guids)
    if not package_guids:
        state = "unknown"
    elif ratio >= 0.9:
        state = "imported"
    elif matching:
        state = "partial"
    elif truncated:
        state = "unknown"
    else:
        state = "not_detected"
    return {
        "state": state,
        "package_guids": len(package_guids),
        "matching_guids": len(matching),
        "match_ratio": round(ratio, 6),
        "project_scan_truncated": truncated,
    }


def _static_checks(
    candidate: Candidate,
    inspection: UnityPackageInspection,
    project: ProjectSnapshot,
    *,
    platform: str,
) -> dict[str, Any]:
    summary = inspection.summary
    markers = summary.get("code_markers")
    markers = markers if isinstance(markers, dict) else {}
    failures: list[str] = []
    warnings: list[str] = list(summary.get("risks") or [])[:20]
    passed: list[str] = []

    structured = assess_candidate_compatibility(
        candidate,
        project=project,
        platform=platform,
    )
    if structured["status"] == "incompatible":
        failures.extend(str(value) for value in structured["reasons"])
    elif structured["status"] == "compatible":
        passed.extend(str(value) for value in structured["reasons"])
    else:
        warnings.extend(str(value) for value in structured["reasons"])

    pipeline = project.render_pipeline.casefold()
    if markers.get("urp") and not markers.get("hdrp") and pipeline == "hdrp":
        failures.append("ローカルコードはURP参照のみですが、プロジェクトはHDRPです")
    if markers.get("hdrp") and not markers.get("urp") and pipeline == "urp":
        failures.append("ローカルコードはHDRP参照のみですが、プロジェクトはURPです")
    if markers.get("input_system") and project.input_backend == "legacy":
        failures.append("Input System参照がありますが、プロジェクトはLegacy Inputのみです")
    if markers.get("legacy_input") and project.input_backend == "input-system":
        warnings.append("Legacy Input API参照がありますが、プロジェクトはInput Systemのみです")

    installed = {item.external_id: item.version for item in project.packages}
    missing_packages: list[dict[str, str]] = []
    version_mismatches: list[dict[str, str]] = []
    for manifest in summary.get("package_manifests", []):
        if not isinstance(manifest, dict):
            continue
        dependencies = manifest.get("dependencies")
        if not isinstance(dependencies, dict):
            continue
        for name, required in dependencies.items():
            current = installed.get(str(name))
            if current is None:
                missing_packages.append({"name": str(name), "required": str(required)})
            elif str(required) and current != str(required):
                version_mismatches.append({
                    "name": str(name),
                    "required": str(required),
                    "installed": str(current),
                })
    if missing_packages:
        failures.append(f"必要UPM packageが{len(missing_packages)}件不足")
    if version_mismatches:
        warnings.append(f"UPM packageの版差異が{len(version_mismatches)}件あります")
    if not failures:
        passed.append("静的検査で明示的な非互換は見つかりませんでした")
    return {
        "status": "incompatible" if failures else "warning" if warnings else "compatible",
        "structured_compatibility": structured,
        "passed": list(dict.fromkeys(passed))[:30],
        "warnings": list(dict.fromkeys(warnings))[:30],
        "failures": list(dict.fromkeys(failures))[:30],
        "missing_packages": missing_packages[:100],
        "version_mismatches": version_mismatches[:100],
    }


def find_unity_editor(unity_version: str | None) -> Path | None:
    configured = str(os.getenv("UNITY_EDITOR_PATH") or "").strip()
    if configured:
        path = Path(configured).expanduser()
        return path.resolve() if path.is_file() else None
    version = str(unity_version or "").strip()
    if not version:
        return None
    if os.name == "nt":
        candidate = (
            Path(os.environ.get("ProgramFiles", "C:/Program Files"))
            / "Unity" / "Hub" / "Editor" / version / "Editor" / "Unity.exe"
        )
    elif sys_platform() == "darwin":
        candidate = Path("/Applications/Unity/Hub/Editor") / version / "Unity.app/Contents/MacOS/Unity"
    else:
        candidate = Path.home() / "Unity/Hub/Editor" / version / "Editor/Unity"
    return candidate.resolve() if candidate.is_file() else None


def sys_platform() -> str:
    import sys

    return sys.platform


def _run_unity(command: list[str], *, timeout_seconds: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(60, min(int(timeout_seconds), 1800)),
            check=False,
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LocalAssetValidationError("Unity staging validation could not run.") from exc


def _compiler_diagnostics(log_text: str) -> list[str]:
    result: list[str] = []
    for line in str(log_text or "").splitlines():
        stripped = line.strip()
        if (
            re.search(r"\berror\s+CS\d+", stripped, re.IGNORECASE)
            or "scripts have compiler errors" in stripped.casefold()
            or "compilation failed" in stripped.casefold()
        ):
            safe = re.sub(r"[A-Za-z]:[\\/][^:(]+", "<staging-path>", stripped)
            result.append(safe[:1000])
            if len(result) >= MAX_COMPILER_DIAGNOSTICS:
                break
    return list(dict.fromkeys(result))


def _has_successful_batchmode_exit(log_text: str) -> bool:
    normalized = str(log_text or "").casefold()
    return (
        "exiting batchmode successfully" in normalized
        or "application will terminate with return code 0" in normalized
    )


def run_staging_compile_validation(
    package_path: Path,
    project: ProjectSnapshot,
    *,
    timeout_seconds: int = 900,
) -> dict[str, Any]:
    editor = find_unity_editor(project.unity_version)
    if editor is None:
        return {
            "state": "unavailable",
            "reason": "プロジェクトと同じUnity Editorが見つかりません",
            "unity_version": project.unity_version,
        }
    with tempfile.TemporaryDirectory(prefix="stackforge-unity-validation-") as directory:
        staging = Path(directory) / "Project"
        create = _run_unity([
            str(editor), "-batchmode", "-nographics", "-accept-apiupdate", "-quit",
            "-createProject", str(staging), "-logFile", "-",
        ], timeout_seconds=timeout_seconds)
        if create.returncode != 0:
            diagnostics = _compiler_diagnostics(create.stdout)
            return {
                "state": "failed",
                "phase": "create_project",
                "exit_code": create.returncode,
                "diagnostics": diagnostics,
            }

        source_manifest = Path(project.path) / "Packages" / "manifest.json"
        target_manifest = staging / "Packages" / "manifest.json"
        if source_manifest.is_file() and source_manifest.stat().st_size <= 4 * 1024 * 1024:
            shutil.copyfile(source_manifest, target_manifest)
        validation = _run_unity([
            str(editor), "-batchmode", "-nographics", "-accept-apiupdate", "-quit",
            "-projectPath", str(staging),
            "-importPackage", str(package_path),
            "-logFile", "-",
        ], timeout_seconds=timeout_seconds)
        diagnostics = _compiler_diagnostics(validation.stdout)
        # Unity's API Updater can log compiler errors for the pre-update source,
        # recompile successfully, and still exit cleanly. Those earlier lines are
        # historical diagnostics rather than the final compiler state.
        resolved_diagnostics: list[str] = []
        if (
            validation.returncode == 0
            and diagnostics
            and _has_successful_batchmode_exit(validation.stdout)
        ):
            resolved_diagnostics = diagnostics
            diagnostics = []
        passed = validation.returncode == 0 and not diagnostics
        return {
            "state": "passed" if passed else "failed",
            "phase": "import_and_compile",
            "unity_version": project.unity_version,
            "exit_code": validation.returncode,
            "diagnostics": diagnostics,
            "resolved_diagnostics": resolved_diagnostics,
            "limitations": (
                "一時的な空プロジェクトへ対象manifestとunitypackageを入れた検査です。"
                "本番シーンの描画・操作・実行時挙動までは保証しません。"
            ),
        }


def run_project_compile_validation(
    project: ProjectSnapshot,
    *,
    timeout_seconds: int = 900,
) -> dict[str, Any]:
    editor = find_unity_editor(project.unity_version)
    project_root = Path(project.path).resolve()
    if editor is None:
        return {
            "state": "unavailable",
            "reason": "プロジェクトと同じUnity Editorが見つかりません",
            "unity_version": project.unity_version,
        }
    if not (project_root / "Assets").is_dir():
        return {
            "state": "unavailable",
            "reason": "対象UnityプロジェクトのAssetsフォルダーが見つかりません",
            "unity_version": project.unity_version,
        }
    validation = _run_unity([
        str(editor), "-batchmode", "-nographics", "-accept-apiupdate", "-quit",
        "-projectPath", str(project_root), "-logFile", "-",
    ], timeout_seconds=timeout_seconds)
    diagnostics = _compiler_diagnostics(validation.stdout)
    resolved_diagnostics: list[str] = []
    if (
        validation.returncode == 0
        and diagnostics
        and _has_successful_batchmode_exit(validation.stdout)
    ):
        resolved_diagnostics = diagnostics
        diagnostics = []
    passed = validation.returncode == 0 and not diagnostics
    return {
        "state": "passed" if passed else "failed",
        "phase": "installed_project_compile",
        "unity_version": project.unity_version,
        "exit_code": validation.returncode,
        "diagnostics": diagnostics,
        "resolved_diagnostics": resolved_diagnostics,
        "limitations": (
            "実プロジェクトのEditorコンパイル検査です。"
            "シーンの描画・操作・実行時挙動までは保証しません。"
        ),
    }


def validate_cached_asset(
    candidate: Candidate,
    cache_candidate: Candidate,
    project: ProjectSnapshot,
    *,
    platform: str,
    explicit_cache_root: str | Path | None = None,
    compile_test: bool = False,
) -> dict[str, Any]:
    package_path = resolve_cached_package_path(
        cache_candidate,
        explicit_cache_root=explicit_cache_root,
    )
    try:
        inspection = inspect_unitypackage(package_path)
    except UnityPackageInspectionError as exc:
        raise LocalAssetValidationError(str(exc)) from exc
    project_root = Path(project.path).resolve()
    current_metadata = dict(candidate.metadata)
    current_metadata.pop("local_validation", None)
    current_metadata.pop("local_validations", None)
    current_candidate = replace(candidate, metadata=current_metadata)
    static = _static_checks(
        current_candidate,
        inspection,
        project,
        platform=platform,
    )
    imported = _import_detection(inspection, project_root)
    compile_result = (
        (
            run_project_compile_validation(project)
            if imported["state"] == "imported"
            else run_staging_compile_validation(package_path, project)
        )
        if compile_test
        else {"state": "not_run", "reason": "明示的なコンパイル検査が未実行"}
    )
    overall = static["status"]
    if compile_result["state"] == "failed":
        overall = "incompatible"
    elif compile_result["state"] == "passed" and overall == "compatible":
        overall = "validated"
    return {
        "schema": "stackforge.local-asset-validation.v1",
        "validated_at": datetime.now(timezone.utc).isoformat(),
        "project": {
            "fingerprint": sha256(str(project_root).encode("utf-8")).hexdigest()[:24],
            "name": project.name,
            "unity_version": project.unity_version,
            "render_pipeline": project.render_pipeline,
            "input_backend": project.input_backend,
            "platform": platform,
        },
        "overall_status": overall,
        "cache_candidate_id": cache_candidate.id,
        "package_inspection": inspection.summary,
        "import_detection": imported,
        "static_checks": static,
        "compile_test": compile_result,
    }


__all__ = [
    "LocalAssetValidationError",
    "find_unity_editor",
    "resolve_cached_package_path",
    "run_project_compile_validation",
    "run_staging_compile_validation",
    "validate_cached_asset",
]
