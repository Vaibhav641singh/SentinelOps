"""Load runbooks into Qdrant.

    python -m copilot.ingest

Kept separate from the service on purpose: the app only ever READS from Qdrant,
so a restart never re-embeds documents or burns API quota. Run this once, and
again whenever the runbooks change.
"""

import logging
import re
import sys
import uuid

from qdrant_client import models

from .chunking import chunk_runbook
from .clients import gemini_client, qdrant_client
from .config import COLLECTION, EMBED_DIM, HYBRID_SEARCH, RUNBOOK_DIR, SPARSE_VECTOR
from .embeddings import DOCUMENT, embed_sync
from .sparse import average_length, encode_document

log = logging.getLogger("copilot.ingest")

# Fixed namespace so re-running produces identical point IDs instead of duplicates.
NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")

# Header fields the runbooks carry as a `key: value` block under the title.
# Whitelisted rather than accepting every `key: value` line, because older runbooks
# carry prose in the same shape — `Owner: Platform Team`, `Applies to: PostgreSQL
# 12.x` — which would otherwise land in the payload as fields nothing ever reads.
METADATA_KEYS = frozenset(
    {"service", "component", "severity", "version_context", "published_at", "last_verified"}
)
METADATA_LINE = re.compile(r"^([a-z_]+):[ \t]*(\S.*?)[ \t]*$")


def sparse_config() -> dict:
    """IDF modifier, so Qdrant weights rare terms itself.

    Without it a sparse search degrades to raw term-frequency matching, where a common
    word scores the same as the one distinctive parameter name in the query.
    """
    return (
        {SPARSE_VECTOR: models.SparseVectorParams(modifier=models.Modifier.IDF)}
        if HYBRID_SEARCH
        else {}
    )


def ensure_collection(client) -> None:
    if client.collection_exists(COLLECTION):
        params = client.get_collection(COLLECTION).config.params
        has_sparse = bool(params.sparse_vectors)
        # A sparse config cannot be added to a live collection, so a mismatch in
        # either direction means recreating it rather than quietly staying dense.
        if params.vectors.size == EMBED_DIM and has_sparse == HYBRID_SEARCH:
            return
        why = (
            f"dim {params.vectors.size} != {EMBED_DIM}"
            if params.vectors.size != EMBED_DIM
            else f"sparse={has_sparse}, wanted {HYBRID_SEARCH}"
        )
        log.info("recreating collection (%s)", why)
        client.delete_collection(COLLECTION)

    client.create_collection(
        COLLECTION,
        vectors_config=models.VectorParams(size=EMBED_DIM, distance=models.Distance.COSINE),
        sparse_vectors_config=sparse_config(),
    )
    log.info(
        "created collection %r (%d dims, cosine%s)",
        COLLECTION,
        EMBED_DIM,
        f" + {SPARSE_VECTOR} sparse" if HYBRID_SEARCH else "",
    )


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


def parse_metadata(text: str) -> dict:
    """Pull the header's `key: value` block out of a runbook as payload fields.

    Stops at the first section heading, because below it the same shape means
    something else entirely: a Diagnosis step reads `state: 'idle in transaction'`
    and a Symptoms block quotes `Reason: OOMKilled`. Neither is metadata.

    Returned for filtering and display. `version_context` in particular also stays
    in the chunk text, since it is what the staleness audit reasons against.
    """
    meta: dict = {}
    for line in text.splitlines():
        if line.startswith(("##", "SECTION")):
            break
        match = METADATA_LINE.match(line)
        if match and match.group(1) in METADATA_KEYS:
            meta[match.group(1)] = match.group(2)
    return meta


def ingest_file(
    client, gemini, path, chunks: list[str], avg_len: float, meta: dict | None = None
) -> int:
    vectors = embed_sync(gemini, chunks, DOCUMENT)

    # Clear this file's previous points so a shrinking runbook leaves no orphans.
    client.delete(
        COLLECTION,
        points_selector=models.Filter(
            must=[models.FieldCondition(key="source", match=models.MatchValue(value=path.name))]
        ),
    )
    points = []
    for i, (chunk, vector) in enumerate(zip(chunks, vectors)):
        if HYBRID_SEARCH:
            indices, values = encode_document(chunk, avg_len)
            stored = {"": vector, SPARSE_VECTOR: models.SparseVector(
                indices=indices, values=values
            )}
        else:
            stored = vector
        points.append(
            models.PointStruct(
                id=str(uuid.uuid5(NAMESPACE, f"{path.name}:{i}")),
                vector=stored,
                # Metadata first, so a stray `source:` or `text:` line in a runbook
                # header can never displace the fields retrieval depends on.
                payload={**(meta or {}), "source": path.name, "chunk_index": i, "text": chunk},
            )
        )

    client.upsert(COLLECTION, points=points)
    return len(chunks)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    files = sorted(p for pattern in ("*.txt", "*.md") for p in RUNBOOK_DIR.glob(pattern))
    if not files:
        log.error("No .txt or .md runbooks found in %s", RUNBOOK_DIR)
        return 1

    # Chunk everything before embedding anything: BM25 length normalization needs the
    # mean chunk length across the whole corpus, not whichever file is in hand.
    #
    # utf-8-sig, so a byte-order mark never reaches the embedding or the BM25
    # tokenizer. A BOM is invisible in an editor and silently becomes part of the
    # first token, which is the kind of bug that costs an afternoon.
    per_file = {}
    for path in files:
        text = path.read_text(encoding="utf-8-sig")
        per_file[path] = (parse_metadata(text), chunk_runbook(text))
    avg_len = average_length([chunk for _, chunks in per_file.values() for chunk in chunks])

    gemini = gemini_client()
    client = qdrant_client()
    try:
        ensure_collection(client)
        ensure_source_index(client)

        total = 0
        for path, (meta, chunks) in per_file.items():
            count = ingest_file(client, gemini, path, chunks, avg_len, meta)
            log.info("  %s: %d chunks", path.name, count)
            total += count

        log.info("Done. %d chunks, %d points in collection.", total, client.count(COLLECTION).count)
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
