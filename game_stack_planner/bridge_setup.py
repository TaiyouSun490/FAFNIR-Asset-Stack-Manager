"""Project-scoped Unity bridge diagnosis and reviewed, recoverable setup.

Only the bundled MIT package is installable. No arbitrary URLs or shell commands.
The existing SQLite one-shot approval store is shared with other install flows.
"""
from __future__ import annotations

import base64
import difflib
import hashlib
import json
import os
import re
from pathlib import Path
import secrets
import tempfile
from datetime import timedelta
from uuid import uuid4

from .asset_store_download import default_bridge_root, _now, _parse_time, _read_json
from .install_service import InstallCoordinatorError, _token_hash
from .repository import StackRepository
from .installer import is_exact_semver

PACKAGE = "com.taiyousun.stackforge"
PACKAGE_ROOT = f"Packages/{PACKAGE}"
RECEIPT = "ProjectSettings/FafnirBridgeInstall.json"
KIND = "unity_bridge_setup"
MAX_BYTES = 4 * 1024 * 1024


def _fail(code: str, message: str) -> None:
    raise InstallCoordinatorError(409, code, message)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hashes(files: dict[str, bytes]) -> dict[str, str]:
    return {name: _sha(data) for name, data in sorted(files.items())}


def _fingerprint(files: dict[str, bytes]) -> str:
    return _sha("".join(f"{name}\0{digest}\n" for name, digest in _hashes(files).items()).encode())


