from __future__ import annotations

import contextlib
import io
import json
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from game_stack_planner.api import GameStackApplication
from game_stack_planner.cli import main as cli_main
from game_stack_planner.native_messaging import (
    MAX_MESSAGE_BYTES,
    NativeMessagingError,
    handle_message,
    read_message,
    run_host,
)
from game_stack_planner.repository import StackRepository
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


class OwnedAssetRagApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.application = GameStackApplication(
            Path(self.temporary.name) / "catalog.db"
        )

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
        rag = self.application.search_asset_rag(query="重量制限", limit=10)
        self.assertEqual(1, rag["count"])
        self.assertEqual("asset_store:77701", rag["items"][0]["candidate_id"])

    def test_search_returns_only_user_authored_ai_safe_fields(self) -> None:
        self.application.save_owned_rag(_owned_payload())

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

    def test_multiple_query_terms_use_and_semantics(self) -> None:
        self.application.save_owned_rag(_owned_payload())

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
        self.assertEqual(0, partially_matching["count"])

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


class OwnedAssetRagNativeMessagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.application = GameStackApplication(
            Path(self.temporary.name) / "catalog.db"
        )

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
    def test_rag_search_prints_only_ai_safe_fields(self) -> None:
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
        self.assertEqual(1, payload["count"])
        self.assertEqual(
            {
                "candidate_id",
                "user_alias",
                "notes",
                "categories",
                "ownership_evidence",
            },
            set(payload["items"][0]),
        )
        serialized = json.dumps(
            payload["items"][0],
            ensure_ascii=False,
        ).casefold()
        self.assertNotIn("assetstore.unity.com", serialized)
        self.assertNotIn("marketplace.unity.com", serialized)
        self.assertNotIn("store supplied inventory workbench", serialized)
        self.assertNotIn("product_url", serialized)
        self.assertNotIn("local_path", serialized)


class OwnedAssetRagHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        static = root / "static"
        static.mkdir()
        (static / "index.html").write_text("<h1>test</h1>", encoding="utf-8")
        self.application = GameStackApplication(root / "catalog.db")
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
            },
            set(payload["items"][0]),
        )
        serialized = json.dumps(payload["items"][0], ensure_ascii=False)
        self.assertNotIn("assetstore.unity.com", serialized)
        self.assertNotIn("Store Supplied Inventory Workbench", serialized)

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


if __name__ == "__main__":
    unittest.main()
