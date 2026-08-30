"""Hardened loopback-only HTTP server for the Game Stack Planner UI."""

from __future__ import annotations

import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from .api import ApiError, GameStackApplication

_MAX_BODY = 1024 * 1024
_STATIC_ROOT = Path(__file__).with_name("static")


def _error(status: int, code: str, message: str, details: dict[str, Any] | None = None):
    return status, {"error": {"code": code, "message": message, "details": details or {}}}


class PlannerHandler(BaseHTTPRequestHandler):
    server_version = "GameStackPlanner"

    @property
    def app(self) -> GameStackApplication:
        return self.server.application  # type: ignore[attr-defined]

    @property
    def static_root(self) -> Path:
        return self.server.static_root  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def _send_json(self, status: int, value: dict[str, Any]) -> None:
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(data)

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        )

    def _trusted_host(self) -> bool:
        host = self.headers.get("Host", "")
        name = host.rsplit(":", 1)[0].strip("[]").casefold()
        return name in {"127.0.0.1", "localhost", "::1"}

    def _trusted_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urlsplit(origin)
        return (
            parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            and parsed.port == self.server.server_port
        )

    def _trusted_install_origin(self) -> bool:
        origin = self.headers.get("Origin")
        host = self.headers.get("Host")
        if not origin or not host:
            return False
        parsed = urlsplit(origin)
        expected = urlsplit(f"http://{host}")
        try:
            return (
                parsed.scheme == "http"
                and parsed.username is None
                and parsed.password is None
                and parsed.hostname is not None
                and expected.hostname is not None
                and parsed.hostname.casefold().rstrip(".")
                == expected.hostname.casefold().rstrip(".")
                and parsed.port == expected.port
                and not parsed.query
                and not parsed.fragment
            )
        except ValueError:
            return False

    def _json_body(self) -> dict[str, Any]:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0]
        if content_type != "application/json":
            raise ApiError(415, "unsupported_media_type", "Use application/json.")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ApiError(400, "invalid_request", "Invalid Content-Length.") from exc
        if length <= 0 or length > _MAX_BODY:
            raise ApiError(413, "request_too_large", "Request body size is invalid.")
        try:
            value = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ApiError(400, "invalid_json", "Request body is not valid JSON.") from exc
        if not isinstance(value, dict):
            raise ApiError(400, "invalid_request", "JSON body must be an object.")
        return value

    def _discard_bounded_body(self) -> None:
        """Drain a small rejected POST so Windows can deliver the HTTP error."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return
        if 0 < length <= _MAX_BODY:
            self.rfile.read(length)

    def _serve_static(self) -> None:
        raw_path = unquote(urlsplit(self.path).path)
        relative = "index.html" if raw_path == "/" else raw_path.lstrip("/")
        if relative.startswith("static/"):
            relative = relative.removeprefix("static/")
        target = (self.static_root / relative).resolve()
        root = self.static_root.resolve()
        if root not in target.parents and target != root:
            self._send_json(*_error(404, "not_found", "Not found."))
            return
        if not target.is_file():
            self._send_json(*_error(404, "not_found", "Not found."))
            return
        data = target.read_bytes()
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", f"{mime}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if not self._trusted_host():
            self._send_json(*_error(400, "invalid_host", "Loopback host required."))
            return
        parsed = urlsplit(self.path)
        try:
            if parsed.path == "/api/status":
                self._send_json(200, self.app.status())
            elif parsed.path == "/api/catalog":
                query = parse_qs(parsed.query, keep_blank_values=True)
                self._send_json(200, self.app.catalog(
                    query=query.get("q", [""])[0],
                    source=query.get("source", [""])[0],
                    ownership=query.get("ownership", [""])[0],
                    scope=query.get("scope", [""])[0],
                    limit=query.get("limit", ["100"])[0],
                ))
            elif parsed.path == "/api/asset-store/rag":
                query = parse_qs(parsed.query, keep_blank_values=True)
                self._send_json(200, self.app.search_asset_rag(
                    query=query.get("q", [""])[0],
                    limit=query.get("limit", ["20"])[0],
                ))
            elif parsed.path.startswith("/api/install/jobs/"):
                job_id = unquote(parsed.path.removeprefix("/api/install/jobs/"))
                if not job_id or "/" in job_id:
                    self._send_json(*_error(404, "not_found", "API route not found."))
                    return
                self._send_json(200, self.app.install_job(job_id))
            elif parsed.path.startswith("/api/"):
                self._send_json(*_error(404, "not_found", "API route not found."))
            else:
                self._serve_static()
        except ApiError as exc:
            self._send_json(*_error(exc.status, exc.code, str(exc), exc.details))
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            self._send_json(*_error(500, "internal_error", "Unexpected server error."))

    def do_POST(self) -> None:
        route = urlsplit(self.path).path
        install_route = route.startswith("/api/install/")
        trusted = (
            self._trusted_host()
            and self._trusted_origin()
            and (not install_route or self._trusted_install_origin())
        )
        if not trusted:
            self._discard_bounded_body()
            message = "Exact same-origin approval is required." if install_route else "Loopback origin required."
            self._send_json(*_error(403, "forbidden", message))
            return
        try:
            payload = self._json_body()
            if route == "/api/project/scan":
                result = self.app.scan(payload)
            elif route == "/api/asset-store/cache/scan":
                result = self.app.scan_cache(payload)
            elif route == "/api/asset-store/my-assets/sync":
                result = self.app.sync_unity_my_assets(payload)
            elif route == "/api/recommend":
                result = self.app.recommend(payload)
            elif route == "/api/catalog/manual":
                result = self.app.save_manual(payload)
            elif route == "/api/asset-store/rag":
                result = self.app.save_owned_rag(payload)
            elif route == "/api/install/prepare":
                result = self.app.prepare_install(payload)
            elif route == "/api/install/execute":
                result = self.app.execute_install(payload)
            elif route == "/api/install/rollback":
                result = self.app.rollback_install(payload)
            elif route.startswith("/api/install/jobs/") and route.endswith("/rollback"):
                job_id = unquote(route.removeprefix("/api/install/jobs/").removesuffix("/rollback").rstrip("/"))
                if not job_id or "/" in job_id:
                    self._send_json(*_error(404, "not_found", "API route not found."))
                    return
                result = self.app.rollback_install({**payload, "job_id": job_id})
            else:
                self._send_json(*_error(404, "not_found", "API route not found."))
                return
            self._send_json(200, result)
        except ApiError as exc:
            self._send_json(*_error(exc.status, exc.code, str(exc), exc.details))
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            self._send_json(*_error(500, "internal_error", "Unexpected server error."))


class PlannerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        application: GameStackApplication,
        *,
        static_root: Path | None = None,
    ) -> None:
        self.application = application
        self.static_root = static_root or _STATIC_ROOT
        super().__init__(address, PlannerHandler)


def create_server(
    application: GameStackApplication,
    *,
    port: int = 8770,
    static_root: Path | None = None,
) -> PlannerHTTPServer:
    if port < 0 or port > 65535:
        raise ValueError("Port must be between 0 and 65535.")
    return PlannerHTTPServer(
        ("127.0.0.1", port),
        application,
        static_root=static_root,
    )


__all__ = ["PlannerHTTPServer", "create_server"]
