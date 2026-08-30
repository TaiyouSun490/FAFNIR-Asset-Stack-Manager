"""Domain models for the Game Stack Planner."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class GameRequirement:
    key: str
    title: str
    priority: str
    query: str
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Candidate:
    id: str
    source: str
    external_id: str
    title: str
    url: str
    description: str = ""
    categories: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    ownership: str = "candidate"
    installed: bool = False
    license: str | None = None
    version: str | None = None
    unity_version: str | None = None
    render_pipeline: str | None = None
    platforms: tuple[str, ...] = ()
    stars: int = 0
    downloads: int = 0
    updated_at: str | None = None
    archived: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ProjectSnapshot:
    path: str
    name: str
    unity_version: str | None
    render_pipeline: str
    input_backend: str
    packages: tuple[Candidate, ...]
    product_name: str | None = None
    company_name: str | None = None
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "packages": [item.to_dict() for item in self.packages],
        }