def _linked(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def _read(path: Path) -> bytes | None:
    if _linked(path):
        _fail("linked_path", f"Refusing linked file: {path}")
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_size > MAX_BYTES:
        _fail("invalid_file", f"Expected a bounded regular file: {path}")
    return path.read_bytes()


def _json(data: bytes | None) -> dict:
    try:
        value = json.loads(data or b"{}")
        if isinstance(value, dict):
            return value
    except (ValueError, UnicodeError):
        pass
    _fail("invalid_json", "A project/package JSON file is invalid; no files were changed.")


def _tree(path: Path) -> dict[str, bytes]:
    if _linked(path):
        _fail("linked_path", f"Refusing linked package: {path}")
    if not path.exists():
        return {}
    if not path.is_dir():
        _fail("invalid_package", f"Package location is not a directory: {path}")
    result = {}
    for child in sorted(path.rglob("*")):
        if _linked(child):
            _fail("linked_path", f"Refusing linked package entry: {child}")
        if child.is_file():
            result[child.relative_to(path).as_posix()] = _read(child)
        if len(result) > 128:
            _fail("invalid_package", "Bridge package has too many files.")
    return result


def bundled_bridge() -> Path:
    # Wheels contain this resource. Source checkouts use the one canonical UPM
    # directory; the build hook copies it, so developers never maintain two copies.
    resource = Path(__file__).parent / "_unity_bridge"
    source = Path(__file__).resolve().parent.parent / "unity_package" / PACKAGE
    for path in (resource, source):
        if (path / "package.json").is_file():
            return path
    _fail("bridge_bundle_missing", "This Fafnir distribution lacks the Unity bridge. Reinstall a complete release.")


def _project(value: str) -> tuple[Path, dict, bytes]:
    if not value or not Path(value).expanduser().is_absolute():
        _fail("invalid_unity_project", "Choose an absolute Unity project path.")
    root = Path(value).expanduser().resolve(strict=True)
    if not (root / "Assets").is_dir():
        _fail("invalid_unity_project", "The selected folder is not a Unity project.")
    for name in ("Packages", "ProjectSettings"):
        if _linked(root / name) or not (root / name).is_dir():
            _fail("invalid_unity_project", f"Expected a real {name} directory.")
    version = _read(root / "ProjectSettings/ProjectVersion.txt")
    manifest = _read(root / "Packages/manifest.json")
    if not version or not manifest:
        _fail("invalid_unity_project", "ProjectVersion.txt and Packages/manifest.json are required.")
    parsed = _json(manifest)
    if not isinstance(parsed.get("dependencies"), dict):
        _fail("invalid_manifest", "manifest.dependencies must be an object.")
    return root, parsed, manifest


def _same_path(left: object, right: Path) -> bool:
    try:
        return bool(left) and os.path.normcase(str(Path(str(left)).resolve())) == os.path.normcase(str(right))
    except (OSError, ValueError):
        return False


def _check_unity(root: Path, info: dict) -> None:
    version = (_read(root / "ProjectSettings/ProjectVersion.txt") or b"").decode("utf-8-sig")
    actual = re.search(r"(?m)^m_EditorVersion:\s*(\d+)\.(\d+)\.", version)
    minimum = re.fullmatch(r"(\d+)\.(\d+)", str(info.get("unity", "")))
    if actual is None or minimum is None:
        _fail("unknown_unity_version", "Cannot establish the project's Unity version or the package minimum.")
    if tuple(map(int, actual.groups())) < tuple(map(int, minimum.groups())):
        _fail("unsupported_unity_version", f"The bundled bridge requires Unity {info['unity']} or later.")


class BridgeSetupCoordinator:
    def __init__(self, repository: StackRepository, *, bundle: Path | None = None,
                 bridge_root: Path | None = None):
        self.repository = repository
        self.bundle = bundle
        self.bridge_root = bridge_root or default_bridge_root()

    def _bundle(self) -> tuple[dict[str, bytes], dict]:
        files = _tree(self.bundle or bundled_bridge())
        info = _json(files.get("package.json"))
        if (info.get("name") != PACKAGE or info.get("license") != "MIT"
                or not isinstance(info.get("version"), str) or not is_exact_semver(info["version"])):
            _fail("invalid_bundle", "The bundled package identity/license/version is invalid.")
        if "LICENSE.md" not in files or "Editor/Stackforge.Editor.asmdef" not in files:
            _fail("invalid_bundle", "The bundled bridge is incomplete.")
        if _json(files["Editor/Stackforge.Editor.asmdef"]).get("includePlatforms") != ["Editor"]:
            _fail("invalid_bundle", "The bridge assembly must be Editor-only.")
        return files, info

    def diagnose(self, project_path: str) -> dict:
        root, manifest, _ = _project(project_path)
        bundled, info = self._bundle()
        _check_unity(root, info)
        installed = _tree(root / PACKAGE_ROOT)
        package = _json(installed.get("package.json"))
        receipt = _json(_read(root / RECEIPT))
        declared = manifest["dependencies"].get(PACKAGE)
        installation = ("embedded" if package.get("name") == PACKAGE else
                        "incomplete" if installed else
                        "declared_unverified" if declared else "not_installed")
        managed = bool(receipt) and receipt.get("files") == _hashes(installed)
        exact = bool(installed) and installed == bundled
        heartbeat = _read_json(self.bridge_root / "unity-download-bridge.json") or {}
        stamp = _parse_time(heartbeat.get("updatedAtUtc"))
        fresh = (heartbeat.get("schema") == "fafnir.asset-store-download-bridge.v1"
                 and stamp is not None and -5 <= (_now() - stamp).total_seconds() <= 20)
        matched = fresh and _same_path(heartbeat.get("projectPath"), root)
        loaded = bool(matched and exact and
                      heartbeat.get("bridgeContentHash") == _fingerprint(bundled))
        editor = ("connected" if matched else "other_project" if fresh and heartbeat.get("projectPath")
                  else "unknown_project" if fresh else "offline")
        compile_state = heartbeat.get("compilationState", "unknown") if matched else "unknown"
        sync = heartbeat.get("syncState", "unknown") if matched else "unknown"
        ready = (loaded and compile_state == "passed" and sync == "succeeded"
                 and heartbeat.get("accountState") != "signed_out")
        if installation == "not_installed":
            action = "prepare_bridge_install"
        elif installation in ("incomplete", "declared_unverified"):
            action = "inspect_existing_installation"
        elif not exact:
            action = "prepare_bridge_update" if managed else "review_local_package_changes"
        elif not matched:
            action = "open_target_project_or_check_editor"  # Offline does not prove stopped.
        elif not loaded:
            action = "wait_for_recompile_or_reload_bridge"
        elif compile_state == "failed":
            action = "fix_unity_compile_errors"
        elif compile_state != "passed":
            action = "wait_for_unity_or_verify_compilation"
        elif heartbeat.get("accountState") == "signed_out":
            action = "sign_in_unity"
        elif sync != "succeeded":
            action = "sync_my_assets_in_target_unity"
        else:
            action = "ready_for_reviewed_download"
        return {
            "project_path": str(root), "installation": installation,
            "declared_dependency": declared, "installed_version": package.get("version"),
            "bundled_version": info["version"], "bundled_sha256": _fingerprint(bundled),
            "matches_bundle": exact, "managed_unmodified": managed,
            "editor": editor, "heartbeat_fresh": fresh,
            "observed_project_path": heartbeat.get("projectPath"),
            "observed_project_name": heartbeat.get("projectName"),
            "heartbeat_at_utc": heartbeat.get("updatedAtUtc"),
            "bridge_version": heartbeat.get("bridgeVersion") if matched else None,
            "loaded_bundle_matches": loaded, "compilation": compile_state,
            "sync": sync, "sync_count": heartbeat.get("syncCount") if matched else None,
            "sync_success_at_utc": heartbeat.get("syncSuccessAtUtc") if matched else None,
            "account_state": heartbeat.get("accountState", "unknown") if matched else "unknown",
            "setup_verified": ready, "next_action": action,
            "limits": [
                "Offline does not distinguish a stopped, frozen, or blocked Editor.",
                "A declared dependency alone does not prove package resolution.",
                "Verification uses the live compiler error flag and this Editor's sync result.",
                "Download, import, runtime compatibility, and multi-Editor job routing are separate checks.",
            ],
        }

    def _plan(self, project_path: str) -> dict:
        root, manifest, manifest_bytes = _project(project_path)
        bundle, info = self._bundle()
        _check_unity(root, info)
        existing = _tree(root / PACKAGE_ROOT)
        receipt_bytes = _read(root / RECEIPT)
        receipt = _json(receipt_bytes)
        declared = manifest["dependencies"].get(PACKAGE)
        if existing and _json(existing.get("package.json")).get("name") != PACKAGE:
            _fail("package_conflict", "The destination contains a different or incomplete package.")
        if declared and declared != f"file:{PACKAGE}" and not existing:
            _fail("external_dependency", "An external bridge dependency exists. Review/migrate it explicitly first.")
        if existing and existing != bundle and receipt.get("files") != _hashes(existing):
            _fail("unmanaged_package", "Existing bridge differs from this release and has no matching receipt. Preserve/review local edits first.")
        before = {f"{PACKAGE_ROOT}/{name}": data for name, data in existing.items()}
        before["Packages/manifest.json"] = manifest_bytes
        before[RECEIPT] = receipt_bytes
        after = {f"{PACKAGE_ROOT}/{name}": data for name, data in bundle.items()}
        if declared == f"file:{PACKAGE}":
            after["Packages/manifest.json"] = manifest_bytes
        else:
            manifest["dependencies"][PACKAGE] = f"file:{PACKAGE}"
            after["Packages/manifest.json"] = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
        receipt_value = {"schema": "fafnir.unity-bridge-install.v1", "package": PACKAGE,
                         "version": info["version"], "bundle_sha256": _fingerprint(bundle),
                         "files": _hashes(bundle)}
        after[RECEIPT] = (json.dumps(receipt_value, sort_keys=True, indent=2) + "\n").encode()
        changes = {}
        for name in sorted(before.keys() | after.keys()):
            old, new = before.get(name), after.get(name)
            if old != new:
                changes[name] = {"before": _encode(old), "after": _encode(new)}
        return {"kind": KIND, "project_path": str(root), "version": info["version"],
                "bundle_sha256": _fingerprint(bundle), "changes": changes,
                "before_files": _hashes(existing), "before_receipt": _encode(receipt_bytes),
                "before_manifest": _encode(manifest_bytes)}

    def prepare(self, project_path: str) -> dict:
        plan = self._plan(project_path)
        plan_id = uuid4().hex
        nonce = secrets.token_urlsafe(32) if plan["changes"] else None
        expiry = (_now() + timedelta(minutes=10)).isoformat() if nonce else None
        self.repository.create_install_job(
            job_id=plan_id, candidate_id=KIND, project_path=plan["project_path"],
            status="awaiting_approval" if nonce else "already_present", plan=plan,
            approval_hash=_token_hash(purpose="bridge-apply", job_id=plan_id, token=nonce, plan=plan) if nonce else None,
            expires_at=expiry)
        return {"plan": self._public_plan(plan_id, plan), "approval_nonce": nonce,
                "expires_at_utc": expiry, "diagnosis": self.diagnose(project_path)}

    def _stored(self, plan_id: str) -> dict:
        stored = self.repository.get_install_job(plan_id)
        if not stored or stored["plan"].get("kind") != KIND:
            _fail("bridge_plan_not_found", "This is not a Unity bridge setup plan.")
        return stored

    def execute(self, plan_id: str, approval_nonce: str) -> dict:
        stored = self._stored(plan_id)
        plan = stored["plan"]
        _closed(Path(plan["project_path"]))
        if self._plan(plan["project_path"]) != plan:
            _fail("bridge_plan_changed", "Project or bundled bridge changed after review. Prepare a new plan.")
        claimed = self.repository.claim_install_job(
            job_id=plan_id, now=_now().isoformat(),
            approval_hash=_token_hash(purpose="bridge-apply", job_id=plan_id,
                                      token=approval_nonce, plan=plan))
        if not claimed:
            _fail("bridge_approval_invalid", "Approval is invalid, expired, or already consumed.")
        try:
            _perform(plan)
        except (OSError, InstallCoordinatorError) as exc:
            self.repository.finish_install_job(job_id=plan_id, expected_status="applying",
                status="failed", result={"code": getattr(exc, "code", "bridge_io_error"), "message": str(exc)})
            raise
        rollback = secrets.token_urlsafe(32)
        self.repository.finish_install_job(
            job_id=plan_id, expected_status="applying", status="applied_waiting_for_unity",
            result={"files_written": len(plan["changes"]), "setup_verified": False},
            rollback_hash=_token_hash(purpose="bridge-rollback", job_id=plan_id, token=rollback, plan=plan),
            expires_at=(_now() + timedelta(hours=24)).isoformat())
        return {**self.get(plan_id), "rollback_nonce": rollback}

    def rollback(self, job_id: str, rollback_nonce: str) -> dict:
        stored = self._stored(job_id)
        plan = stored["plan"]
        _closed(Path(plan["project_path"]))
        if not self.repository.claim_rollback_job(
                job_id=job_id, now=_now().isoformat(),
                rollback_hash=_token_hash(purpose="bridge-rollback", job_id=job_id,
                                          token=rollback_nonce, plan=plan)):
            _fail("bridge_rollback_invalid", "Rollback approval is invalid, expired, or consumed.")
        try:
            _perform(plan, reverse=True)
        except (OSError, InstallCoordinatorError) as exc:
            self.repository.finish_install_job(job_id=job_id, expected_status="rolling_back",
                status="rollback_conflict", result={"code": getattr(exc, "code", "bridge_io_error"), "message": str(exc)})
            raise
        self.repository.finish_install_job(job_id=job_id, expected_status="rolling_back",
                                          status="rolled_back", result={"files_restored": len(plan["changes"])})
        return self.get(job_id)

    def get(self, job_id: str) -> dict:
        stored = self._stored(job_id)
        try:
            diagnosis = self.diagnose(stored["project_path"])
        except (OSError, InstallCoordinatorError) as exc:
            diagnosis = {"setup_verified": False, "error": str(exc)}
        return {"job": {"id": job_id, "status": stored["status"], "result": stored["result"],
                        "plan": self._public_plan(job_id, stored["plan"])}, "diagnosis": diagnosis}

    @staticmethod
    def _public_plan(plan_id: str, plan: dict) -> dict:
        files = []
        for name, change in plan["changes"].items():
            old, new = _decode(change["before"]), _decode(change["after"])
            files.append({"path": name, "action": "add" if old is None else "delete" if new is None else "update",
                          "before_sha256": _sha(old) if old is not None else None,
                          "after_sha256": _sha(new) if new is not None else None,
                          "bytes": len(new) if new is not None else 0})
        manifest = plan["changes"].get("Packages/manifest.json")
        diff = "" if not manifest else "".join(difflib.unified_diff(
            (_decode(manifest["before"]) or b"").decode().splitlines(True),
            (_decode(manifest["after"]) or b"").decode().splitlines(True),
            fromfile="Packages/manifest.json (before)", tofile="Packages/manifest.json (after)"))
        return {"id": plan_id, "kind": KIND, "project_path": plan["project_path"],
                "package": PACKAGE, "version": plan["version"], "license": "MIT",
                "bundle_sha256": plan["bundle_sha256"], "files": files, "manifest_diff": diff,
                "requires_approval": bool(files), "effect": "Embed only the bundled, pinned Editor bridge.",
                "precondition": "Close the target Unity Editor before apply or rollback. No automatic launch/kill.",
                "rollback_limit": "Restores only listed files if unchanged; cannot undo effects of Editor code after launch."}


def _encode(value: bytes | None) -> str | None:
    return None if value is None else base64.b64encode(value).decode("ascii")


def _decode(value: str | None) -> bytes | None:
    return None if value is None else base64.b64decode(value, validate=True)


def _closed(root: Path) -> None:
    if (root / "Temp/UnityLockfile").exists():
        _fail("close_target_unity", "Close the target Unity Editor before writing. A stale lock requires manual verification; it is never deleted here.")


def _destination(root: Path, name: str) -> Path:
    parts = name.split("/")
    if any(part in ("", ".", "..") for part in parts) or "\\" in name:
        _fail("invalid_plan_path", "Unsafe path in bridge plan.")
    if name not in ("Packages/manifest.json", RECEIPT) and not name.startswith(PACKAGE_ROOT + "/"):
        _fail("invalid_plan_path", "Bridge plan cannot write outside its package, receipt, and manifest.")
    path = root.joinpath(*parts)
    current = root
    for part in parts:
        current = current / part
        if _linked(current):
            _fail("linked_path", f"Refusing linked path: {current}")
    if not path.resolve().is_relative_to(root):
        _fail("invalid_plan_path", "Bridge target escaped the project.")
    return path


def _write_atomic(path: Path, value: bytes | None) -> None:
    if value is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".fafnir-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _expected_package(plan: dict, after: bool) -> dict[str, str]:
    expected = dict(plan["before_files"])
    if after:
        for name, change in plan["changes"].items():
            if name.startswith(PACKAGE_ROOT + "/"):
                relative = name[len(PACKAGE_ROOT) + 1:]
                value = _decode(change["after"])
                if value is None:
                    expected.pop(relative, None)
                else:
                    expected[relative] = _sha(value)
    return expected


def _perform(plan: dict, *, reverse: bool = False) -> None:
    root, _, _ = _project(plan["project_path"])
    _closed(root)
    lock = root / ".fafnir-bridge-setup.lock"
    try:
        handle = lock.open("x")
    except FileExistsError:
        _fail("bridge_setup_busy", "Another setup or interrupted transaction owns the project lock. Inspect the saved job before recovery.")
    written = []
    created_directories = set()
    source, target = ("after", "before") if reverse else ("before", "after")
    try:
        if _hashes(_tree(root / PACKAGE_ROOT)) != _expected_package(plan, reverse):
            _fail("bridge_files_changed", "Package files changed after review; nothing was overwritten.")
        for name in ("Packages/manifest.json", RECEIPT):
            change = plan["changes"].get(name)
            expected = _decode(change[source]) if change else _decode(
                plan["before_manifest" if name == "Packages/manifest.json" else "before_receipt"])
            if _read(_destination(root, name)) != expected:
                _fail("bridge_files_changed", f"{name} changed after review.")
        for name, change in plan["changes"].items():
            path = _destination(root, name)
            if _read(path) != _decode(change[source]):
                _fail("bridge_files_changed", f"{name} changed during setup.")
        for name, change in plan["changes"].items():
            path = _destination(root, name)
            parent = path.parent
            while not parent.exists():
                created_directories.add(parent)
                parent = parent.parent
            if _read(path) != _decode(change[source]):
                _fail("bridge_files_changed", f"{name} changed during setup.")
            _write_atomic(path, _decode(change[target]))
            written.append((name, change))
    except Exception:
        # Restore only bytes we just wrote, never a user's concurrently changed file.
        for name, change in reversed(written):
            path = _destination(root, name)
            if _read(path) != _decode(change[target]):
                _fail("bridge_recovery_required", f"Concurrent change in {name}; inspect the saved job. Do not retry blindly.")
            _write_atomic(path, _decode(change[source]))
        raise
    finally:
        handle.close()
        lock.unlink()
        if reverse:
            # Empty package directories only. Never recursively delete a tree.
            created_directories.update(p for p in (root / PACKAGE_ROOT).rglob("*") if p.is_dir())
            created_directories.add(root / PACKAGE_ROOT)
        for directory in sorted(created_directories, key=lambda p: len(p.parts), reverse=True):
            try:
                directory.rmdir()
            except OSError:
                pass
