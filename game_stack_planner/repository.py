"""Separate SQLite catalog for game stack planning."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from dataclasses import replace
from hashlib import sha256
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .asset_store import normalize_asset_store_product_url
from .models import Candidate, ProjectSnapshot
from .unity_my_assets import UnityMyAssetsExport


def default_database_path() -> Path:
    base = os.getenv("LOCALAPPDATA") or os.getenv("APPDATA")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "game-stack-planner" / "catalog.sqlite3"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class StackRepository:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or default_database_path()).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.path,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self.init()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def init(self) -> None:
        with self._lock:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS candidates (
                    id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL,
                    description TEXT NOT NULL,
                    categories_json TEXT NOT NULL,
                    tags_json TEXT NOT NULL,
                    ownership TEXT NOT NULL,
                    installed INTEGER NOT NULL,
                    license TEXT,
                    version TEXT,
                    unity_version TEXT,
                    render_pipeline TEXT,
                    platforms_json TEXT NOT NULL,
                    stars INTEGER NOT NULL,
                    downloads INTEGER NOT NULL,
                    updated_at TEXT,
                    archived INTEGER NOT NULL,
                    metadata_json TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS candidates_source_idx
                    ON candidates(source);
                CREATE INDEX IF NOT EXISTS candidates_ownership_idx
                    ON candidates(ownership);

                CREATE TABLE IF NOT EXISTS project_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    path TEXT NOT NULL,
                    name TEXT NOT NULL,
                    unity_version TEXT,
                    render_pipeline TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    scanned_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS project_path_idx
                    ON project_snapshots(path, scanned_at DESC);

                CREATE TABLE IF NOT EXISTS recommendation_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    prompt TEXT NOT NULL,
                    project_path TEXT,
                    platform TEXT NOT NULL,
                    budget TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS install_jobs (
                    id TEXT PRIMARY KEY,
                    candidate_id TEXT NOT NULL,
                    project_path TEXT NOT NULL,
                    status TEXT NOT NULL,
                    plan_json TEXT NOT NULL,
                    approval_hash TEXT,
                    rollback_hash TEXT,
                    expires_at TEXT,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS install_jobs_status_idx
                    ON install_jobs(status, updated_at DESC);

                CREATE TABLE IF NOT EXISTS asset_store_purchase_evidence (
                    candidate_id TEXT PRIMARY KEY
                        REFERENCES candidates(id) ON DELETE CASCADE,
                    evidence_kind TEXT NOT NULL,
                    verified INTEGER NOT NULL CHECK (verified IN (0, 1)),
                    source TEXT NOT NULL,
                    first_asserted_at TEXT NOT NULL,
                    last_asserted_at TEXT NOT NULL,
                    assertion_count INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS asset_rag_documents (
                    id TEXT PRIMARY KEY,
                    candidate_id TEXT NOT NULL UNIQUE
                        REFERENCES candidates(id) ON DELETE CASCADE,
                    user_alias TEXT NOT NULL,
                    notes TEXT NOT NULL,
                    categories_json TEXT NOT NULL,
                    search_text TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS asset_rag_documents_updated_idx
                    ON asset_rag_documents(updated_at DESC);

                CREATE VIRTUAL TABLE IF NOT EXISTS asset_rag_search USING fts5(
                    document_id UNINDEXED,
                    user_alias,
                    notes,
                    search_text,
                    tokenize = 'unicode61'
                );
                """
            )
            rag_columns = {
                str(row["name"])
                for row in self._connection.execute(
                    "PRAGMA table_info(asset_rag_documents)"
                ).fetchall()
            }
            if "document_source" not in rag_columns:
                self._connection.execute(
                    "ALTER TABLE asset_rag_documents "
                    "ADD COLUMN document_source TEXT NOT NULL "
                    "DEFAULT 'user_authored'"
                )
            self._connection.commit()

    def upsert_candidates(self, candidates: Iterable[Candidate]) -> int:
        timestamp = _now()
        count = 0
        with self._lock:
            for candidate in candidates:
                existing = self._connection.execute(
                    """
                    SELECT categories_json, tags_json, metadata_json
                    FROM candidates
                    WHERE id = ?
                    """,
                    (candidate.id,),
                ).fetchone()

                def merged_values(
                    column: str,
                    current: tuple[str, ...],
                ) -> tuple[str, ...]:
                    if existing is None:
                        return tuple(dict.fromkeys(current))
                    raw = json.loads(str(existing[column]))
                    persisted = (
                        tuple(str(item) for item in raw)
                        if isinstance(raw, list)
                        else ()
                    )
                    return tuple(dict.fromkeys((*persisted, *current)))

                categories = merged_values(
                    "categories_json",
                    candidate.categories,
                )
                tags = merged_values("tags_json", candidate.tags)
                persisted_metadata: dict[str, Any] = {}
                if existing is not None:
                    raw_metadata = json.loads(str(existing["metadata_json"]))
                    if isinstance(raw_metadata, dict):
                        persisted_metadata = raw_metadata
                metadata = {**persisted_metadata, **candidate.metadata}
                self._connection.execute(
                    """
                    INSERT INTO candidates (
                        id, source, external_id, title, url, description,
                        categories_json, tags_json, ownership, installed,
                        license, version, unity_version, render_pipeline,
                        platforms_json, stars, downloads, updated_at, archived,
                        metadata_json, first_seen_at, last_seen_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?
                    )
                    ON CONFLICT(id) DO UPDATE SET
                        source = excluded.source,
                        external_id = excluded.external_id,
                        title = excluded.title,
                        url = excluded.url,
                        description = CASE
                            WHEN excluded.description <> ''
                            THEN excluded.description
                            ELSE candidates.description
                        END,
                        categories_json = excluded.categories_json,
                        tags_json = excluded.tags_json,
                        ownership = CASE
                            WHEN candidates.ownership IN ('owned', 'installed')
                            THEN candidates.ownership
                            ELSE excluded.ownership
                        END,
                        installed = MAX(candidates.installed, excluded.installed),
                        license = COALESCE(excluded.license, candidates.license),
                        version = COALESCE(excluded.version, candidates.version),
                        unity_version = COALESCE(
                            excluded.unity_version,
                            candidates.unity_version
                        ),
                        render_pipeline = COALESCE(
                            excluded.render_pipeline,
                            candidates.render_pipeline
                        ),
                        platforms_json = excluded.platforms_json,
                        stars = excluded.stars,
                        downloads = excluded.downloads,
                        updated_at = COALESCE(
                            excluded.updated_at,
                            candidates.updated_at
                        ),
                        archived = excluded.archived,
                        metadata_json = excluded.metadata_json,
                        last_seen_at = excluded.last_seen_at
                    """,
                    (
                        candidate.id,
                        candidate.source,
                        candidate.external_id,
                        candidate.title,
                        candidate.url,
                        candidate.description,
                        _json(categories),
                        _json(tags),
                        candidate.ownership,
                        int(candidate.installed),
                        candidate.license,
                        candidate.version,
                        candidate.unity_version,
                        candidate.render_pipeline,
                        _json(candidate.platforms),
                        max(0, int(candidate.stars)),
                        max(0, int(candidate.downloads)),
                        candidate.updated_at,
                        int(candidate.archived),
                        _json(metadata),
                        timestamp,
                        timestamp,
                    ),
                )
                count += 1
            self._connection.commit()
        return count

    @staticmethod
    def _candidate(row: sqlite3.Row) -> Candidate:
        def values(name: str) -> tuple[str, ...]:
            raw = json.loads(str(row[name]))
            return tuple(str(item) for item in raw) if isinstance(raw, list) else ()

        metadata = json.loads(str(row["metadata_json"]))
        return Candidate(
            id=str(row["id"]),
            source=str(row["source"]),
            external_id=str(row["external_id"]),
            title=str(row["title"]),
            url=str(row["url"]),
            description=str(row["description"]),
            categories=values("categories_json"),
            tags=values("tags_json"),
            ownership=str(row["ownership"]),
            installed=bool(row["installed"]),
            license=str(row["license"]) if row["license"] is not None else None,
            version=str(row["version"]) if row["version"] is not None else None,
            unity_version=(
                str(row["unity_version"])
                if row["unity_version"] is not None
                else None
            ),
            render_pipeline=(
                str(row["render_pipeline"])
                if row["render_pipeline"] is not None
                else None
            ),
            platforms=values("platforms_json"),
            stars=int(row["stars"]),
            downloads=int(row["downloads"]),
            updated_at=(
                str(row["updated_at"]) if row["updated_at"] is not None else None
            ),
            archived=bool(row["archived"]),
            metadata=metadata if isinstance(metadata, dict) else {},
        )

    def get_candidate(self, candidate_id: str) -> Candidate | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM candidates WHERE id = ?",
                (str(candidate_id),),
            ).fetchone()
        return self._candidate(row) if row is not None else None

    def list_candidates(
        self,
        *,
        query: str = "",
        source: str | None = None,
        ownership: str | None = None,
        limit: int = 100,
    ) -> list[Candidate]:
        clauses: list[str] = []
        parameters: list[Any] = []
        normalized_query = str(query or "").strip()
        if normalized_query:
            clauses.append(
                "(title LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\' "
                "OR tags_json LIKE ? ESCAPE '\\' OR categories_json LIKE ? "
                "ESCAPE '\\' OR metadata_json LIKE ? ESCAPE '\\')"
            )
            escaped = (
                normalized_query.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            pattern = f"%{escaped}%"
            parameters.extend((pattern, pattern, pattern, pattern, pattern))
        if source:
            clauses.append("source = ?")
            parameters.append(source)
        if ownership:
            clauses.append("ownership = ?")
            parameters.append(ownership)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        bounded_limit = max(1, min(int(limit), 5000))
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT * FROM candidates
                {where}
                ORDER BY installed DESC,
                         CASE ownership
                            WHEN 'owned' THEN 0
                            WHEN 'installed' THEN 1
                            ELSE 2
                         END,
                         stars DESC,
                         downloads DESC,
                         title COLLATE NOCASE
                LIMIT ?
                """,
                (*parameters, bounded_limit),
            ).fetchall()
        return [self._candidate(row) for row in rows]

    def save_project(self, snapshot: ProjectSnapshot) -> int:
        with self._lock:
            cursor = self._connection.execute(
                """
                INSERT INTO project_snapshots (
                    path, name, unity_version, render_pipeline,
                    snapshot_json, scanned_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.path,
                    snapshot.name,
                    snapshot.unity_version,
                    snapshot.render_pipeline,
                    _json(snapshot.to_dict()),
                    _now(),
                ),
            )
            self._connection.commit()
            return int(cursor.lastrowid)

    def save_recommendation(
        self,
        *,
        prompt: str,
        project_path: str | None,
        platform: str,
        budget: str,
        result: dict[str, Any],
    ) -> int:
        with self._lock:
            cursor = self._connection.execute(
                """
                INSERT INTO recommendation_runs (
                    prompt, project_path, platform, budget,
                    result_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    prompt,
                    project_path,
                    platform,
                    budget,
                    _json(result),
                    _now(),
                ),
            )
            self._connection.commit()
            return int(cursor.lastrowid)

    def save_manual_asset(
        self,
        *,
        url: str,
        title: str,
        notes: str = "",
        ownership: str = "candidate",
        categories: tuple[str, ...] = (),
    ) -> Candidate:
        product_url = normalize_asset_store_product_url(url)
        candidate_id = f"asset_store:{product_url.product_id}"
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM candidates WHERE id = ?",
                (candidate_id,),
            ).fetchone()
        existing = self._candidate(row) if row is not None else None
        merged_categories = tuple(dict.fromkeys((
            *(existing.categories if existing is not None else ()),
            *categories,
        )))
        effective_ownership = (
            existing.ownership
            if existing is not None
            and existing.ownership in {"owned", "installed"}
            else ownership
        )
        candidate = Candidate(
            id=candidate_id,
            source="asset_store",
            external_id=product_url.product_id,
            title=title,
            url=product_url.canonical_url,
            description=notes or (
                existing.description if existing is not None else ""
            ),
            categories=merged_categories,
            tags=("manual",),
            ownership=effective_ownership,
            installed=False,
            metadata={
                "capture": "manual",
                "fetched": False,
                "inventory_state": (
                    "confirmed_owned"
                    if effective_ownership in {"owned", "installed"}
                    else "unknown"
                ),
                "ownership_evidence": {
                    "kind": (
                        "user_asserted"
                        if effective_ownership in {"owned", "installed"}
                        else "none"
                    ),
                    "verified": False,
                },
            },
        )
        self.upsert_candidates((candidate,))
        return candidate

    def save_owned_asset_rag(
        self,
        *,
        url: str,
        title: str,
        user_alias: str,
        notes: str,
        categories: tuple[str, ...],
        category_titles: tuple[str, ...],
    ) -> Candidate:
        """Index only user-authored retrieval fields for an asserted purchase."""
        candidate = self.save_manual_asset(
            url=url,
            title=title,
            notes=notes,
            ownership="owned",
            categories=categories,
        )
        timestamp = _now()
        candidate = replace(
            candidate,
            tags=tuple(dict.fromkeys((*candidate.tags, "user_indexed"))),
            metadata={
                **candidate.metadata,
                "rag_indexed": True,
                "rag_fields": ["user_alias", "notes", "categories"],
                "local_availability": "unknown",
                "ownership_evidence": {
                    "kind": "user_asserted",
                    "verified": False,
                },
            },
        )
        self.upsert_candidates((candidate,))

        document_id = f"asset_rag:{candidate.external_id}"
        safe_categories = tuple(dict.fromkeys(categories))
        search_text = " ".join((
            *safe_categories,
            *tuple(dict.fromkeys(category_titles)),
        ))
        content_hash = sha256(_json({
            "user_alias": user_alias,
            "notes": notes,
            "categories": safe_categories,
        }).encode("utf-8")).hexdigest()

        with self._lock:
            self._connection.execute(
                """
                INSERT INTO asset_store_purchase_evidence (
                    candidate_id, evidence_kind, verified, source,
                    first_asserted_at, last_asserted_at, assertion_count
                ) VALUES (?, 'user_asserted', 0, 'asset_store_extension', ?, ?, 1)
                ON CONFLICT(candidate_id) DO UPDATE SET
                    evidence_kind = 'user_asserted',
                    verified = 0,
                    source = 'asset_store_extension',
                    last_asserted_at = excluded.last_asserted_at,
                    assertion_count =
                        asset_store_purchase_evidence.assertion_count + 1
                """,
                (candidate.id, timestamp, timestamp),
            )
            self._connection.execute(
                """
                INSERT INTO asset_rag_documents (
                    id, candidate_id, user_alias, notes, categories_json,
                    search_text, content_hash, created_at, updated_at,
                    document_source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'user_authored')
                ON CONFLICT(candidate_id) DO UPDATE SET
                    user_alias = excluded.user_alias,
                    notes = excluded.notes,
                    categories_json = excluded.categories_json,
                    search_text = excluded.search_text,
                    content_hash = excluded.content_hash,
                    updated_at = excluded.updated_at,
                    document_source = 'user_authored'
                """,
                (
                    document_id,
                    candidate.id,
                    user_alias,
                    notes,
                    _json(safe_categories),
                    search_text,
                    content_hash,
                    timestamp,
                    timestamp,
                ),
            )
            self._connection.execute(
                "DELETE FROM asset_rag_search WHERE document_id = ?",
                (document_id,),
            )
            self._connection.execute(
                """
                INSERT INTO asset_rag_search (
                    document_id, user_alias, notes, search_text
                ) VALUES (?, ?, ?, ?)
                """,
                (document_id, user_alias, notes, search_text),
            )
            self._connection.commit()
        return candidate

    def import_unity_my_assets(
        self,
        export: UnityMyAssetsExport,
        *,
        export_path: str,
    ) -> int:
        """Upsert authenticated Unity Editor My Assets metadata and RAG rows."""
        timestamp = _now()
        candidates = tuple(
            Candidate(
                id=f"asset_store:{asset.product_id}",
                source="asset_store",
                external_id=asset.product_id,
                title=asset.display_name,
                url=(
                    "https://assetstore.unity.com/packages/package/"
                    f"{asset.product_id}"
                ),
                description="",
                tags=tuple(dict.fromkeys((*asset.tags, "unity_my_assets"))),
                ownership="owned",
                installed=False,
                metadata={
                    "capture": "unity_editor_bridge",
                    "fetched": True,
                    "inventory_state": "confirmed_owned",
                    "purchase_time": asset.purchased_time,
                    "hidden_in_my_assets": asset.hidden,
                    "unity_version": export.unity_version,
                    "my_assets_generated_at": export.generated_at_utc,
                    "ownership_evidence": {
                        "kind": "unity_editor_my_assets",
                        "verified": True,
                        "verification_scope": "logged_in_unity_editor_session",
                    },
                },
            )
            for asset in export.assets
        )
        self.upsert_candidates(candidates)

        with self._lock:
            current_ids = {candidate.id for candidate in candidates}
            stale_rows = self._connection.execute(
                """
                SELECT e.candidate_id, c.metadata_json,
                       d.id AS document_id, d.document_source
                FROM asset_store_purchase_evidence AS e
                JOIN candidates AS c ON c.id = e.candidate_id
                LEFT JOIN asset_rag_documents AS d
                  ON d.candidate_id = e.candidate_id
                WHERE e.source = 'stackforge_unity_editor_bridge'
                """
            ).fetchall()
            for row in stale_rows:
                candidate_id = str(row["candidate_id"])
                if candidate_id in current_ids:
                    continue
                document_source = str(row["document_source"] or "")
                raw_metadata = json.loads(str(row["metadata_json"]))
                metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
                if document_source == "user_authored":
                    evidence = {"kind": "user_asserted", "verified": False}
                    self._connection.execute(
                        """
                        UPDATE asset_store_purchase_evidence
                        SET evidence_kind = 'user_asserted', verified = 0,
                            source = 'asset_store_extension',
                            last_asserted_at = ?
                        WHERE candidate_id = ?
                        """,
                        (timestamp, candidate_id),
                    )
                    metadata.update({
                        "inventory_state": "confirmed_owned",
                        "ownership_evidence": evidence,
                    })
                else:
                    document_id = str(row["document_id"] or "")
                    if document_id:
                        self._connection.execute(
                            "DELETE FROM asset_rag_search WHERE document_id = ?",
                            (document_id,),
                        )
                        self._connection.execute(
                            "DELETE FROM asset_rag_documents WHERE id = ?",
                            (document_id,),
                        )
                    self._connection.execute(
                        "DELETE FROM asset_store_purchase_evidence "
                        "WHERE candidate_id = ?",
                        (candidate_id,),
                    )
                    metadata.update({
                        "inventory_state": "not_in_latest_my_assets",
                        "ownership_evidence": {
                            "kind": "unity_editor_my_assets_stale",
                            "verified": False,
                        },
                    })
                    self._connection.execute(
                        "UPDATE candidates SET ownership = 'unknown' WHERE id = ?",
                        (candidate_id,),
                    )
                self._connection.execute(
                    "UPDATE candidates SET metadata_json = ?, last_seen_at = ? "
                    "WHERE id = ?",
                    (_json(metadata), timestamp, candidate_id),
                )

            for candidate, asset in zip(candidates, export.assets, strict=True):
                self._connection.execute(
                    """
                    INSERT INTO asset_store_purchase_evidence (
                        candidate_id, evidence_kind, verified, source,
                        first_asserted_at, last_asserted_at, assertion_count
                    ) VALUES (
                        ?, 'unity_editor_my_assets', 1,
                        'stackforge_unity_editor_bridge', ?, ?, 1
                    )
                    ON CONFLICT(candidate_id) DO UPDATE SET
                        evidence_kind = 'unity_editor_my_assets',
                        verified = 1,
                        source = 'stackforge_unity_editor_bridge',
                        last_asserted_at = excluded.last_asserted_at,
                        assertion_count =
                            asset_store_purchase_evidence.assertion_count + 1
                    """,
                    (candidate.id, timestamp, timestamp),
                )
                existing = self._connection.execute(
                    """
                    SELECT document_source
                    FROM asset_rag_documents
                    WHERE candidate_id = ?
                    """,
                    (candidate.id,),
                ).fetchone()
                if (
                    existing is not None
                    and str(existing["document_source"]) == "user_authored"
                ):
                    continue
                document_id = f"asset_rag:{candidate.external_id}"
                search_tags = tuple(dict.fromkeys(asset.tags))
                search_text = " ".join((asset.display_name, *search_tags))
                content_hash = sha256(_json({
                    "display_name": asset.display_name,
                    "tags": search_tags,
                    "source": "unity_editor_my_assets",
                }).encode("utf-8")).hexdigest()
                self._connection.execute(
                    """
                    INSERT INTO asset_rag_documents (
                        id, candidate_id, user_alias, notes, categories_json,
                        search_text, content_hash, created_at, updated_at,
                        document_source
                    ) VALUES (?, ?, ?, '', ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(candidate_id) DO UPDATE SET
                        user_alias = excluded.user_alias,
                        notes = '',
                        categories_json = excluded.categories_json,
                        search_text = excluded.search_text,
                        content_hash = excluded.content_hash,
                        updated_at = excluded.updated_at,
                        document_source = excluded.document_source
                    """,
                    (
                        document_id,
                        candidate.id,
                        asset.display_name,
                        _json(search_tags),
                        search_text,
                        content_hash,
                        timestamp,
                        timestamp,
                        "unity_editor_my_assets",
                    ),
                )
                self._connection.execute(
                    "DELETE FROM asset_rag_search WHERE document_id = ?",
                    (document_id,),
                )
                self._connection.execute(
                    """
                    INSERT INTO asset_rag_search (
                        document_id, user_alias, notes, search_text
                    ) VALUES (?, ?, '', ?)
                    """,
                    (document_id, asset.display_name, search_text),
                )
            self._connection.commit()
        return len(candidates)

    def search_asset_rag(
        self,
        *,
        query: str = "",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Return the deliberately narrow, AI-safe retrieval projection."""
        clauses: list[str] = []
        parameters: list[Any] = []
        normalized_query = str(query or "").strip()
        if normalized_query:
            for term in tuple(dict.fromkeys(normalized_query.split())):
                escaped = (
                    term.replace("\\", "\\\\")
                    .replace("%", "\\%")
                    .replace("_", "\\_")
                )
                pattern = f"%{escaped}%"
                clauses.append(
                    "(d.user_alias LIKE ? ESCAPE '\\' "
                    "OR d.notes LIKE ? ESCAPE '\\' "
                    "OR d.categories_json LIKE ? ESCAPE '\\' "
                    "OR d.search_text LIKE ? ESCAPE '\\')"
                )
                parameters.extend((pattern, pattern, pattern, pattern))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        bounded_limit = max(1, min(int(limit), 100))
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT d.candidate_id, d.user_alias, d.notes,
                       d.categories_json, d.document_source,
                       e.evidence_kind, e.verified
                FROM asset_rag_documents AS d
                LEFT JOIN asset_store_purchase_evidence AS e
                  ON e.candidate_id = d.candidate_id
                {where}
                ORDER BY d.updated_at DESC, d.candidate_id
                LIMIT ?
                """,
                (*parameters, bounded_limit),
            ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            raw_categories = json.loads(str(row["categories_json"]))
            results.append({
                "candidate_id": str(row["candidate_id"]),
                "user_alias": str(row["user_alias"]),
                "notes": str(row["notes"]),
                "categories": (
                    tuple(str(value) for value in raw_categories)
                    if isinstance(raw_categories, list)
                    else ()
                ),
                "ownership_evidence": {
                    "kind": str(row["evidence_kind"] or "user_asserted"),
                    "verified": bool(row["verified"] or False),
                },
            })
        return results

    @staticmethod
    def _install_job(row: sqlite3.Row) -> dict[str, Any]:
        plan = json.loads(str(row["plan_json"]))
        result = json.loads(str(row["result_json"]))
        return {
            "id": str(row["id"]),
            "candidate_id": str(row["candidate_id"]),
            "project_path": str(row["project_path"]),
            "status": str(row["status"]),
            "plan": plan if isinstance(plan, dict) else {},
            "result": result if isinstance(result, dict) else {},
            "expires_at": (
                str(row["expires_at"])
                if row["expires_at"] is not None
                else None
            ),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    def create_install_job(
        self,
        *,
        job_id: str,
        candidate_id: str,
        project_path: str,
        status: str,
        plan: dict[str, Any],
        approval_hash: str | None = None,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        timestamp = _now()
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO install_jobs (
                    id, candidate_id, project_path, status, plan_json,
                    approval_hash, rollback_hash, expires_at, result_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    candidate_id,
                    project_path,
                    status,
                    _json(plan),
                    approval_hash,
                    expires_at,
                    _json({}),
                    timestamp,
                    timestamp,
                ),
            )
            self._connection.commit()
        job = self.get_install_job(job_id)
        if job is None:
            raise RuntimeError("Install job was not persisted.")
        return job

    def get_install_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM install_jobs WHERE id = ?",
                (str(job_id),),
            ).fetchone()
        return self._install_job(row) if row is not None else None

    def claim_install_job(
        self,
        *,
        job_id: str,
        approval_hash: str,
        now: str,
    ) -> dict[str, Any] | None:
        with self._lock:
            cursor = self._connection.execute(
                """
                UPDATE install_jobs
                SET status = 'applying', approval_hash = NULL, updated_at = ?
                WHERE id = ?
                  AND status = 'awaiting_approval'
                  AND approval_hash = ?
                  AND expires_at IS NOT NULL
                  AND expires_at >= ?
                """,
                (now, job_id, approval_hash, now),
            )
            self._connection.commit()
            if cursor.rowcount != 1:
                return None
        return self.get_install_job(job_id)

    def finish_install_job(
        self,
        *,
        job_id: str,
        expected_status: str,
        status: str,
        result: dict[str, Any],
        rollback_hash: str | None = None,
        expires_at: str | None = None,
    ) -> dict[str, Any] | None:
        timestamp = _now()
        with self._lock:
            cursor = self._connection.execute(
                """
                UPDATE install_jobs
                SET status = ?, result_json = ?, rollback_hash = ?,
                    expires_at = ?, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    status,
                    _json(result),
                    rollback_hash,
                    expires_at,
                    timestamp,
                    job_id,
                    expected_status,
                ),
            )
            self._connection.commit()
            if cursor.rowcount != 1:
                return None
        return self.get_install_job(job_id)

    def claim_rollback_job(
        self,
        *,
        job_id: str,
        rollback_hash: str,
        now: str,
    ) -> dict[str, Any] | None:
        with self._lock:
            cursor = self._connection.execute(
                """
                UPDATE install_jobs
                SET status = 'rolling_back', rollback_hash = NULL,
                    updated_at = ?
                WHERE id = ?
                  AND status IN ('applied_waiting_for_unity', 'verified')
                  AND rollback_hash = ?
                  AND expires_at IS NOT NULL
                  AND expires_at >= ?
                """,
                (now, job_id, rollback_hash, now),
            )
            self._connection.commit()
            if cursor.rowcount != 1:
                return None
        return self.get_install_job(job_id)

    def summary(self) -> dict[str, int]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT source, COUNT(*) AS count
                FROM candidates
                GROUP BY source
                """
            ).fetchall()
            owned = self._connection.execute(
                """
                SELECT COUNT(*) FROM candidates
                WHERE ownership IN ('owned', 'installed')
                """
            ).fetchone()[0]
            total = self._connection.execute(
                "SELECT COUNT(*) FROM candidates"
            ).fetchone()[0]
            projects = self._connection.execute(
                "SELECT COUNT(DISTINCT path) FROM project_snapshots"
            ).fetchone()[0]
            runs = self._connection.execute(
                "SELECT COUNT(*) FROM recommendation_runs"
            ).fetchone()[0]
            owned_assets = self._connection.execute(
                """
                SELECT COUNT(*) FROM candidates
                WHERE source = 'asset_store_cache'
                   OR source = 'local'
                   OR (
                        source = 'asset_store'
                        AND ownership IN ('owned', 'installed')
                   )
                """
            ).fetchone()[0]
            market = self._connection.execute(
                """
                SELECT COUNT(*) FROM candidates
                WHERE source = 'asset_store'
                  AND ownership NOT IN ('owned', 'installed')
                """
            ).fetchone()[0]
            community = self._connection.execute(
                """
                SELECT COUNT(*) FROM candidates
                WHERE source IN ('github', 'openupm')
                """
            ).fetchone()[0]
            rag_owned = self._connection.execute(
                "SELECT COUNT(*) FROM asset_rag_documents"
            ).fetchone()[0]
        return {
            "total": int(total),
            "owned_or_installed": int(owned),
            "projects": int(projects),
            "recommendation_runs": int(runs),
            "owned_assets": int(owned_assets),
            "asset_store_market": int(market),
            "community": int(community),
            "asset_rag_owned": int(rag_owned),
            **{str(row["source"]): int(row["count"]) for row in rows},
        }


__all__ = ["StackRepository", "default_database_path"]
