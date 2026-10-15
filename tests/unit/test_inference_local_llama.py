"""The local Llama provider's binding and runtime boundary, without the model.

A synthetic Ollama model store (small fake blobs and a manifest) and a fake transport stand in for
the installation and the server. The real model runs in tests/integration/test_local_llama.py.
"""

from __future__ import annotations

import hashlib
import json
import socket
from collections.abc import Mapping
from pathlib import Path

import pytest
from inference_world import TIER2, make_purchase
from nacl.signing import SigningKey

from credence.core.signatures import KeyRole, verify_signed
from credence.inference.local_llama import (
    ADAPTER_LAYER,
    MODEL_LAYER,
    OLLAMA_REGISTRY,
    TEMPLATE_LAYER,
    ArtifactBindingError,
    LocalLlamaProvider,
    LoopbackOllamaTransport,
    SamplingConfig,
    bind_local_artifact,
)
from credence.inference.provider import (
    ProviderExecutionError,
    ProviderRequest,
    ProviderUnavailableError,
)

TAG = "llama3.1:8b-instruct-fp16"
MODEL_BYTES = b"synthetic GGUF bytes for a unit test, not a model"
TEMPLATE_BYTES = b"{{ synthetic template }}"
KEY = SigningKey(
    hashlib.sha256(b"CREDENCE INFERENCE TEST KEY - NOT FOR PRODUCTION - local").digest()
)


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def write_store(
    root: Path,
    *,
    model: bytes = MODEL_BYTES,
    template: bytes | None = TEMPLATE_BYTES,
    extra_layers: list[dict[str, object]] | None = None,
    manifest_size: int | None = None,
) -> Path:
    blobs = root / "blobs"
    blobs.mkdir(parents=True, exist_ok=True)
    layers: list[dict[str, object]] = []
    for data, media in ((model, MODEL_LAYER), (template, TEMPLATE_LAYER)):
        if data is None:
            continue
        digest = _digest(data)
        (blobs / digest.replace(":", "-")).write_bytes(data)
        layers.append({"mediaType": media, "digest": digest, "size": len(data)})
    if manifest_size is not None:
        layers[0]["size"] = manifest_size
    layers += extra_layers or []
    name, version = TAG.split(":")
    manifest = root / "manifests" / OLLAMA_REGISTRY / "library" / name / version
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"layers": layers}))
    return root


class FakeTransport:
    def __init__(
        self,
        *,
        file_type: int = 1,
        tensor_types: tuple[str, ...] = ("F16", "F32"),
        reply: Mapping[str, object] | None = None,
    ) -> None:
        self.file_type = file_type
        self.tensor_types = tensor_types
        self.reply = reply
        self.posts: list[tuple[str, Mapping[str, object]]] = []

    def get(self, path: str) -> Mapping[str, object]:
        assert path == "/api/version"
        return {"version": "0.0.0-test"}

    def post(self, path: str, body: Mapping[str, object]) -> Mapping[str, object]:
        self.posts.append((path, body))
        if path == "/api/show":
            return {
                "model_info": {"general.file_type": self.file_type},
                "tensors": [{"name": f"t{i}", "type": t} for i, t in enumerate(self.tensor_types)],
            }
        assert path == "/api/chat"
        if self.reply is not None:
            return self.reply
        return {
            "model": TAG,
            "done": True,
            "message": {"role": "assistant", "content": "Synthetic local reply."},
            "prompt_eval_count": 17,
            "eval_count": 5,
            "total_duration": 1000,
        }


def provider(tmp_path: Path, transport: FakeTransport | None = None) -> LocalLlamaProvider:
    store = write_store(tmp_path / "models")
    transport = transport or FakeTransport()
    bound = bind_local_artifact(TAG, transport=transport, models_dir=store)
    return LocalLlamaProvider(bound, transport=transport, signing_key=KEY, models_dir=store)


def request() -> ProviderRequest:
    purchase = make_purchase(TIER2)
    return ProviderRequest(
        purchase.request, purchase.execution_profile, purchase.quote, purchase.prompt
    )


# --- binding ---------------------------------------------------------------------------------------


