"""Provider B: local Llama 3.1 8B Instruct through a local Ollama runtime (Tier 2).

Tech: Local model behind a signing provider (build plan: Provider B)

What this provider binds, and what it does not (contracts/INFERENCE-GATEWAY-CONTRACT.md, Local
Tier 2 provider):
- At bind time it reads the Ollama manifest for one model tag, hashes the exact GGUF file and the
  chat-template file it names (SHA-256 of the exact bytes), checks both against the manifest's
  content addresses, and builds the ExecutionProfile from what it observed: those digests, the
  runtime version Ollama reports, and the weight types Ollama reports for every tensor.
- Before every execution it re-reads the manifest and refuses if the tag now names another file.
- It runs the request through Ollama's /api/chat on a loopback address only, returns the response
  bytes and Ollama's own token counts, and signs a credence.provider_attestation over them
  through Core C3A.

Artifact binding is not execution measurement. Hashing the file on disk shows which file the tag
names; it does not prove those weights were resident in GPU memory, and there is no TEE or other
hardware attestation. This is Tier 2, never Tier 3.

The tokenizer is embedded in the GGUF file and has no separate bytes, so tokenizer_digest is null
(NOT ASSERTED separately; the artifact digest covers the file it lives in).

Owner: Inference.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from nacl.signing import SigningKey

from credence.core.objects import ExecutionProfile, ProviderAttestationBody, SamplingPolicy
from credence.core.primitives import ProviderId, SeedPolicy, Sha256Digest
from credence.core.signatures import sign
from credence.inference.payload import payload_digest
from credence.inference.provider import (
    AttestationCapability,
    ProviderAttestation,
    ProviderExecutionError,
    ProviderRequest,
    ProviderResult,
    ProviderUnavailableError,
)

LOCAL_PROVIDER_ID = ProviderId("local-llama-provider")
# The local Qwen3 30B A3B Instruct 2507 provider (Inference CP3): same runtime path, its own ID.
LOCAL_QWEN_PROVIDER_ID = ProviderId("local-qwen-provider")
DEFAULT_BASE_URL = "http://127.0.0.1:11434"
OLLAMA_REGISTRY = "registry.ollama.ai"
MODEL_LAYER = "application/vnd.ollama.image.model"
TEMPLATE_LAYER = "application/vnd.ollama.image.template"
ADAPTER_LAYER = "application/vnd.ollama.image.adapter"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
# GGUF general.file_type 1 is "mostly F16": weights stored as F16, small tensors (norms) as F32.
GGUF_FILE_TYPE_MOSTLY_F16 = 1
UNQUANTIZED_TENSOR_TYPES = frozenset({"F16", "F32"})
# GGUF general.file_type 7 is "mostly Q8_0": weights in Q8_0 blocks, small tensors (norms) in F32.
GGUF_FILE_TYPE_MOSTLY_Q8_0 = 7
Q8_0_TENSOR_TYPES = frozenset({"Q8_0", "F32"})
_CHUNK = 16 * 1024 * 1024


class ArtifactBindingError(RuntimeError):
    """The local installation does not match what the provider would claim. Nothing is served."""


def sha256_file(path: Path) -> Sha256Digest:
    """SHA-256 of a file's exact bytes, streamed."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return Sha256Digest.from_hex(digest.hexdigest())


def default_models_dir() -> Path:
    """Ollama's model store: $OLLAMA_MODELS, else ~/.ollama/models."""
    configured = os.environ.get("OLLAMA_MODELS")
    return Path(configured) if configured else Path.home() / ".ollama" / "models"


# --- the local installation ----------------------------------------------------------------------


@dataclass(frozen=True)
class InstalledModel:
    """One Ollama model tag as installed: the files its manifest names, by content address."""

    tag: str
    manifest_path: Path
    model_digest: Sha256Digest
    model_path: Path
    model_size: int
    template_digest: Sha256Digest | None
    template_path: Path | None
    has_adapter: bool


def _manifest_path(models_dir: Path, tag: str) -> Path:
    name, _, version = tag.partition(":")
    if not name or "/" in name or not version:
        raise ArtifactBindingError(f"expected a library tag like name:version, got {tag!r}")
    return models_dir / "manifests" / OLLAMA_REGISTRY / "library" / name / version


