"""Deterministic, local-only planning for safe Unity package installation.

The module deliberately has no network or subprocess integration.  A caller must
resolve a stored :class:`~game_stack_planner.models.Candidate` by ID before calling
``prepare_install_plan``; model-generated URLs, commands, or paths are never
accepted as executable input here.

Only an exact-version OpenUPM dependency is executable in the MVP.  Other sources
produce explicit no-op, inspection, manual, or blocked plans.  Execution edits
``Packages/manifest.json`` atomically and never edits ``packages-lock.json``.
"""

from __future__ import annotations

import copy
import difflib
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .models import Candidate


OPENUPM_REGISTRY_URL = "https://package.openupm.com"
OPENUPM_REGISTRY_NAME = "package.openupm.com"

STATUS_READY = "ready"
STATUS_ALREADY_PRESENT = "already_present"
STATUS_NEEDS_INSPECTION = "needs_inspection"
STATUS_MANUAL = "manual"
STATUS_BLOCKED = "blocked"

KIND_UPM_REGISTRY_ADD = "upm_registry_add"
KIND_NOOP = "noop"
KIND_INSPECT_REPOSITORY = "inspect_repository"
KIND_MANUAL_ASSET_STORE = "manual_asset_store"
KIND_MANUAL_UNITYPACKAGE = "manual_unitypackage"
KIND_UNSUPPORTED = "unsupported"

_MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_PACKAGE_NAME_RE = re.compile(
    r"^(?:[a-z0-9]+(?:-[a-z0-9]+)*\.)+"
    r"[a-z0-9]+(?:-[a-z0-9]+)*$"
)
_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\."
    r"(0|[1-9]\d*)\."
    r"(0|[1-9]\d*)"
    r"(?:-(?:"
    r"(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*"
    r"))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)

# Conservative allow-list.  Expressions (for example ``MIT OR GPL-3.0``) are
# intentionally not accepted: an executor must not interpret license algebra.
_PERMISSIVE_LICENSE_ALIASES = {
    "0bsd": "0BSD",
    "apache-2.0": "Apache-2.0",
    "apache 2.0": "Apache-2.0",
    "apache license 2.0": "Apache-2.0",
    "bsd-2-clause": "BSD-2-Clause",
    "bsd 2-clause": "BSD-2-Clause",
    "bsd-3-clause": "BSD-3-Clause",
    "bsd 3-clause": "BSD-3-Clause",
    "bsl-1.0": "BSL-1.0",
    "cc0-1.0": "CC0-1.0",
    "isc": "ISC",
    "mit": "MIT",
    "mit license": "MIT",
    "unlicense": "Unlicense",
    "the unlicense": "Unlicense",
    "zlib": "Zlib",
}


class InstallerError(RuntimeError):
    """Base error with a stable machine-readable code."""

    code = "installer_error"

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": str(self)}


class ProjectValidationError(InstallerError):
    code = "invalid_unity_project"


class ManifestFormatError(InstallerError):
    code = "invalid_package_manifest"


class PlanNotExecutableError(InstallerError):
    code = "plan_not_executable"


class ManifestChangedError(InstallerError):
    code = "manifest_changed"


class PlanIntegrityError(InstallerError):
    code = "plan_integrity_error"


class RollbackConflictError(InstallerError):
    code = "rollback_conflict"


