"""Separate SQLite catalog for game stack planning."""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import struct
import threading
import unicodedata
import uuid
from dataclasses import replace
from hashlib import sha256
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .asset_store import normalize_asset_store_product_url
from .models import Candidate, ProjectSnapshot
from .requirements import derive_requirements
from .text_embeddings import TextEmbeddingBackend, normalize_vector
from .unity_my_assets import UnityMyAssetsExport


class RagIndexNotReadyError(RuntimeError):
    def __init__(self, status: dict[str, Any]) -> None:
        super().__init__(
            "owned-asset semantic index is not ready; "
            f"current state is {status['state']}"
        )
        self.status = status


class RagIndexBusyError(RuntimeError):
    """Another local indexing run is already active."""


class RagIndexCancelledError(RuntimeError):
    """The active local indexing run was cancelled."""


_RAG_INDEX_LEASE_SECONDS = 20.0
_RAG_INDEX_HEARTBEAT_SECONDS = 5.0


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
    def __init__(
        self,
        path: str | Path | None = None,
        *,
        embedding_backend: TextEmbeddingBackend | None = None,
    ) -> None:
        self.path = Path(path or default_database_path()).expanduser()
        self.embedding_backend = embedding_backend
        raw_minimum = (
            os.getenv("FAFNIR_RAG_MIN_SIMILARITY")
            or os.getenv("STACKFORGE_RAG_MIN_SIMILARITY")
            or "0.72"
        )
        try:
            self.rag_min_similarity = float(raw_minimum)
        except ValueError as exc:
            raise ValueError("FAFNIR_RAG_MIN_SIMILARITY must be numeric") from exc
        if not -1.0 <= self.rag_min_similarity <= 1.0:
            raise ValueError("FAFNIR_RAG_MIN_SIMILARITY must be between -1 and 1")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._index_run_lock = threading.Lock()
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

                CREATE TABLE IF NOT EXISTS source_sync_states (
                    source TEXT PRIMARY KEY,
                    revision TEXT NOT NULL,
                    item_count INTEGER NOT NULL DEFAULT 0,
                    source_generated_at TEXT NOT NULL DEFAULT '',
                    synced_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS asset_store_product_details (
                    candidate_id TEXT PRIMARY KEY
                        REFERENCES candidates(id) ON DELETE CASCADE,
                    source_url TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    etag TEXT NOT NULL DEFAULT '',
                    last_modified TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS asset_store_detail_jobs (
                    candidate_id TEXT PRIMARY KEY
                        REFERENCES candidates(id) ON DELETE CASCADE,
                    status TEXT NOT NULL CHECK (
                        status IN ('pending', 'fetching', 'ready', 'error')
                    ),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT NOT NULL,
                    lease_expires_at TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS asset_store_detail_jobs_state_idx
                    ON asset_store_detail_jobs(status, next_attempt_at);
                """
            )
            # The earlier lexical implementation maintained this derived FTS
            # table but never queried it. Dense search is now fail-closed until
            # a complete generation exists, so the redundant index is removed.
            self._connection.execute("DROP TABLE IF EXISTS asset_rag_search")
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
            self._ensure_rag_embedding_schema()
            self._connection.commit()

    def _ensure_rag_embedding_schema(self) -> None:
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS asset_rag_index_generations (
                generation_id TEXT PRIMARY KEY,
                model_id TEXT NOT NULL,
                model_revision TEXT NOT NULL,
                config_json TEXT NOT NULL,
                state TEXT NOT NULL,
                document_count INTEGER NOT NULL DEFAULT 0,
                indexed_count INTEGER NOT NULL DEFAULT 0,
                dimension INTEGER,
                error TEXT NOT NULL DEFAULT '',
                owner_token TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 0 CHECK (active IN (0, 1)),
                started_at TEXT,
                completed_at TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        generation_columns = {
            str(row["name"])
            for row in self._connection.execute(
                "PRAGMA table_info(asset_rag_index_generations)"
            ).fetchall()
        }
        if "owner_token" not in generation_columns:
            self._connection.execute(
                "ALTER TABLE asset_rag_index_generations "
                "ADD COLUMN owner_token TEXT NOT NULL DEFAULT ''"
            )
        existing = self._connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name = 'asset_rag_embeddings'"
        ).fetchone()
        if existing is not None:
            columns = {
                str(row["name"])
                for row in self._connection.execute(
                    "PRAGMA table_info(asset_rag_embeddings)"
                ).fetchall()
            }
            if "generation_id" not in columns:
                self._migrate_legacy_rag_embeddings()
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS asset_rag_embeddings (
                document_id TEXT NOT NULL
                    REFERENCES asset_rag_documents(id) ON DELETE CASCADE,
                generation_id TEXT NOT NULL
                    REFERENCES asset_rag_index_generations(generation_id)
                    ON DELETE CASCADE,
                dimension INTEGER NOT NULL,
                vector BLOB NOT NULL,
                content_hash TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (document_id, generation_id)
            );
            CREATE INDEX IF NOT EXISTS asset_rag_embeddings_generation_idx
                ON asset_rag_embeddings(generation_id, updated_at DESC);
            CREATE UNIQUE INDEX IF NOT EXISTS asset_rag_one_active_generation_idx
                ON asset_rag_index_generations(active) WHERE active = 1;
            """
        )

    def _migrate_legacy_rag_embeddings(self) -> None:
        """Preserve pre-generation vectors as inactive, explicitly stale data."""
        timestamp = _now()
        self._connection.execute(
            "ALTER TABLE asset_rag_embeddings RENAME TO asset_rag_embeddings_legacy"
        )
        self._connection.execute("DROP INDEX IF EXISTS asset_rag_embeddings_model_idx")
        self._connection.execute(
            """
            CREATE TABLE asset_rag_embeddings (
                document_id TEXT NOT NULL
                    REFERENCES asset_rag_documents(id) ON DELETE CASCADE,
                generation_id TEXT NOT NULL
                    REFERENCES asset_rag_index_generations(generation_id)
                    ON DELETE CASCADE,
                dimension INTEGER NOT NULL,
                vector BLOB NOT NULL,
                content_hash TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (document_id, generation_id)
            )
            """
        )
        model_rows = self._connection.execute(
            "SELECT DISTINCT model_id FROM asset_rag_embeddings_legacy"
        ).fetchall()
        for row in model_rows:
            model_id = str(row["model_id"])
            generation_id = "legacy:" + sha256(
                model_id.encode("utf-8")
            ).hexdigest()[:24]
            count = int(self._connection.execute(
                "SELECT COUNT(*) FROM asset_rag_embeddings_legacy WHERE model_id = ?",
                (model_id,),
            ).fetchone()[0])
            self._connection.execute(
                """
                INSERT INTO asset_rag_index_generations (
                    generation_id, model_id, model_revision, config_json,
                    state, document_count, indexed_count, error, active, updated_at
                ) VALUES (?, ?, 'unknown', '{}', 'stale', ?, ?,
                          'Migrated from the pre-generation index format.', 0, ?)
                """,
                (generation_id, model_id, count, count, timestamp),
            )
            self._connection.execute(
                """
                INSERT INTO asset_rag_embeddings (
                    document_id, generation_id, dimension, vector,
                    content_hash, updated_at
                )
                SELECT document_id, ?, dimension, vector, content_hash, updated_at
                FROM asset_rag_embeddings_legacy WHERE model_id = ?
                """,
                (generation_id, model_id),
            )
        self._connection.execute("DROP TABLE asset_rag_embeddings_legacy")

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
                        url = CASE
                            WHEN excluded.url LIKE
                                'https://assetstore.unity.com/packages/package/%'
                             AND candidates.url NOT LIKE
                                'https://assetstore.unity.com/packages/package/%'
                            THEN candidates.url
                            ELSE excluded.url
                        END,
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
            "search_text": search_text,
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
            self._connection.commit()
        return len(candidates)

    def enqueue_asset_store_detail_sync(
        self,
        *,
        stale_after_days: int = 30,
        force: bool = False,
    ) -> int:
        """Queue owned Asset Store records whose public metadata is absent/stale."""
        days = max(1, min(int(stale_after_days), 3650))
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        timestamp = _now()
        queued = 0
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT c.id, d.fetched_at, j.status, j.lease_expires_at,
                       j.next_attempt_at, j.last_error, j.updated_at
                FROM candidates AS c
                LEFT JOIN asset_store_product_details AS d
                  ON d.candidate_id = c.id
                LEFT JOIN asset_store_detail_jobs AS j
                  ON j.candidate_id = c.id
                WHERE c.source = 'asset_store'
                  AND c.ownership IN ('owned', 'installed')
                """
            ).fetchall()
            for row in rows:
                fetched_at = str(row["fetched_at"] or "")
                needs_refresh = bool(force or not fetched_at or fetched_at < cutoff)
                if not needs_refresh:
                    continue
                status = str(row["status"] or "")
                lease_expires = str(row["lease_expires_at"] or "")
                if status == "fetching" and lease_expires > timestamp:
                    continue
                last_error = str(row["last_error"] or "")
                if (
                    not force
                    and status == "error"
                    and last_error.startswith(
                        "Product URL was not found in Unity"
                    )
                ):
                    try:
                        failed_at = datetime.fromisoformat(
                            str(row["updated_at"] or "")
                        )
                        deferred_until = (
                            failed_at + timedelta(days=days)
                        ).isoformat()
                    except ValueError:
                        deferred_until = (
                            datetime.now(timezone.utc) + timedelta(days=days)
                        ).isoformat()
                    if str(row["next_attempt_at"] or "") < deferred_until:
                        self._connection.execute(
                            """
                            UPDATE asset_store_detail_jobs
                            SET next_attempt_at = ? WHERE candidate_id = ?
                            """,
                            (deferred_until, str(row["id"])),
                        )
                    continue
                if (
                    not force
                    and status == "error"
                    and str(row["next_attempt_at"] or "") > timestamp
                ):
                    continue
                self._connection.execute(
                    """
                    INSERT INTO asset_store_detail_jobs (
                        candidate_id, status, attempts, next_attempt_at,
                        lease_expires_at, last_error, updated_at
                    ) VALUES (?, 'pending', 0, ?, '', '', ?)
                    ON CONFLICT(candidate_id) DO UPDATE SET
                        status = 'pending',
                        next_attempt_at = excluded.next_attempt_at,
                        lease_expires_at = '',
                        last_error = '',
                        updated_at = excluded.updated_at
                    """,
                    (str(row["id"]), timestamp, timestamp),
                )
                queued += 1
            self._connection.commit()
        return queued

    def asset_store_detail_status(self) -> dict[str, Any]:
        timestamp = _now()
        with self._lock:
            counts = self._connection.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM asset_store_detail_jobs
                GROUP BY status
                """
            ).fetchall()
            due = self._connection.execute(
                """
                SELECT COUNT(*)
                FROM asset_store_detail_jobs
                WHERE status = 'pending'
                   OR (status = 'error' AND next_attempt_at <= ?)
                   OR (status = 'fetching' AND lease_expires_at <= ?)
                """,
                (timestamp, timestamp),
            ).fetchone()[0]
            enriched = self._connection.execute(
                "SELECT COUNT(*) FROM asset_store_product_details"
            ).fetchone()[0]
            owned = self._connection.execute(
                """
                SELECT COUNT(*) FROM candidates
                WHERE source = 'asset_store'
                  AND ownership IN ('owned', 'installed')
                """
            ).fetchone()[0]
            last_error = self._connection.execute(
                """
                SELECT candidate_id, last_error, updated_at
                FROM asset_store_detail_jobs
                WHERE status = 'error'
                ORDER BY updated_at DESC
                LIMIT 1
                """
            ).fetchone()
            unavailable = self._connection.execute(
                """
                SELECT COUNT(*) FROM asset_store_detail_jobs
                WHERE status = 'error'
                  AND last_error LIKE 'Product URL was not found in Unity%'
                """
            ).fetchone()[0]
        by_state = {str(row["status"]): int(row["count"]) for row in counts}
        error_count = by_state.get("error", 0)
        return {
            "owned": int(owned),
            "enriched": int(enriched),
            "coverage": 1.0 if not owned else round(int(enriched) / int(owned), 6),
            "due": int(due),
            "pending": by_state.get("pending", 0),
            "fetching": by_state.get("fetching", 0),
            "ready": by_state.get("ready", 0),
            "error": error_count,
            "public_page_unavailable": int(unavailable),
            "retryable_errors": max(0, error_count - int(unavailable)),
            "last_error": (
                {
                    "candidate_id": str(last_error["candidate_id"]),
                    "message": str(last_error["last_error"]),
                    "updated_at": str(last_error["updated_at"]),
                }
                if last_error is not None
                else None
            ),
        }

    def enqueue_asset_store_detail_candidate(self, candidate_id: str) -> bool:
        """Queue one explicitly saved Asset Store candidate, owned or not."""
        timestamp = _now()
        with self._lock:
            exists = self._connection.execute(
                "SELECT 1 FROM candidates WHERE id = ? AND source = 'asset_store'",
                (str(candidate_id),),
            ).fetchone()
            if exists is None:
                return False
            self._connection.execute(
                """
                INSERT INTO asset_store_detail_jobs (
                    candidate_id, status, attempts, next_attempt_at,
                    lease_expires_at, last_error, updated_at
                ) VALUES (?, 'pending', 0, ?, '', '', ?)
                ON CONFLICT(candidate_id) DO UPDATE SET
                    status = CASE
                        WHEN asset_store_detail_jobs.status = 'fetching'
                        THEN 'fetching'
                        ELSE 'pending'
                    END,
                    next_attempt_at = excluded.next_attempt_at,
                    last_error = '',
                    updated_at = excluded.updated_at
                """,
                (str(candidate_id), timestamp, timestamp),
            )
            self._connection.commit()
        return True

    def due_asset_store_detail_candidates(
        self,
        *,
        limit: int = 20_000,
    ) -> list[Candidate]:
        timestamp = _now()
        bounded = max(1, min(int(limit), 20_000))
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT c.*
                FROM candidates AS c
                JOIN asset_store_detail_jobs AS j ON j.candidate_id = c.id
                WHERE c.source = 'asset_store'
                  AND (
                    j.status = 'pending'
                    OR (j.status = 'error' AND j.next_attempt_at <= ?)
                    OR (j.status = 'fetching' AND j.lease_expires_at <= ?)
                  )
                ORDER BY j.updated_at, c.title COLLATE NOCASE
                LIMIT ?
                """,
                (timestamp, timestamp, bounded),
            ).fetchall()
        return [self._candidate(row) for row in rows]

    def set_asset_store_product_url(
        self,
        candidate_id: str,
        source_url: str,
    ) -> None:
        with self._lock:
            self._connection.execute(
                """
                UPDATE candidates
                SET url = ?, last_seen_at = ?
                WHERE id = ? AND source = 'asset_store'
                """,
                (str(source_url), _now(), str(candidate_id)),
            )
            self._connection.commit()

    def claim_asset_store_detail_job(
        self,
        *,
        lease_seconds: int = 300,
    ) -> Candidate | None:
        now = datetime.now(timezone.utc)
        timestamp = now.isoformat()
        lease_expires = (
            now + timedelta(seconds=max(30, min(int(lease_seconds), 3600)))
        ).isoformat()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute(
                    """
                    UPDATE asset_store_detail_jobs
                    SET status = 'pending', lease_expires_at = '', updated_at = ?
                    WHERE status = 'fetching' AND lease_expires_at <= ?
                    """,
                    (timestamp, timestamp),
                )
                # One polite public-page fetch is allowed per catalog at a
                # time. Multiple UI/MCP processes may all request maintenance;
                # job leases make the extra workers exit instead of multiplying
                # Asset Store request rate.
                active = self._connection.execute(
                    """
                    SELECT 1 FROM asset_store_detail_jobs
                    WHERE status = 'fetching' AND lease_expires_at > ?
                    LIMIT 1
                    """,
                    (timestamp,),
                ).fetchone()
                if active is not None:
                    self._connection.commit()
                    return None
                row = self._connection.execute(
                    """
                    SELECT c.*
                    FROM candidates AS c
                    JOIN asset_store_detail_jobs AS j ON j.candidate_id = c.id
                    WHERE c.source = 'asset_store'
                      AND (
                        j.status = 'pending'
                        OR (j.status = 'error' AND j.next_attempt_at <= ?)
                      )
                    ORDER BY j.updated_at, c.title COLLATE NOCASE
                    LIMIT 1
                    """,
                    (timestamp,),
                ).fetchone()
                if row is None:
                    self._connection.commit()
                    return None
                candidate_id = str(row["id"])
                cursor = self._connection.execute(
                    """
                    UPDATE asset_store_detail_jobs
                    SET status = 'fetching', attempts = attempts + 1,
                        lease_expires_at = ?, updated_at = ?
                    WHERE candidate_id = ?
                      AND status IN ('pending', 'error')
                    """,
                    (lease_expires, timestamp, candidate_id),
                )
                self._connection.commit()
                return self._candidate(row) if cursor.rowcount == 1 else None
            except Exception:
                self._connection.rollback()
                raise

    @staticmethod
    def _asset_store_detail_search_text(
        candidate: Candidate,
        details: dict[str, Any],
    ) -> str:
        publisher = details.get("publisher")
        publisher = publisher if isinstance(publisher, dict) else {}
        category = details.get("category")
        category = category if isinstance(category, dict) else {}
        dependencies = details.get("dependencies")
        dependency_titles = (
            [
                str(value.get("title") or "")
                for value in dependencies
                if isinstance(value, dict)
            ]
            if isinstance(dependencies, list)
            else []
        )
        parts = (
            candidate.title,
            str(details.get("name") or ""),
            str(publisher.get("name") or ""),
            str(category.get("name") or ""),
            str(category.get("slug") or ""),
            " ".join(str(value) for value in details.get("tags", [])),
            str(details.get("elevator_pitch") or ""),
            str(details.get("ai_description") or ""),
            str(details.get("description") or ""),
            str(details.get("key_features") or ""),
            str(details.get("compatibility_info") or ""),
            " ".join(dependency_titles),
        )
        return "\n".join(value for value in parts if value).strip()[:60_000]

    def complete_asset_store_detail_job(
        self,
        candidate_id: str,
        details: dict[str, Any],
        *,
        etag: str = "",
        last_modified: str = "",
    ) -> Candidate:
        candidate = self.get_candidate(candidate_id)
        if candidate is None or candidate.source != "asset_store":
            raise ValueError("Asset Store detail candidate does not exist.")
        if str(details.get("product_id") or "") != candidate.external_id:
            raise ValueError("Asset Store product details do not match the candidate.")
        timestamp = _now()
        publisher = details.get("publisher")
        publisher = publisher if isinstance(publisher, dict) else {}
        category = details.get("category")
        category = category if isinstance(category, dict) else {}
        official_tags = tuple(
            str(value).strip()
            for value in details.get("tags", [])
            if str(value).strip()
        )
        state = str(details.get("product_state") or "unknown").casefold()
        source_url = str(details.get("source_url") or candidate.url)
        official_description = str(details.get("description") or "")
        capture = str(candidate.metadata.get("capture") or "")
        description = (
            candidate.description
            if capture == "manual" and candidate.description
            else official_description or candidate.description
        )
        metadata = {
            **candidate.metadata,
            "asset_store_details": details,
            "details_fetched_at": str(details.get("fetched_at") or timestamp),
            "details_source": "unity_asset_store_public_product_page",
        }
        updated = replace(
            candidate,
            title=str(details.get("name") or candidate.title),
            url=source_url,
            description=description,
            tags=tuple(dict.fromkeys((*candidate.tags, *official_tags))),
            version=str(details.get("latest_version") or "") or candidate.version,
            unity_version=(
                str(details.get("original_unity_version") or "")
                or candidate.unity_version
            ),
            render_pipeline=(
                str(details.get("render_pipeline_summary") or "")
                or candidate.render_pipeline
            ),
            platforms=tuple(
                str(value) for value in details.get("platforms", [])
                if str(value).strip()
            ) or candidate.platforms,
            updated_at=(
                str(details.get("latest_release_date") or "")
                or candidate.updated_at
            ),
            archived=state in {"deprecated", "disabled", "unpublished"},
            metadata=metadata,
        )
        self.upsert_candidates((updated,))

        fetched_at = str(details.get("fetched_at") or timestamp)
        search_text = self._asset_store_detail_search_text(updated, details)
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO asset_store_product_details (
                    candidate_id, source_url, details_json, fetched_at,
                    etag, last_modified
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(candidate_id) DO UPDATE SET
                    source_url = excluded.source_url,
                    details_json = excluded.details_json,
                    fetched_at = excluded.fetched_at,
                    etag = excluded.etag,
                    last_modified = excluded.last_modified
                """,
                (
                    updated.id,
                    source_url,
                    _json(details),
                    fetched_at,
                    str(etag)[:500],
                    str(last_modified)[:500],
                ),
            )
            row = self._connection.execute(
                """
                SELECT id, user_alias, notes, categories_json, document_source,
                       created_at
                FROM asset_rag_documents
                WHERE candidate_id = ?
                """,
                (updated.id,),
            ).fetchone()
            if row is not None:
                categories = json.loads(str(row["categories_json"]))
                categories = categories if isinstance(categories, list) else []
                content_hash = sha256(_json({
                    "user_alias": str(row["user_alias"]),
                    "notes": str(row["notes"]),
                    "categories": categories,
                    "official_search_text": search_text,
                    "details_schema": str(details.get("schema") or ""),
                }).encode("utf-8")).hexdigest()
                self._connection.execute(
                    """
                    UPDATE asset_rag_documents
                    SET search_text = ?, content_hash = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (search_text, content_hash, timestamp, str(row["id"])),
                )
            self._connection.execute(
                """
                UPDATE asset_store_detail_jobs
                SET status = 'ready', next_attempt_at = ?, lease_expires_at = '',
                    last_error = '', updated_at = ?
                WHERE candidate_id = ?
                """,
                (timestamp, timestamp, updated.id),
            )
            self._connection.commit()
        result = self.get_candidate(updated.id)
        assert result is not None
        return result

    def fail_asset_store_detail_job(
        self,
        candidate_id: str,
        message: str,
        *,
        retry_after_seconds: int | None = None,
    ) -> None:
        timestamp = datetime.now(timezone.utc)
        with self._lock:
            row = self._connection.execute(
                "SELECT attempts FROM asset_store_detail_jobs WHERE candidate_id = ?",
                (str(candidate_id),),
            ).fetchone()
            attempts = int(row["attempts"]) if row is not None else 1
            delay = (
                int(retry_after_seconds)
                if retry_after_seconds is not None
                else min(86_400, 60 * (2 ** min(max(attempts - 1, 0), 10)))
            )
            next_attempt = (
                timestamp + timedelta(seconds=max(30, delay))
            ).isoformat()
            self._connection.execute(
                """
                UPDATE asset_store_detail_jobs
                SET status = 'error', next_attempt_at = ?, lease_expires_at = '',
                    last_error = ?, updated_at = ?
                WHERE candidate_id = ?
                """,
                (
                    next_attempt,
                    str(message).strip()[:2000],
                    timestamp.isoformat(),
                    str(candidate_id),
                ),
            )
            self._connection.commit()

    @staticmethod
    def _asset_title_identity(value: str) -> str:
        normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
        return "".join(character for character in normalized if character.isalnum())

    def link_asset_store_cache_candidates(self) -> int:
        """Link a uniquely named cache archive to its catalog product conservatively."""
        caches = self.list_candidates(source="asset_store_cache", limit=5000)
        products = self.list_candidates(source="asset_store", limit=5000)
        by_title: dict[str, list[Candidate]] = {}
        for cache in caches:
            identity = self._asset_title_identity(cache.title)
            if identity:
                by_title.setdefault(identity, []).append(cache)
        linked = 0
        timestamp = _now()
        with self._lock:
            for product in products:
                identity = self._asset_title_identity(product.title)
                matches = by_title.get(identity, [])
                if len(matches) != 1:
                    continue
                cache = matches[0]
                product_details = product.metadata.get("asset_store_details")
                product_details = (
                    product_details if isinstance(product_details, dict) else {}
                )
                publisher = product_details.get("publisher")
                publisher = publisher if isinstance(publisher, dict) else {}
                expected_publisher = self._asset_title_identity(
                    str(publisher.get("name") or "")
                )
                cached_publisher = self._asset_title_identity(
                    str(cache.metadata.get("publisher") or "")
                )
                if (
                    expected_publisher
                    and cached_publisher
                    and expected_publisher != cached_publisher
                ):
                    continue
                if cache.version and product.version:
                    version_state = (
                        "current" if cache.version == product.version else "different"
                    )
                else:
                    version_state = "unknown"
                product_metadata = {
                    **product.metadata,
                    "local_availability": "cached",
                    "cache_candidate_id": cache.id,
                    "cache_package": {
                        "relative_path": cache.metadata.get("cache_relative_path"),
                        "root_kind": cache.metadata.get("cache_root_kind"),
                        "size_bytes": cache.metadata.get("size_bytes"),
                        "downloaded_version": cache.version,
                        "latest_version": product.version,
                        "version_state": version_state,
                        "inspection": cache.metadata.get("package_inspection"),
                    },
                }
                cache_metadata = {
                    **cache.metadata,
                    "asset_store_candidate_id": product.id,
                    "asset_store_product_id": product.external_id,
                }
                self._connection.execute(
                    "UPDATE candidates SET metadata_json = ?, last_seen_at = ? WHERE id = ?",
                    (_json(product_metadata), timestamp, product.id),
                )
                self._connection.execute(
                    "UPDATE candidates SET metadata_json = ?, last_seen_at = ? WHERE id = ?",
                    (_json(cache_metadata), timestamp, cache.id),
                )
                linked += 1
            self._connection.commit()
        return linked

    def record_candidate_local_validation(
        self,
        candidate_id: str,
        validation: dict[str, Any],
    ) -> Candidate:
        candidate = self.get_candidate(candidate_id)
        if candidate is None:
            raise ValueError("Candidate does not exist.")
        project = validation.get("project")
        project = project if isinstance(project, dict) else {}
        fingerprint = str(project.get("fingerprint") or "")
        existing = candidate.metadata.get("local_validations")
        validations = dict(existing) if isinstance(existing, dict) else {}
        if fingerprint:
            validations[fingerprint] = validation
        while len(validations) > 5:
            validations.pop(next(iter(validations)))
        import_detection = validation.get("import_detection")
        import_detection = (
            import_detection if isinstance(import_detection, dict) else {}
        )
        updated = replace(
            candidate,
            installed=(
                candidate.installed
                or import_detection.get("state") == "imported"
            ),
            metadata={
                **candidate.metadata,
                "local_validation": validation,
                "local_validations": validations,
            },
        )
        self.upsert_candidates((updated,))
        result = self.get_candidate(candidate_id)
        assert result is not None
        return result

    def source_sync_state(self, source: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM source_sync_states WHERE source = ?",
                (str(source),),
            ).fetchone()
        if row is None:
            return None
        return {
            "source": str(row["source"]),
            "revision": str(row["revision"]),
            "item_count": int(row["item_count"]),
            "source_generated_at": str(row["source_generated_at"]),
            "synced_at": str(row["synced_at"]),
        }

    def record_source_sync(
        self,
        *,
        source: str,
        revision: str,
        item_count: int,
        source_generated_at: str = "",
    ) -> dict[str, Any]:
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO source_sync_states (
                    source, revision, item_count, source_generated_at, synced_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source) DO UPDATE SET
                    revision = excluded.revision,
                    item_count = excluded.item_count,
                    source_generated_at = excluded.source_generated_at,
                    synced_at = excluded.synced_at
                """,
                (
                    str(source),
                    str(revision),
                    max(0, int(item_count)),
                    str(source_generated_at),
                    _now(),
                ),
            )
            self._connection.commit()
        state = self.source_sync_state(source)
        assert state is not None
        return state

    @staticmethod
    def _rag_document_text(row: sqlite3.Row) -> str:
        categories = json.loads(str(row["categories_json"]))
        category_text = " ".join(
            str(value) for value in categories
        ) if isinstance(categories, list) else ""
        return "\n".join(filter(None, (
            str(row["user_alias"]),
            str(row["notes"]),
            category_text,
            str(row["search_text"]),
        )))

    @staticmethod
    def _pack_embedding(vector: tuple[float, ...]) -> bytes:
        return struct.pack(f"<{len(vector)}f", *vector)

    @staticmethod
    def _unpack_embedding(blob: bytes, dimension: int) -> tuple[float, ...]:
        if dimension < 1 or len(blob) != dimension * 4:
            raise ValueError("stored RAG embedding has an invalid dimension")
        vector = tuple(struct.unpack(f"<{dimension}f", blob))
        if any(not math.isfinite(value) for value in vector):
            raise ValueError("stored RAG embedding contains invalid values")
        return vector

    def _embedding_identity(self) -> dict[str, Any] | None:
        backend = self.embedding_backend
        if backend is None:
            return None
        identity_method = getattr(backend, "identity", None)
        if callable(identity_method):
            identity = dict(identity_method())
        else:
            model_id = str(backend.model_id)
            generation_id = str(
                getattr(backend, "generation_id", "")
                or "embedding:" + sha256(model_id.encode("utf-8")).hexdigest()[:24]
            )
            identity = {
                "generation_id": generation_id,
                "model_id": model_id,
                "revision": "test-or-external-backend",
                "pipeline": "backend-defined",
            }
        required = {"generation_id", "model_id", "revision"}
        if not required.issubset(identity):
            raise RuntimeError("embedding backend identity is incomplete")
        return identity

    def _missing_embedding_dependencies(self) -> tuple[str, ...]:
        backend = self.embedding_backend
        if backend is None:
            return ()
        method = getattr(backend, "missing_dependencies", None)
        return tuple(str(value) for value in method()) if callable(method) else ()

    def _write_rag_index_state(
        self,
        *,
        identity: dict[str, Any],
        state: str,
        document_count: int,
        indexed_count: int,
        dimension: int | None = None,
        error: str = "",
        active: bool | None = None,
        started: bool = False,
        completed: bool = False,
        owner_token: str | None = None,
    ) -> None:
        timestamp = _now()
        existing = self._connection.execute(
            "SELECT active, started_at, completed_at, owner_token "
            "FROM asset_rag_index_generations WHERE generation_id = ?",
            (str(identity["generation_id"]),),
        ).fetchone()
        active_value = (
            int(active)
            if active is not None
            else int(existing["active"] if existing is not None else 0)
        )
        started_at = (
            timestamp if started
            else (existing["started_at"] if existing is not None else None)
        )
        completed_at = (
            timestamp if completed
            else (existing["completed_at"] if existing is not None else None)
        )
        owner_value = (
            str(owner_token)
            if owner_token is not None
            else str(existing["owner_token"] if existing is not None else "")
        )
        self._connection.execute(
            """
            INSERT INTO asset_rag_index_generations (
                generation_id, model_id, model_revision, config_json, state,
                document_count, indexed_count, dimension, error, owner_token, active,
                started_at, completed_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(generation_id) DO UPDATE SET
                model_id = excluded.model_id,
                model_revision = excluded.model_revision,
                config_json = excluded.config_json,
                state = excluded.state,
                document_count = excluded.document_count,
                indexed_count = excluded.indexed_count,
                dimension = excluded.dimension,
                error = excluded.error,
                owner_token = excluded.owner_token,
                active = excluded.active,
                started_at = excluded.started_at,
                completed_at = excluded.completed_at,
                updated_at = excluded.updated_at
            """,
            (
                str(identity["generation_id"]),
                str(identity["model_id"]),
                str(identity["revision"]),
                _json(identity),
                state,
                document_count,
                indexed_count,
                dimension,
                error[:2000],
                owner_value,
                active_value,
                started_at,
                completed_at,
                timestamp,
            ),
        )

    def rag_index_status(self) -> dict[str, Any]:
        identity = self._embedding_identity()
        missing = self._missing_embedding_dependencies()
        download_status = None
        if self.embedding_backend is not None:
            download_method = getattr(
                self.embedding_backend,
                "model_download_status",
                None,
            )
            if callable(download_method):
                download_status = download_method()
        with self._lock:
            documents = int(self._connection.execute(
                "SELECT COUNT(*) FROM asset_rag_documents"
            ).fetchone()[0])
            total_vectors = int(self._connection.execute(
                "SELECT COUNT(*) FROM asset_rag_embeddings"
            ).fetchone()[0])
            generation_row = None
            valid_vectors = 0
            if identity is not None:
                generation_row = self._connection.execute(
                    "SELECT * FROM asset_rag_index_generations WHERE generation_id = ?",
                    (str(identity["generation_id"]),),
                ).fetchone()
                valid_vectors = int(self._connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM asset_rag_embeddings AS v
                    JOIN asset_rag_documents AS d ON d.id = v.document_id
                    WHERE v.generation_id = ? AND v.content_hash = d.content_hash
                    """,
                    (str(identity["generation_id"]),),
                ).fetchone()[0])

        if identity is None:
            state = "disabled"
        elif missing:
            state = "dependency_missing"
        elif documents == 0:
            state = "empty"
        elif valid_vectors == documents:
            state = "ready"
        elif generation_row is not None:
            persisted_state = str(generation_row["state"])
            if persisted_state in {"preparing_model", "building"}:
                has_live_lease = bool(str(generation_row["owner_token"])) and (
                    self._fresh_index_lease(str(generation_row["updated_at"]))
                )
                state = persisted_state if has_live_lease else "pending"
            elif persisted_state in {"error", "cancelled"}:
                state = persisted_state
            else:
                state = "pending"
        else:
            state = "pending"
        coverage = 1.0 if documents == 0 else valid_vectors / documents
        return {
            "state": state,
            "ready": state == "ready",
            "documents": documents,
            "indexed": valid_vectors,
            "coverage": round(coverage, 6),
            "total_stored_vectors": total_vectors,
            "generation_id": (
                str(identity["generation_id"]) if identity is not None else None
            ),
            "model_id": str(identity["model_id"]) if identity is not None else None,
            "model_revision": (
                str(identity["revision"]) if identity is not None else None
            ),
            "minimum_similarity": self.rag_min_similarity,
            "missing_dependencies": missing,
            "model_download": download_status,
            "error": (
                str(generation_row["error"])
                if generation_row is not None else ""
            ),
            "updated_at": (
                str(generation_row["updated_at"])
                if generation_row is not None else None
            ),
        }

    def cancel_current_rag_index(self, reason: str = "application shutdown") -> bool:
        identity = self._embedding_identity()
        if identity is None:
            return False
        with self._lock:
            cursor = self._connection.execute(
                """
                UPDATE asset_rag_index_generations
                SET state = 'cancelled', error = ?, owner_token = '', updated_at = ?
                WHERE generation_id = ?
                  AND state IN ('preparing_model', 'building')
                  AND owner_token <> ''
                """,
                (
                    str(reason)[:2000],
                    _now(),
                    str(identity["generation_id"]),
                ),
            )
            self._connection.commit()
        return cursor.rowcount == 1

    @staticmethod
    def _fresh_index_lease(
        updated_at: str,
        *,
        seconds: float = _RAG_INDEX_LEASE_SECONDS,
    ) -> bool:
        try:
            updated = datetime.fromisoformat(updated_at)
            return (datetime.now(timezone.utc) - updated).total_seconds() < seconds
        except (TypeError, ValueError):
            return False

    def _claim_rag_index_run(
        self,
        *,
        identity: dict[str, Any],
        owner_token: str,
        document_count: int,
        indexed_count: int,
        initial_state: str,
    ) -> None:
        self._connection.execute("BEGIN IMMEDIATE")
        active_runs = self._connection.execute(
            """
            SELECT generation_id, updated_at
            FROM asset_rag_index_generations
            WHERE state IN ('preparing_model', 'building')
              AND owner_token <> ''
            """
        ).fetchall()
        if any(
            self._fresh_index_lease(str(row["updated_at"]))
            for row in active_runs
        ):
            self._connection.rollback()
            raise RagIndexBusyError(
                "another Fafnir process is already building the RAG index"
            )
        self._write_rag_index_state(
            identity=identity,
            state=initial_state,
            document_count=document_count,
            indexed_count=indexed_count,
            started=True,
            owner_token=owner_token,
        )
        self._connection.commit()

    def _assert_rag_index_owner(
        self,
        *,
        generation_id: str,
        owner_token: str,
    ) -> None:
        row = self._connection.execute(
            "SELECT owner_token FROM asset_rag_index_generations "
            "WHERE generation_id = ?",
            (generation_id,),
        ).fetchone()
        if row is None or str(row["owner_token"]) != owner_token:
            raise RagIndexBusyError("RAG index lease was taken by another process")

    def reindex_asset_rag_embeddings(
        self,
        *,
        force: bool = False,
        batch_size: int = 32,
        should_cancel: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Create versioned dense vectors for all AI-safe owned-asset documents."""
        backend = self.embedding_backend
        if backend is None:
            raise RuntimeError("no text embedding backend is configured")
        missing = self._missing_embedding_dependencies()
        if missing:
            raise RuntimeError(
                "text embeddings require missing dependencies: " + ", ".join(missing)
            )
        if not self._index_run_lock.acquire(blocking=False):
            raise RagIndexBusyError("an owned-asset indexing run is already active")
        identity = self._embedding_identity()
        assert identity is not None
        bounded_batch = max(1, min(int(batch_size), 128))
        owner_token = uuid.uuid4().hex
        claimed = False
        heartbeat_stop = threading.Event()
        heartbeat_thread: threading.Thread | None = None
        try:
            with self._lock:
                rows = self._connection.execute(
                    """
                    SELECT d.*
                    FROM asset_rag_documents AS d
                    LEFT JOIN asset_rag_embeddings AS v
                      ON v.document_id = d.id AND v.generation_id = ?
                    WHERE ? OR v.document_id IS NULL
                       OR v.content_hash <> d.content_hash
                    ORDER BY d.id
                    """,
                    (str(identity["generation_id"]), 1 if force else 0),
                ).fetchall()
                document_count = int(self._connection.execute(
                    "SELECT COUNT(*) FROM asset_rag_documents"
                ).fetchone()[0])
                current_count = document_count - len(rows) if not force else 0
                self._claim_rag_index_run(
                    identity=identity,
                    owner_token=owner_token,
                    document_count=document_count,
                    indexed_count=current_count,
                    initial_state="preparing_model" if rows else "building",
                )
                claimed = True

            def heartbeat() -> None:
                while not heartbeat_stop.wait(_RAG_INDEX_HEARTBEAT_SECONDS):
                    with self._lock:
                        cursor = self._connection.execute(
                            "UPDATE asset_rag_index_generations SET updated_at = ? "
                            "WHERE generation_id = ? AND owner_token = ?",
                            (
                                _now(),
                                str(identity["generation_id"]),
                                owner_token,
                            ),
                        )
                        self._connection.commit()
                    if cursor.rowcount != 1:
                        return

            heartbeat_thread = threading.Thread(
                target=heartbeat,
                name="stackforge-rag-index-heartbeat",
                daemon=True,
            )
            heartbeat_thread.start()

            if rows:
                prepare = getattr(backend, "prepare", None)
                if callable(prepare):
                    prepare()
            indexed = 0
            dimension: int | None = None
            for offset in range(0, len(rows), bounded_batch):
                if should_cancel is not None and should_cancel():
                    raise RagIndexCancelledError("owned-asset indexing was cancelled")
                batch = rows[offset:offset + bounded_batch]
                vectors = backend.encode_documents(
                    tuple(self._rag_document_text(row) for row in batch)
                )
                if len(vectors) != len(batch):
                    raise RuntimeError("embedding backend returned the wrong batch size")
                timestamp = _now()
                with self._lock:
                    self._assert_rag_index_owner(
                        generation_id=str(identity["generation_id"]),
                        owner_token=owner_token,
                    )
                    for row, raw_vector in zip(batch, vectors, strict=True):
                        vector = normalize_vector(raw_vector)
                        if dimension is None:
                            dimension = len(vector)
                        elif len(vector) != dimension:
                            raise RuntimeError(
                                "embedding dimension changed within one index run"
                            )
                        self._connection.execute(
                            """
                            INSERT INTO asset_rag_embeddings (
                                document_id, generation_id, dimension, vector,
                                content_hash, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?)
                            ON CONFLICT(document_id, generation_id) DO UPDATE SET
                                dimension = excluded.dimension,
                                vector = excluded.vector,
                                content_hash = excluded.content_hash,
                                updated_at = excluded.updated_at
                            """,
                            (
                                str(row["id"]),
                                str(identity["generation_id"]),
                                len(vector),
                                self._pack_embedding(vector),
                                str(row["content_hash"]),
                                timestamp,
                            ),
                        )
                        indexed += 1
                    self._write_rag_index_state(
                        identity=identity,
                        state="building",
                        document_count=document_count,
                        indexed_count=current_count + indexed,
                        dimension=dimension,
                        owner_token=owner_token,
                    )
                    self._connection.commit()

            with self._lock:
                self._assert_rag_index_owner(
                    generation_id=str(identity["generation_id"]),
                    owner_token=owner_token,
                )
                total = int(self._connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM asset_rag_embeddings AS v
                    JOIN asset_rag_documents AS d ON d.id = v.document_id
                    WHERE v.generation_id = ? AND v.content_hash = d.content_hash
                    """,
                    (str(identity["generation_id"]),),
                ).fetchone()[0])
                final_documents = int(self._connection.execute(
                    "SELECT COUNT(*) FROM asset_rag_documents"
                ).fetchone()[0])
                dimension_row = self._connection.execute(
                    "SELECT dimension FROM asset_rag_embeddings "
                    "WHERE generation_id = ? LIMIT 1",
                    (str(identity["generation_id"]),),
                ).fetchone()
                final_dimension = (
                    int(dimension_row["dimension"])
                    if dimension_row is not None else None
                )
                if total != final_documents:
                    raise RuntimeError(
                        "owned-asset documents changed during indexing; retry required"
                    )
                self._connection.execute(
                    "UPDATE asset_rag_index_generations SET active = 0 "
                    "WHERE generation_id <> ?",
                    (str(identity["generation_id"]),),
                )
                self._write_rag_index_state(
                    identity=identity,
                    state="ready",
                    document_count=final_documents,
                    indexed_count=total,
                    dimension=final_dimension,
                    active=True,
                    completed=True,
                    owner_token="",
                )
                self._connection.commit()
            return {
                "state": "ready",
                "generation_id": str(identity["generation_id"]),
                "model_id": str(identity["model_id"]),
                "model_revision": str(identity["revision"]),
                "indexed": indexed,
                "total": total,
                "dimension": final_dimension,
            }
        except Exception as exc:
            if claimed:
                with self._lock:
                    owner_row = self._connection.execute(
                        "SELECT owner_token FROM asset_rag_index_generations "
                        "WHERE generation_id = ?",
                        (str(identity["generation_id"]),),
                    ).fetchone()
                    if (
                        owner_row is not None
                        and str(owner_row["owner_token"]) == owner_token
                    ):
                        state = (
                            "cancelled"
                            if isinstance(exc, RagIndexCancelledError)
                            else "error"
                        )
                        current_status = self.rag_index_status()
                        self._write_rag_index_state(
                            identity=identity,
                            state=state,
                            document_count=current_status["documents"],
                            indexed_count=current_status["indexed"],
                            error=str(exc),
                            owner_token="",
                        )
                        self._connection.commit()
            raise
        finally:
            heartbeat_stop.set()
            if heartbeat_thread is not None:
                heartbeat_thread.join(timeout=1.0)
            self._index_run_lock.release()

    def search_asset_rag(
        self,
        *,
        query: str = "",
        limit: int = 20,
        candidate_ids: set[str] | frozenset[str] | None = None,
        category_hints: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        """Return complete-generation dense results and explicit retrieval metadata."""
        normalized_query = str(query or "").strip()
        bounded_limit = max(1, min(int(limit), 100))
        eligible_ids = (
            frozenset(str(value) for value in candidate_ids)
            if candidate_ids is not None
            else None
        )
        status = self.rag_index_status()
        if not normalized_query:
            with self._lock:
                rows = self._connection.execute(
                    """
                    SELECT d.*, e.evidence_kind, e.verified
                    FROM asset_rag_documents AS d
                    LEFT JOIN asset_store_purchase_evidence AS e
                      ON e.candidate_id = d.candidate_id
                    ORDER BY d.updated_at DESC, d.candidate_id
                    LIMIT ?
                    """,
                    (
                        status["documents"]
                        if eligible_ids is not None
                        else bounded_limit,
                    ),
                ).fetchall()
            items = [
                self._rag_result_view(row) for row in rows
                if eligible_ids is None or str(row["candidate_id"]) in eligible_ids
            ][:bounded_limit]
            return {
                "items": items,
                "count": len(items),
                "retrieval_mode": "browse",
                "index": status,
            }

        if status["documents"] == 0:
            return {
                "items": [],
                "count": 0,
                "retrieval_mode": "empty_catalog",
                "index": status,
            }
        backend = self.embedding_backend
        if backend is None or not status["ready"]:
            return self._search_asset_rag_fallback(
                query=normalized_query,
                limit=bounded_limit,
                status=status,
                candidate_ids=eligible_ids,
                category_hints=category_hints,
            )
        identity = self._embedding_identity()
        assert identity is not None
        query_vector = normalize_vector(backend.encode_query(normalized_query))
        terms = tuple(dict.fromkeys(normalized_query.casefold().split()))
        phrase = normalized_query.casefold()
        lexical = self._search_asset_rag_fallback(
            query=normalized_query,
            limit=status["documents"],
            status=status,
            candidate_ids=eligible_ids,
            category_hints=category_hints,
        )
        lexical_scores = {
            str(item["candidate_id"]): float(item["rank_score"] or 0.0)
            for item in lexical["items"]
        }
        maximum_lexical = max(lexical_scores.values(), default=0.0)
        scored: list[tuple[float, str, dict[str, Any]]] = []
        with self._lock:
            cursor = self._connection.execute(
                """
                SELECT d.*, e.evidence_kind, e.verified,
                       v.dimension AS embedding_dimension,
                       v.vector AS embedding_vector
                FROM asset_rag_documents AS d
                JOIN asset_rag_embeddings AS v ON v.document_id = d.id
                LEFT JOIN asset_store_purchase_evidence AS e
                  ON e.candidate_id = d.candidate_id
                WHERE v.generation_id = ? AND v.content_hash = d.content_hash
                """,
                (str(identity["generation_id"]),),
            )
            for row in cursor:
                candidate_id = str(row["candidate_id"])
                if eligible_ids is not None and candidate_id not in eligible_ids:
                    continue
                vector = self._unpack_embedding(
                    bytes(row["embedding_vector"]),
                    int(row["embedding_dimension"]),
                )
                if len(vector) != len(query_vector):
                    raise RuntimeError("ready RAG generation contains invalid vectors")
                cosine = sum(
                    a * b for a, b in zip(query_vector, vector, strict=True)
                )
                lexical_score = lexical_scores.get(candidate_id, 0.0)
                searchable = self._rag_document_text(row).casefold()
                phrase_match = phrase in searchable
                if (
                    cosine < self.rag_min_similarity
                    and not phrase_match
                    and lexical_score <= 0.0
                ):
                    continue
                rank_score = cosine
                if phrase_match:
                    rank_score += 0.04
                rank_score += 0.01 * sum(term in searchable for term in terms)
                if maximum_lexical > 0.0 and lexical_score > 0.0:
                    rank_score += 0.35 * (lexical_score / maximum_lexical)
                scored.append((
                    rank_score,
                    candidate_id,
                    self._rag_result_view(
                        row,
                        score=cosine,
                        rank_score=rank_score,
                    ),
                ))
        scored.sort(key=lambda item: (-item[0], item[1]))
        results = [item[2] for item in scored[:bounded_limit]]
        return {
            "items": results,
            "count": len(results),
            "retrieval_mode": "hybrid_dense",
            "index": status,
        }

    @staticmethod
    def _normalized_search_text(value: str) -> str:
        return unicodedata.normalize("NFKC", str(value or "")).casefold()

    def _search_asset_rag_fallback(
        self,
        *,
        query: str,
        limit: int,
        status: dict[str, Any],
        candidate_ids: frozenset[str] | None = None,
        category_hints: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        """Return transparent local lexical evidence while dense RAG is warming."""
        normalized = self._normalized_search_text(query)
        requirements = (
            ()
            if category_hints
            else derive_requirements(query, platform="pc", maximum=20)
        )
        stop_words = {
            "unity", "asset", "assets", "package", "system", "tool", "tools",
            "framework", "game", "art", "models", "processing", "item", "items",
        }
        weighted_terms: dict[str, float] = {}
        category_weights: dict[str, float] = {}
        for category in category_hints:
            normalized_category = self._normalized_search_text(category)
            if normalized_category:
                category_weights[normalized_category] = 5.0
        for requirement in requirements:
            priority_weight = 1.25 if requirement.priority == "high" else 0.25
            category_weights[requirement.key] = (
                5.0 if requirement.priority == "high" else 1.5
            )
            for term in re.findall(r"[a-z0-9]+", requirement.query.casefold()):
                if len(term) > 2 and term not in stop_words:
                    weighted_terms[term] = max(
                        weighted_terms.get(term, 0.0),
                        priority_weight,
                    )

        domain_expansions = (
            (("海底", "海中", "水中"), ("underwater", "subsea", "submarine", "ocean")),
            (("浸水", "水没"), ("flood", "flooded", "water", "submerged")),
            (("研究施設", "研究所"), ("research", "laboratory", "lab", "facility")),
            (("通路", "廊下"), ("corridor", "hallway", "passage")),
            (("非常灯", "非常照明"), ("emergency", "warning", "light", "lighting")),
            (("異形", "怪物", "化け物"), ("monster", "creature", "mutant", "horror")),
            (("足音",), ("footstep", "footsteps", "foley", "surface")),
            (("暗証番号", "暗証", "番号錠"), ("keypad", "code", "combination", "lock")),
            (("チェックポイント",), ("checkpoint", "save", "persistence")),
            (("持ち物", "インベントリ"), ("inventory",)),
        )
        for triggers, expansions in domain_expansions:
            if any(trigger in normalized for trigger in triggers):
                for term in expansions:
                    weighted_terms[term] = max(weighted_terms.get(term, 0.0), 4.0)

        raw_latin_terms = {
            term for term in re.findall(r"[a-z0-9]+", normalized)
            if len(term) > 2 and term not in stop_words
        }
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT d.*, e.evidence_kind, e.verified
                FROM asset_rag_documents AS d
                LEFT JOIN asset_store_purchase_evidence AS e
                  ON e.candidate_id = d.candidate_id
                """
            ).fetchall()

        scored: list[tuple[float, str, dict[str, Any]]] = []
        for row in rows:
            if (
                candidate_ids is not None
                and str(row["candidate_id"]) not in candidate_ids
            ):
                continue
            searchable = self._normalized_search_text(self._rag_document_text(row))
            raw_categories = json.loads(str(row["categories_json"]))
            categories = {
                self._normalized_search_text(value)
                for value in raw_categories
            } if isinstance(raw_categories, list) else set()
            score = 0.0
            if normalized and normalized in searchable:
                score += 10.0
            score += 4.0 * sum(term in searchable for term in raw_latin_terms)
            score += sum(
                weight for term, weight in weighted_terms.items()
                if term in searchable
            )
            score += sum(
                weight for category, weight in category_weights.items()
                if category in categories
            )
            if score < 1.25:
                continue
            scored.append((
                score,
                str(row["candidate_id"]),
                self._rag_result_view(row, rank_score=score),
            ))
        scored.sort(key=lambda item: (-item[0], item[1]))
        items = [item[2] for item in scored[:limit]]
        return {
            "items": items,
            "count": len(items),
            "retrieval_mode": "lexical_fallback",
            "degraded": True,
            "warning": (
                "Dense multilingual retrieval is still preparing; these are "
                "owned-only lexical candidates for model judgment."
            ),
            "index": status,
        }

    @staticmethod
    def _rag_result_view(
        row: sqlite3.Row,
        *,
        score: float | None = None,
        rank_score: float | None = None,
    ) -> dict[str, Any]:
        raw_categories = json.loads(str(row["categories_json"]))
        return {
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
            "score": round(score, 6) if score is not None else None,
            "rank_score": round(rank_score, 6) if rank_score is not None else None,
        }

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
        rag_status = self.rag_index_status()
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
            enriched_assets = self._connection.execute(
                "SELECT COUNT(*) FROM asset_store_product_details"
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
            "asset_store_details": int(enriched_assets),
            "asset_rag_vectors": int(rag_status["indexed"]),
            "asset_rag_vector_total": int(rag_status["total_stored_vectors"]),
            **{str(row["source"]): int(row["count"]) for row in rows},
        }


__all__ = [
    "RagIndexBusyError",
    "RagIndexCancelledError",
    "RagIndexNotReadyError",
    "StackRepository",
    "default_database_path",
]
