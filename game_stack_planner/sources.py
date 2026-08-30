"""Authorized remote discovery sources for Unity-compatible packages."""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from .models import Candidate, GameRequirement

_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_OPENUPM_QUERIES = {
    "networking": "networking",
    "lobby_matchmaking": "lobby",
    "character_controller": "character controller",
    "camera": "camera",
    "input": "input",
    "save_system": "save",
    "inventory": "inventory",
    "combat": "combat",
    "enemy_ai": "ai",
    "procedural_generation": "procedural",
    "dialogue_quest": "dialogue",
    "localization": "localization",
    "ui": "ui",
    "audio": "audio",
    "xr": "xr",
    "mobile": "mobile",
    "steam": "steam",
    "addressables": "addressables",
}


class SourceError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SourceResult:
    source: str
    candidates: tuple[Candidate, ...]
    status: str = "ok"
    message: str = ""
    remaining: int | None = None


class JsonFetcher:
    def __init__(
        self,
        *,
        allowed_hosts: tuple[str, ...],
        opener: Callable[..., Any] = urlopen,
        timeout: float = 10.0,
    ) -> None:
        self.allowed_hosts = {
            item.casefold().rstrip(".") for item in allowed_hosts
        }
        self.opener = opener
        self.timeout = max(1.0, min(float(timeout), 30.0))

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        target = urlsplit(url)
        if (
            target.scheme != "https"
            or target.hostname is None
            or target.hostname.casefold().rstrip(".") not in self.allowed_hosts
            or target.username
            or target.password
        ):
            raise SourceError("Remote source URL is not allowed.")
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "game-stack-planner/0.1",
                **(headers or {}),
            },
        )
        try:
            response = self.opener(request, timeout=self.timeout)
            with response:
                final_url = urlsplit(response.geturl())
                if (
                    final_url.scheme != "https"
                    or final_url.hostname is None
                    or final_url.hostname.casefold().rstrip(".")
                    not in self.allowed_hosts
                ):
                    raise SourceError("Remote source redirected outside its host.")
                declared = response.headers.get("Content-Length")
                if declared and int(declared) > _MAX_RESPONSE_BYTES:
                    raise SourceError("Remote source response is too large.")
                payload = response.read(_MAX_RESPONSE_BYTES + 1)
                if len(payload) > _MAX_RESPONSE_BYTES:
                    raise SourceError("Remote source response is too large.")
                response_headers = {
                    str(key).casefold(): str(value)
                    for key, value in response.headers.items()
                }
        except SourceError:
            raise
        except Exception as exc:
            raise SourceError(f"Could not reach remote source: {exc}") from exc
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SourceError("Remote source returned invalid JSON.") from exc
        if not isinstance(value, dict):
            raise SourceError("Remote source returned an unexpected payload.")
        return value, response_headers


