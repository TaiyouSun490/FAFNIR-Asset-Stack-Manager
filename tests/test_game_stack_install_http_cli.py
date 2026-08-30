from __future__ import annotations

import contextlib
import http.client
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

from game_stack_planner.api import GameStackApplication
from game_stack_planner.cli import build_parser
from game_stack_planner.models import Candidate
from game_stack_planner.server import create_server


PACKAGE_NAME = "com.example.http-boundary"
PACKAGE_VERSION = "1.2.3"


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _make_unity_project(root: Path) -> Path:
    packages = root / "Packages"
    settings = root / "ProjectSettings"
    packages.mkdir(parents=True)
    settings.mkdir()
    _write_json(
        packages / "manifest.json",
        {"dependencies": {"com.unity.inputsystem": "1.11.2"}},
    )
    _write_json(
        packages / "packages-lock.json",
        {
            "dependencies": {
                "com.unity.inputsystem": {
                    "version": "1.11.2",
                    "depth": 0,
                    "source": "registry",
                }
            }
        },
    )
    (settings / "ProjectVersion.txt").write_text(
        "m_EditorVersion: 6000.0.32f1\n",
        encoding="utf-8",
    )
    (settings / "ProjectSettings.asset").write_text(
        "PlayerSettings:\n  productName: HTTP Boundary Test\n",
        encoding="utf-8",
    )
    return root


def _openupm_candidate() -> Candidate:
    return Candidate(
        id=f"openupm:{PACKAGE_NAME}",
        source="openupm",
        external_id=PACKAGE_NAME,
        title="HTTP Boundary Package",
        url=f"https://openupm.com/packages/{PACKAGE_NAME}/",
        description="An exact-version package used only by local tests.",
        categories=("tools",),
        license="MIT",
        version=PACKAGE_VERSION,
    )


class InstallHTTPBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = _make_unity_project(self.root / "UnityProject")
        self.app = GameStackApplication(self.root / "catalog.db")
        self.candidate = _openupm_candidate()
        self.app.repository.upsert_candidates((self.candidate,))
        self.server = create_server(self.app, port=0)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.thread.start()
        self.host = "127.0.0.1"
        self.port = self.server.server_port
        self.origin = f"http://{self.host}:{self.port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.app.close()
        self.temp.cleanup()

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        origin: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        headers: dict[str, str] = {}
        body: bytes | None = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if origin is not None:
            headers["Origin"] = origin
        connection = http.client.HTTPConnection(
            self.host,
            self.port,
            timeout=5,
        )
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            return response.status, json.loads(raw.decode("utf-8"))
        finally:
            connection.close()

    @property
    def prepare_payload(self) -> dict[str, str]:
        return {
            "candidate_id": self.candidate.id,
            "project_path": str(self.project),
        }

    def test_prepare_requires_the_exact_loopback_origin(self) -> None:
        manifest = self.project / "Packages" / "manifest.json"
        before = manifest.read_bytes()
        rejected_origins = (
            None,
            "https://evil.example",
            f"http://localhost:{self.port}",
            f"http://{self.host}:{self.port + 1}",
        )

        for origin in rejected_origins:
            with self.subTest(origin=origin):
                status, payload = self.request(
                    "POST",
                    "/api/install/prepare",
                    self.prepare_payload,
                    origin=origin,
                )
                self.assertEqual(403, status)
                self.assertEqual("forbidden", payload["error"]["code"])

        status, payload = self.request(
            "POST",
            "/api/install/prepare",
            self.prepare_payload,
            origin=self.origin,
        )

        self.assertEqual(200, status)
        self.assertEqual("ready", payload["plan"]["status"])
        self.assertTrue(payload["approval_nonce"])
        self.assertEqual(before, manifest.read_bytes())

    def test_execute_requires_same_origin_and_job_is_readable_by_get(self) -> None:
        prepare_status, prepared = self.request(
            "POST",
            "/api/install/prepare",
            self.prepare_payload,
            origin=self.origin,
        )
        self.assertEqual(200, prepare_status)
        execute_payload = {
            "plan_id": prepared["plan"]["id"],
            "approval_nonce": prepared["approval_nonce"],
        }

        for origin in (None, "https://evil.example"):
            with self.subTest(origin=origin):
                status, payload = self.request(
                    "POST",
                    "/api/install/execute",
                    execute_payload,
                    origin=origin,
                )
                self.assertEqual(403, status)
                self.assertEqual("forbidden", payload["error"]["code"])

        execute_status, executed = self.request(
            "POST",
            "/api/install/execute",
            execute_payload,
            origin=self.origin,
        )
        self.assertEqual(200, execute_status)
        self.assertEqual(
            "applied_waiting_for_unity",
            executed["job"]["status"],
        )

        job_id = executed["job"]["id"]
        get_status, fetched = self.request(
            "GET",
            f"/api/install/jobs/{job_id}",
        )
        self.assertEqual(200, get_status)
        self.assertEqual(job_id, fetched["job"]["id"])
        self.assertEqual(executed["job"]["status"], fetched["job"]["status"])

    def test_install_http_payloads_reject_command_and_url_fields(self) -> None:
        for field, value in (
            ("command", "powershell -Command arbitrary"),
            ("url", "https://attacker.example/package.git"),
        ):
            with self.subTest(field=field):
                status, payload = self.request(
                    "POST",
                    "/api/install/prepare",
                    {**self.prepare_payload, field: value},
                    origin=self.origin,
                )
                self.assertEqual(400, status)
                self.assertEqual("invalid_request", payload["error"]["code"])


class InstallCLIParserBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = build_parser()

    def parse_rejected(self, argv: list[str]) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as context:
                self.parser.parse_args(argv)
        self.assertEqual(2, context.exception.code)

    def test_install_commands_are_exposed_with_closed_arguments(self) -> None:
        cases = (
            (
                ["install-plan", "openupm:com.example.pkg", "--project", "P"],
                {"command": "install-plan", "candidate_id": "openupm:com.example.pkg"},
            ),
            (
                ["install-apply", "plan-1", "--approval-nonce", "nonce-1"],
                {"command": "install-apply", "plan_id": "plan-1"},
            ),
            (
                ["install-status", "job-1"],
                {"command": "install-status", "job_id": "job-1"},
            ),
            (
                ["install-rollback", "job-1", "--rollback-nonce", "nonce-2"],
                {"command": "install-rollback", "job_id": "job-1"},
            ),
        )

        for argv, expected in cases:
            with self.subTest(command=argv[0]):
                parsed = self.parser.parse_args(argv)
                for name, value in expected.items():
                    self.assertEqual(value, getattr(parsed, name))
                self.assertFalse(hasattr(parsed, "url"))

    def test_unknown_command_and_command_or_url_options_are_rejected(self) -> None:
        rejected = (
            ["install-url", "https://attacker.example/package.git"],
            [
                "install-plan",
                "openupm:com.example.pkg",
                "--project",
                "P",
                "--url",
                "https://attacker.example/package.git",
            ],
            [
                "install-apply",
                "plan-1",
                "--approval-nonce",
                "nonce-1",
                "--command",
                "arbitrary",
            ],
        )
        for argv in rejected:
            with self.subTest(argv=argv):
                self.parse_rejected(argv)


if __name__ == "__main__":
    unittest.main()
