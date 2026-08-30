"""MCP tools that let an external coding agent reason over Stackforge data."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from . import __version__
from .api import ApiError, GameStackApplication
from .scopes import candidate_view


_PLATFORMS = {"pc", "mobile", "webgl", "vr", "quest"}
_BUDGETS = {"free", "mixed", "owned_first"}
_SCOPES = {"", "owned_assets", "asset_store_market", "community"}
_SOURCES = {"", "local", "github", "openupm", "asset_store", "asset_store_cache"}
_OWNERSHIP = {"", "candidate", "owned", "installed", "unknown"}


def _bounded_text(value: str, *, name: str, maximum: int) -> str:
    result = str(value or "").strip()
    if len(result) > maximum:
        raise ValueError(f"{name} is too long (maximum {maximum}).")
    return result


def _candidate_evidence(value: dict[str, Any]) -> dict[str, Any]:
    """Return only fields useful to model judgment, never local metadata."""
    return {
        "id": value.get("id"),
        "title": value.get("title"),
        "source": value.get("source"),
        "url": value.get("url"),
        "description": str(value.get("description") or "")[:800],
        "scope": value.get("scope"),
        "ownership": value.get("ownership"),
        "inventory_state": value.get("inventory_state"),
        "ownership_evidence": value.get("ownership_evidence"),
        "categories": list(value.get("categories") or [])[:20],
        "tags": list(value.get("tags") or [])[:24],
        "license": value.get("license"),
        "version": value.get("version"),
        "unity_version": value.get("unity_version"),
        "render_pipeline": value.get("render_pipeline"),
        "platforms": list(value.get("platforms") or [])[:12],
        "stars": value.get("stars", 0),
        "downloads": value.get("downloads", 0),
        "updated_at": value.get("updated_at"),
        "installed": bool(value.get("installed")),
    }


def _api_error(exc: ApiError) -> ValueError:
    return ValueError(f"{exc.code}: {exc}")


class StackforgeMcpTools:
    """Testable MCP-facing boundary around the local application."""

    def __init__(self, app: GameStackApplication) -> None:
        self.app = app

    def status(self) -> dict[str, Any]:
        value = self.app.status()
        return {
            "version": value["version"],
            "catalog": value["catalog"],
            "requirement_categories": value["requirement_categories"],
            "workflow": (
                "Call retrieve_game_stack_evidence for a game brief, judge concrete "
                "uses and combinations yourself, then call prepare_candidate_install "
                "only for chosen IDs. Applying a reviewed plan is a separate tool."
            ),
        }

    def search_catalog(
        self,
        query: str = "",
        scope: str = "owned_assets",
        source: str = "",
        ownership: str = "",
        limit: int = 20,
    ) -> dict[str, Any]:
        query = _bounded_text(query, name="query", maximum=300)
        scope = str(scope or "").casefold()
        source = str(source or "").casefold()
        ownership = str(ownership or "").casefold()
        if scope not in _SCOPES:
            raise ValueError(f"Unsupported scope: {scope}")
        if source not in _SOURCES:
            raise ValueError(f"Unsupported source: {source}")
        if ownership not in _OWNERSHIP:
            raise ValueError(f"Unsupported ownership: {ownership}")
        try:
            value = self.app.catalog(
                query=query,
                scope=scope,
                source=source,
                ownership=ownership,
                limit=max(1, min(int(limit), 50)),
            )
        except ApiError as exc:
            raise _api_error(exc) from exc
        return {
            "count": value["count"],
            "items": [_candidate_evidence(item) for item in value["items"]],
            "catalog": value["summary"],
        }

    def search_owned_asset_rag(
        self,
        query: str,
        limit: int = 20,
    ) -> dict[str, Any]:
        query = _bounded_text(query, name="query", maximum=300)
        if not query:
            raise ValueError("query is required.")
        try:
            return self.app.search_asset_rag(
                query=query,
                limit=max(1, min(int(limit), 50)),
            )
        except ApiError as exc:
            raise _api_error(exc) from exc

    def get_candidate(self, candidate_id: str) -> dict[str, Any]:
        candidate_id = _bounded_text(
            candidate_id, name="candidate_id", maximum=300
        )
        candidate = self.app.repository.get_candidate(candidate_id)
        if candidate is None:
            raise ValueError("candidate_not_found: Candidate is not in the local catalog.")
        return {"candidate": _candidate_evidence(candidate_view(candidate))}

    def retrieve_game_stack_evidence(
        self,
        game_brief: str,
        project_path: str = "",
        platform: str = "pc",
        budget: str = "owned_first",
        remote: bool = True,
    ) -> dict[str, Any]:
        game_brief = _bounded_text(
            game_brief, name="game_brief", maximum=8000
        )
        project_path = _bounded_text(
            project_path, name="project_path", maximum=2048
        )
        platform = str(platform or "pc").casefold()
        budget = str(budget or "owned_first").casefold()
        if not game_brief:
            raise ValueError("game_brief is required.")
        if platform not in _PLATFORMS:
            raise ValueError(f"Unsupported platform: {platform}")
        if budget not in _BUDGETS:
            raise ValueError(f"Unsupported budget: {budget}")
        try:
            result = self.app.recommend({
                "prompt": game_brief,
                "project_path": project_path,
                "platform": platform,
                "budget": budget,
                "remote": bool(remote),
            })
        except ApiError as exc:
            raise _api_error(exc) from exc

        candidate_groups: list[dict[str, Any]] = []
        for requirement in result["requirements"]:
            options = []
            for item in result["recommendations"].get(requirement["key"], [])[:12]:
                options.append({
                    "candidate": _candidate_evidence(item["candidate"]),
                    "local_score": item["score"],
                    "reasons": item.get("reasons", []),
                    "risks": item.get("risks", []),
                })
            candidate_groups.append({
                "requirement": requirement,
                "candidates": options,
            })
        strategy_plans = [
            {
                "id": plan["id"],
                "title": plan["title"],
                "coverage": plan["coverage"],
                "selected_candidate_ids": [
                    item["candidate"]["id"] for item in plan["selected"]
                ],
                "missing": plan["missing"],
            }
            for plan in result["plans"]
        ]
        return {
            "game_brief": result["prompt"],
            "platform": result["platform"],
            "budget": result["budget"],
            "project": result["project"],
            "candidate_groups": candidate_groups,
            "strategy_plans": strategy_plans,
            "official_asset_store_searches": result["asset_store_searches"],
            "source_status": result["source_status"],
            "llm_guidance": (
                "Do not blindly repeat local scores or select an item merely because "
                "it is owned. For each chosen ID, explain its concrete role in this "
                "game, how it combines with other choices, compatibility checks, and "
                "which requirements remain unfilled. Never invent candidate IDs."
            ),
        }

    def prepare_candidate_install(
        self,
        candidate_id: str,
        project_path: str,
    ) -> dict[str, Any]:
        try:
            return self.app.prepare_install({
                "candidate_id": candidate_id,
                "project_path": project_path,
            })
        except ApiError as exc:
            raise _api_error(exc) from exc

    def apply_reviewed_install(
        self,
        plan_id: str,
        approval_nonce: str,
    ) -> dict[str, Any]:
        try:
            return self.app.execute_install({
                "plan_id": plan_id,
                "approval_nonce": approval_nonce,
            })
        except ApiError as exc:
            raise _api_error(exc) from exc

    def get_install_status(self, job_id: str) -> dict[str, Any]:
        try:
            return self.app.install_job(job_id)
        except ApiError as exc:
            raise _api_error(exc) from exc

    def rollback_reviewed_install(
        self,
        job_id: str,
        rollback_nonce: str,
    ) -> dict[str, Any]:
        try:
            return self.app.rollback_install({
                "job_id": job_id,
                "rollback_nonce": rollback_nonce,
            })
        except ApiError as exc:
            raise _api_error(exc) from exc


def build_mcp_server(
    db_path: str | None = None,
) -> tuple[MCPServer[Any], GameStackApplication]:
    app = GameStackApplication(db_path)
    tools = StackforgeMcpTools(app)
    server = MCPServer(
        "stackforge",
        title="Stackforge Unity Asset Planner",
        description=(
            "Search owned Unity assets, Asset Store candidates, GitHub and OpenUPM; "
            "retrieve evidence for a complete game stack; and safely review installs."
        ),
        instructions=(
            "Use retrieve_game_stack_evidence before recommending a stack. Explain a "
            "concrete use for every selected candidate. Treat local scores as retrieval "
            "hints, verify relevance yourself, and never invent IDs. Prepare installs "
            "before applying them and show the plan to the user for approval."
        ),
        version=__version__,
    )

    read_only = ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
    network_read = ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    )

    server.tool(
        name="stackforge_status",
        description="Inspect catalog counts and the recommended MCP workflow.",
        annotations=read_only,
    )(tools.status)
    server.tool(
        name="search_unity_assets",
        description=(
            "Search the local catalog. Defaults to owned/local assets; scope can also "
            "select unpurchased Asset Store candidates or GitHub/OpenUPM."
        ),
        annotations=read_only,
    )(tools.search_catalog)
    server.tool(
        name="search_owned_asset_rag",
        description="Search AI-safe owned-asset names, tags, aliases, notes, and categories.",
        annotations=read_only,
    )(tools.search_owned_asset_rag)
    server.tool(
        name="get_unity_asset_candidate",
        description="Read one exact catalog candidate by its stable candidate ID.",
        annotations=read_only,
    )(tools.get_candidate)
    server.tool(
        name="retrieve_game_stack_evidence",
        description=(
            "Decompose a game brief and retrieve owned, unpurchased, GitHub, and OpenUPM "
            "candidates per implementation requirement. The calling LLM must judge and "
            "explain concrete uses and combinations."
        ),
        annotations=network_read,
    )(tools.retrieve_game_stack_evidence)
    server.tool(
        name="prepare_candidate_install",
        description=(
            "Create a non-mutating, approval-gated install plan for one known candidate "
            "and Unity project. Return the exact diff and one-time approval when executable."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )(tools.prepare_candidate_install)
    server.tool(
        name="apply_reviewed_install",
        description=(
            "Apply a previously reviewed exact install plan using its one-time approval. "
            "Call only after the user has approved the returned diff."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )(tools.apply_reviewed_install)
    server.tool(
        name="get_stackforge_install_status",
        description="Read one existing Stackforge install job without modifying it.",
        annotations=read_only,
    )(tools.get_install_status)
    server.tool(
        name="rollback_reviewed_install",
        description=(
            "Restore the reviewed manifest preimage using the one-time rollback nonce."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )(tools.rollback_reviewed_install)
    return server, app


def run_mcp_server(db_path: str | None = None) -> None:
    server, app = build_mcp_server(db_path)
    try:
        server.run(transport="stdio")
    finally:
        app.close()


__all__ = ["StackforgeMcpTools", "build_mcp_server", "run_mcp_server"]
