"""Requirement planning, retrieval, scoring, and plan assembly."""

from __future__ import annotations

import math
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

from .asset_store_cache import CacheScanResult, scan_asset_store_cache
from .compatibility import assess_candidate_compatibility
from .models import Candidate, GameRequirement, ProjectSnapshot
from .repository import StackRepository
from .requirements import derive_requirements
from .scopes import SEARCH_SCOPES, candidate_view
from .sources import GitHubSource, OpenUpmSource, SourceError, SourceResult
from .unity_project import scan_unity_project

_PERMISSIVE_LICENSES = {
    "mit", "apache-2.0", "bsd-2-clause", "bsd-3-clause",
    "isc", "zlib", "unlicense", "cc0-1.0",
}
_RECIPROCAL_LICENSES = {
    "gpl", "gpl-2.0", "gpl-3.0", "agpl-3.0",
    "lgpl", "lgpl-2.1", "lgpl-3.0",
}
_REMOTE_REQUIREMENT_LIMIT = 14
_RECOMMENDATION_SCOPE_LIMITS = {
    "owned_assets": 8,
    "asset_store_market": 5,
    "community": 8,
}

_GENERIC_MATCH_TOKENS = {
    "adaptive", "asset", "assets", "audio", "code", "controller", "environment",
    "framework", "game", "kit", "logic", "manager", "package", "room", "system",
    "tool", "toolkit", "unity",
}


def _normalized(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _tokens(value: str) -> set[str]:
    return {
        item for item in re.findall(r"[a-z0-9][a-z0-9_.+-]{1,}", _normalized(value))
        if item not in _GENERIC_MATCH_TOKENS
    }


def _age_days(value: str | None) -> int | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0, (datetime.now(timezone.utc) - parsed).days)


def _rag_query_for_requirement(requirement: GameRequirement, prompt: str) -> str:
    """Add only requirement-relevant scene context to the retrieval query."""
    normalized = _normalized(prompt)
    context: list[str] = []

    def includes(*terms: str) -> bool:
        return any(_normalized(term) in normalized for term in terms)

    if requirement.key in {
        "horror_atmosphere", "lighting", "water_environment", "visual_assets",
    }:
        if includes("海底", "海中", "水中", "subsea", "underwater"):
            context.extend(("underwater", "subsea", "ocean"))
        if includes("研究施設", "研究所", "laboratory", "research facility"):
            context.extend(("research", "laboratory", "facility"))
        if includes("通路", "廊下", "corridor", "hallway"):
            context.extend(("interior", "corridor", "hallway"))
        if includes("浸水", "水没", "flooded", "submerged"):
            context.extend(("flooded", "submerged"))
    if requirement.key in {"lighting", "horror_atmosphere"} and includes(
        "非常灯", "非常照明", "emergency light"
    ):
        context.extend(("emergency", "warning", "light"))
    if requirement.key in {"enemy_ai", "character_art"} and includes(
        "異形", "怪物", "化け物", "creature", "monster", "mutant"
    ):
        context.extend(("creature", "monster", "mutant"))
    if requirement.key in {"audio", "footstep_audio"} and includes(
        "足音", "歩行音", "footstep", "footsteps"
    ):
        context.extend(("footsteps", "surface", "foley", "wet"))
    if requirement.key in {"interaction", "puzzle"} and includes(
        "暗証番号", "暗証", "番号錠", "keypad", "combination lock"
    ):
        context.extend(("keypad", "combination", "code", "lock"))

    if not context:
        return requirement.query
    base = requirement.query
    if requirement.key == "visual_assets":
        base = "unity environment art models modular props"
    elif requirement.key == "audio":
        base = "unity horror ambient audio"
    return " ".join((base, *dict.fromkeys(context)))