def test_profile_is_built_from_observed_files_and_metadata(tmp_path: Path) -> None:
    profile = provider(tmp_path).profile
    assert profile.model_identifier == f"ollama:{TAG}"
    assert profile.model_artifact_digest is not None
    assert profile.model_artifact_digest.value == _digest(MODEL_BYTES)
    assert profile.chat_template_digest is not None
    assert profile.chat_template_digest.value == _digest(TEMPLATE_BYTES)
    assert profile.tokenizer_digest is None  # embedded in the GGUF; not asserted separately
    assert profile.adapter_digest is None
    assert profile.runtime == "ollama 0.0.0-test"
    assert (profile.precision, profile.quantization) == ("F16", "none")
    assert profile.sampling.temperature_milli == 0 and profile.sampling.seed == 42
    assert profile.context_min_tokens == 8192


@pytest.mark.parametrize(
    "file_type,tensors",
    [(7, ("Q8_0", "F32")), (1, ("F16", "Q4_K")), (0, ("F32",))],
    ids=["q8_0-file", "quantized-tensor", "not-mostly-f16"],
)
def test_precision_and_quantization_not_asserted_unless_exact(
    tmp_path: Path, file_type: int, tensors: tuple[str, ...]
) -> None:
    profile = provider(tmp_path, FakeTransport(file_type=file_type, tensor_types=tensors)).profile
    assert profile.precision is None and profile.quantization is None


def test_no_template_layer_means_template_not_asserted(tmp_path: Path) -> None:
    store = write_store(tmp_path / "models", template=None)
    bound = bind_local_artifact(TAG, transport=FakeTransport(), models_dir=store)
    assert bound.profile.chat_template_digest is None


def test_sampling_config_is_what_the_profile_states(tmp_path: Path) -> None:
    store = write_store(tmp_path / "models")
    config = SamplingConfig(temperature_milli=0, seed=7, context_tokens=4096)
    bound = bind_local_artifact(TAG, transport=FakeTransport(), models_dir=store, sampling=config)
    assert bound.profile.sampling.seed == 7 and bound.profile.context_min_tokens == 4096


def test_bind_refuses_bytes_that_do_not_match_the_manifest(tmp_path: Path) -> None:
    store = write_store(tmp_path / "models")
    (store / "blobs" / _digest(MODEL_BYTES).replace(":", "-")).write_bytes(
        b"synthetic GGUF bytes for a unit test, not a mode!"  # same size, different bytes
    )
    with pytest.raises(ArtifactBindingError, match="do not match"):
        bind_local_artifact(TAG, transport=FakeTransport(), models_dir=store)


def test_bind_refuses_a_changed_template(tmp_path: Path) -> None:
    store = write_store(tmp_path / "models")
    (store / "blobs" / _digest(TEMPLATE_BYTES).replace(":", "-")).write_bytes(b"other")
    with pytest.raises(ArtifactBindingError, match="template"):
        bind_local_artifact(TAG, transport=FakeTransport(), models_dir=store)


def test_bind_refuses_a_size_mismatch(tmp_path: Path) -> None:
    store = write_store(tmp_path / "models", manifest_size=len(MODEL_BYTES) + 1)
    with pytest.raises(ArtifactBindingError, match="size"):
        bind_local_artifact(TAG, transport=FakeTransport(), models_dir=store)


def test_bind_refuses_an_adapter(tmp_path: Path) -> None:
    adapter = {"mediaType": ADAPTER_LAYER, "digest": _digest(b"a"), "size": 1}
    store = write_store(tmp_path / "models", extra_layers=[adapter])
    with pytest.raises(ArtifactBindingError, match="adapter"):
        bind_local_artifact(TAG, transport=FakeTransport(), models_dir=store)


def test_bind_refuses_multiple_model_layers(tmp_path: Path) -> None:
    second = {"mediaType": MODEL_LAYER, "digest": _digest(b"b"), "size": 1}
    store = write_store(tmp_path / "models", extra_layers=[second])
    with pytest.raises(ArtifactBindingError, match="one model layer"):
        bind_local_artifact(TAG, transport=FakeTransport(), models_dir=store)


