"""Native Messaging bridge for the minimal Unity Asset Store extension."""

from __future__ import annotations

import json
import os
import re
import struct
import sys
from pathlib import Path
from typing import Any, BinaryIO, Sequence

from .api import ApiError, GameStackApplication
from .repository import default_database_path

HOST_NAME = "jp.game_stack_planner"
SCHEMA_VERSION = 1
MAX_MESSAGE_BYTES = 64 * 1024
_EXTENSION_ORIGIN = re.compile(r"^chrome-extension://[a-p]{32}/$")


class NativeMessagingError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def default_configuration_path() -> Path:
    base = os.getenv("LOCALAPPDATA") or os.getenv("APPDATA")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "GameStackPlanner" / "NativeMessaging" / "host_config.json"


def load_configuration(path: str | Path | None = None) -> dict[str, str]:
    target = Path(path or default_configuration_path())
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NativeMessagingError(
            "host_not_configured",
            "Stackforge Native Messaging host is not configured.",
        ) from exc
    if not isinstance(value, dict) or set(value) != {
        "allowed_origin",
        "database_path",
    }:
        raise NativeMessagingError(
            "invalid_host_configuration",
            "Stackforge Native Messaging configuration is invalid.",
        )
    origin = value.get("allowed_origin")
    database = value.get("database_path")
    if (
        not isinstance(origin, str)
        or _EXTENSION_ORIGIN.fullmatch(origin) is None
        or not isinstance(database, str)
        or not database.strip()
        or len(database) > 2048
    ):
        raise NativeMessagingError(
            "invalid_host_configuration",
            "Stackforge Native Messaging configuration is invalid.",
        )
    return {"allowed_origin": origin, "database_path": database}


def _read_exact(stream: BinaryIO, length: int) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise NativeMessagingError(
                "invalid_message_frame",
                "Native message ended before the declared length.",
            )
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_message(stream: BinaryIO) -> dict[str, Any] | None:
    prefix = stream.read(4)
    if prefix == b"":
        return None
    if len(prefix) != 4:
        raise NativeMessagingError(
            "invalid_message_frame",
            "Native message length prefix is incomplete.",
        )
    length = struct.unpack("<I", prefix)[0]
    if length <= 0 or length > MAX_MESSAGE_BYTES:
        raise NativeMessagingError(
            "message_too_large",
            "Native message size is outside the allowed range.",
        )
    try:
        value = json.loads(_read_exact(stream, length).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NativeMessagingError(
            "invalid_message_json",
            "Native message is not valid JSON.",
        ) from exc
    if not isinstance(value, dict):
        raise NativeMessagingError(
            "invalid_message",
            "Native message must be a JSON object.",
        )
    return value


def write_message(stream: BinaryIO, value: dict[str, Any]) -> None:
    body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    if len(body) > MAX_MESSAGE_BYTES:
        body = json.dumps({
            "ok": False,
            "schema_version": SCHEMA_VERSION,
            "error": {
                "code": "response_too_large",
                "message": "Native response exceeded the size limit.",
            },
        }, separators=(",", ":")).encode("utf-8")
    stream.write(struct.pack("<I", len(body)))
    stream.write(body)
    stream.flush()


def _error(error: Exception) -> dict[str, Any]:
    if isinstance(error, NativeMessagingError):
        code = error.code
        message = str(error)
    elif isinstance(error, ApiError):
        code = error.code
        message = str(error)
    else:
        code = "native_host_error"
        message = "Stackforge could not process the browser request."
    return {
        "ok": False,
        "schema_version": SCHEMA_VERSION,
        "error": {"code": code, "message": message[:500]},
    }


def _message_fields(
    message: dict[str, Any],
    *,
    action: str,
    allowed: set[str],
) -> None:
    if (
        message.get("schema_version") != SCHEMA_VERSION
        or message.get("action") != action
        or set(message) - allowed
    ):
        raise NativeMessagingError(
            "invalid_message",
            "The extension message shape is not supported.",
        )


def handle_message(
    message: dict[str, Any],
    application: GameStackApplication,
) -> dict[str, Any]:
    action = message.get("action")
    if action == "get_stackforge_status":
        _message_fields(
            message,
            action=action,
            allowed={"action", "schema_version"},
        )
        status = application.status()
        return {
            "ok": True,
            "schema_version": SCHEMA_VERSION,
            "catalog": status["catalog"],
            "requirement_categories": status["requirement_categories"],
        }
    if action == "save_asset_store_candidate":
        _message_fields(
            message,
            action=action,
            allowed={
                "action",
                "schema_version",
                "url",
                "title",
                "notes",
                "categories",
            },
        )
        result = application.save_manual({
            "url": message.get("url"),
            "title": message.get("title"),
            "notes": message.get("notes", ""),
            "categories": message.get("categories", []),
            "ownership": "candidate",
        })
        return {
            "ok": True,
            "schema_version": SCHEMA_VERSION,
            "item": result["item"],
            "catalog": result["catalog"],
        }
    if action == "save_owned_asset_rag":
        _message_fields(
            message,
            action=action,
            allowed={
                "action",
                "schema_version",
                "url",
                "title",
                "user_alias",
                "notes",
                "categories",
                "purchase_confirmation",
            },
        )
        if message.get("purchase_confirmation") is not True:
            raise NativeMessagingError(
                "purchase_confirmation_required",
                "Explicit self-asserted purchase confirmation is required.",
            )
        result = application.save_owned_rag({
            "url": message.get("url"),
            "title": message.get("title"),
            "user_alias": message.get("user_alias", ""),
            "notes": message.get("notes", ""),
            "categories": message.get("categories", []),
            "purchase_confirmation": True,
        })
        return {
            "ok": True,
            "schema_version": SCHEMA_VERSION,
            "item": result["item"],
            "catalog": result["catalog"],
        }
    raise NativeMessagingError(
        "unsupported_action",
        "The extension action is not supported.",
    )


def run_host(
    *,
    origin: str,
    input_stream: BinaryIO,
    output_stream: BinaryIO,
    configuration_path: str | Path | None = None,
) -> int:
    try:
        configuration = load_configuration(configuration_path)
        if origin != configuration["allowed_origin"]:
            raise NativeMessagingError(
                "forbidden_extension",
                "This Chrome extension is not allowed to use Stackforge.",
            )
    except Exception as exc:
        write_message(output_stream, _error(exc))
        return 2

    application = GameStackApplication(
        configuration.get("database_path") or default_database_path()
    )
    try:
        while True:
            try:
                message = read_message(input_stream)
                if message is None:
                    return 0
                response = handle_message(message, application)
            except Exception as exc:
                response = _error(exc)
            write_message(output_stream, response)
    finally:
        application.close()


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    origin = arguments[0] if arguments else ""
    return run_host(
        origin=origin,
        input_stream=sys.stdin.buffer,
        output_stream=sys.stdout.buffer,
    )


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "HOST_NAME",
    "MAX_MESSAGE_BYTES",
    "NativeMessagingError",
    "SCHEMA_VERSION",
    "default_configuration_path",
    "handle_message",
    "load_configuration",
    "main",
    "read_message",
    "run_host",
    "write_message",
]
