from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from game_stack_planner.api import ApiError, GameStackApplication
from game_stack_planner.cli import main as cli_main
from game_stack_planner.native_messaging import (
    MAX_MESSAGE_BYTES,
    NativeMessagingError,
    handle_message,
    read_message,
    run_host,
)
from game_stack_planner.repository import RagIndexBusyError, StackRepository
from game_stack_planner.text_embeddings import (
    DEFAULT_TEXT_EMBEDDING_WEIGHT_BYTES,
    TransformersTextEmbeddingBackend,
    normalize_vector,
)
from game_stack_planner.server import create_server


PRODUCT_URL = (
    "https://assetstore.unity.com/packages/tools/game-toolkits/"
    "inventory-workbench-77701"
)


def _owned_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "url": PRODUCT_URL,
        # The store title is needed to maintain the ordinary catalog card, but
        # it is deliberately not part of the AI-facing RAG result contract.
        "title": "Store Supplied Inventory Workbench",
        "user_alias": "手持ちの持ち物管理ツール",
        "notes": "装備スロットと重量制限を実装するときに使う",
        "categories": ["inventory"],
        "purchase_confirmation": True,
    }
    payload.update(overrides)
    return payload


class _TestEmbeddingBackend:
    model_id = "test:owned-assets"
    generation_id = "test:owned-assets:generation-1"

    def identity(self) -> dict[str, str]:
        return {
            "generation_id": self.generation_id,
            "model_id": self.model_id,
            "revision": "fixed-test-revision",
            "pipeline": "deterministic-test-v1",
        }

    @staticmethod
    def _vector(text: str) -> tuple[float, ...]:
        value = text.casefold()
        inventory = any(term in value for term in (
            "inventory", "持ち物", "装備", "重量", "インベントリ",
        ))
        hospital = any(term in value for term in (
            "hospital", "病院", "医療施設", "horror location",
        ))
        effects = any(term in value for term in (
            "vfx", "effect", "skills", "ボス戦", "temporary",
        ))
        return normalize_vector((
            1.0 if inventory else 0.01,
            1.0 if hospital else 0.01,
            1.0 if effects else 0.01,
            1.0 if not (inventory or hospital or effects) else 0.01,
        ))

    def encode_documents(self, texts):
        return tuple(self._vector(text) for text in texts)

    def encode_query(self, text):
        return self._vector(text)


class OwnedAssetRagApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.application = GameStackApplication(repository=StackRepository(
            Path(self.temporary.name) / "catalog.db",
            embedding_backend=_TestEmbeddingBackend(),
        ))

    def tearDown(self) -> None:
        self.application.close()
        self.temporary.cleanup()

    def test_explicit_registration_upgrades_candidate_and_is_idempotent(
        self,
    ) -> None:
        candidate = self.application.save_manual({
            "url": PRODUCT_URL + "?utm_source=extension#description",
            "title": "検討中の候補",
            "notes": "候補メモ",
            "categories": ["inventory"],
            "ownership": "candidate",
        })
        self.assertEqual("candidate", candidate["item"]["ownership"])

        first = self.application.save_owned_rag(_owned_payload())
        second = self.application.save_owned_rag(_owned_payload(
            url=(
                "https://marketplace.unity.com/packages/tools/game-toolkits/"
                "inventory-workbench-77701/reviews?tracking=ignored"
            ),
        ))

        self.assertEqual("asset_store:77701", first["item"]["id"])
        self.assertEqual(first["item"]["id"], second["item"]["id"])
        self.assertEqual("owned", second["item"]["ownership"])
        self.assertEqual(
            {
                "kind": "user_asserted",
                "verified": False,
            },
            second["item"]["ownership_evidence"],
        )
        catalog = self.application.catalog(source="asset_store")
        self.assertEqual(1, catalog["count"])
        self.application.reindex_asset_rag()
        rag = self.application.search_asset_rag(query="重量制限", limit=10)
        self.assertEqual(1, rag["count"])
        self.assertEqual("asset_store:77701", rag["items"][0]["candidate_id"])

    def test_search_returns_only_user_authored_ai_safe_fields(self) -> None:
        self.application.save_owned_rag(_owned_payload())
        self.application.reindex_asset_rag()

        result = self.application.search_asset_rag(query="装備スロット", limit=5)

        self.assertEqual(1, result["count"])
        hit = result["items"][0]
        self.assertEqual(
            {
                "candidate_id",
                "user_alias",
                "notes",
                "categories",
                "ownership_evidence",
                "score",
                "rank_score",
            },
            set(hit),
        )
        self.assertEqual("手持ちの持ち物管理ツール", hit["user_alias"])
        self.assertEqual(
            "装備スロットと重量制限を実装するときに使う",
            hit["notes"],
        )
        self.assertEqual(("inventory",), tuple(hit["categories"]))
        self.assertEqual(
            {"kind": "user_asserted", "verified": False},
            hit["ownership_evidence"],
        )
        serialized = json.dumps(hit, ensure_ascii=False).casefold()
        for forbidden in (
            "store supplied inventory workbench",
            "assetstore.unity.com",
            "marketplace.unity.com",
            "product_url",
            "local_path",
            "store_description",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, serialized)

    def test_japanese_notes_alias_and_category_title_are_searchable(self) -> None:
        self.application.save_owned_rag(_owned_payload())
        self.application.reindex_asset_rag()

        for query in ("重量制限", "持ち物管理", "インベントリ"):
            with self.subTest(query=query):
                result = self.application.search_asset_rag(query=query, limit=10)
                self.assertEqual(1, result["count"])
                self.assertEqual(
                    "asset_store:77701",
                    result["items"][0]["candidate_id"],
                )
        self.assertEqual(
            0,
            self.application.search_asset_rag(
                query="水面シェーダー",
                limit=10,
            )["count"],
        )

    def test_multiple_query_terms_do_not_require_every_term(self) -> None:
        self.application.save_owned_rag(_owned_payload())
        self.application.reindex_asset_rag()

        matching = self.application.search_asset_rag(
            query="装備 重量",
            limit=10,
        )
        partially_matching = self.application.search_asset_rag(
            query="装備 水面",
            limit=10,
        )

        self.assertEqual(1, matching["count"])
        self.assertEqual(
            "asset_store:77701",
            matching["items"][0]["candidate_id"],
        )
        self.assertEqual(1, partially_matching["count"])

    def test_dense_index_retrieves_semantic_match_without_literal_overlap(self) -> None:
        class FakeSemanticBackend:
            model_id = "test:multilingual-semantic-v1"

            @staticmethod
            def _vector(text: str) -> tuple[float, ...]:
                value = text.casefold()
                hospital = any(term in value for term in (
                    "hospital", "病院", "医療施設", "horror location",
                ))
                inventory = any(term in value for term in (
                    "inventory", "持ち物", "装備", "重量",
                ))
                return normalize_vector((
                    1.0 if hospital else 0.01,
                    1.0 if inventory else 0.01,
                ))

            def encode_documents(self, texts):
                return tuple(self._vector(text) for text in texts)

            def encode_query(self, text):
                return self._vector(text)

        self.application.close()
        repository = StackRepository(
            Path(self.temporary.name) / "semantic.db",
            embedding_backend=FakeSemanticBackend(),
        )
        self.application = GameStackApplication(repository=repository)
        self.application.save_owned_rag(_owned_payload(
            url="https://assetstore.unity.com/packages/package/80001",
            title="Store title must remain private",
            user_alias="Abandoned Hospital Environment",
            notes="dark horror location with patient rooms",
            categories=["visual_assets"],
        ))
        self.application.save_owned_rag(_owned_payload(
            url="https://assetstore.unity.com/packages/package/80002",
            user_alias="持ち物管理",
            notes="装備と重量を管理する",
            categories=["inventory"],
        ))

        indexed = self.application.reindex_asset_rag(batch_size=1)
        result = self.application.search_asset_rag(
            query="怖い医療施設",
            limit=1,
        )

        self.assertEqual(2, indexed["indexed"])
        self.assertEqual("test:multilingual-semantic-v1", indexed["model_id"])
        self.assertEqual("asset_store:80001", result["items"][0]["candidate_id"])
        self.assertEqual(2, self.application.status()["catalog"]["asset_rag_vectors"])
        self.assertEqual("hybrid_dense", result["retrieval_mode"])

    def test_hybrid_ranking_preserves_strong_cross_language_catalog_terms(self) -> None:
        class FlatSemanticBackend:
            model_id = "test:flat-semantic-v1"
            generation_id = "test:flat-semantic-v1:generation-1"

            @staticmethod
            def identity():
                return {
                    "generation_id": FlatSemanticBackend.generation_id,
                    "model_id": FlatSemanticBackend.model_id,
                    "revision": "fixed-test-revision",
                    "pipeline": "flat-test-v1",
                }

            @staticmethod
            def encode_documents(texts):
                return tuple(normalize_vector((1.0, 1.0)) for _ in texts)

            @staticmethod
            def encode_query(text):
                return normalize_vector((1.0, 1.0))

        self.application.close()
        repository = StackRepository(
            Path(self.temporary.name) / "hybrid.db",
            embedding_backend=FlatSemanticBackend(),
        )
        self.application = GameStackApplication(repository=repository)
        self.application.save_owned_rag(_owned_payload(
            url="https://assetstore.unity.com/packages/package/80011",
            user_alias="Getting Started Mods Asset Pack",
            categories=["editor_tools"],
        ))
        self.application.save_owned_rag(_owned_payload(
            url="https://assetstore.unity.com/packages/package/80012",
            user_alias="Underwater Sunken Ship Environment",
            categories=["visual_assets"],
        ))
        self.application.reindex_asset_rag()

        result = self.application.search_asset_rag(
            query="閉鎖された海底研究施設",
            limit=2,
        )

        self.assertEqual("hybrid_dense", result["retrieval_mode"])
        self.assertEqual("asset_store:80012", result["items"][0]["candidate_id"])
        self.assertGreater(
            result["items"][0]["rank_score"],
            result["items"][1]["rank_score"],
        )

    def test_partial_generation_uses_explicit_fallback_not_partial_vectors(self) -> None:
        self.application.save_owned_rag(_owned_payload(
            url="https://assetstore.unity.com/packages/package/81001",
            user_alias="First inventory tool",
        ))
        self.application.reindex_asset_rag()
        self.application.save_owned_rag(_owned_payload(
            url="https://assetstore.unity.com/packages/package/81002",
            user_alias="Second inventory tool",
            notes="UniqueSecondTerm",
        ))

        result = self.application.search_asset_rag(
            query="UniqueSecondTerm",
            limit=1,
        )

        self.assertEqual("lexical_fallback", result["retrieval_mode"])
        self.assertTrue(result["degraded"])
        self.assertEqual("asset_store:81002", result["items"][0]["candidate_id"])
        self.assertIsNone(result["items"][0]["score"])
        status = result["index"]
        self.assertEqual(2, status["documents"])
        self.assertEqual(1, status["indexed"])
        self.assertEqual("pending", status["state"])

    def test_dense_threshold_allows_an_explicit_no_match(self) -> None:
        self.application.save_owned_rag(_owned_payload())
        self.application.reindex_asset_rag()

        result = self.application.search_asset_rag(
            query="水面シェーダー",
            limit=10,
        )

        self.assertEqual(0, result["count"])
        self.assertEqual("hybrid_dense", result["retrieval_mode"])

    def test_status_counts_only_the_configured_generation(self) -> None:
        self.application.save_owned_rag(_owned_payload())
        self.application.reindex_asset_rag()
        database = Path(self.temporary.name) / "catalog.db"
        self.application.close()

        class NextGenerationBackend(_TestEmbeddingBackend):
            generation_id = "test:owned-assets:generation-2"

        self.application = GameStackApplication(repository=StackRepository(
            database,
            embedding_backend=NextGenerationBackend(),
        ))
        status = self.application.status()

        self.assertEqual(0, status["catalog"]["asset_rag_vectors"])
        self.assertEqual(1, status["catalog"]["asset_rag_vector_total"])
        self.assertEqual("pending", status["rag_index"]["state"])

    def test_automatic_indexing_reaches_ready_without_manual_command(self) -> None:
        self.application.save_owned_rag(_owned_payload())

        started = self.application.enable_automatic_rag_indexing()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if self.application.status()["rag_index"]["ready"]:
                break
            time.sleep(0.01)

        self.assertTrue(started)
        self.assertTrue(self.application.status()["rag_index"]["ready"])

    def test_detached_indexing_uses_an_independent_cli_process(self) -> None:
        self.application.close()
        database = Path(self.temporary.name) / "detached.db"
        self.application = GameStackApplication(database)
        self.application.save_owned_rag(_owned_payload())

        with patch("game_stack_planner.api.subprocess.Popen") as launch:
            launch.return_value.poll.return_value = None
            started = self.application.enable_automatic_rag_indexing(
                detached=True,
            )

        self.assertTrue(started)
        command = launch.call_args.args[0]
        self.assertEqual(command[0], __import__("sys").executable)
        self.assertIn("game_stack_planner", command)
        self.assertIn("rag-index", command)
        self.assertIn(str(database.resolve()), command)
        self.assertTrue(
            self.application.status()["rag_index"]["worker_active"]
        )

    def test_database_lease_blocks_a_second_indexing_process(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        class BlockingBackend(_TestEmbeddingBackend):
            generation_id = "test:blocking-generation"

            @staticmethod
            def prepare() -> None:
                entered.set()
                release.wait(timeout=2.0)

        self.application.close()
        database = Path(self.temporary.name) / "lease.db"
        first_repository = StackRepository(
            database,
            embedding_backend=BlockingBackend(),
        )
        self.application = GameStackApplication(repository=first_repository)
        self.application.save_owned_rag(_owned_payload())
        second_repository = StackRepository(
            database,
            embedding_backend=BlockingBackend(),
        )
        thread = threading.Thread(
            target=first_repository.reindex_asset_rag_embeddings,
            daemon=True,
        )
        thread.start()
        self.assertTrue(entered.wait(timeout=1.0))

        try:
            with self.assertRaises(RagIndexBusyError):
                second_repository.reindex_asset_rag_embeddings()
        finally:
            release.set()
            thread.join(timeout=2.0)
            second_repository.close()

        self.assertFalse(thread.is_alive())

    def test_expired_process_lease_is_reported_as_pending(self) -> None:
        self.application.save_owned_rag(_owned_payload())
        repository = self.application.repository
        identity = repository._embedding_identity()
        assert identity is not None
        with repository._lock:
            repository._write_rag_index_state(
                identity=identity,
                state="preparing_model",
                document_count=1,
                indexed_count=0,
                owner_token="process-that-exited",
            )
            repository._connection.execute(
                "UPDATE asset_rag_index_generations SET updated_at = ? "
                "WHERE generation_id = ?",
                ("2000-01-01T00:00:00+00:00", identity["generation_id"]),
            )
            repository._connection.commit()

        status = repository.rag_index_status()

        self.assertEqual("pending", status["state"])
        self.assertFalse(status["ready"])

    def test_embedding_generation_changes_with_preprocessing_configuration(self) -> None:
        standard = TransformersTextEmbeddingBackend(max_tokens=256)
        shorter = TransformersTextEmbeddingBackend(max_tokens=128)

        self.assertNotEqual(standard.generation_id, shorter.generation_id)
        self.assertEqual(standard.revision, standard.identity()["revision"])

    def test_custom_embedding_model_requires_an_immutable_revision(self) -> None:
        with self.assertRaisesRegex(ValueError, "immutable revision"):
            TransformersTextEmbeddingBackend(model_id="example/custom-e5")

    def test_pinned_model_download_progress_is_resumable_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"HF_HUB_CACHE": directory},
        ):
            backend = TransformersTextEmbeddingBackend()
            initial = backend.model_download_status()
            assert initial is not None
            _, _, parts = backend._default_weight_paths()
            parts[0].parent.mkdir(parents=True)
            parts[0].write_bytes(b"progress")
            resumed = backend.model_download_status()

        assert resumed is not None
        self.assertEqual(0, initial["downloaded_bytes"])
        self.assertEqual(len(b"progress"), resumed["downloaded_bytes"])
        self.assertEqual(
            DEFAULT_TEXT_EMBEDDING_WEIGHT_BYTES,
            resumed["total_bytes"],
        )
        ranges = backend._default_weight_ranges()
        self.assertEqual(0, ranges[0][0])
        self.assertEqual(DEFAULT_TEXT_EMBEDDING_WEIGHT_BYTES - 1, ranges[-1][1])
        self.assertTrue(all(
            previous[1] + 1 == current[0]
            for previous, current in zip(ranges, ranges[1:])
        ))

    def test_completed_pinned_model_loads_without_remote_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"HF_HUB_CACHE": directory},
        ):
            backend = TransformersTextEmbeddingBackend()
            snapshot, _, _ = backend._default_weight_paths()
            snapshot.mkdir(parents=True)
            weight = snapshot / "model.safetensors"
            with weight.open("wb") as stream:
                stream.truncate(DEFAULT_TEXT_EMBEDDING_WEIGHT_BYTES)
            fake_torch = MagicMock()
            fake_torch.cuda.is_available.return_value = False
            fake_transformers = MagicMock()
            fake_model = fake_transformers.AutoModel.from_pretrained.return_value

            def fake_import(name: str):
                return {
                    "torch": fake_torch,
                    "transformers": fake_transformers,
                }[name]

            with (
                patch.object(backend, "_configure_huggingface_tls"),
                patch(
                    "game_stack_planner.text_embeddings.importlib.import_module",
                    side_effect=fake_import,
                ),
            ):
                backend.prepare()

        tokenizer_call = fake_transformers.AutoTokenizer.from_pretrained.call_args
        model_call = fake_transformers.AutoModel.from_pretrained.call_args
        self.assertEqual(str(snapshot), tokenizer_call.args[0])
        self.assertTrue(tokenizer_call.kwargs["local_files_only"])
        self.assertEqual(str(snapshot), model_call.args[0])
        self.assertTrue(model_call.kwargs["local_files_only"])
        fake_model.to.assert_called_once_with("cpu")

    def test_cache_scan_does_not_create_owned_rag_evidence(self) -> None:
        cache = Path(self.temporary.name) / "Asset Store-5.x"
        package = (
            cache
            / "Example Publisher"
            / "Tools"
            / "Inventory Workbench.unitypackage"
        )
        package.parent.mkdir(parents=True)
        package.write_bytes(b"scanner must not inspect this archive")

        scan = self.application.scan_cache({"path": str(cache)})
        rag = self.application.search_asset_rag(
            query="Inventory Workbench",
            limit=10,
        )

        self.assertFalse(scan["scan"]["ownership_confirmed"])
        self.assertEqual(0, rag["count"])

    def test_application_requires_literal_purchase_confirmation(self) -> None:
        for value in (None, False, "true", 1):
            payload = _owned_payload()
            if value is None:
                payload.pop("purchase_confirmation")
            else:
                payload["purchase_confirmation"] = value
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    ValueError,
                    "purchase|confirm|購入|確認",
                ):
                    self.application.save_owned_rag(payload)

    def test_application_rejects_page_content_and_secrets(self) -> None:
        for field in ("html", "cookie", "description"):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    self.application.save_owned_rag(
                        _owned_payload(**{field: "must never be accepted"})
                    )


