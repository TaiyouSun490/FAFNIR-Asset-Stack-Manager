"""Command line interface for Fafnir Asset Stack Manager."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Sequence

from . import __version__
from .api import ApiError, GameStackApplication
from .launcher import run_local_ui


def _print(value: Any, *, as_json: bool) -> None:
    # Windows PowerShell can still expose a legacy cp932 stdout even when the
    # payload is valid UTF-8 JSON. Fafnir responses contain publisher text
    # and punctuation outside that code page, so make the CLI contract UTF-8.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass
    if as_json or isinstance(value, (dict, list)):
        print(json.dumps(value, ensure_ascii=False, indent=2))
    else:
        print(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fafnir",
        description=(
            "Turn a game idea and Unity project into an evidence-backed "
            "asset/package/repository plan."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--db", help="Use a custom catalog database.")
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    subparsers = parser.add_subparsers(dest="command")

    ui = subparsers.add_parser("ui", help="Open the local graphical interface.")
    ui.add_argument("--port", type=int, default=8770)
    ui.add_argument("--no-browser", action="store_true")

    subparsers.add_parser(
        "mcp",
        help="Run the local stdio MCP server for Codex and other MCP clients.",
    )

    scan = subparsers.add_parser("scan", help="Inspect a local Unity project.")
    scan.add_argument("project")

    cache_scan = subparsers.add_parser(
        "scan-cache",
        help="Import downloaded .unitypackage files from Unity's local cache.",
    )
    cache_scan.add_argument(
        "--path",
        help="Use an explicit Asset Store-5.x cache directory.",
    )
    cache_scan.add_argument(
        "--inspect",
        action="store_true",
        help="Inspect archive manifests/content types without extracting files.",
    )

    my_assets = subparsers.add_parser(
        "sync-my-assets",
        help="Import the owned-asset export created by the Unity Editor bridge.",
    )
    my_assets.add_argument(
        "--path",
        help="Use an explicit unity-my-assets.json export.",
    )

    recommend = subparsers.add_parser(
        "recommend", help="Build stack recommendations for a game idea."
    )
    recommend.add_argument("--prompt", required=True)
    recommend.add_argument("--project")
    recommend.add_argument(
        "--platform",
        choices=("pc", "mobile", "webgl", "vr", "quest"),
        default="pc",
    )
    recommend.add_argument(
        "--budget",
        choices=("free", "mixed", "owned_first"),
        default="mixed",
    )
    recommend.add_argument(
        "--offline",
        action="store_true",
        help="Use only the local catalog and project.",
    )

    catalog = subparsers.add_parser("catalog", help="Search the saved catalog.")
    catalog.add_argument("--query", default="")
    catalog.add_argument(
        "--source", choices=(
            "local", "github", "openupm", "asset_store", "asset_store_cache"
        )
    )
    catalog.add_argument(
        "--ownership", choices=("candidate", "owned", "installed", "unknown")
    )
    catalog.add_argument(
        "--scope",
        choices=("owned_assets", "asset_store_market", "community"),
    )
    catalog.add_argument("--limit", type=int, default=100)

    pin = subparsers.add_parser(
        "pin", help="Manually save an official Asset Store page."
    )
    pin.add_argument("url")
    pin.add_argument("--title", required=True)
    pin.add_argument("--notes", default="")
    pin.add_argument(
        "--ownership",
        choices=("candidate", "owned", "unknown"),
        default="candidate",
    )
    pin.add_argument("--category", action="append", default=[])

    rag_search = subparsers.add_parser(
        "rag-search",
        help="Retrieve AI-safe notes for self-asserted purchased assets.",
    )
    rag_search.add_argument("--query", required=True)
    rag_search.add_argument("--limit", type=int, default=20)

    subparsers.add_parser(
        "rag-status",
        help="Inspect the active model generation, progress, and index coverage.",
    )

    rag_index = subparsers.add_parser(
        "rag-index",
        help="Build local dense embeddings for owned-asset RAG documents.",
    )
    rag_index.add_argument("--force", action="store_true")
    rag_index.add_argument("--batch-size", type=int, default=32)

    subparsers.add_parser(
        "asset-details-status",
        help="Inspect public Asset Store product metadata coverage and queue state.",
    )

    asset_details = subparsers.add_parser(
        "asset-details-sync",
        help="Resume a rate-limited sync of official public product metadata.",
    )
    asset_details.add_argument("--force", action="store_true")
    asset_details.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Maximum products this run; 0 processes every due product.",
    )
    asset_details.add_argument("--delay", type=float, default=1.5)
    asset_details.add_argument(
        "--reindex",
        action="store_true",
        help="Refresh owned-asset embeddings after metadata changes.",
    )

    validate_asset = subparsers.add_parser(
        "validate-asset",
        help="Inspect a cached Asset Store package against a Unity project.",
    )
    validate_asset.add_argument("candidate_id")
    validate_asset.add_argument("--project", required=True)
    validate_asset.add_argument(
        "--platform",
        choices=("pc", "mobile", "webgl", "vr", "quest"),
        default="pc",
    )
    validate_asset.add_argument("--cache-path")
    validate_asset.add_argument(
        "--compile",
        action="store_true",
        help="Import into a temporary project with the matching Editor and compile.",
    )

    install_plan = subparsers.add_parser(
        "install-plan",
        help="Prepare a read-only, approval-gated install plan for one candidate ID.",
    )
    install_plan.add_argument("candidate_id")
    install_plan.add_argument("--project", required=True)

    install_apply = subparsers.add_parser(
        "install-apply",
        help="Apply one previously reviewed install plan with its one-time approval.",
    )
    install_apply.add_argument("plan_id")
    install_apply.add_argument("--approval-nonce", required=True)

    install_status = subparsers.add_parser(
        "install-status",
        help="Read a persisted install job without changing the project.",
    )
    install_status.add_argument("job_id")

    install_rollback = subparsers.add_parser(
        "install-rollback",
        help="Restore the reviewed manifest preimage with a one-time approval.",
    )
    install_rollback.add_argument("job_id")
    install_rollback.add_argument("--rollback-nonce", required=True)
    for command in ("bridge-doctor", "bridge-plan"):
        bridge = subparsers.add_parser(command, help="Diagnose or prepare reviewed Unity bridge setup.")
        bridge.add_argument("--project", required=True)
    bridge_apply = subparsers.add_parser("bridge-apply", help="Apply an exact reviewed bridge plan; target Unity must be closed.")
    bridge_apply.add_argument("plan_id")
    bridge_apply.add_argument("--approval-nonce", required=True)
    bridge_status = subparsers.add_parser("bridge-status", help="Read bridge setup job and live verification evidence.")
    bridge_status.add_argument("job_id")
    bridge_rollback = subparsers.add_parser("bridge-rollback", help="Restore an unchanged bridge installation with its one-shot approval.")
    bridge_rollback.add_argument("job_id")
    bridge_rollback.add_argument("--rollback-nonce", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        args.command = "ui"
        args.port = 8770
        args.no_browser = False

    if args.command == "ui":
        run_local_ui(
            port=args.port,
            db_path=args.db,
            open_browser=not args.no_browser,
        )
        return 0

    if args.command == "mcp":
        from .mcp_server import run_mcp_server

        run_mcp_server(args.db)
        return 0

    app = GameStackApplication(args.db)
    try:
        if args.command == "scan":
            result = app.scan({"path": args.project})
        elif args.command == "scan-cache":
            result = app.scan_cache({
                "path": args.path or "",
                "inspect": bool(args.inspect),
            })
        elif args.command == "sync-my-assets":
            result = app.sync_unity_my_assets({"path": args.path or ""})
        elif args.command == "recommend":
            result = app.recommend({
                "prompt": args.prompt,
                "project_path": args.project or "",
                "platform": args.platform,
                "budget": args.budget,
                "remote": not args.offline,
            })
        elif args.command == "catalog":
            result = app.catalog(
                query=args.query,
                source=args.source or "",
                ownership=args.ownership or "",
                scope=args.scope or "",
                limit=args.limit,
            )
        elif args.command == "pin":
            result = app.save_manual({
                "url": args.url,
                "title": args.title,
                "notes": args.notes,
                "ownership": args.ownership,
                "categories": args.category,
            })
        elif args.command == "rag-search":
            result = app.search_asset_rag(
                query=args.query,
                limit=args.limit,
            )
        elif args.command == "rag-status":
            result = app.status()["rag_index"]
        elif args.command == "rag-index":
            result = app.reindex_asset_rag(
                force=args.force,
                batch_size=args.batch_size,
            )
        elif args.command == "asset-details-status":
            result = app.status()["asset_store_details"]
        elif args.command == "asset-details-sync":
            result = app.sync_asset_store_details(
                force=args.force,
                limit=args.limit,
                delay_seconds=args.delay,
            )
            if args.reindex and result["completed"]:
                try:
                    result["rag_index"] = app.reindex_asset_rag(
                        force=False,
                        batch_size=32,
                    )
                except ApiError as exc:
                    result["rag_index"] = {
                        "state": "deferred",
                        "code": exc.code,
                        "message": str(exc),
                    }
        elif args.command == "validate-asset":
            result = app.validate_asset_candidate({
                "candidate_id": args.candidate_id,
                "project_path": args.project,
                "platform": args.platform,
                "cache_path": args.cache_path or "",
                "compile": bool(args.compile),
            })
        elif args.command in ("bridge-doctor", "bridge-plan"):
            result = app.manage_unity_bridge(
                "diagnose" if args.command == "bridge-doctor" else "prepare",
                {"project_path": args.project})
        elif args.command == "bridge-apply":
            result = app.manage_unity_bridge("apply", {
                "plan_id": args.plan_id, "approval_nonce": args.approval_nonce})
        elif args.command == "bridge-status":
            result = app.manage_unity_bridge("get", {"job_id": args.job_id})
        elif args.command == "bridge-rollback":
            result = app.manage_unity_bridge("rollback", {
                "job_id": args.job_id, "rollback_nonce": args.rollback_nonce})
        elif args.command == "install-plan":
            result = app.prepare_install({
                "candidate_id": args.candidate_id,
                "project_path": args.project,
            })
        elif args.command == "install-apply":
            result = app.execute_install({
                "plan_id": args.plan_id,
                "approval_nonce": args.approval_nonce,
            })
        elif args.command == "install-status":
            result = app.install_job(args.job_id)
        elif args.command == "install-rollback":
            result = app.rollback_install({
                "job_id": args.job_id,
                "rollback_nonce": args.rollback_nonce,
            })
        else:
            parser.error(f"unknown command: {args.command}")
            return 2
        _print(result, as_json=args.json)
        return 0
    except ApiError as exc:
        payload = {
            "error": {
                "code": exc.code,
                "message": str(exc),
                "details": exc.details,
            }
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        app.close()


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_parser", "main"]
