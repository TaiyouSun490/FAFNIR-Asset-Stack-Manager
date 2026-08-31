"""Local multilingual text embeddings for owned-asset retrieval.

The runtime is deliberately lazy: importing Stackforge does not import PyTorch,
load a model, or access the network.  An explicit RAG indexing operation loads
the configured Hugging Face model and persists only normalized float vectors.
"""

from __future__ import annotations

import concurrent.futures
import importlib
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from hashlib import sha256
from typing import Any, Protocol, runtime_checkable


DEFAULT_TEXT_EMBEDDING_MODEL = "intfloat/multilingual-e5-small"
DEFAULT_TEXT_EMBEDDING_REVISION = (
    "614241f622f53c4eeff9890bdc4f31cfecc418b3"
)
DEFAULT_TEXT_EMBEDDING_WEIGHT_FILE = "model.safetensors"
DEFAULT_TEXT_EMBEDDING_WEIGHT_BYTES = 470_641_600
DEFAULT_TEXT_EMBEDDING_WEIGHT_SHA256 = (
    "1a55775f53449dac10a2bcbc312469fac40b96d53198c407081a831f81c98477"
)
DEFAULT_TEXT_EMBEDDING_DOWNLOAD_PARTS = 4
EMBEDDING_PIPELINE_VERSION = "e5-mean-pool-query-passage-v1"
MAX_EMBEDDING_TEXT_LENGTH = 8192


class TextEmbeddingError(RuntimeError):
    """A local text-embedding operation could not be completed."""


class TextEmbeddingDependencyError(TextEmbeddingError):
    """Optional local inference dependencies are unavailable."""


class TextEmbeddingModelError(TextEmbeddingError):
    """The embedding model returned invalid vectors."""


@runtime_checkable
class TextEmbeddingBackend(Protocol):
    @property
    def model_id(self) -> str: ...

    @property
    def generation_id(self) -> str: ...

    def encode_documents(
        self, texts: Sequence[str]
    ) -> tuple[tuple[float, ...], ...]: ...

    def encode_query(self, text: str) -> tuple[float, ...]: ...


def normalize_vector(values: Sequence[float]) -> tuple[float, ...]:
    vector = tuple(float(value) for value in values)
    if not vector or any(not math.isfinite(value) for value in vector):
        raise TextEmbeddingModelError("embedding contains invalid values")
    norm = math.sqrt(sum(value * value for value in vector))
    if not math.isfinite(norm) or norm <= 0:
        raise TextEmbeddingModelError("embedding has zero or invalid norm")
    return tuple(value / norm for value in vector)