def test_bind_refuses_a_missing_tag(tmp_path: Path) -> None:
    with pytest.raises(ArtifactBindingError, match="not installed"):
        bind_local_artifact(TAG, transport=FakeTransport(), models_dir=tmp_path)


# --- execution -------------------------------------------------------------------------------------


def test_execute_sends_the_bound_configuration(tmp_path: Path) -> None:
    transport = FakeTransport()
    local = provider(tmp_path, transport)
    req = request()
    local.execute(req)
    path, body = transport.posts[-1]
    assert path == "/api/chat"
    assert body["model"] == TAG and body["stream"] is False
    assert body["messages"] == [{"role": "user", "content": req.prompt.decode()}]
    assert body["options"] == {
        "temperature": 0.0,
        "seed": 42,
        "num_ctx": 8192,
        "num_predict": req.quote.max_output_tokens,
    }


def test_execute_returns_runtime_counts_and_a_signed_attestation(tmp_path: Path) -> None:
    local = provider(tmp_path)
    req = request()
    result = local.execute(req)

    assert result.response == b"Synthetic local reply."
    assert (result.input_token_count, result.output_token_count) == (17, 5)
    assert result.execution_profile_digest == local.profile.digest()
    assert result.attestation is not None and result.attestation.signature is not None
    body = result.attestation.body
    assert body.purchase_request_digest == req.purchase_request.digest()
    assert body.execution_profile_digest == local.profile.digest()
    assert (body.input_token_count, body.output_token_count) == (17, 5)
    from credence.core.signatures import KeyRegistry, KeyStatus, RegisteredKey, key_id_for

    reg = KeyRegistry(
        [
            RegisteredKey(
                key_id_for(KEY.verify_key),
                KeyRole.PROVIDER,
                "local-llama-provider",
                KeyStatus.CURRENT,
            )
        ]
    )
    check = verify_signed(
        body,
        result.attestation.signature,
        reg,
        role=KeyRole.PROVIDER,
        subject="local-llama-provider",
    )
    assert check.accepted
    assert local.last_timing == {"total_duration": 1000}


def test_execute_refuses_when_the_tag_now_names_another_file(tmp_path: Path) -> None:
    local = provider(tmp_path)
    write_store(tmp_path / "models", model=b"a different synthetic GGUF file")
    with pytest.raises(ProviderUnavailableError, match="different model file"):
        local.execute(request())


@pytest.mark.parametrize(
    "reply",
    [
        {
            "model": "other:tag",
            "done": True,
            "message": {"content": "x"},
            "prompt_eval_count": 1,
            "eval_count": 1,
        },
        {
            "model": TAG,
            "done": False,
            "message": {"content": "x"},
            "prompt_eval_count": 1,
            "eval_count": 1,
        },
        {"model": TAG, "done": True, "message": {}, "prompt_eval_count": 1, "eval_count": 1},
        {"model": TAG, "done": True, "message": {"content": "x"}, "eval_count": 1},
        {
            "model": TAG,
            "done": True,
            "message": {"content": "x"},
            "prompt_eval_count": -1,
            "eval_count": 1,
        },
        {
            "model": TAG,
            "done": True,
            "message": {"content": "\ud800"},
            "prompt_eval_count": 1,
            "eval_count": 1,
        },
    ],
    ids=[
        "other-model",
        "not-done",
        "no-content",
        "no-input-count",
        "negative-count",
        "lone-surrogate",
    ],
)
def test_malformed_runtime_reply_is_an_execution_error(
    tmp_path: Path, reply: Mapping[str, object]
) -> None:
    local = provider(tmp_path, FakeTransport(reply=reply))
    with pytest.raises(ProviderExecutionError):
        local.execute(request())


# --- transport -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url", ["http://example.com:11434", "https://127.0.0.1:11434", "http://10.0.0.5:11434"]
)
def test_transport_refuses_non_loopback(url: str) -> None:
    with pytest.raises(ValueError, match="loopback"):
        LoopbackOllamaTransport(url)


def test_transport_maps_a_closed_port_to_unavailable() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    transport = LoopbackOllamaTransport(f"http://127.0.0.1:{port}", timeout_seconds=2)
    with pytest.raises(ProviderUnavailableError):
        transport.get("/api/version")