def _blob_path(models_dir: Path, digest: Sha256Digest) -> Path:
    return models_dir / "blobs" / f"sha256-{digest.hex}"


def read_installed_model(models_dir: Path, tag: str) -> InstalledModel:
    """Read the manifest for `tag`. Exactly one model layer is required; no adapter is supported."""
    path = _manifest_path(models_dir, tag)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ArtifactBindingError(f"{tag} is not installed") from None
    except (OSError, ValueError) as exc:
        raise ArtifactBindingError(f"unreadable manifest for {tag}: {exc}") from None
    layers = manifest.get("layers") if isinstance(manifest, dict) else None
    if not isinstance(layers, list):
        raise ArtifactBindingError(f"manifest for {tag} has no layers")

    def of(media_type: str) -> list[dict[str, object]]:
        return [x for x in layers if isinstance(x, dict) and x.get("mediaType") == media_type]

    models, templates = of(MODEL_LAYER), of(TEMPLATE_LAYER)
    if len(models) != 1:
        raise ArtifactBindingError(f"{tag}: expected one model layer, found {len(models)}")
    if len(templates) > 1:
        raise ArtifactBindingError(f"{tag}: more than one template layer")
    try:
        model_digest = Sha256Digest(str(models[0]["digest"]))
        template_digest = Sha256Digest(str(templates[0]["digest"])) if templates else None
        size = models[0]["size"]
    except (KeyError, ValueError) as exc:
        raise ArtifactBindingError(f"{tag}: malformed layer: {exc}") from None
    if type(size) is not int:
        raise ArtifactBindingError(f"{tag}: model layer size is not an integer")
    return InstalledModel(
        tag=tag,
        manifest_path=path,
        model_digest=model_digest,
        model_path=_blob_path(models_dir, model_digest),
        model_size=size,
        template_digest=template_digest,
        template_path=None if template_digest is None else _blob_path(models_dir, template_digest),
        has_adapter=bool(of(ADAPTER_LAYER)),
    )


# --- talking to the runtime ----------------------------------------------------------------------


class OllamaTransport(Protocol):
    """POST a JSON body to an Ollama API path and return the decoded JSON object."""

    def get(self, path: str) -> Mapping[str, object]: ...

    def post(self, path: str, body: Mapping[str, object]) -> Mapping[str, object]: ...


class LoopbackOllamaTransport:
    """HTTP to an Ollama server on a loopback address only. Maps transport failures to the
    provider boundary's typed errors; anything unexpected propagates."""

    def __init__(self, base_url: str = DEFAULT_BASE_URL, timeout_seconds: float = 600) -> None:
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme != "http" or parsed.hostname not in LOOPBACK_HOSTS:
            raise ValueError(f"the local provider only talks to loopback http, not {base_url!r}")
        self._base = base_url.rstrip("/")
        self._timeout = timeout_seconds

    def get(self, path: str) -> Mapping[str, object]:
        return self._send(urllib.request.Request(self._base + path, method="GET"))

    def post(self, path: str, body: Mapping[str, object]) -> Mapping[str, object]:
        data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self._base + path,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        return self._send(request)

    def _send(self, request: urllib.request.Request) -> Mapping[str, object]:
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise ProviderUnavailableError(
                    f"runtime reports {request.full_url} not found"
                ) from None
            raise ProviderExecutionError(f"runtime returned HTTP {exc.code}") from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise ProviderExecutionError("runtime timed out") from None
            raise ProviderUnavailableError(f"runtime unreachable: {exc.reason}") from None
        except TimeoutError:
            raise ProviderExecutionError("runtime timed out") from None
        try:
            decoded = json.loads(raw)
        except ValueError:
            raise ProviderExecutionError("runtime returned a body that is not JSON") from None
        if not isinstance(decoded, dict):
            raise ProviderExecutionError("runtime returned JSON that is not an object")
        return decoded


# --- the execution profile, from what was observed -----------------------------------------------