class TransformersTextEmbeddingBackend:
    """Mean-pooled multilingual E5 embeddings using local Transformers inference."""

    def __init__(
        self,
        model_id: str | None = None,
        *,
        revision: str | None = None,
        device: str = "auto",
        local_files_only: bool = False,
        batch_size: int = 32,
        max_tokens: int = 256,
    ) -> None:
        self._model_id = str(
            model_id
            or os.getenv("STACKFORGE_TEXT_EMBEDDING_MODEL")
            or DEFAULT_TEXT_EMBEDDING_MODEL
        ).strip()
        if not self._model_id or len(self._model_id) > 256:
            raise ValueError("model_id is invalid")
        configured_revision = str(
            revision
            or os.getenv("STACKFORGE_TEXT_EMBEDDING_REVISION")
            or (
                DEFAULT_TEXT_EMBEDDING_REVISION
                if self._model_id == DEFAULT_TEXT_EMBEDDING_MODEL
                else ""
            )
        ).strip()
        if not configured_revision:
            raise ValueError(
                "custom embedding models require an immutable revision"
            )
        if len(configured_revision) > 256:
            raise ValueError("embedding model revision is invalid")
        if device not in {"auto", "cpu", "cuda"}:
            raise ValueError("device must be auto, cpu, or cuda")
        if not 1 <= int(batch_size) <= 128:
            raise ValueError("batch_size must be between 1 and 128")
        if not 8 <= int(max_tokens) <= 512:
            raise ValueError("max_tokens must be between 8 and 512")
        self.device_request = device
        self.revision = configured_revision
        self.local_files_only = bool(local_files_only)
        self.batch_size = int(batch_size)
        self.max_tokens = int(max_tokens)
        self._runtime: tuple[Any, Any, Any, str] | None = None
        self._huggingface_tls_configured = False

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def generation_id(self) -> str:
        payload = json.dumps(
            {
                "model_id": self._model_id,
                "revision": self.revision,
                "pipeline": EMBEDDING_PIPELINE_VERSION,
                "max_tokens": self.max_tokens,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = sha256(payload.encode("utf-8")).hexdigest()[:24]
        return f"embedding:{digest}"

    def identity(self) -> dict[str, Any]:
        return {
            "generation_id": self.generation_id,
            "model_id": self._model_id,
            "revision": self.revision,
            "pipeline": EMBEDDING_PIPELINE_VERSION,
            "max_tokens": self.max_tokens,
        }

    @staticmethod
    def missing_dependencies() -> tuple[str, ...]:
        required = ["torch", "transformers"]
        if os.name == "nt":
            required.append("truststore")
        return tuple(
            name for name in required
            if importlib.util.find_spec(name) is None
        )

    def prepare(self) -> None:
        """Load or download the pinned model before document indexing."""
        self._load()

    @staticmethod
    def _huggingface_hub_cache() -> Path:
        configured = os.getenv("HF_HUB_CACHE")
        if configured:
            return Path(configured).expanduser()
        home = os.getenv("HF_HOME")
        root = Path(home).expanduser() if home else Path.home() / ".cache" / "huggingface"
        return root / "hub"

    def _default_weight_paths(self) -> tuple[Path, Path, tuple[Path, ...]]:
        repo = self._huggingface_hub_cache() / (
            "models--" + DEFAULT_TEXT_EMBEDDING_MODEL.replace("/", "--")
        )
        snapshot = repo / "snapshots" / self.revision
        blob = repo / "blobs" / DEFAULT_TEXT_EMBEDDING_WEIGHT_SHA256
        download = (
            repo
            / "stackforge-downloads"
            / self.revision
            / DEFAULT_TEXT_EMBEDDING_WEIGHT_SHA256
        )
        parts = tuple(
            download / f"part-{index:02d}.bin"
            for index in range(DEFAULT_TEXT_EMBEDDING_DOWNLOAD_PARTS)
        )
        return snapshot, blob, parts

    @staticmethod
    def _default_weight_ranges() -> tuple[tuple[int, int], ...]:
        size = DEFAULT_TEXT_EMBEDDING_WEIGHT_BYTES
        count = DEFAULT_TEXT_EMBEDDING_DOWNLOAD_PARTS
        base, remainder = divmod(size, count)
        ranges: list[tuple[int, int]] = []
        start = 0
        for index in range(count):
            length = base + (1 if index < remainder else 0)
            ranges.append((start, start + length - 1))
            start += length
        return tuple(ranges)

    def model_download_status(self) -> dict[str, Any] | None:
        if (
            self._model_id != DEFAULT_TEXT_EMBEDDING_MODEL
            or self.revision != DEFAULT_TEXT_EMBEDDING_REVISION
        ):
            return None
        snapshot, blob, parts = self._default_weight_paths()
        completed = snapshot / DEFAULT_TEXT_EMBEDDING_WEIGHT_FILE
        if completed.is_file() and completed.stat().st_size == DEFAULT_TEXT_EMBEDDING_WEIGHT_BYTES:
            downloaded = DEFAULT_TEXT_EMBEDDING_WEIGHT_BYTES
        elif blob.is_file() and blob.stat().st_size == DEFAULT_TEXT_EMBEDDING_WEIGHT_BYTES:
            downloaded = DEFAULT_TEXT_EMBEDDING_WEIGHT_BYTES
        else:
            downloaded = 0
            for part, (start, end) in zip(
                parts,
                self._default_weight_ranges(),
                strict=True,
            ):
                expected = end - start + 1
                try:
                    downloaded += min(expected, int(part.stat().st_size))
                except OSError:
                    continue
        total = DEFAULT_TEXT_EMBEDDING_WEIGHT_BYTES
        return {
            "file": DEFAULT_TEXT_EMBEDDING_WEIGHT_FILE,
            "downloaded_bytes": downloaded,
            "total_bytes": total,
            "progress": round(downloaded / total, 6),
            "parts": DEFAULT_TEXT_EMBEDDING_DOWNLOAD_PARTS,
        }

    @staticmethod
    def _download_default_weight_part(
        part: Path,
        byte_range: tuple[int, int],
    ) -> None:
        range_start, range_end = byte_range
        expected = range_end - range_start + 1
        part.parent.mkdir(parents=True, exist_ok=True)
        try:
            current = int(part.stat().st_size)
        except OSError:
            current = 0
        if current > expected:
            with part.open("wb"):
                pass
            current = 0
        url = (
            "https://huggingface.co/"
            f"{DEFAULT_TEXT_EMBEDDING_MODEL}/resolve/"
            f"{DEFAULT_TEXT_EMBEDDING_REVISION}/"
            f"{DEFAULT_TEXT_EMBEDDING_WEIGHT_FILE}"
        )
        failures = 0
        while current < expected:
            absolute_start = range_start + current
            request = urllib.request.Request(
                url,
                headers={
                    "Range": f"bytes={absolute_start}-{range_end}",
                    "User-Agent": "Stackforge/0.4 pinned-model-downloader",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=90) as response:
                    content_range = str(response.headers.get("Content-Range") or "")
                    if response.status != 206 or not content_range.startswith(
                        f"bytes {absolute_start}-"
                    ):
                        raise TextEmbeddingModelError(
                            "pinned model server did not honor a verified byte range"
                        )
                    with part.open("ab") as stream:
                        while current < expected:
                            chunk = response.read(min(1024 * 1024, expected - current))
                            if not chunk:
                                break
                            stream.write(chunk)
                            stream.flush()
                            current += len(chunk)
                failures = 0
            except (
                OSError,
                TimeoutError,
                urllib.error.URLError,
            ) as exc:
                failures += 1
                if failures >= 8:
                    raise TextEmbeddingModelError(
                        "pinned model download repeatedly failed"
                    ) from exc
                time.sleep(min(30.0, float(2 ** failures)))
        if int(part.stat().st_size) != expected:
            raise TextEmbeddingModelError("pinned model part has an invalid size")

    def _ensure_default_model_weight(self) -> Path:
        snapshot, blob, parts = self._default_weight_paths()
        target = snapshot / DEFAULT_TEXT_EMBEDDING_WEIGHT_FILE
        if target.is_file() and target.stat().st_size == DEFAULT_TEXT_EMBEDDING_WEIGHT_BYTES:
            for part in parts:
                part.unlink(missing_ok=True)
            return snapshot
        ranges = self._default_weight_ranges()
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=DEFAULT_TEXT_EMBEDDING_DOWNLOAD_PARTS,
            thread_name_prefix="stackforge-model-download",
        ) as executor:
            futures = [
                executor.submit(self._download_default_weight_part, part, byte_range)
                for part, byte_range in zip(parts, ranges, strict=True)
            ]
            for future in futures:
                future.result()

        assembly = parts[0].parent / "model.safetensors.assembling"
        digest = sha256()
        with assembly.open("wb") as destination:
            for part in parts:
                with part.open("rb") as source:
                    while chunk := source.read(1024 * 1024):
                        digest.update(chunk)
                        destination.write(chunk)
        if assembly.stat().st_size != DEFAULT_TEXT_EMBEDDING_WEIGHT_BYTES:
            assembly.unlink(missing_ok=True)
            raise TextEmbeddingModelError("assembled pinned model has an invalid size")
        if digest.hexdigest() != DEFAULT_TEXT_EMBEDDING_WEIGHT_SHA256:
            assembly.unlink(missing_ok=True)
            raise TextEmbeddingModelError("pinned model SHA-256 verification failed")

        blob.parent.mkdir(parents=True, exist_ok=True)
        os.replace(assembly, blob)
        snapshot.mkdir(parents=True, exist_ok=True)
        target.unlink(missing_ok=True)
        try:
            os.link(blob, target)
        except OSError:
            shutil.copyfile(blob, target)
        for part in parts:
            part.unlink(missing_ok=True)
        return snapshot

    def _configure_huggingface_tls(self) -> None:
        if (
            self._huggingface_tls_configured
            or self.local_files_only
            or os.name != "nt"
        ):
            return
        try:
            ssl = importlib.import_module("ssl")
            httpx = importlib.import_module("httpx")
            truststore = importlib.import_module("truststore")
            huggingface_hub = importlib.import_module("huggingface_hub")
        except ImportError as exc:
            raise TextEmbeddingDependencyError(
                "verified Hugging Face downloads on Windows require truststore"
            ) from exc

        def client_factory() -> Any:
            context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            return httpx.Client(
                verify=context,
                follow_redirects=True,
                timeout=httpx.Timeout(60.0, connect=20.0),
            )

        huggingface_hub.set_client_factory(client_factory)
        self._huggingface_tls_configured = True

    def _load(self) -> tuple[Any, Any, Any, str]:
        if self._runtime is not None:
            return self._runtime
        # Hugging Face's Xet transport can repeatedly restart a large first-run
        # model download on slow or short-lived Windows sessions. Prefer the
        # ordinary resumable HTTP cache and allow long reads before importing
        # libraries whose download settings are initialized at import time.
        os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
        os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "600")
        os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "60")
        try:
            torch = importlib.import_module("torch")
            transformers = importlib.import_module("transformers")
        except ImportError as exc:
            raise TextEmbeddingDependencyError(
                "text embeddings require the optional 'rag' dependencies"
            ) from exc
        device = self.device_request
        if device == "auto":
            device = "cuda" if bool(torch.cuda.is_available()) else "cpu"
        if device == "cuda" and not bool(torch.cuda.is_available()):
            raise TextEmbeddingDependencyError("CUDA was requested but is unavailable")
        self._configure_huggingface_tls()
        try:
            default_pinned_model = bool(
                not self.local_files_only
                and self._model_id == DEFAULT_TEXT_EMBEDDING_MODEL
                and self.revision == DEFAULT_TEXT_EMBEDDING_REVISION
            )
            local_snapshot: Path | None = None
            if default_pinned_model:
                snapshot, _, _ = self._default_weight_paths()
                weight = snapshot / DEFAULT_TEXT_EMBEDDING_WEIGHT_FILE
                if (
                    weight.is_file()
                    and weight.stat().st_size == DEFAULT_TEXT_EMBEDDING_WEIGHT_BYTES
                ):
                    local_snapshot = self._ensure_default_model_weight()
            tokenizer_source = (
                str(local_snapshot) if local_snapshot is not None else self._model_id
            )
            tokenizer = transformers.AutoTokenizer.from_pretrained(
                tokenizer_source,
                revision=None if local_snapshot is not None else self.revision,
                local_files_only=(
                    True if local_snapshot is not None else self.local_files_only
                ),
            )
            model_source = self._model_id
            model_options: dict[str, Any] = {
                "revision": self.revision,
                "local_files_only": self.local_files_only,
            }
            if default_pinned_model:
                model_source = str(
                    local_snapshot or self._ensure_default_model_weight()
                )
                model_options = {"local_files_only": True}
            model = transformers.AutoModel.from_pretrained(
                model_source,
                **model_options,
            )
            model.to(device)
            model.eval()
        except Exception as exc:  # provider errors vary across versions
            raise TextEmbeddingModelError(
                f"could not load pinned text embedding model {self._model_id!r} "
                f"({type(exc).__name__})"
            ) from exc
        self._runtime = (torch, tokenizer, model, device)
        return self._runtime

    @staticmethod
    def _bounded(value: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("embedding text must not be empty")
        if len(text) > MAX_EMBEDDING_TEXT_LENGTH:
            text = text[:MAX_EMBEDDING_TEXT_LENGTH]
        return text

    def _encode(
        self,
        texts: Sequence[str],
        *,
        prefix: str,
    ) -> tuple[tuple[float, ...], ...]:
        values = tuple(self._bounded(value) for value in texts)
        if not values:
            return ()
        torch, tokenizer, model, device = self._load()
        results: list[tuple[float, ...]] = []
        for offset in range(0, len(values), self.batch_size):
            batch = [prefix + value for value in values[offset:offset + self.batch_size]]
            encoded = tokenizer(
                batch,
                padding=True,
                truncation=True,
                max_length=self.max_tokens,
                return_tensors="pt",
            )
            encoded = {key: value.to(device) for key, value in encoded.items()}
            with torch.inference_mode():
                if hasattr(model, "get_text_features"):
                    pooled = model.get_text_features(**encoded)
                    if not hasattr(pooled, "norm"):
                        pooled = getattr(pooled, "pooler_output", None)
                    if pooled is None or not hasattr(pooled, "norm"):
                        raise TextEmbeddingModelError(
                            "text feature model did not return a pooled tensor"
                        )
                else:
                    hidden = model(**encoded).last_hidden_state
                    mask = encoded["attention_mask"].unsqueeze(-1).to(hidden.dtype)
                    pooled = (
                        (hidden * mask).sum(dim=1)
                        / mask.sum(dim=1).clamp(min=1)
                    )
                pooled = torch.nn.functional.normalize(pooled, p=2, dim=1)
            for row in pooled.detach().cpu().tolist():
                results.append(normalize_vector(row))
        return tuple(results)

    def encode_documents(
        self, texts: Sequence[str]
    ) -> tuple[tuple[float, ...], ...]:
        return self._encode(texts, prefix="passage: ")

    def encode_query(self, text: str) -> tuple[float, ...]:
        return self._encode((text,), prefix="query: ")[0]


__all__ = [
    "DEFAULT_TEXT_EMBEDDING_MODEL",
    "DEFAULT_TEXT_EMBEDDING_REVISION",
    "DEFAULT_TEXT_EMBEDDING_WEIGHT_BYTES",
    "DEFAULT_TEXT_EMBEDDING_WEIGHT_FILE",
    "DEFAULT_TEXT_EMBEDDING_WEIGHT_SHA256",
    "EMBEDDING_PIPELINE_VERSION",
    "TextEmbeddingBackend",
    "TextEmbeddingDependencyError",
    "TextEmbeddingError",
    "TextEmbeddingModelError",
    "TransformersTextEmbeddingBackend",
    "normalize_vector",
]