def _candidate_score(
    requirement: GameRequirement,
    candidate: Candidate,
    *,
    project: ProjectSnapshot | None,
    platform: str,
    rag_match: dict[str, Any] | None = None,
) -> dict[str, Any]:
    reasons: list[str] = []
    risks: list[str] = []
    score = 0.0

    query_tokens = _tokens(requirement.query)
    identity_overlap = query_tokens & _tokens(" ".join((
        candidate.title,
        *candidate.tags,
    )))
    description_overlap = query_tokens & _tokens(candidate.description)
    overlap = identity_overlap | description_overlap
    remote_candidate = candidate.source in {"github", "openupm"}
    category_match = (
        requirement.key in candidate.categories
        and not remote_candidate
    )
    if category_match:
        score += 48
        reasons.append("必要機能のカテゴリと一致")
    # Remote search APIs can return broad list repositories whose README happens
    # to mention a term. Require the package/repository identity itself to match,
    # or at least two distinct terms in its description. Search-assigned
    # categories are retrieval provenance, not proof of relevance.
    if remote_candidate and not identity_overlap and len(description_overlap) < 2:
        overlap = set()
    if overlap:
        score += min(
            32.0,
            len(identity_overlap) * 22.0 + len(description_overlap) * 4.0,
        )
        reasons.append("検索語一致: " + ", ".join(sorted(overlap)[:4]))

    # Ownership is useful only after relevance is established. Without this
    # gate, any installed package could be selected for an unrelated slot.
    installed_in_project = (
        candidate.installed
        if project is None
        else any(item.id == candidate.id for item in project.packages)
    )
    candidate_payload = candidate_view(candidate)
    if (
        project is not None
        and candidate.source == "local"
        and not installed_in_project
    ):
        candidate_payload["installed"] = False
        if candidate_payload["ownership"] == "installed":
            candidate_payload["ownership"] = "unknown"

    compatibility = assess_candidate_compatibility(
        candidate,
        project=project,
        platform=platform,
    )
    candidate_payload["compatibility"] = compatibility
    if compatibility["status"] == "incompatible":
        return {
            "candidate": candidate_payload,
            "score": 0.0,
            "reasons": [],
            "risks": list(compatibility["reasons"])[:5],
            "installed_in_project": installed_in_project,
        }

    if rag_match is not None:
        rank = max(0, int(rag_match.get("rank") or 0))
        dense_score = rag_match.get("score")
        mode = str(rag_match.get("retrieval_mode") or "")
        if isinstance(dense_score, (int, float)):
            score += min(48.0, 34.0 + max(0.0, float(dense_score) - 0.65) * 45.0)
            reasons.append(f"所有アセットRAG意味一致 {float(dense_score):.2f}")
        else:
            lexical_score = float(rag_match.get("rank_score") or 0.0)
            score += min(26.0, 12.0 + lexical_score)
            reasons.append("所有アセット文字列索引で関連候補")
        candidate_payload["retrieval"] = {
            "mode": mode,
            "score": dense_score,
            "rank": rank + 1,
        }

    fallback_rag = (
        rag_match is not None
        and str(rag_match.get("retrieval_mode") or "") == "lexical_fallback"
    )
    fallback_relevant = (
        category_match or bool(identity_overlap) or len(description_overlap) >= 2
    )
    if (
        not category_match
        and not overlap
        and (
            rag_match is None
            or fallback_rag
        )
    ) or (fallback_rag and not fallback_relevant):
        return {
            "candidate": candidate_payload,
            "score": 0.0,
            "reasons": [],
            "risks": [],
            "installed_in_project": installed_in_project,
        }

    if candidate.metadata.get("rag_indexed") is True:
        score += 5
        reasons.append("自分の購入済みRAGメモを参照")
    if installed_in_project:
        score += 18
        reasons.append("現在のプロジェクトに導入済み")
    elif candidate.ownership == "owned":
        score += 14
        reasons.append("所有済み")
    elif candidate_payload["inventory_state"] == "locally_cached":
        score += 9
        reasons.append("Unityのローカルキャッシュで利用可能")
    if candidate.source == "openupm":
        score += 6
        reasons.append("Unity Package Manager形式")
    if candidate.source == "github":
        score += min(12.0, math.log10(candidate.stars + 1) * 3.5)
        if candidate.stars:
            reasons.append(f"GitHub Stars {candidate.stars:,}")
    if candidate.downloads:
        score += min(8.0, math.log10(candidate.downloads + 1) * 2.0)

    license_key = (candidate.license or "").casefold()
    if license_key in _PERMISSIVE_LICENSES:
        score += 10
        reasons.append(f"許容的ライセンス: {candidate.license}")
    elif license_key in _RECIPROCAL_LICENSES:
        score -= 8
        risks.append(f"{candidate.license}の配布条件を要確認")
    elif candidate.source == "github" and not candidate.license:
        score -= 16
        risks.append("GitHub上でライセンスを確認できません")
    elif candidate.source == "asset_store":
        risks.append("商品ページとAsset Store EULAを手動確認")

    age = _age_days(candidate.updated_at)
    if age is not None:
        if age <= 180:
            score += 8
            reasons.append("直近6か月に更新")
        elif age > 1095:
            score -= 10
            risks.append("最終更新から3年以上")
    if candidate.archived:
        score -= 40
        risks.append("リポジトリがアーカイブ済み")

    if compatibility["status"] == "compatible":
        score += 8
        reasons.extend(list(compatibility["reasons"])[:1])
        risks.extend(list(compatibility.get("local_warnings") or [])[:2])
    elif (
        project is not None
        and candidate.source == "asset_store"
        and compatibility["status"] == "unknown"
    ):
        risks.extend(list(compatibility["reasons"])[:1])

    if (
        candidate.source != "asset_store"
        and project is not None
        and candidate.render_pipeline
    ):
        pipeline = candidate.render_pipeline.casefold()
        if pipeline not in {"any", "all", project.render_pipeline.casefold()}:
            score -= 25
            risks.append(f"{project.render_pipeline.upper()}との互換性を確認")
        else:
            score += 6
            reasons.append("Render Pipeline一致")
    if candidate.platforms:
        supported = {item.casefold() for item in candidate.platforms}
        if platform.casefold() not in supported and "all" not in supported:
            score -= 20
            risks.append(f"{platform}対応を確認")

    details = candidate.metadata.get("asset_store_details")
    if isinstance(details, dict):
        dependencies = details.get("dependencies")
        if isinstance(dependencies, list) and dependencies:
            risks.append(f"依存Asset Store商品 {len(dependencies)}件を確認")

    return {
        "candidate": candidate_payload,
        "score": round(max(0.0, min(score, 100.0)), 1),
        "reasons": reasons[:6],
        "risks": risks[:5],
        "installed_in_project": installed_in_project,
    }


