"""Approval-gated coordination for deterministic Unity install plans."""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .installer import (
    InstallPlan,
    InstallerError,
    ManifestChangedError,
    PlanIntegrityError,
    RollbackConflictError,
    apply_install_plan,
    prepare_install_plan,
    rollback_install_plan,
)
from .repository import StackRepository

_APPROVAL_TTL = timedelta(minutes=10)
_ROLLBACK_TTL = timedelta(hours=24)


class InstallCoordinatorError(RuntimeError):
    """Stable error raised at the API/CLI coordination boundary."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _digest_plan(plan: dict[str, Any]) -> str:
    encoded = json.dumps(
        plan,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _token_hash(
    *,
    purpose: str,
    job_id: str,
    token: str,
    plan: dict[str, Any],
) -> str:
    binding = "\0".join((purpose, job_id, _digest_plan(plan), token))
    return hashlib.sha256(binding.encode("utf-8")).hexdigest()


class InstallCoordinator:
    """Resolve candidate IDs, persist plans, and consume one-shot approvals."""

    def __init__(self, repository: StackRepository) -> None:
        self.repository = repository

    def prepare(self, *, candidate_id: str, project_path: str) -> dict[str, Any]:
        candidate = self.repository.get_candidate(candidate_id)
        if candidate is None:
            raise InstallCoordinatorError(
                404,
                "candidate_not_found",
                "Install candidate was not found in the local catalog.",
            )
        try:
            plan = prepare_install_plan(candidate, project_path)
        except InstallerError as exc:
            raise InstallCoordinatorError(422, exc.code, str(exc)) from exc

        job_id = uuid4().hex
        complete_plan = plan.to_dict()
        approval_nonce: str | None = None
        approval_hash: str | None = None
        expires_at: str | None = None
        job_status = plan.status
        if plan.status == "ready" and plan.executable and plan.requires_approval:
            approval_nonce = secrets.token_urlsafe(32)
            approval_hash = _token_hash(
                purpose="execute",
                job_id=job_id,
                token=approval_nonce,
                plan=complete_plan,
            )
            expires_at = _iso(_now() + _APPROVAL_TTL)
            job_status = "awaiting_approval"

        self.repository.create_install_job(
            job_id=job_id,
            candidate_id=candidate.id,
            project_path=plan.project_path,
            status=job_status,
            plan=complete_plan,
            approval_hash=approval_hash,
            expires_at=expires_at,
        )
        response: dict[str, Any] = {
            "plan": self._public_plan(job_id, plan),
        }
        if approval_nonce is not None:
            response["approval_nonce"] = approval_nonce
        return response

    def execute(self, *, plan_id: str, approval_nonce: str) -> dict[str, Any]:
        stored = self.repository.get_install_job(plan_id)
        if stored is None:
            raise InstallCoordinatorError(404, "install_plan_not_found", "Install plan was not found.")
        complete_plan = stored["plan"]
        expected_hash = _token_hash(
            purpose="execute",
            job_id=plan_id,
            token=approval_nonce,
            plan=complete_plan,
        )
        claimed = self.repository.claim_install_job(
            job_id=plan_id,
            approval_hash=expected_hash,
            now=_iso(_now()),
        )
        if claimed is None:
            raise InstallCoordinatorError(
                409,
                "install_plan_consumed",
                "Install approval is invalid, expired, or already consumed.",
            )
        try:
            plan = InstallPlan.from_dict(complete_plan)
            result = apply_install_plan(plan)
        except (ManifestChangedError, PlanIntegrityError) as exc:
            self._record_failure(plan_id, exc)
            raise InstallCoordinatorError(409, exc.code, str(exc)) from exc
        except InstallerError as exc:
            self._record_failure(plan_id, exc)
            raise InstallCoordinatorError(422, exc.code, str(exc)) from exc

        rollback_nonce = secrets.token_urlsafe(32)
        rollback_hash = _token_hash(
            purpose="rollback",
            job_id=plan_id,
            token=rollback_nonce,
            plan=complete_plan,
        )
        finished = self.repository.finish_install_job(
            job_id=plan_id,
            expected_status="applying",
            status="applied_waiting_for_unity",
            result=result.to_dict(),
            rollback_hash=rollback_hash,
            expires_at=_iso(_now() + _ROLLBACK_TTL),
        )
        if finished is None:
            raise InstallCoordinatorError(
                409,
                "install_conflict",
                "Install job state changed while applying the plan.",
            )
        return {
            "job": self._public_job(finished),
            "rollback_nonce": rollback_nonce,
        }

    def get(self, job_id: str) -> dict[str, Any]:
        stored = self.repository.get_install_job(job_id)
        if stored is None:
            raise InstallCoordinatorError(404, "install_job_not_found", "Install job was not found.")
        return {"job": self._public_job(stored)}

    def rollback(self, *, job_id: str, rollback_nonce: str) -> dict[str, Any]:
        stored = self.repository.get_install_job(job_id)
        if stored is None:
            raise InstallCoordinatorError(404, "install_job_not_found", "Install job was not found.")
        complete_plan = stored["plan"]
        expected_hash = _token_hash(
            purpose="rollback",
            job_id=job_id,
            token=rollback_nonce,
            plan=complete_plan,
        )
        claimed = self.repository.claim_rollback_job(
            job_id=job_id,
            rollback_hash=expected_hash,
            now=_iso(_now()),
        )
        if claimed is None:
            raise InstallCoordinatorError(
                409,
                "rollback_consumed",
                "Rollback approval is invalid, expired, or already consumed.",
            )
        try:
            plan = InstallPlan.from_dict(complete_plan)
            result = rollback_install_plan(plan)
        except RollbackConflictError as exc:
            self.repository.finish_install_job(
                job_id=job_id,
                expected_status="rolling_back",
                status="rollback_conflict",
                result=exc.to_dict(),
            )
            raise InstallCoordinatorError(409, exc.code, str(exc)) from exc
        except InstallerError as exc:
            self.repository.finish_install_job(
                job_id=job_id,
                expected_status="rolling_back",
                status="rollback_failed",
                result=exc.to_dict(),
            )
            raise InstallCoordinatorError(422, exc.code, str(exc)) from exc
        finished = self.repository.finish_install_job(
            job_id=job_id,
            expected_status="rolling_back",
            status="rolled_back",
            result=result.to_dict(),
        )
        if finished is None:
            raise InstallCoordinatorError(
                409,
                "install_conflict",
                "Install job state changed while rolling back.",
            )
        return {"job": self._public_job(finished)}

    def _record_failure(self, job_id: str, error: InstallerError) -> None:
        self.repository.finish_install_job(
            job_id=job_id,
            expected_status="applying",
            status="failed",
            result=error.to_dict(),
        )

    @staticmethod
    def _public_plan(job_id: str, plan: InstallPlan) -> dict[str, Any]:
        value = plan.to_public_dict()
        value["id"] = job_id
        package = value.get("package")
        if isinstance(package, dict):
            package["scope"] = plan.package_name if plan.registry_url else None
        return value

    @classmethod
    def _public_job(cls, stored: dict[str, Any]) -> dict[str, Any]:
        try:
            plan = InstallPlan.from_dict(stored["plan"])
        except InstallerError as exc:
            raise InstallCoordinatorError(500, exc.code, str(exc)) from exc
        return {
            "id": stored["id"],
            "status": stored["status"],
            "candidate_id": stored["candidate_id"],
            "project_path": stored["project_path"],
            "plan": cls._public_plan(stored["id"], plan),
            "result": stored.get("result", {}),
            "created_at": stored["created_at"],
            "updated_at": stored["updated_at"],
        }


__all__ = ["InstallCoordinator", "InstallCoordinatorError"]
