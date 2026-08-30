"""Optional LLM judgment over candidate IDs retrieved by Stackforge.

The model never supplies install commands, URLs, or arbitrary package names. It
can only select IDs that the local planner retrieved and explain how those
known items could be used together.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .models import GameRequirement, ProjectSnapshot


_DEFAULT_ENDPOINT = "https://api.openai.com/v1/responses"
_DEFAULT_MODEL = "gpt-5-mini"
_MAX_CANDIDATES_PER_REQUIREMENT = 12


def _post_json(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout: float,
) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:  # noqa: S310
        value = json.loads(response.read().decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("LLM response must be an object.")
    return value


_STACK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string"},
        "selections": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "candidate_id": {"type": "string"},
                    "requirement_key": {"type": "string"},
                    "role": {"type": "string"},
                    "use_case": {"type": "string"},
                    "integration": {"type": "string"},
                    "confidence": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                    },
                },
                "required": [
                    "candidate_id", "requirement_key", "role", "use_case",
                    "integration", "confidence",
                ],
            },
        },
        "gaps": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "requirement_key": {"type": "string"},
                    "missing": {"type": "string"},
                    "search_query": {"type": "string"},
                },
                "required": ["requirement_key", "missing", "search_query"],
            },
        },
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "selections", "gaps", "warnings"],
}


@dataclass(slots=True)
class LlmStackRecommender:
    api_key: str = ""
    model: str = _DEFAULT_MODEL
    endpoint: str = _DEFAULT_ENDPOINT
    timeout: float = 45.0
    fetcher: Callable[
        [str, dict[str, Any], dict[str, str], float], dict[str, Any]
    ] = _post_json

    @classmethod
    def from_environment(cls) -> "LlmStackRecommender":
        return cls(
            api_key=os.getenv("OPENAI_API_KEY", "").strip(),
            model=os.getenv("STACKFORGE_OPENAI_MODEL", _DEFAULT_MODEL).strip()
            or _DEFAULT_MODEL,
        )

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def state(self, *, enabled: bool) -> dict[str, Any]:
        if not enabled:
            status = "disabled"
        elif not self.configured:
            status = "not_configured"
        else:
            status = "ready"
        return {
            "status": status,
            "provider": "openai",
            "model": self.model,
            "data_sent": False,
        }

    @staticmethod
    def _input(
        *,
        prompt: str,
        platform: str,
        budget: str,
        project: ProjectSnapshot | None,
        requirements: tuple[GameRequirement, ...],
        recommendations: dict[str, list[dict[str, Any]]],
    ) -> dict[str, Any]:
        candidate_groups: list[dict[str, Any]] = []
        for requirement in requirements:
            candidates: list[dict[str, Any]] = []
            for item in recommendations.get(requirement.key, [
            ])[:_MAX_CANDIDATES_PER_REQUIREMENT]:
                candidate = item["candidate"]
                candidates.append({
                    "id": candidate["id"],
                    "title": candidate["title"],
                    "source": candidate["source"],
                    "scope": candidate["scope"],
                    "ownership": candidate["ownership"],
                    "description": str(candidate.get("description") or "")[:500],
                    "categories": list(candidate.get("categories") or [])[:12],
                    "tags": list(candidate.get("tags") or [])[:16],
                    "license": candidate.get("license"),
                    "local_score": item["score"],
                    "risks": list(item.get("risks") or []),
                })
            candidate_groups.append({
                "requirement": requirement.to_dict(),
                "candidates": candidates,
            })
        return {
            "game_brief": prompt,
            "platform": platform,
            "budget": budget,
            "project": (
                {
                    "unity_version": project.unity_version,
                    "render_pipeline": project.render_pipeline,
                    "input_backend": project.input_backend,
                }
                if project is not None else None
            ),
            "candidate_groups": candidate_groups,
        }

    @staticmethod
    def _output_text(response: dict[str, Any]) -> str:
        for output in response.get("output", []):
            if not isinstance(output, dict):
                continue
            for content in output.get("content", []):
                if (
                    isinstance(content, dict)
                    and content.get("type") == "output_text"
                    and isinstance(content.get("text"), str)
                ):
                    return content["text"]
        raise ValueError("LLM response did not contain structured output.")

    def recommend(
        self,
        *,
        enabled: bool,
        prompt: str,
        platform: str,
        budget: str,
        project: ProjectSnapshot | None,
        requirements: tuple[GameRequirement, ...],
        recommendations: dict[str, list[dict[str, Any]]],
    ) -> dict[str, Any]:
        state = self.state(enabled=enabled)
        if state["status"] != "ready":
            return state
        model_input = self._input(
            prompt=prompt,
            platform=platform,
            budget=budget,
            project=project,
            requirements=requirements,
            recommendations=recommendations,
        )
        payload = {
            "model": self.model,
            "instructions": (
                "You are a senior Unity technical director. Build one coherent, "
                "minimal implementation stack for the supplied game brief. Select "
                "only candidate IDs present in candidate_groups. Never select an "
                "item merely because it is owned. Explain a concrete in-game use, "
                "how it integrates with the other choices, and reject candidates "
                "whose title or metadata does not establish relevance. Select up "
                "to three complementary items per requirement, avoid redundant "
                "frameworks, and report uncovered requirements as gaps. Write all "
                "human-facing fields in Japanese."
            ),
            "input": json.dumps(model_input, ensure_ascii=False),
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "stackforge_implementation_stack",
                    "strict": True,
                    "schema": _STACK_SCHEMA,
                }
            },
        }
        try:
            response = self.fetcher(
                self.endpoint,
                payload,
                {"Authorization": f"Bearer {self.api_key}"},
                self.timeout,
            )
            parsed = json.loads(self._output_text(response))
            if not isinstance(parsed, dict):
                raise ValueError("Structured recommendation must be an object.")
        except (HTTPError, URLError, OSError, ValueError, json.JSONDecodeError) as exc:
            return {
                **state,
                "status": "degraded",
                "error": str(exc)[:300],
                "data_sent": True,
            }
        return {
            **state,
            "status": "used",
            "data_sent": True,
            "summary": str(parsed.get("summary") or "")[:2000],
            "selections": parsed.get("selections", []),
            "gaps": parsed.get("gaps", []),
            "warnings": parsed.get("warnings", []),
        }

    @staticmethod
    def build_plan(
        *,
        llm_result: dict[str, Any],
        requirements: tuple[GameRequirement, ...],
        recommendations: dict[str, list[dict[str, Any]]],
    ) -> dict[str, Any]:
        requirements_by_key = {item.key: item for item in requirements}
        candidates_by_slot = {
            (requirement_key, str(item["candidate"]["id"])): item
            for requirement_key, items in recommendations.items()
            for item in items
        }
        selected_by_id: dict[str, dict[str, Any]] = {}
        covered: set[str] = set()
        for selection in llm_result.get("selections", []):
            if not isinstance(selection, dict):
                continue
            requirement_key = str(selection.get("requirement_key") or "")
            candidate_id = str(selection.get("candidate_id") or "")
            requirement = requirements_by_key.get(requirement_key)
            item = candidates_by_slot.get((requirement_key, candidate_id))
            if requirement is None or item is None:
                continue
            covered.add(requirement_key)
            current = selected_by_id.get(candidate_id)
            usage = {
                "requirement_key": requirement_key,
                "requirement_title": requirement.title,
                "role": str(selection.get("role") or "")[:200],
                "use_case": str(selection.get("use_case") or "")[:1000],
                "integration": str(selection.get("integration") or "")[:1000],
                "confidence": str(selection.get("confidence") or "low"),
            }
            if current is None:
                selected_by_id[candidate_id] = {
                    **item,
                    "covers": [requirement_key],
                    "requirement_titles": [requirement.title],
                    "usage": [usage],
                }
            else:
                if requirement_key not in current["covers"]:
                    current["covers"].append(requirement_key)
                    current["requirement_titles"].append(requirement.title)
                current["usage"].append(usage)

        missing = [item.key for item in requirements if item.key not in covered]
        total = len(requirements)
        return {
            "id": "ai_recommended",
            "title": "AI推奨・実装スタック",
            "description": llm_result.get("summary") or (
                "候補の具体的な用途と組み合わせをLLMが評価しました。"
            ),
            "coverage": 0 if not total else round((total - len(missing)) / total * 100),
            "selected": list(selected_by_id.values()),
            "missing": missing,
            "gaps": llm_result.get("gaps", []),
            "warnings": llm_result.get("warnings", []),
        }


__all__ = ["LlmStackRecommender"]
