"""Command line interface for the standalone Game Stack Planner."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Sequence

from . import __version__
from .api import ApiError, GameStackApplication
from .launcher import run_local_ui


def _print(value: Any, *, as_json: bool) -> None:
    if as_json or isinstance(value, (dict, list)):
        print(json.dumps(value, ensure_ascii=False, indent=2))
    else:
        print(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="game-stack",
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

    app = GameStackApplication(args.db)
    try:
        if args.command == "scan":
            result = app.scan({"path": args.project})
        elif args.command == "scan-cache":
            result = app.scan_cache({"path": args.path or ""})
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