def _variant_value(item: dict[str, Any], variant: str) -> float:
    candidate = item["candidate"]
    score = float(item["score"])
    if variant == "installed_first":
        if item.get("installed_in_project") or candidate["ownership"] == "owned":
            score += 40
    elif variant == "open_source":
        if candidate["source"] in {"github", "openupm", "local"}:
            score += 18
        if candidate["source"] == "asset_store":
            score -= 35
        if str(candidate.get("license") or "").casefold() in _PERMISSIVE_LICENSES:
            score += 12
    elif variant == "low_risk":
        score -= len(item.get("risks") or []) * 12
        if item.get("installed_in_project"):
            score += 10
        if candidate["source"] == "openupm":
            score += 5
    return score


def _make_plan(
    variant: str,
    requirements: tuple[GameRequirement, ...],
    recommendations: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    selected_by_id: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    for requirement in requirements:
        ranked = sorted(
            recommendations.get(requirement.key, []),
            key=lambda item: (
                -_variant_value(item, variant),
                str(item["candidate"]["title"]).casefold(),
            ),
        )
        choice = next(
            (item for item in ranked if _variant_value(item, variant) >= 20),
            None,
        )
        if choice is None:
            missing.append(requirement.key)
            continue
        candidate_id = str(choice["candidate"]["id"])
        current = selected_by_id.get(candidate_id)
        if current is None:
            selected_by_id[candidate_id] = {
                **choice,
                "covers": [requirement.key],
                "requirement_titles": [requirement.title],
            }
        else:
            current["covers"].append(requirement.key)
            current["requirement_titles"].append(requirement.title)
            current["score"] = max(current["score"], choice["score"])

    copy = {
        "installed_first": (
            "手持ち・導入済み優先",
            "既存プロジェクトを崩さず、追加導入を最小化します。",
        ),
        "open_source": (
            "OSS・無料優先",
            "OpenUPMとライセンス明示済みGitHub候補を優先します。",
        ),
        "low_risk": (
            "保守性・低リスク",
            "更新状況、ライセンス、互換性警告の少なさを重視します。",
        ),
    }[variant]
    total = len(requirements)
    coverage = 0 if total == 0 else round((total - len(missing)) / total * 100)
    return {
        "id": variant,
        "title": copy[0],
        "description": copy[1],
        "coverage": coverage,
        "selected": list(selected_by_id.values()),
        "missing": missing,
    }


class GameStackPlanner:
    def __init__(
        self,
        repository: StackRepository,
        *,
        github: GitHubSource | None = None,
        openupm: OpenUpmSource | None = None,
    ) -> None:
        self.repository = repository
        self.github = github or GitHubSource()
        self.openupm = openupm or OpenUpmSource()

    def scan_project(self, path: str) -> ProjectSnapshot:
        snapshot = scan_unity_project(path)
        self.repository.upsert_candidates(snapshot.packages)
        self.repository.save_project(snapshot)
        return snapshot

    def scan_asset_store_cache(
        self,
        path: str | None = None,
        *,
        inspect_packages: bool = False,
    ) -> CacheScanResult:
        result = scan_asset_store_cache(
            path,
            inspect_packages=bool(inspect_packages),
        )
        self.repository.upsert_candidates(result.candidates)
        self.repository.link_asset_store_cache_candidates()
        return result

    @staticmethod
    def _asset_store_search(
        requirement: GameRequirement,
        *,
        project: ProjectSnapshot | None,
        platform: str,
    ) -> dict[str, str]:
        query_parts = [requirement.query, platform]
        if project is not None:
            query_parts.append(project.render_pipeline)
        query = " ".join(dict.fromkeys(query_parts))
        return {
            "requirement": requirement.key,
            "title": requirement.title,
            "query": query,
            "url": "https://assetstore.unity.com/?" + urlencode(
                {"q": query, "orderBy": "1"}
            ),
            "mode": "official_search_link",
        }

    def _remote_candidates(
        self,
        requirements: tuple[GameRequirement, ...],
    ) -> tuple[list[Candidate], dict[str, Any]]:
        tasks: dict[Any, tuple[str, str]] = {}
        candidates: list[Candidate] = []
        statuses: dict[str, dict[str, Any]] = {
            "github": {"status": "ok", "count": 0},
            "openupm": {"status": "ok", "count": 0},
        }
        with ThreadPoolExecutor(max_workers=4) as executor:
            for requirement in requirements[:_REMOTE_REQUIREMENT_LIMIT]:
                tasks[executor.submit(self.github.search, requirement, limit=5)] = (
                    "github", requirement.key
                )
                tasks[executor.submit(self.openupm.search, requirement, limit=5)] = (
                    "openupm", requirement.key
                )
            for future in as_completed(tasks):
                source, requirement_key = tasks[future]
                try:
                    result: SourceResult = future.result()
                except (SourceError, OSError, ValueError) as exc:
                    statuses[source]["status"] = "degraded"
                    statuses[source].setdefault("errors", []).append({
                        "requirement": requirement_key,
                        "message": str(exc)[:300],
                    })
                    continue
                candidates.extend(result.candidates)
                statuses[source]["count"] += len(result.candidates)
                if result.remaining is not None:
                    statuses[source]["remaining"] = result.remaining
        return candidates, statuses

    def recommend(
        self,
        *,
        prompt: str,
        project_path: str | None = None,
        platform: str = "pc",
        budget: str = "mixed",
        remote: bool = True,
    ) -> dict[str, Any]:
        started = time.monotonic()
        normalized_prompt = str(prompt or "").strip()
        if not normalized_prompt:
            raise ValueError("作りたいゲームを入力してください。")
        if len(normalized_prompt) > 8000:
            raise ValueError("ゲーム説明が長すぎます。")
        if platform not in {"pc", "mobile", "webgl", "vr", "quest"}:
            raise ValueError("未対応のターゲットです。")
        if budget not in {"free", "mixed", "owned_first"}:
            raise ValueError("未対応の予算モードです。")

        project = (
            self.scan_project(project_path)
            if project_path and str(project_path).strip()
            else None
        )
        requirements = derive_requirements(normalized_prompt, platform=platform)
        if remote:
            remote_candidates, source_status = self._remote_candidates(requirements)
            self.repository.upsert_candidates(remote_candidates)
        else:
            source_status = {
                "github": {"status": "offline", "count": 0},
                "openupm": {"status": "offline", "count": 0},
            }

        # Planning must consider the whole personal library. The public catalog
        # endpoint remains paginated, but silently searching only its first page
        # makes large My Assets libraries effectively random.
        catalog = self.repository.list_candidates(limit=5000)
        if project is not None:
            by_id = {item.id: item for item in catalog}
            by_id.update({item.id: item for item in project.packages})
            catalog = list(by_id.values())

        recommendations: dict[str, list[dict[str, Any]]] = {}
        recommendation_groups: dict[
            str,
            dict[str, list[dict[str, Any]]],
        ] = {}
        # Compatibility, project state, product state, and explicit platform
        # support are hard filters before any vector/lexical RAG scoring.  This
        # keeps semantically similar but unusable owned assets out of retrieval.
        eligible_owned_ids = {
            candidate.id
            for candidate in catalog
            if not candidate.archived
            and candidate.source == "asset_store"
            and candidate.ownership in {"owned", "installed"}
            and assess_candidate_compatibility(
                candidate,
                project=project,
                platform=platform,
            )["status"] != "incompatible"
        }
        rag_matches: dict[str, dict[str, dict[str, Any]]] = {}
        rag_modes: set[str] = set()
        rag_error = ""
        for requirement in requirements:
            try:
                retrieval = self.repository.search_asset_rag(
                    query=_rag_query_for_requirement(requirement, normalized_prompt),
                    limit=40,
                    candidate_ids=eligible_owned_ids,
                    category_hints=(requirement.key,),
                )
                mode = str(retrieval.get("retrieval_mode") or "unknown")
                rag_modes.add(mode)
                rag_matches[requirement.key] = {
                    str(item["candidate_id"]): {
                        **item,
                        "rank": index,
                        "retrieval_mode": mode,
                    }
                    for index, item in enumerate(retrieval.get("items") or [])
                    if isinstance(item, dict) and item.get("candidate_id")
                }
            except (RuntimeError, ValueError) as exc:
                rag_error = str(exc)[:500]
                rag_matches[requirement.key] = {}
        source_status["owned_asset_rag"] = {
            "status": "error" if rag_error else "+".join(sorted(rag_modes)) or "empty",
            "count": sum(len(value) for value in rag_matches.values()),
            "eligible": len(eligible_owned_ids),
            "structured_prefilter": True,
        }
        if rag_error:
            source_status["owned_asset_rag"]["error"] = rag_error

        for requirement in requirements:
            scored = [
                _candidate_score(
                    requirement,
                    candidate,
                    project=project,
                    platform=platform,
                    rag_match=rag_matches.get(requirement.key, {}).get(candidate.id),
                )
                for candidate in catalog
                if not candidate.archived
                and (
                    budget != "free"
                    or candidate.source != "asset_store"
                    or candidate.ownership in {"owned", "installed"}
                )
            ]
            ranked = sorted(
                (item for item in scored if item["score"] >= 8),
                key=lambda item: (
                    -float(item["score"]),
                    str(item["candidate"]["title"]).casefold(),
                ),
            )
            groups = {
                scope: [
                    item for item in ranked
                    if item["candidate"]["scope"] == scope
                ][:_RECOMMENDATION_SCOPE_LIMITS[scope]]
                for scope in SEARCH_SCOPES
            }
            recommendation_groups[requirement.key] = groups
            recommendations[requirement.key] = sorted(
                (
                    item
                    for scope in SEARCH_SCOPES
                    for item in groups[scope]
                ),
                key=lambda item: (
                    -float(item["score"]),
                    str(item["candidate"]["title"]).casefold(),
                ),
            )

        plan_order = {
            "owned_first": ("installed_first", "low_risk", "open_source"),
            "free": ("open_source", "low_risk", "installed_first"),
            "mixed": ("low_risk", "installed_first", "open_source"),
        }[budget]
        plans = [
            _make_plan(variant, requirements, recommendations)
            for variant in plan_order
        ]
        result: dict[str, Any] = {
            "prompt": normalized_prompt,
            "platform": platform,
            "budget": budget,
            "project": project.to_dict() if project is not None else None,
            "requirements": [item.to_dict() for item in requirements],
            "recommendations": recommendations,
            "recommendation_groups": recommendation_groups,
            "plans": plans,
            "recommended_plan_id": plan_order[0],
            "asset_store_searches": [
                self._asset_store_search(
                    requirement, project=project, platform=platform
                )
                for requirement in requirements
            ],
            "source_status": source_status,
            "policy": {
                "asset_store_access": (
                    "official_search_links_manual_pins_and_public_product_metadata"
                ),
                "asset_store_automated_fetch": True,
                "asset_store_local_cache_scan": True,
                "github_api": True,
                "openupm_registry": True,
            },
            "elapsed_ms": round((time.monotonic() - started) * 1000),
        }
        run_id = self.repository.save_recommendation(
            prompt=normalized_prompt,
            project_path=project.path if project is not None else None,
            platform=platform,
            budget=budget,
            result=result,
        )
        result["run_id"] = run_id
        return result


__all__ = ["GameStackPlanner"]
