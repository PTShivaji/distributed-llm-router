"""Embedding provider abstraction + vector helpers (pure Python, no NumPy)."""

import math
from abc import ABC, abstractmethod
from array import array


class EmbeddingError(Exception):
    """Embedding generation failed. Message must be safe to log.

    `kind` (a ProviderErrorKind value, when known) lets the budget decide whether the
    failed call may have been billed. None = unknown -> treated as possibly billed.
    """

    def __init__(self, message: str = "", *, kind: str | None = None) -> None:
        super().__init__(message)
        self.kind = kind


class EmbeddingBudgetRefused(EmbeddingError):
    """Step 8A: the spend safeguards refused a paid embedding call (it was NOT made).

    The cache treats this as "skip approximate search": exact-match caching continues.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"embedding refused by budget: {reason}")
        self.reason = reason


class EmbeddingProvider(ABC):
    """Turns text into an L2-normalised vector.

    `id` identifies the vector space (provider + model + dimensions). Vectors are
    only ever compared within the same id, so switching providers can never
    compare incompatible vectors.
    """

    id: str
    dimensions: int
    is_semantic: bool  # True only for learned (meaning-aware) embedding models

    @abstractmethod
    async def embed(self, text: str) -> list[float]: ...

    async def embed_with_usage(self, text: str) -> tuple[list[float], int | None]:
        """Vector plus provider-reported input tokens (None = not reported / not applicable)."""
        return await self.embed(text), None


def normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector))
    return [x / norm for x in vector] if norm > 0 else vector


def cosine_similarity(a: list[float] | array, b: list[float] | array) -> float:
    """Cosine similarity. Both inputs are normalised already, so this is a dot product.

    math.sumprod (Python 3.12+) computes the dot product in C with extended
    precision; measured ~4x faster than the previous pure-Python generator
    (Phase 5 step 6B) with identical results to float rounding.
    """
    if len(a) != len(b):
        raise ValueError("vectors have different dimensions")
    return math.sumprod(a, b)


def to_bytes(vector: list[float]) -> bytes:
    return array("f", vector).tobytes()  # float32: 4 bytes per dimension


def from_bytes(data: bytes) -> array:
    vec = array("f")
    vec.frombytes(data)
    return vec