class GitHubSource:
    name = "github"

    def __init__(
        self,
        *,
        token: str | None = None,
        fetcher: JsonFetcher | None = None,
    ) -> None:
        self.token = token if token is not None else os.getenv("GITHUB_TOKEN")
        self.fetcher = fetcher or JsonFetcher(
            allowed_hosts=("api.github.com",)
        )

    def search(
        self,
        requirement: GameRequirement,
        *,
        limit: int = 5,
    ) -> SourceResult:
        result_limit = max(1, min(int(limit), 10))
        query = (
            f"{requirement.query} unity "
            "in:name,description,readme archived:false"
        )
        url = "https://api.github.com/search/repositories?" + urlencode(
            {
                "q": query,
                "sort": "stars",
                "order": "desc",
                "per_page": result_limit,
            }
        )
        headers = {
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        payload, response_headers = self.fetcher.get(url, headers=headers)
        items = payload.get("items", [])
        candidates: list[Candidate] = []
        if isinstance(items, list):
            for item in items[:result_limit]:
                if not isinstance(item, dict) or bool(item.get("archived")):
                    continue
                full_name = str(item.get("full_name") or "").strip()
                html_url = str(item.get("html_url") or "").strip()
                if not full_name or not html_url.startswith("https://github.com/"):
                    continue
                topics = item.get("topics", [])
                if not isinstance(topics, list):
                    topics = []
                license_value = item.get("license")
                license_id = (
                    str(license_value.get("spdx_id") or "")
                    if isinstance(license_value, dict)
                    else ""
                )
                candidates.append(
                    Candidate(
                        id=f"github:{full_name.casefold()}",
                        source="github",
                        external_id=full_name,
                        title=full_name,
                        url=html_url,
                        description=str(item.get("description") or ""),
                        categories=(requirement.key,),
                        tags=tuple(str(value) for value in topics[:24]),
                        license=license_id or None,
                        stars=max(0, int(item.get("stargazers_count") or 0)),
                        updated_at=str(
                            item.get("pushed_at") or item.get("updated_at") or ""
                        )
                        or None,
                        archived=False,
                        metadata={
                            "forks": int(item.get("forks_count") or 0),
                            "open_issues": int(
                                item.get("open_issues_count") or 0
                            ),
                            "default_branch": item.get("default_branch"),
                        },
                    )
                )
        remaining_text = response_headers.get("x-ratelimit-remaining")
        remaining = (
            int(remaining_text)
            if remaining_text and remaining_text.isdigit()
            else None
        )
        return SourceResult(
            source=self.name,
            candidates=tuple(candidates),
            remaining=remaining,
        )


class OpenUpmSource:
    name = "openupm"

    def __init__(self, *, fetcher: JsonFetcher | None = None) -> None:
        self.fetcher = fetcher or JsonFetcher(
            allowed_hosts=("package.openupm.com",)
        )

    @staticmethod
    def _keywords(value: Any) -> tuple[str, ...]:
        if isinstance(value, str):
            return tuple(
                item.strip()
                for item in value.replace(",", " ").split()
                if item.strip()
            )[:24]
        if isinstance(value, list):
            return tuple(str(item) for item in value[:24])
        return ()

    @staticmethod
    def _repository_url(package: dict[str, Any]) -> str:
        links = package.get("links")
        if isinstance(links, dict):
            for name in ("repository", "homepage", "npm"):
                value = str(links.get(name) or "")
                if value.startswith("https://"):
                    return value
        repository = package.get("repository")
        if isinstance(repository, dict):
            value = str(repository.get("url") or "")
        else:
            value = str(repository or "")
        value = value.removeprefix("git+")
        return value if value.startswith("https://") else ""

    def search(
        self,
        requirement: GameRequirement,
        *,
        limit: int = 5,
    ) -> SourceResult:
        result_limit = max(1, min(int(limit), 10))
        url = "https://package.openupm.com/-/v1/search?" + urlencode(
            {
                "text": _OPENUPM_QUERIES.get(
                    requirement.key,
                    requirement.key.replace("_", " "),
                ),
                "size": result_limit,
                "from": 0,
            }
        )
        payload, _headers = self.fetcher.get(url)
        objects = payload.get("objects", [])
        candidates: list[Candidate] = []
        if isinstance(objects, list):
            for entry in objects[:result_limit]:
                if not isinstance(entry, dict):
                    continue
                package = entry.get("package")
                if not isinstance(package, dict):
                    continue
                name = str(package.get("name") or "").strip()
                if not name:
                    continue
                score_value = entry.get("score", {})
                score = (
                    float(score_value.get("final") or 0.0)
                    if isinstance(score_value, dict)
                    else 0.0
                )
                candidates.append(
                    Candidate(
                        id=f"openupm:{name.casefold()}",
                        source="openupm",
                        external_id=name,
                        title=name,
                        url=self._repository_url(package)
                        or f"https://openupm.com/packages/{name}/",
                        description=str(package.get("description") or ""),
                        categories=(requirement.key,),
                        tags=self._keywords(package.get("keywords")),
                        license=str(package.get("license") or "") or None,
                        version=str(package.get("version") or "") or None,
                        downloads=max(0, int(round(score * 1000)))
                        if math.isfinite(score)
                        else 0,
                        updated_at=str(package.get("date") or "") or None,
                        metadata={
                            "publisher": package.get("publisher"),
                            "search_score": score,
                        },
                    )
                )
        return SourceResult(
            source=self.name,
            candidates=tuple(candidates),
        )


__all__ = [
    "GitHubSource",
    "JsonFetcher",
    "OpenUpmSource",
    "SourceError",
    "SourceResult",
]
