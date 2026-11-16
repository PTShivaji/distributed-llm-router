"""Embedding providers and vector helpers."""

import asyncio
import math
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from app.embeddings.base import EmbeddingError, cosine_similarity, from_bytes, normalize, to_bytes
from app.embeddings.litellm_embedder import LiteLLMEmbedder
from app.embeddings.local import LocalHashingEmbedder

E = LocalHashingEmbedder(256)


def sim(a: str, b: str) -> float:
    return cosine_similarity(E.embed_sync(a), E.embed_sync(b))


def test_deterministic_across_instances():
    assert LocalHashingEmbedder(256).embed_sync("Explain Python variables") == E.embed_sync("Explain Python variables")


def test_vectors_are_normalized_and_sized():
    v = E.embed_sync("Explain Python variables")
    assert len(v) == 256
    assert math.isclose(sum(x * x for x in v), 1.0, rel_tol=1e-9)


def test_empty_text_gives_zero_vector():
    assert E.embed_sync("") == [0.0] * 256


def test_case_and_punctuation_insensitive():
    assert sim("Explain Python variables", "explain python variables!") == pytest.approx(1.0)


def test_similarity_ordering():
    base = "Explain Python variables"
    assert sim(base, "Explain Python variables please") > sim(base, "Explain JavaScript variables")
    assert sim(base, "Explain JavaScript variables") > sim(base, "What is the capital of France?")
    assert sim(base, "What is the capital of France?") < 0.2


def test_lexical_limitation_is_real():
    """Documented limitation: different meanings with shared words still score high."""
    assert sim("How do I reverse a list in Python?", "How do I reverse a string in Python?") > 0.7


def test_id_identifies_vector_space():
    assert LocalHashingEmbedder(128).id != LocalHashingEmbedder(256).id
    assert not E.is_semantic


def test_bytes_roundtrip():
    v = E.embed_sync("hello world")
    restored = from_bytes(to_bytes(v))
    assert len(restored) == 256
    assert cosine_similarity(v, restored) == pytest.approx(1.0, abs=1e-6)


def test_cosine_dimension_mismatch():
    with pytest.raises(ValueError):
        cosine_similarity([1.0], [1.0, 0.0])


def test_normalize_zero_vector():
    assert normalize([0.0, 0.0]) == [0.0, 0.0]


def test_local_embed_async():
    assert asyncio.run(E.embed("hi")) == E.embed_sync("hi")


# --- LiteLLM embedder (fake function; no network) -----------------------------
def test_litellm_embedder_normalizes_and_passes_kwargs():
    seen = {}

    async def fn(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(data=[SimpleNamespace(embedding=[3.0, 4.0])])

    e = LiteLLMEmbedder("openai/text-embedding-3-small", api_key=SecretStr("sk-test-abcdefghijkl"), embedding_fn=fn)
    v = asyncio.run(e.embed("hello"))
    assert v == pytest.approx([0.6, 0.8])
    assert e.dimensions == 2 and e.is_semantic and e.id == "litellm:openai/text-embedding-3-small"
    assert seen["input"] == ["hello"] and seen["api_key"] == "sk-test-abcdefghijkl"


def test_litellm_embedder_dict_response():
    async def fn(**_):
        return {"data": [{"embedding": [1.0, 0.0]}]}

    assert asyncio.run(LiteLLMEmbedder("m", embedding_fn=fn).embed("x")) == [1.0, 0.0]


def test_litellm_embedder_error_is_redacted():
    secret = "sk-live-zzzzzzzzzzzzzzzz"

    async def fn(**_):
        raise RuntimeError(f"bad key {secret}")

    with pytest.raises(EmbeddingError) as info:
        asyncio.run(LiteLLMEmbedder("m", api_key=SecretStr(secret), embedding_fn=fn).embed("x"))
    assert secret not in str(info.value)


def test_litellm_embedder_requires_model():
    with pytest.raises(ValueError):
        LiteLLMEmbedder("")