@dataclass(frozen=True)
class SamplingConfig:
    """What the provider sends the runtime. temperature_milli 0 is greedy decoding."""

    temperature_milli: int = 0
    seed: int = 42
    context_tokens: int = 8192


DEFAULT_SAMPLING = SamplingConfig()


@dataclass(frozen=True)
class BoundArtifact:
    """The result of binding: the installation, its observed metadata, and the profile."""

    installed: InstalledModel
    runtime_version: str
    file_type: int
    tensor_types: frozenset[str]
    runtime_template_sha256: Sha256Digest | None
    sampling: SamplingConfig
    profile: ExecutionProfile


def _require_str(value: object, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise ArtifactBindingError(f"runtime metadata: {what} missing")
    return value


def asserted_representation(
    file_type: int, tensor_types: frozenset[str]
) -> tuple[str | None, str | None]:
    """(precision, quantization) asserted from the file's own GGUF metadata, never from a tag.

    Only two cases are stated, each from general.file_type AND the type of every tensor:
    - "mostly F16" (1), every tensor F16 or F32: precision F16, quantization none.
    - "mostly Q8_0" (7), every tensor Q8_0 or F32, at least one Q8_0: quantization Q8_0. Precision
      is not asserted: Q8_0 stores 8-bit blocks with per-block scales, which no single precision
      label describes.
    Anything else is NOT ASSERTED (null, null).
    """
    if file_type == GGUF_FILE_TYPE_MOSTLY_F16 and tensor_types <= UNQUANTIZED_TENSOR_TYPES:
        return "F16", "none"
    if (
        file_type == GGUF_FILE_TYPE_MOSTLY_Q8_0
        and tensor_types <= Q8_0_TENSOR_TYPES
        and "Q8_0" in tensor_types
    ):
        return None, "Q8_0"
    return None, None


def bind_local_artifact(
    tag: str,
    *,
    transport: OllamaTransport,
    models_dir: Path | None = None,
    sampling: SamplingConfig = DEFAULT_SAMPLING,
) -> BoundArtifact:
    """Hash the installed files, check them against the manifest, and build the profile from
    observed metadata. Refuses (ArtifactBindingError) anything it cannot state truthfully."""
    installed = read_installed_model(models_dir or default_models_dir(), tag)
    if installed.has_adapter:
        raise ArtifactBindingError(f"{tag} has an adapter layer; adapters are not supported")
    if installed.model_path.stat().st_size != installed.model_size:
        raise ArtifactBindingError(f"{tag}: model file size differs from the manifest")
    if sha256_file(installed.model_path) != installed.model_digest:
        raise ArtifactBindingError(f"{tag}: model file bytes do not match the manifest digest")
    if installed.template_path is not None and installed.template_digest is not None:
        if sha256_file(installed.template_path) != installed.template_digest:
            raise ArtifactBindingError(f"{tag}: template bytes do not match the manifest digest")

    version = _require_str(transport.get("/api/version").get("version"), "version")
    shown = transport.post("/api/show", {"model": tag, "verbose": True})
    model_info = shown.get("model_info")
    tensors = shown.get("tensors")
    if not isinstance(model_info, dict) or not isinstance(tensors, list) or not tensors:
        raise ArtifactBindingError("runtime metadata: model_info or tensors missing")
    file_type = model_info.get("general.file_type")
    if type(file_type) is not int:
        raise ArtifactBindingError("runtime metadata: general.file_type missing")
    tensor_types = frozenset(
        _require_str(t.get("type") if isinstance(t, dict) else None, "tensor type") for t in tensors
    )

    # Which chat template the runtime renders with is the runtime's choice: a GGUF file may embed
    # its own template, and Ollama may use it instead of the manifest's template file. The
    # template file's digest is asserted only when the template the runtime reports (/api/show
    # `template`) is byte-for-byte that file. Otherwise the template in use is not separately
    # bound, and chat_template_digest is NOT ASSERTED (null).
    reported = shown.get("template")
    runtime_template = (
        Sha256Digest.from_hex(hashlib.sha256(reported.encode("utf-8")).hexdigest())
        if isinstance(reported, str) and reported
        else None
    )
    template_in_use = (
        installed.template_digest
        if installed.template_digest is not None and runtime_template == installed.template_digest
        else None
    )

    precision, quantization = asserted_representation(file_type, tensor_types)
    profile = ExecutionProfile(
        model_identifier=f"ollama:{tag}",
        model_artifact_digest=installed.model_digest,
        tokenizer_digest=None,
        adapter_digest=None,
        chat_template_digest=template_in_use,
        runtime=f"ollama {version}",
        precision=precision,
        quantization=quantization,
        context_min_tokens=sampling.context_tokens,
        sampling=SamplingPolicy(
            temperature_milli=sampling.temperature_milli,
            seed_policy=SeedPolicy.FIXED,
            seed=sampling.seed,
        ),
    )
    return BoundArtifact(
        installed, version, file_type, tensor_types, runtime_template, sampling, profile
    )


# --- the provider --------------------------------------------------------------------------------


def _count(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ProviderExecutionError(f"runtime response: {field} is not a non-negative integer")
    return value


class LocalLlamaProvider:
    """Tier 2 local provider. Serves exactly the bound artifact; signs every attestation."""

    attestation_capability = AttestationCapability.SIGNS_ATTESTATION

    def __init__(
        self,
        bound: BoundArtifact,
        *,
        transport: OllamaTransport,
        signing_key: SigningKey,
        models_dir: Path | None = None,
        provider_id: ProviderId = LOCAL_PROVIDER_ID,
    ) -> None:
        self._bound = bound
        self._transport = transport
        self._signing_key = signing_key
        self._models_dir = models_dir or default_models_dir()
        self._provider_id = provider_id
        self.last_timing: Mapping[str, int] = {}

    @property
    def provider_id(self) -> ProviderId:
        return self._provider_id

    @property
    def profile(self) -> ExecutionProfile:
        return self._bound.profile

    def execute(self, request: ProviderRequest) -> ProviderResult:
        tag = self._bound.installed.tag
        try:
            current = read_installed_model(self._models_dir, tag)
        except ArtifactBindingError as exc:
            raise ProviderUnavailableError(f"bound artifact unavailable: {exc}") from None
        if current.model_digest != self._bound.installed.model_digest:
            raise ProviderUnavailableError(f"{tag} now names a different model file than was bound")
        if current.template_digest != self._bound.installed.template_digest:
            raise ProviderUnavailableError(f"{tag} now names a different template than was bound")
        try:
            prompt = request.prompt.decode("utf-8")
        except UnicodeDecodeError:
            raise ProviderExecutionError("prompt is not UTF-8") from None

        sampling = self._bound.sampling
        reply = self._transport.post(
            "/api/chat",
            {
                "model": tag,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "options": {
                    "temperature": sampling.temperature_milli / 1000,
                    "seed": sampling.seed,
                    "num_ctx": sampling.context_tokens,
                    "num_predict": request.quote.max_output_tokens,
                },
            },
        )
        if reply.get("model") != tag or reply.get("done") is not True:
            raise ProviderExecutionError("runtime response names another model or is incomplete")
        message = reply.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise ProviderExecutionError("runtime response has no message content")
        try:
            response = content.encode("utf-8")
        except UnicodeEncodeError:
            raise ProviderExecutionError("runtime response is not valid Unicode") from None
        input_tokens = _count(reply.get("prompt_eval_count"), "prompt_eval_count")
        output_tokens = _count(reply.get("eval_count"), "eval_count")
        self.last_timing = {
            k: v
            for k in ("total_duration", "load_duration", "prompt_eval_duration", "eval_duration")
            if type(v := reply.get(k)) is int
        }

        body = ProviderAttestationBody(
            purchase_request_digest=request.purchase_request.digest(),
            execution_profile_digest=self._bound.profile.digest(),
            response_digest=payload_digest(response),
            input_token_count=input_tokens,
            output_token_count=output_tokens,
        )
        return ProviderResult(
            provider_id=self._provider_id,
            execution_profile_digest=body.execution_profile_digest,
            response=response,
            input_token_count=input_tokens,
            output_token_count=output_tokens,
            attestation=ProviderAttestation(body=body, signature=sign(body, self._signing_key)),
        )