@dataclass(frozen=True, slots=True)
class InstallPlan:
    """A deterministic installation decision and, when safe, exact file patch."""

    schema_version: int
    status: str
    kind: str
    candidate_id: str
    candidate_source: str
    candidate_title: str
    candidate_url: str
    project_path: str
    unity_version: str | None
    package_name: str | None
    package_version: str | None
    license_id: str | None
    registry_url: str | None
    requires_approval: bool
    executable: bool
    reason: str
    manifest_path: str
    manifest_before_sha256: str
    manifest_after_sha256: str
    manifest_before: str
    manifest_after: str
    manifest_diff: str
    risks: tuple[str, ...]
    verification: tuple[str, ...]
    rollback_limit: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return the complete plan for trusted local persistence/execution."""

        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "kind": self.kind,
            "candidate_id": self.candidate_id,
            "candidate_source": self.candidate_source,
            "candidate_title": self.candidate_title,
            "candidate_url": self.candidate_url,
            "project_path": self.project_path,
            "unity_version": self.unity_version,
            "package_name": self.package_name,
            "package_version": self.package_version,
            "license_id": self.license_id,
            "registry_url": self.registry_url,
            "requires_approval": self.requires_approval,
            "executable": self.executable,
            "reason": self.reason,
            "manifest_path": self.manifest_path,
            "manifest_before_sha256": self.manifest_before_sha256,
            "manifest_after_sha256": self.manifest_after_sha256,
            "manifest_before": self.manifest_before,
            "manifest_after": self.manifest_after,
            "manifest_diff": self.manifest_diff,
            "risks": list(self.risks),
            "verification": list(self.verification),
            "rollback_limit": list(self.rollback_limit),
        }

    def to_public_dict(self) -> dict[str, Any]:
        """Return UI/API fields without embedding complete manifest contents."""

        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "kind": self.kind,
            "candidate": {
                "id": self.candidate_id,
                "source": self.candidate_source,
                "title": self.candidate_title,
                "url": self.candidate_url,
                "license": self.license_id,
            },
            "project": {
                "path": self.project_path,
                "unity_version": self.unity_version,
            },
            "package": {
                "name": self.package_name,
                "version": self.package_version,
                "registry": self.registry_url,
                "scope": self.package_name if self.registry_url else None,
            },
            "manifest": {
                "path": self.manifest_path,
                "before_sha256": self.manifest_before_sha256,
                "after_sha256": self.manifest_after_sha256,
                "changed": (
                    self.manifest_before_sha256 != self.manifest_after_sha256
                ),
                "diff": self.manifest_diff,
            },
            "requires_approval": self.requires_approval,
            "executable": self.executable,
            "reason": self.reason,
            "risks": list(self.risks),
            "verification": list(self.verification),
            "rollback_limit": list(self.rollback_limit),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> InstallPlan:
        """Rehydrate a plan stored by a trusted local repository."""

        try:
            return cls(
                schema_version=int(value["schema_version"]),
                status=str(value["status"]),
                kind=str(value["kind"]),
                candidate_id=str(value["candidate_id"]),
                candidate_source=str(value["candidate_source"]),
                candidate_title=str(value["candidate_title"]),
                candidate_url=str(value.get("candidate_url") or ""),
                project_path=str(value["project_path"]),
                unity_version=_optional_string(value.get("unity_version")),
                package_name=_optional_string(value.get("package_name")),
                package_version=_optional_string(value.get("package_version")),
                license_id=_optional_string(value.get("license_id")),
                registry_url=_optional_string(value.get("registry_url")),
                requires_approval=bool(value["requires_approval"]),
                executable=bool(value["executable"]),
                reason=str(value["reason"]),
                manifest_path=str(value["manifest_path"]),
                manifest_before_sha256=str(value["manifest_before_sha256"]),
                manifest_after_sha256=str(value["manifest_after_sha256"]),
                manifest_before=str(value["manifest_before"]),
                manifest_after=str(value["manifest_after"]),
                manifest_diff=str(value.get("manifest_diff") or ""),
                risks=_string_tuple(value.get("risks")),
                verification=_string_tuple(value.get("verification")),
                rollback_limit=_string_tuple(value.get("rollback_limit")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PlanIntegrityError("Stored install plan is incomplete or invalid.") from exc


@dataclass(frozen=True, slots=True)
class InstallExecutionResult:
    status: str
    kind: str
    candidate_id: str
    project_path: str
    package_name: str
    package_version: str
    manifest_path: str
    before_sha256: str
    after_sha256: str
    rollback_available: bool
    verification: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "kind": self.kind,
            "candidate_id": self.candidate_id,
            "project_path": self.project_path,
            "package_name": self.package_name,
            "package_version": self.package_version,
            "manifest_path": self.manifest_path,
            "before_sha256": self.before_sha256,
            "after_sha256": self.after_sha256,
            "rollback_available": self.rollback_available,
            "verification": list(self.verification),
        }


@dataclass(frozen=True, slots=True)
class RollbackResult:
    status: str
    candidate_id: str
    project_path: str
    manifest_path: str
    removed_sha256: str
    restored_sha256: str
    verification: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "candidate_id": self.candidate_id,
            "project_path": self.project_path,
            "manifest_path": self.manifest_path,
            "removed_sha256": self.removed_sha256,
            "restored_sha256": self.restored_sha256,
            "verification": list(self.verification),
        }


@dataclass(frozen=True, slots=True)
class _ProjectManifest:
    root: Path
    manifest_path: Path
    unity_version: str | None
    raw: bytes
    text: str
    value: dict[str, Any]


def prepare_install_plan(
    candidate: Candidate,
    project_path: str | Path,
) -> InstallPlan:
    """Build a read-only install decision for one repository-resolved candidate."""

    project = _load_project_manifest(project_path)
    source = candidate.source.strip().casefold()
    package_name = candidate.external_id.strip() or None
    version = candidate.version.strip() if candidate.version else None
    before_hash = _sha256(project.raw)
    base = {
        "schema_version": 1,
        "candidate_id": candidate.id,
        "candidate_source": source,
        "candidate_title": candidate.title,
        "candidate_url": candidate.url,
        "project_path": str(project.root),
        "unity_version": project.unity_version,
        "package_name": package_name,
        "package_version": version,
        "license_id": _permissive_license_id(candidate.license),
        "registry_url": None,
        "manifest_path": str(project.manifest_path),
        "manifest_before_sha256": before_hash,
        "manifest_after_sha256": before_hash,
        "manifest_before": project.text,
        "manifest_after": project.text,
        "manifest_diff": "",
    }

    if source == "local":
        dependencies = _dependencies(project.value)
        if package_name and package_name in dependencies:
            return InstallPlan(
                **base,
                status=STATUS_ALREADY_PRESENT,
                kind=KIND_NOOP,
                requires_approval=False,
                executable=False,
                reason="対象パッケージはすでにこのUnityプロジェクトに存在します。",
                risks=(),
                verification=(
                    f"Packages/manifest.json に {package_name} が登録済みです。",
                ),
                rollback_limit=("変更を行わないためロールバックは不要です。",),
            )
        return InstallPlan(
            **base,
            status=STATUS_BLOCKED,
            kind=KIND_NOOP,
            requires_approval=False,
            executable=False,
            reason="local候補は別プロジェクトへ転用できる導入情報を持ちません。",
            risks=("候補の取得元と再配布条件を確認できません。",),
            verification=("導入元の正式なパッケージ識別子を確認してください。",),
            rollback_limit=("変更は行われません。",),
        )

    if source == "github":
        return InstallPlan(
            **base,
            status=STATUS_NEEDS_INSPECTION,
            kind=KIND_INSPECT_REPOSITORY,
            requires_approval=False,
            executable=False,
            reason="GitHub検索結果だけでは安全なUnity Package Manager導入情報が不足しています。",
            risks=(
                "リポジトリにはUnity Editor上で実行される任意コードが含まれ得ます。",
                "ブランチ名やタグは後から別コミットへ移動できます。",
            ),
            verification=(
                "package.jsonの場所とnameを検証する。",
                "完全なコミットSHAへ固定する。",
                "ライセンス、Unity対応版、依存関係を確認する。",
            ),
            rollback_limit=("検査段階のためプロジェクト変更は行われません。",),
        )

    if source == "asset_store":
        return InstallPlan(
            **base,
            status=STATUS_MANUAL,
            kind=KIND_MANUAL_ASSET_STORE,
            requires_approval=False,
            executable=False,
            reason="Asset Store商品の購入・ダウンロード・導入はUnityの公式UIで行う必要があります。",
            risks=(
                "価格、シート数、ライセンス、対応Unity版を人が確認する必要があります。",
                "インポート内容はPackages/manifest.json以外にも及ぶ場合があります。",
            ),
            verification=(
                "公式商品ページで所有権とライセンスを確認する。",
                "Unity Package Managerで対象プロジェクトへ導入する。",
                "導入後にコンパイル、シーン、レンダーパイプラインを確認する。",
            ),
            rollback_limit=(
                "このプランはAsset Storeの購入やダウンロードを自動化しません。",
                "インポート済みAssetsの自動ロールバックは保証できません。",
            ),
        )

    if source == "asset_store_cache":
        return InstallPlan(
            **base,
            status=STATUS_MANUAL,
            kind=KIND_MANUAL_UNITYPACKAGE,
            requires_approval=False,
            executable=False,
            reason="ローカルキャッシュは検出済みですが、所有権と内容を確認して手動インポートします。",
            risks=(
                "キャッシュの存在だけでは購入・利用権を証明できません。",
                ".unitypackageは既存Assetsを上書きする可能性があります。",
            ),
            verification=(
                "Unityアカウントの所有権とAsset Store EULAを確認する。",
                "インポート対象ファイルをUnityのダイアログで確認する。",
                "導入後にコンパイルし、変更ファイルをバージョン管理で確認する。",
            ),
            rollback_limit=(
                "Assetsへのインポートはこのmanifestロールバックの対象外です。",
                "導入前のコミットまたはプロジェクトバックアップが必要です。",
            ),
        )

    if source != "openupm":
        return InstallPlan(
            **base,
            status=STATUS_BLOCKED,
            kind=KIND_UNSUPPORTED,
            requires_approval=False,
            executable=False,
            reason=f"未対応の候補ソースです: {source or '(empty)'}",
            risks=("安全な導入方式を決定できません。",),
            verification=("候補ソースを確認してください。",),
            rollback_limit=("変更は行われません。",),
        )

    return _prepare_openupm(candidate, project, base)


def apply_install_plan(plan: InstallPlan) -> InstallExecutionResult:
    """Atomically apply a previously approved, ready OpenUPM plan.

    This function deliberately does not perform approval itself.  The caller must
    bind a one-time approval to the persisted plan before invoking it.
    """

    _validate_executable_plan(plan)
    project = _load_project_manifest(plan.project_path)
    if str(project.manifest_path) != plan.manifest_path:
        raise PlanIntegrityError("Install plan points to a different manifest path.")
    current_hash = _sha256(project.raw)
    if current_hash != plan.manifest_before_sha256:
        raise ManifestChangedError(
            "Packages/manifest.json changed after the install plan was prepared."
        )
    before_bytes = _text_bytes(plan.manifest_before)
    if _sha256(before_bytes) != plan.manifest_before_sha256:
        raise PlanIntegrityError("Install plan's original manifest hash is invalid.")
    if project.raw != before_bytes:
        raise ManifestChangedError(
            "Packages/manifest.json no longer exactly matches the prepared plan."
        )

    assert plan.package_name is not None
    assert plan.package_version is not None
    expected_value = _mutate_openupm_manifest(
        project.value,
        plan.package_name,
        plan.package_version,
    )
    expected_after = _render_manifest(expected_value)
    planned_after = _text_bytes(plan.manifest_after)
    if expected_after != planned_after:
        raise PlanIntegrityError("Install plan manifest patch is not canonical.")
    if _sha256(planned_after) != plan.manifest_after_sha256:
        raise PlanIntegrityError("Install plan's resulting manifest hash is invalid.")
    if plan.manifest_after_sha256 == plan.manifest_before_sha256:
        raise PlanIntegrityError("Executable install plan contains no manifest change.")

    _atomic_replace(project.manifest_path, planned_after)
    written = _read_limited(project.manifest_path, "package manifest")
    _parse_manifest(written, project.manifest_path)
    if _sha256(written) != plan.manifest_after_sha256:
        raise InstallerError("Manifest verification failed after atomic replacement.")
    return InstallExecutionResult(
        status="applied_waiting_for_unity",
        kind=plan.kind,
        candidate_id=plan.candidate_id,
        project_path=plan.project_path,
        package_name=plan.package_name,
        package_version=plan.package_version,
        manifest_path=plan.manifest_path,
        before_sha256=plan.manifest_before_sha256,
        after_sha256=plan.manifest_after_sha256,
        rollback_available=True,
        verification=(
            "Unityでプロジェクトを開き、Package Managerの解決完了を待つ。",
            f"packages-lock.jsonで {plan.package_name} の解決版を確認する。",
            "Consoleのコンパイルエラーとプロジェクトの動作を確認する。",
        ),
    )


def rollback_install_plan(plan: InstallPlan) -> RollbackResult:
    """Restore the exact pre-plan manifest only if the post-plan hash still matches."""

    _validate_executable_plan(plan)
    project = _load_project_manifest(plan.project_path)
    if str(project.manifest_path) != plan.manifest_path:
        raise PlanIntegrityError("Install plan points to a different manifest path.")
    current_hash = _sha256(project.raw)
    if current_hash != plan.manifest_after_sha256:
        raise RollbackConflictError(
            "Rollback refused because Packages/manifest.json changed after apply."
        )
    after_bytes = _text_bytes(plan.manifest_after)
    before_bytes = _text_bytes(plan.manifest_before)
    if project.raw != after_bytes or _sha256(after_bytes) != plan.manifest_after_sha256:
        raise PlanIntegrityError("Install plan's applied manifest is not intact.")
    if _sha256(before_bytes) != plan.manifest_before_sha256:
        raise PlanIntegrityError("Install plan's rollback manifest hash is invalid.")
    _parse_manifest(before_bytes, project.manifest_path)

    _atomic_replace(project.manifest_path, before_bytes)
    restored = _read_limited(project.manifest_path, "package manifest")
    _parse_manifest(restored, project.manifest_path)
    if _sha256(restored) != plan.manifest_before_sha256:
        raise InstallerError("Manifest verification failed after rollback.")
    return RollbackResult(
        status="rolled_back_waiting_for_unity",
        candidate_id=plan.candidate_id,
        project_path=plan.project_path,
        manifest_path=plan.manifest_path,
        removed_sha256=plan.manifest_after_sha256,
        restored_sha256=plan.manifest_before_sha256,
        verification=(
            "Unityでプロジェクトを開き、Package Managerにlock fileを再解決させる。",
            "Consoleとプロジェクト動作を再確認する。",
        ),
    )


def is_valid_package_name(value: str) -> bool:
    """Return whether *value* is a conservative Unity package identifier."""

    return 3 <= len(value) <= 214 and bool(_PACKAGE_NAME_RE.fullmatch(value))


def is_exact_semver(value: str) -> bool:
    """Return whether *value* is one exact SemVer 2.0.0 version."""

    return len(value) <= 128 and bool(_SEMVER_RE.fullmatch(value))


def permissive_license_id(value: str | None) -> str | None:
    """Return a canonical allow-listed license ID, otherwise ``None``."""

    return _permissive_license_id(value)


def _prepare_openupm(
    candidate: Candidate,
    project: _ProjectManifest,
    base: dict[str, Any],
) -> InstallPlan:
    package_name = base["package_name"]
    version = base["package_version"]
    license_id = base["license_id"]
    if not package_name or not is_valid_package_name(package_name):
        return _blocked_openupm(
            base,
            "OpenUPM候補のpackage nameが安全なUnity識別子ではありません。",
            "候補データのpackage nameを確認してください。",
        )
    if not version or not is_exact_semver(version):
        return _blocked_openupm(
            base,
            "OpenUPM候補がSemVerの完全な版へ固定されていません。",
            "1.2.3のような完全な版を選んでください。",
        )
    if not license_id:
        return _blocked_openupm(
            base,
            "許容ライセンスを機械的に確認できないため自動導入できません。",
            "候補の正確なSPDXライセンスを人が確認してください。",
        )

    dependencies = _dependencies(project.value)
    if package_name in dependencies:
        current = dependencies[package_name]
        if current == version:
            return InstallPlan(
                **{**base, "registry_url": OPENUPM_REGISTRY_URL},
                status=STATUS_ALREADY_PRESENT,
                kind=KIND_NOOP,
                requires_approval=False,
                executable=False,
                reason=f"{package_name}@{version} はすでに導入済みです。",
                risks=(),
                verification=(
                    f"Packages/manifest.json に {package_name}@{version} が登録済みです。",
                ),
                rollback_limit=("変更を行わないためロールバックは不要です。",),
            )
        return _blocked_openupm(
            base,
            f"{package_name} は別の指定 ({current}) で導入済みです。",
            "既存依存の互換性を確認してから、版変更として別途承認してください。",
        )

    conflict = _registry_routing_conflict(project.value, package_name)
    if conflict:
        return _blocked_openupm(
            base,
            f"{package_name} は別のscoped registry ({conflict}) にも経路設定されています。",
            "registry scopeの競合を手動で解消してください。",
        )
    try:
        after_value = _mutate_openupm_manifest(project.value, package_name, version)
    except ManifestFormatError as exc:
        return _blocked_openupm(base, str(exc), "manifestのregistry設定を確認してください。")
    after_bytes = _render_manifest(after_value)
    after_text = after_bytes.decode("utf-8")
    return InstallPlan(
        **{
            **base,
            "registry_url": OPENUPM_REGISTRY_URL,
            "manifest_after_sha256": _sha256(after_bytes),
            "manifest_after": after_text,
            "manifest_diff": _manifest_diff(project.text, after_text),
        },
        status=STATUS_READY,
        kind=KIND_UPM_REGISTRY_ADD,
        requires_approval=True,
        executable=True,
        reason=f"{package_name}@{version} を固定OpenUPM registryから追加できます。",
        risks=(
            "第三者パッケージのEditor/runtimeコードがUnity内で実行されます。",
            "Unityによる解決時にネットワークアクセスと間接依存の取得が発生します。",
            f"ライセンスは候補メタデータ上 {license_id} です。原文も確認してください。",
        ),
        verification=(
            "承認時に表示されたmanifest差分とハッシュを再確認する。",
            f"Unity解決後にpackages-lock.jsonで {package_name}@{version} を確認する。",
            "Consoleのコンパイルエラーとプロジェクト動作を確認する。",
        ),
        rollback_limit=(
            "自動ロールバックは、この適用後にmanifestが未変更の場合だけ実行できます。",
            "Packages/manifest.jsonだけを復元し、packages-lock.jsonとLibraryは直接編集しません。",
            "パッケージがAssetsやProjectSettingsへ行った副作用は元に戻せません。",
        ),
    )


def _blocked_openupm(
    base: dict[str, Any],
    reason: str,
    verification: str,
) -> InstallPlan:
    return InstallPlan(
        **{**base, "registry_url": OPENUPM_REGISTRY_URL},
        status=STATUS_BLOCKED,
        kind=KIND_UPM_REGISTRY_ADD,
        requires_approval=False,
        executable=False,
        reason=reason,
        risks=("安全な自動導入条件を満たしていません。",),
        verification=(verification,),
        rollback_limit=("変更は行われません。",),
    )


def _validate_executable_plan(plan: InstallPlan) -> None:
    if (
        plan.schema_version != 1
        or plan.status != STATUS_READY
        or plan.kind != KIND_UPM_REGISTRY_ADD
        or plan.candidate_source != "openupm"
        or not plan.executable
        or not plan.requires_approval
    ):
        raise PlanNotExecutableError("Only a ready OpenUPM plan can be applied.")
    if plan.registry_url != OPENUPM_REGISTRY_URL:
        raise PlanIntegrityError("Install plan uses an unapproved package registry.")
    if not plan.package_name or not is_valid_package_name(plan.package_name):
        raise PlanIntegrityError("Install plan contains an invalid package name.")
    if not plan.package_version or not is_exact_semver(plan.package_version):
        raise PlanIntegrityError("Install plan contains a non-exact package version.")
    if not plan.license_id or plan.license_id not in set(
        _PERMISSIVE_LICENSE_ALIASES.values()
    ):
        raise PlanIntegrityError("Install plan lacks an allow-listed license.")


def _load_project_manifest(path: str | Path) -> _ProjectManifest:
    try:
        root = Path(path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProjectValidationError("Unity project directory does not exist.") from exc
    if not root.is_dir():
        raise ProjectValidationError("Unity project path is not a directory.")
    manifest_path = root / "Packages" / "manifest.json"
    version_path = root / "ProjectSettings" / "ProjectVersion.txt"
    if manifest_path.is_symlink() or version_path.is_symlink():
        raise ProjectValidationError("Unity project control files must not be symlinks.")
    if not manifest_path.is_file() or not version_path.is_file():
        raise ProjectValidationError(
            "Select a Unity project containing Packages/manifest.json and "
            "ProjectSettings/ProjectVersion.txt."
        )
    raw = _read_limited(manifest_path, "package manifest")
    value = _parse_manifest(raw, manifest_path)
    text = _decode_utf8(raw, manifest_path)
    version_raw = _read_limited(version_path, "Unity project version")
    version_text = _decode_utf8(version_raw, version_path)
    match = re.search(r"(?m)^m_EditorVersion:\s*([^\r\n]+)", version_text)
    unity_version = match.group(1).strip() if match else None
    return _ProjectManifest(
        root=root,
        manifest_path=manifest_path,
        unity_version=unity_version,
        raw=raw,
        text=text,
        value=value,
    )


def _read_limited(path: Path, description: str) -> bytes:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ProjectValidationError(f"Could not read {description}: {path}") from exc
    if size > _MAX_MANIFEST_BYTES:
        raise ProjectValidationError(f"{description.capitalize()} is too large: {path}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ProjectValidationError(f"Could not read {description}: {path}") from exc


def _decode_utf8(raw: bytes, path: Path) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ManifestFormatError(f"Unity metadata is not valid UTF-8: {path}") from exc


def _parse_manifest(raw: bytes, path: Path) -> dict[str, Any]:
    text = _decode_utf8(raw, path)
    if text.startswith("\ufeff"):
        text = text[1:]

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ManifestFormatError(
                    f"Duplicate JSON key in Unity package manifest: {key}"
                )
            result[key] = value
        return result

    def reject_constant(value: str) -> Any:
        raise ManifestFormatError(
            f"Non-standard JSON constant in Unity package manifest: {value}"
        )

    try:
        value = json.loads(
            text,
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except ManifestFormatError:
        raise
    except json.JSONDecodeError as exc:
        raise ManifestFormatError(f"Invalid Unity package manifest: {path}") from exc
    if not isinstance(value, dict):
        raise ManifestFormatError("Unity package manifest must be a JSON object.")
    _dependencies(value)
    _scoped_registries(value)
    return value


def _dependencies(manifest: Mapping[str, Any]) -> dict[str, str]:
    value = manifest.get("dependencies")
    if not isinstance(value, dict):
        raise ManifestFormatError("Unity package manifest dependencies must be an object.")
    for name, version in value.items():
        if not isinstance(name, str) or not isinstance(version, str):
            raise ManifestFormatError(
                "Unity package manifest dependencies must map strings to strings."
            )
    return value


def _scoped_registries(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    value = manifest.get("scopedRegistries", [])
    if not isinstance(value, list):
        raise ManifestFormatError("scopedRegistries must be an array.")
    for registry in value:
        if not isinstance(registry, dict):
            raise ManifestFormatError("Each scoped registry must be an object.")
        if not isinstance(registry.get("url"), str):
            raise ManifestFormatError("Each scoped registry must have a string URL.")
        scopes = registry.get("scopes")
        if not isinstance(scopes, list) or not all(
            isinstance(scope, str) and scope for scope in scopes
        ):
            raise ManifestFormatError(
                "Each scoped registry must contain non-empty string scopes."
            )
    return value


def _registry_routing_conflict(
    manifest: Mapping[str, Any],
    package_name: str,
) -> str | None:
    for registry in _scoped_registries(manifest):
        url = str(registry["url"])
        if _same_registry_url(url, OPENUPM_REGISTRY_URL):
            continue
        for scope in registry["scopes"]:
            if package_name == scope or package_name.startswith(scope + "."):
                return url
    return None


def _mutate_openupm_manifest(
    manifest: Mapping[str, Any],
    package_name: str,
    version: str,
) -> dict[str, Any]:
    if not is_valid_package_name(package_name) or not is_exact_semver(version):
        raise PlanIntegrityError("Unsafe package identity in OpenUPM mutation.")
    result = copy.deepcopy(dict(manifest))
    dependencies = _dependencies(result)
    if package_name in dependencies:
        raise PlanIntegrityError("OpenUPM mutation cannot replace an existing dependency.")

    registries = _scoped_registries(result)
    fixed_matches = [
        registry
        for registry in registries
        if _same_registry_url(str(registry["url"]), OPENUPM_REGISTRY_URL)
    ]
    if len(fixed_matches) > 1:
        raise ManifestFormatError("Multiple OpenUPM scoped registry entries are ambiguous.")
    if fixed_matches:
        registry = fixed_matches[0]
        scopes = registry["scopes"]
        if package_name not in scopes:
            scopes.append(package_name)
    else:
        if "scopedRegistries" not in result:
            result["scopedRegistries"] = registries
        registries.append(
            {
                "name": OPENUPM_REGISTRY_NAME,
                "url": OPENUPM_REGISTRY_URL,
                "scopes": [package_name],
            }
        )
    dependencies[package_name] = version
    return result


def _render_manifest(value: Mapping[str, Any]) -> bytes:
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        ) + "\n"
    except (TypeError, ValueError) as exc:
        raise ManifestFormatError("Unity package manifest is not serializable JSON.") from exc
    return text.encode("utf-8")


def _manifest_diff(before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile="Packages/manifest.json (before)",
            tofile="Packages/manifest.json (after)",
        )
    )


def _atomic_replace(path: Path, content: bytes) -> None:
    fd = -1
    temporary: Path | None = None
    try:
        fd, temporary_name = tempfile.mkstemp(
            prefix=".manifest-",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary = Path(temporary_name)
        try:
            mode = path.stat().st_mode
            os.chmod(temporary, mode)
        except OSError:
            pass
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise InstallerError(f"Could not atomically replace package manifest: {path}") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _permissive_license_id(value: str | None) -> str | None:
    if not value:
        return None
    normalized = " ".join(value.strip().casefold().split())
    return _PERMISSIVE_LICENSE_ALIASES.get(normalized)


def _same_registry_url(left: str, right: str) -> bool:
    return left.strip().rstrip("/").casefold() == right.rstrip("/").casefold()


def _text_bytes(value: str) -> bytes:
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise PlanIntegrityError("Install plan contains invalid manifest text.") from exc


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise TypeError("Expected a list of strings.")
    if not all(isinstance(item, str) for item in value):
        raise TypeError("Expected a list of strings.")
    return tuple(value)


__all__ = [
    "InstallerError",
    "ProjectValidationError",
    "ManifestFormatError",
    "PlanNotExecutableError",
    "ManifestChangedError",
    "PlanIntegrityError",
    "RollbackConflictError",
    "InstallPlan",
    "InstallExecutionResult",
    "RollbackResult",
    "OPENUPM_REGISTRY_URL",
    "STATUS_READY",
    "STATUS_ALREADY_PRESENT",
    "STATUS_NEEDS_INSPECTION",
    "STATUS_MANUAL",
    "STATUS_BLOCKED",
    "KIND_UPM_REGISTRY_ADD",
    "KIND_NOOP",
    "KIND_INSPECT_REPOSITORY",
    "KIND_MANUAL_ASSET_STORE",
    "KIND_MANUAL_UNITYPACKAGE",
    "KIND_UNSUPPORTED",
    "prepare_install_plan",
    "apply_install_plan",
    "rollback_install_plan",
    "is_valid_package_name",
    "is_exact_semver",
    "permissive_license_id",
]
