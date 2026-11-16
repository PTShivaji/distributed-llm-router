"""Deterministic local embedding: feature hashing of words, bigrams and char trigrams.

HONEST SCOPE: this is a *lexical* embedding. Queries that share words / word
fragments get high cosine similarity ("Explain Python variables" vs "please
explain python variables"), but it has no notion of meaning ("car" vs
"automobile" score low, and negations can score high). It is real vector
similarity search, suitable for development, tests and catching near-duplicate
queries. For meaning-aware matching configure EMBEDDING_PROVIDER=litellm.

Deterministic across processes and machines (blake2b, not Python's salted hash()).
"""

import hashlib
import re

from app.embeddings.base import EmbeddingProvider, normalize

_WORD = re.compile(r"[a-z0-9]+(?:[+#][a-z0-9+#]*)?")

WORD_WEIGHT = 1.0
BIGRAM_WEIGHT = 0.6
TRIGRAM_WEIGHT = 0.35


def _bucket(feature: str, dims: int) -> tuple[int, float]:
    digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
    value = int.from_bytes(digest, "big")
    return value % dims, (1.0 if (value >> 63) & 1 else -1.0)  # signed hashing reduces collision bias


class LocalHashingEmbedder(EmbeddingProvider):
    is_semantic = False

    def __init__(self, dimensions: int = 256) -> None:
        self.dimensions = dimensions
        self.id = f"local-hashing-v1-{dimensions}"

    def embed_sync(self, text: str) -> list[float]:
        words = _WORD.findall(text.lower())
        vec = [0.0] * self.dimensions

        def add(feature: str, weight: float) -> None:
            index, sign = _bucket(feature, self.dimensions)
            vec[index] += sign * weight

        for word in words:
            add("w:" + word, WORD_WEIGHT)
            padded = f"<{word}>"
            for i in range(len(padded) - 2):
                add("c:" + padded[i : i + 3], TRIGRAM_WEIGHT)
        for first, second in zip(words, words[1:]):
            add(f"b:{first} {second}", BIGRAM_WEIGHT)
        return normalize(vec)

    async def embed(self, text: str) -> list[float]:
        return self.embed_sync(text)  # CPU-only and fast (~0.1 ms for a short query)
