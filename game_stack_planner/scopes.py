"""Stable search lanes for the federated Fafnir catalog."""

from __future__ import annotations

from typing import Any

from .models import Candidate


SEARCH_SCOPES = (
    "owned_assets",
    "asset_store_market",
    "community",
)


def candidate_scope(candidate: Candidate) -> str:
    """Classify a candidate without claiming an unverified purchase state."""
    if candidate.source in {"github", "openupm"}:
        return "community"
    if candidate.source == "asset_store" and candidate.ownership not in {
        "owned",
        "installed",
    }:
        return "asset_store_market"
    return "owned_assets"


def inventory_state(candidate: Candidate) -> str:
    explicit = str(candidate.metadata.get("inventory_state") or "").strip()
    if explicit:
        return explicit
    if candidate.source == "asset_store" and candidate.ownership in {
        "owned",
        "installed",
    }:
        return "confirmed_owned"
    if candidate.source == "local" or candidate.installed:
        return "project_present"
    return "unknown"


def candidate_view(candidate: Candidate) -> dict[str, Any]:
    value = candidate.to_dict()
    state = inventory_state(candidate)
    value["scope"] = candidate_scope(candidate)
    value["inventory_state"] = state
    evidence = candidate.metadata.get("ownership_evidence")
    if not isinstance(evidence, dict):
        evidence = {
            "kind": (
                "user_asserted"
                if state == "confirmed_owned"
                else "unity_project"
                if state == "project_present"
                else "none"
            ),
            "verified": False,
        }
    value["ownership_evidence"] = evidence
    return value


__all__ = [
    "SEARCH_SCOPES",
    "candidate_scope",
    "candidate_view",
    "inventory_state",
]