class RagIndexMigrationTests(unittest.TestCase):
    def test_legacy_vectors_are_preserved_but_never_activated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "legacy.db"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE asset_rag_documents (
                    id TEXT PRIMARY KEY,
                    candidate_id TEXT UNIQUE,
                    user_alias TEXT NOT NULL,
                    notes TEXT NOT NULL,
                    categories_json TEXT NOT NULL,
                    search_text TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE asset_rag_embeddings (
                    document_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    dimension INTEGER NOT NULL,
                    vector BLOB NOT NULL,
                    content_hash TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(document_id, model_id)
                );
                """
            )
            connection.execute(
                "INSERT INTO asset_rag_documents VALUES "
                "('d1','c1','alias','notes','[]','search','hash','t','t')"
            )
            connection.execute(
                "INSERT INTO asset_rag_embeddings VALUES (?, ?, ?, ?, ?, ?)",
                ("d1", "old-model", 2, struct.pack("<2f", 1.0, 0.0), "hash", "t"),
            )
            connection.commit()
            connection.close()

            repository = StackRepository(
                database,
                embedding_backend=_TestEmbeddingBackend(),
            )
            try:
                status = repository.rag_index_status()
                generations = repository._connection.execute(
                    "SELECT state, active FROM asset_rag_index_generations"
                ).fetchall()
            finally:
                repository.close()

        self.assertEqual(0, status["indexed"])
        self.assertEqual(1, status["total_stored_vectors"])
        self.assertEqual([("stale", 0)], [tuple(row) for row in generations])


class OwnedAssetRagNativeMessagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.application = GameStackApplication(repository=StackRepository(
            Path(self.temporary.name) / "catalog.db",
            embedding_backend=_TestEmbeddingBackend(),
        ))

    def tearDown(self) -> None:
        self.application.close()
        self.temporary.cleanup()

    def message(self, **overrides: object) -> dict[str, object]:
        message = {
            "action": "save_owned_asset_rag",
            "schema_version": 1,
            **_owned_payload(),
        }
        message.update(overrides)
        return message

    def test_native_action_requires_explicit_self_assertion(self) -> None:
        response = handle_message(self.message(), self.application)

        self.assertTrue(response["ok"])
        self.assertEqual("owned", response["item"]["ownership"])
        self.assertEqual(
            {"kind": "user_asserted", "verified": False},
            response["item"]["ownership_evidence"],
        )
        self.application.reindex_asset_rag()
        self.assertEqual(
            1,
            self.application.search_asset_rag(
                query="重量制限",
                limit=10,
            )["count"],
        )

    def test_native_action_rejects_page_content_and_secrets(self) -> None:
        for field in ("html", "cookie", "description"):
            with self.subTest(field=field):
                with self.assertRaises(NativeMessagingError):
                    handle_message(
                        self.message(**{field: "must never be accepted"}),
                        self.application,
                    )

    def test_native_action_requires_literal_purchase_confirmation(self) -> None:
        for value in (None, False, "true", 1):
            message = self.message()
            if value is None:
                message.pop("purchase_confirmation")
            else:
                message["purchase_confirmation"] = value
            with self.subTest(value=value):
                with self.assertRaises(NativeMessagingError):
                    handle_message(message, self.application)

    def test_existing_native_size_and_exact_origin_boundaries_remain(self) -> None:
        oversized = io.BytesIO(struct.pack("<I", MAX_MESSAGE_BYTES + 1))
        with self.assertRaises(NativeMessagingError):
            read_message(oversized)

        allowed = "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/"
        config = Path(self.temporary.name) / "host_config.json"
        config.write_text(
            json.dumps({
                "allowed_origin": allowed,
                "database_path": str(
                    Path(self.temporary.name).resolve() / "other.db"
                ),
            }),
            encoding="utf-8",
        )
        output = io.BytesIO()
        exit_code = run_host(
            origin="chrome-extension://bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb/",
            input_stream=io.BytesIO(),
            output_stream=output,
            configuration_path=config,
        )
        output.seek(0)
        response = read_message(output)
        self.assertEqual(2, exit_code)
        self.assertIsNotNone(response)
        assert response is not None
        self.assertFalse(response["ok"])
        self.assertEqual("forbidden_extension", response["error"]["code"])


class OwnedAssetRagCliTests(unittest.TestCase):
    def test_rag_search_reports_explicit_fallback_while_index_is_pending(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "catalog.db"
            application = GameStackApplication(database)
            try:
                application.save_owned_rag(_owned_payload())
            finally:
                application.close()

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = cli_main([
                    "--db",
                    str(database),
                    "--json",
                    "rag-search",
                    "--query",
                    "重量制限",
                    "--limit",
                    "3",
                ])

        self.assertEqual(0, exit_code)
        payload = json.loads(stdout.getvalue())
        self.assertEqual("lexical_fallback", payload["retrieval_mode"])
        self.assertTrue(payload["degraded"])
        self.assertEqual("pending", payload["index"]["state"])


class OwnedAssetRagHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        static = root / "static"
        static.mkdir()
        (static / "index.html").write_text("<h1>test</h1>", encoding="utf-8")
        self.application = GameStackApplication(repository=StackRepository(
            root / "catalog.db",
            embedding_backend=_TestEmbeddingBackend(),
        ))
        self.server = create_server(
            self.application,
            port=0,
            static_root=static,
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.application.close()
        self.temporary.cleanup()

    def request(
        self,
        payload: dict[str, object],
        *,
        origin: str | None = None,
    ) -> Request:
        headers = {"Content-Type": "application/json"}
        if origin is not None:
            headers["Origin"] = origin
        return Request(
            f"{self.base}/api/asset-store/rag",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers=headers,
        )

    def test_same_origin_post_registers_explicit_owned_asset(self) -> None:
        with urlopen(
            self.request(_owned_payload(), origin=self.base),
            timeout=5,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))

        self.assertEqual(200, response.status)
        self.assertEqual("asset_store:77701", payload["item"]["id"])
        self.assertEqual("owned", payload["item"]["ownership"])
        self.assertEqual(
            {"kind": "user_asserted", "verified": False},
            payload["item"]["ownership_evidence"],
        )

    def test_post_rejects_missing_or_false_confirmation(self) -> None:
        missing = _owned_payload()
        missing.pop("purchase_confirmation")
        false = _owned_payload(purchase_confirmation=False)
        for payload in (missing, false):
            with self.subTest(payload=payload):
                with self.assertRaises(HTTPError) as context:
                    urlopen(
                        self.request(payload, origin=self.base),
                        timeout=5,
                    )
                self.assertEqual(400, context.exception.code)
                error = json.loads(
                    context.exception.read().decode("utf-8")
                )
                self.assertEqual(
                    "purchase_confirmation_required",
                    error["error"]["code"],
                )

    def test_get_returns_only_ai_safe_rag_fields(self) -> None:
        with urlopen(
            self.request(_owned_payload(), origin=self.base),
            timeout=5,
        ):
            pass
        self.application.reindex_asset_rag()
        query = urlencode({"q": "重量制限", "limit": "10"})

        with urlopen(
            f"{self.base}/api/asset-store/rag?{query}",
            timeout=5,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))

        self.assertEqual(1, payload["count"])
        self.assertEqual(
            {
                "candidate_id",
                "user_alias",
                "notes",
                "categories",
                "ownership_evidence",
                "score",
                "rank_score",
            },
            set(payload["items"][0]),
        )
        serialized = json.dumps(payload["items"][0], ensure_ascii=False)
        self.assertNotIn("assetstore.unity.com", serialized)
        self.assertNotIn("Store Supplied Inventory Workbench", serialized)

    def test_get_returns_labeled_fallback_until_dense_index_is_ready(self) -> None:
        with urlopen(
            self.request(_owned_payload(), origin=self.base),
            timeout=5,
        ):
            pass
        query = urlencode({"q": "重量制限", "limit": "10"})

        with urlopen(
            f"{self.base}/api/asset-store/rag?{query}",
            timeout=5,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))

        self.assertEqual(200, response.status)
        self.assertEqual("lexical_fallback", payload["retrieval_mode"])
        self.assertTrue(payload["degraded"])
        self.assertEqual("pending", payload["index"]["state"])

    def test_cross_origin_post_is_forbidden(self) -> None:
        with self.assertRaises(HTTPError) as context:
            urlopen(
                self.request(
                    _owned_payload(),
                    origin="https://evil.example",
                ),
                timeout=5,
            )
        self.assertEqual(403, context.exception.code)
        error = json.loads(context.exception.read().decode("utf-8"))
        self.assertEqual("forbidden", error["error"]["code"])


class OwnedAssetRagExtensionStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = (
            Path(__file__).parents[1]
            / "browser_extension"
            / "unity_asset_store"
        )
        cls.manifest = json.loads(
            (cls.root / "manifest.json").read_text(encoding="utf-8")
        )
        cls.popup = (cls.root / "popup.html").read_text(encoding="utf-8")
        cls.popup_script = (cls.root / "popup.js").read_text(encoding="utf-8")
        cls.worker = (cls.root / "service_worker.js").read_text(encoding="utf-8")

    def test_extension_keeps_minimal_non_scraping_permissions(self) -> None:
        self.assertEqual(
            {"activeTab", "nativeMessaging"},
            set(self.manifest["permissions"]),
        )
        self.assertNotIn("host_permissions", self.manifest)
        self.assertNotIn("content_scripts", self.manifest)
        for forbidden in (
            "chrome.scripting",
            "executeScript",
            "fetch(",
            "document.",
            "innerHTML",
            "outerHTML",
            "cookie",
            "localStorage",
            "sessionStorage",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.worker)

    def test_popup_has_explicit_user_asserted_owned_rag_action(self) -> None:
        self.assertIn('id="save-owned-rag"', self.popup)
        self.assertIn('id="owned-confirmation"', self.popup)
        self.assertIn("購入済みとしてRAG登録（自己申告）", self.popup)
        self.assertIn("save_current_asset_store_owned_rag", self.popup_script)
        self.assertIn("save_owned_asset_rag", self.worker)
        self.assertIn("save-owned-rag", self.popup_script)
        self.assertIn("owned-confirmation", self.popup_script)
        self.assertIn("purchase_confirmation", self.worker)
        self.assertNotIn("購入確認済み", self.popup)


class OwnedAssetRagLocalUiStaticTests(unittest.TestCase):
    def test_local_ui_exposes_and_polls_rag_index_progress(self) -> None:
        root = Path(__file__).parents[1] / "game_stack_planner" / "static"
        markup = (root / "index.html").read_text(encoding="utf-8")
        script = (root / "app.js").read_text(encoding="utf-8")

        self.assertIn('id="rag-index-state"', markup)
        self.assertIn("renderRagIndex(status.rag_index)", script)
        self.assertIn("preparing_model", script)
        self.assertIn("window.setTimeout(loadStatus, 2000)", script)


if __name__ == "__main__":
    unittest.main()
