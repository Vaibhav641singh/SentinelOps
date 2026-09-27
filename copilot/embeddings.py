"""Embedding calls and the normalization they require.

Task types are asymmetric on purpose: documents are embedded as
RETRIEVAL_DOCUMENT at ingest, queries as RETRIEVAL_QUERY at search time.
Mismatching them measurably degrades retrieval.
"""

import math

from google import genai
from google.genai import types

from .config import EMBED_DIM, EMBED_MODEL
from .errors import UpstreamRejected

DOCUMENT = "RETRIEVAL_DOCUMENT"
QUERY = "RETRIEVAL_QUERY"


def l2_normalize(vector: list[float]) -> list[float]:
    """Rescale to unit length.

    gemini-embedding-001 only returns a unit vector at its native 3072 dims.
    Truncated output must be renormalized or cosine scores are silently wrong —
    no error, just quietly meaningless rankings.
    """
    norm = math.sqrt(sum(v * v for v in vector))
    return [v / norm for v in vector] if norm else vector


def _config(task_type: str) -> types.EmbedContentConfig:
    return types.EmbedContentConfig(task_type=task_type, output_dimensionality=EMBED_DIM)


def _read(response: types.EmbedContentResponse) -> list[list[float]]:
    if not response.embeddings:
        raise UpstreamRejected("Gemini returned no embeddings")
    return [l2_normalize(e.values or []) for e in response.embeddings]


def embed_sync(client: genai.Client, texts: list[str], task_type: str) -> list[list[float]]:
    return _read(
        client.models.embed_content(
            model=EMBED_MODEL, contents=texts, config=_config(task_type)
        )
    )


async def embed_async(client: genai.Client, texts: list[str], task_type: str) -> list[list[float]]:
    return _read(
        await client.aio.models.embed_content(
            model=EMBED_MODEL, contents=texts, config=_config(task_type)
        )
    )
