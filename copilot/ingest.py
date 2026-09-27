"""Load runbooks into Qdrant.

    python -m copilot.ingest

Kept separate from the service on purpose: the app only ever READS from Qdrant,
so a restart never re-embeds documents or burns API quota. Run this once, and
again whenever the runbooks change.
"""

import logging
import sys
import uuid

from qdrant_client import models

from .chunking import chunk_runbook
from .clients import gemini_client, qdrant_client
from .config import COLLECTION, EMBED_DIM, RUNBOOK_DIR
from .embeddings import DOCUMENT, embed_sync

log = logging.getLogger("copilot.ingest")

# Fixed namespace so re-running produces identical point IDs instead of duplicates.
NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")


def ensure_collection(client) -> None:
    if client.collection_exists(COLLECTION):
        existing = client.get_collection(COLLECTION).config.params.vectors.size
        if existing == EMBED_DIM:
            return
        log.info("collection dim %d != %d, recreating", existing, EMBED_DIM)
        client.delete_collection(COLLECTION)

    client.create_collection(
        COLLECTION,
        vectors_config=models.VectorParams(size=EMBED_DIM, distance=models.Distance.COSINE),
    )
    log.info("created collection %r (%d dims, cosine)", COLLECTION, EMBED_DIM)


def ensure_source_index(client) -> None:
    """Payload index on `source`, required to delete a file's old points by filter.

    Embedded Qdrant filters without one; a Qdrant server rejects the filter with
    400 unless the index exists. Creating it keeps local and cloud identical —
    this is the one difference that passes every local test and fails on Cloud.
    """
    try:
        client.create_payload_index(
            COLLECTION, field_name="source", field_schema=models.PayloadSchemaType.KEYWORD
        )
    except Exception:
        pass  # already present


def ingest_file(client, gemini, path) -> int:
    chunks = chunk_runbook(path.read_text(encoding="utf-8"))
    vectors = embed_sync(gemini, chunks, DOCUMENT)

    # Clear this file's previous points so a shrinking runbook leaves no orphans.
    client.delete(
        COLLECTION,
        points_selector=models.Filter(
            must=[models.FieldCondition(key="source", match=models.MatchValue(value=path.name))]
        ),
    )
    client.upsert(
        COLLECTION,
        points=[
            models.PointStruct(
                id=str(uuid.uuid5(NAMESPACE, f"{path.name}:{i}")),
                vector=vector,
                payload={"source": path.name, "chunk_index": i, "text": chunk},
            )
            for i, (chunk, vector) in enumerate(zip(chunks, vectors))
        ],
    )
    return len(chunks)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    files = sorted(RUNBOOK_DIR.glob("*.txt"))
    if not files:
        log.error("No .txt runbooks found in %s", RUNBOOK_DIR)
        return 1

    gemini = gemini_client()
    client = qdrant_client()
    try:
        ensure_collection(client)
        ensure_source_index(client)

        total = 0
        for path in files:
            count = ingest_file(client, gemini, path)
            log.info("  %s: %d chunks", path.name, count)
            total += count

        log.info("Done. %d chunks, %d points in collection.", total, client.count(COLLECTION).count)
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
