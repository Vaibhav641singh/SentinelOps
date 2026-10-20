"""Measure dense vs BM25-sparse vs fused retrieval before committing to hybrid search.

    python scripts/measure_hybrid.py

Answers one question: does lexical matching improve chunk selection on this corpus
enough to justify adding `fastembed` (~200MB of onnxruntime) and rebuilding the Cloud
collection?

READ-ONLY against the live Qdrant. Sparse vectors cannot be added to an existing
collection in place, so rather than mutate production this script reads the stored
dense vectors out and rebuilds them in an ephemeral in-memory collection alongside
locally-computed BM25 vectors. Nothing is written to Cloud.

The BM25 encoder here is deliberately throwaway: it emits the term-frequency component
and lets Qdrant apply IDF via Modifier.IDF, which is exactly how fastembed's BM25
works. So these numbers are a fair proxy for the real implementation without taking
the dependency to find out.
"""

import argparse
import asyncio
import json
import logging
import math
import re
import sys
import zlib
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from qdrant_client import AsyncQdrantClient, models  # noqa: E402

from copilot.clients import gemini_client, qdrant_client  # noqa: E402
from copilot.config import COLLECTION, MIN_MATCH_SCORE  # noqa: E402
from copilot.embeddings import QUERY, embed_async  # noqa: E402

log = logging.getLogger("measure_hybrid")

SPARSE = "bm25"
K1 = 1.5
B = 0.75

# Keep underscores and dots. This is the whole thesis: `wal_keep_segments`,
# `recovery.conf` and `pg_resetxlog` have to survive tokenization as single terms,
# because exact-token recall is the only thing BM25 offers that dense embedding does not.
TOKEN = re.compile(r"[a-z0-9_.]+")

# Probe queries that stress lexical-vs-semantic retrieval, with the chunk each should
# return. The corpus is 4 chunks, so the sample logs alone cannot show a ranking
# difference; these can. Expected `None` means "nothing covers this, expect the gate".
# Grounded in runbooks/database_guide.txt: SECTION 1 holds max_connections,
# password_encryption (L60) and wal_keep_segments (L65, L71); SECTION 2 holds
# "No space left" and VACUUM FULL; SECTION 3 holds recovery.conf (L128).
PROBES: list[tuple[str, int | None]] = [
    ("wal_keep_segments unknown configuration parameter", 1),
    ("SHOW wal_keep_segments returns an error", 1),
    ("password_encryption md5 no longer accepted", 1),
    ("FATAL sorry too many clients already", 1),
    ("max_connections reached postmaster refuses connections", 1),
    ("could not extend file No space left on device", 2),
    ("VACUUM FULL exclusive lock needs free space", 2),
    ("recovery.conf not found on standby", 3),
    ("promote standby replica to primary", 3),
    ("nginx SSL certificate expired PEM routines", None),
    ("kafka consumer group rebalancing timeout", None),
    ("redis OOM command not allowed over maxmemory", None),
]

SAMPLE_EXPECTED: dict[str, int | None] = {
    "connections.log": 1,
    "disk_full.log": 2,
    "nginx_ssl.log": None,
    # No correct answer. SECTION 3 is planned replica promotion when the primary is
    # unreachable; this log is a single server that crashed and will not restart, and
    # the runbook never mentions pg_resetxlog. The library does not cover this incident,
    # so the right behaviour is the gate - which it currently clears at 0.66.
    "pg_crash.log": None,
}


def tokenize(text: str, subtokens: bool = False) -> list[str]:
    terms = TOKEN.findall(text.lower())
    if not subtokens:
        return terms
    out = list(terms)
    for term in terms:
        parts = [p for p in re.split(r"[_.]", term) if p]
        if len(parts) > 1:
            out.extend(parts)
    return out


def term_id(token: str) -> int:
    # crc32 rather than Python's salted hash, so ids are stable across runs.
    # Collisions are possible in principle and irrelevant at four documents.
    return zlib.crc32(token.encode()) & 0x7FFFFFFF


class Bm25:
    """BM25 term-frequency weights. IDF is applied by Qdrant, not here."""

    def __init__(self, corpus: list[str], subtokens: bool = False) -> None:
        self.subtokens = subtokens
        lengths = [len(tokenize(doc, subtokens)) for doc in corpus]
        self.avg_len = sum(lengths) / len(lengths) if lengths else 0.0

    def document(self, text: str) -> models.SparseVector:
        tokens = tokenize(text, self.subtokens)
        counts = Counter(tokens)
        length = len(tokens)
        indices, values = [], []
        for token, freq in counts.items():
            norm = 1 - B + B * (length / self.avg_len if self.avg_len else 1.0)
            indices.append(term_id(token))
            values.append(freq * (K1 + 1) / (freq + K1 * norm))
        return models.SparseVector(indices=indices, values=values)

    def query(self, text: str) -> models.SparseVector:
        # Query side is presence-weighted; IDF does the discriminating.
        tokens = set(tokenize(text, self.subtokens))
        return models.SparseVector(
            indices=[term_id(t) for t in tokens], values=[1.0] * len(tokens)
        )


async def load_points(subtokens: bool) -> tuple[list[dict], Bm25]:
    """Read the live collection's dense vectors and payloads. Read-only."""
    client = qdrant_client()
    try:
        if not client.collection_exists(COLLECTION):
            raise SystemExit(f"Collection '{COLLECTION}' is missing. Run: python -m copilot.ingest")
        records, _ = client.scroll(
            COLLECTION, limit=1000, with_payload=True, with_vectors=True
        )
    finally:
        client.close()

    points = []
    for record in records:
        vector = record.vector
        if isinstance(vector, dict):  # named-vector collections hand back a mapping
            vector = vector.get("") or next(iter(vector.values()))
        payload = record.payload or {}
        points.append(
            {
                "index": payload.get("chunk_index", 0),
                "text": payload["text"],
                "label": payload["text"].splitlines()[0][:46],
                "dense": vector,
            }
        )
    points.sort(key=lambda p: p["index"])
    return points, Bm25([p["text"] for p in points], subtokens)


async def build_memory_index(points: list[dict], bm25: Bm25) -> AsyncQdrantClient:
    """Rebuild the corpus in-memory with both dense and sparse vectors."""
    client = AsyncQdrantClient(":memory:")
    await client.create_collection(
        COLLECTION,
        vectors_config=models.VectorParams(
            size=len(points[0]["dense"]), distance=models.Distance.COSINE
        ),
        sparse_vectors_config={SPARSE: models.SparseVectorParams(modifier=models.Modifier.IDF)},
    )
    await client.upsert(
        COLLECTION,
        points=[
            models.PointStruct(
                id=p["index"],
                vector={"": p["dense"], SPARSE: bm25.document(p["text"])},
                payload={"label": p["label"]},
            )
            for p in points
        ],
    )
    return client


def margin(points) -> float:
    return points[0].score - points[1].score if len(points) > 1 else float("nan")


async def run_query(client: AsyncQdrantClient, dense, sparse, depth: int) -> dict:
    """Four retrieval strategies over the same query."""
    prefetch = [
        models.Prefetch(query=dense, using="", limit=depth),
        models.Prefetch(query=sparse, using=SPARSE, limit=depth),
    ]
    dense_hits, sparse_hits, rrf_hits, dbsf_hits = await asyncio.gather(
        client.query_points(COLLECTION, query=dense, using="", limit=depth),
        client.query_points(COLLECTION, query=sparse, using=SPARSE, limit=depth),
        client.query_points(
            COLLECTION, prefetch=prefetch,
            query=models.FusionQuery(fusion=models.Fusion.RRF), limit=depth,
        ),
        client.query_points(
            COLLECTION, prefetch=prefetch,
            query=models.FusionQuery(fusion=models.Fusion.DBSF), limit=depth,
        ),
    )

    out = {}
    for name, result in (
        ("dense", dense_hits), ("sparse", sparse_hits),
        ("rrf", rrf_hits), ("dbsf", dbsf_hits),
    ):
        pts = result.points
        out[name] = {
            "top": pts[0].id if pts else None,
            "score": pts[0].score if pts else float("nan"),
            "margin": margin(pts) if pts else float("nan"),
        }
    return out


def hit(result: dict, expected: int | None) -> str:
    if expected is None:
        return "-"
    return "yes" if result["top"] == expected else "NO"


def fmt(value: float) -> str:
    return "  n/a " if math.isnan(value) else f"{value:.4f}"


def report(rows: list[dict], title: str) -> None:
    print(f"\n### {title}\n")
    header = (
        f"| {'query':<52} | exp | dense top/score/margin  | sparse top/score/margin | "
        f"rrf | dbsf | hit d/s/r |"
    )
    print(header)
    print("|" + "-" * (len(header) - 2) + "|")
    for row in rows:
        r = row["results"]
        exp = "-" if row["expected"] is None else str(row["expected"])
        print(
            f"| {row['query'][:52]:<52} | {exp:^3} "
            f"| {r['dense']['top']} {fmt(r['dense']['score'])} {fmt(r['dense']['margin'])} "
            f"| {r['sparse']['top']} {fmt(r['sparse']['score'])} {fmt(r['sparse']['margin'])} "
            f"|  {r['rrf']['top']}  |  {r['dbsf']['top']}   "
            f"|  {hit(r['dense'], row['expected'])}/{hit(r['sparse'], row['expected'])}/"
            f"{hit(r['rrf'], row['expected'])}  |"
        )


def summarize(rows: list[dict]) -> None:
    scored = [r for r in rows if r["expected"] is not None]
    offtopic = [r for r in rows if r["expected"] is None]

    print("\n### Summary\n")
    for name in ("dense", "sparse", "rrf", "dbsf"):
        hits = sum(1 for r in scored if r["results"][name]["top"] == r["expected"])
        margins = [
            r["results"][name]["margin"]
            for r in scored
            if not math.isnan(r["results"][name]["margin"])
        ]
        avg = sum(margins) / len(margins) if margins else float("nan")
        print(f"  {name:<7} hit@1 {hits}/{len(scored)}   mean top1-top2 margin {fmt(avg)}")

    changed = sum(1 for r in rows if r["results"]["rrf"]["top"] != r["results"]["dense"]["top"])
    print(f"\n  RRF picked a different chunk than dense on {changed}/{len(rows)} queries")

    print(f"\n### Relevance gate (dense cosine only, MIN_MATCH_SCORE={MIN_MATCH_SCORE})\n")
    on = [r["results"]["dense"]["score"] for r in scored]
    off = [r["results"]["dense"]["score"] for r in offtopic]
    if on:
        print(f"  on-topic  dense top1: min {min(on):.4f}  max {max(on):.4f}")
    if off:
        print(f"  off-topic dense top1: min {min(off):.4f}  max {max(off):.4f}")
    if on and off:
        gap = min(on) - max(off)
        print(f"  gap between the two populations: {gap:+.4f}")
        if gap <= 0:
            print("  WARNING: populations overlap - no single threshold separates them.")
    leaked = [r for r in offtopic if r["results"]["dense"]["score"] >= MIN_MATCH_SCORE]
    for r in leaked:
        print(f"  WARNING: off-topic query passes the gate: {r['query']!r}")


async def measure(subtokens: bool, depth: int) -> list[dict]:
    points, bm25 = await load_points(subtokens)
    log.info("read %d points from the live collection (read-only)", len(points))
    for p in points:
        log.info("  chunk %d: %s", p["index"], p["label"])

    samples = sorted((PROJECT_ROOT / "samples").glob("*.log"))
    queries = [(q, e, "probe") for q, e in PROBES]
    queries += [
        (s.read_text(encoding="utf-8"), SAMPLE_EXPECTED.get(s.name), f"sample:{s.name}")
        for s in samples
        if s.name in SAMPLE_EXPECTED
    ]

    gemini = gemini_client()
    log.info("embedding %d queries", len(queries))
    vectors = await embed_async(gemini, [q for q, _, _ in queries], QUERY)

    memory = await build_memory_index(points, bm25)
    try:
        rows = []
        for (text, expected, kind), dense in zip(queries, vectors):
            results = await run_query(memory, dense, bm25.query(text), depth)
            label = kind if kind.startswith("sample") else text
            rows.append({"query": label, "expected": expected, "kind": kind, "results": results})
        return rows
    finally:
        await memory.close()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--subtokens", action="store_true",
        help="also emit `_`/`.`-split sub-tokens (wal_keep_segments -> wal, keep, segments)",
    )
    parser.add_argument("--depth", type=int, default=4, help="retrieval depth (default: 4)")
    parser.add_argument("--json", dest="json_path", help="also write raw results here")
    args = parser.parse_args()

    rows = asyncio.run(measure(args.subtokens, args.depth))

    print(f"\n{'=' * 118}")
    print(f"HYBRID SEARCH MEASUREMENT  (subtokens={args.subtokens}, depth={args.depth})")
    print("=" * 118)
    report([r for r in rows if r["kind"] == "probe"], "Adversarial probe queries")
    report([r for r in rows if r["kind"].startswith("sample")], "Sample logs (regression baseline)")
    summarize(rows)

    if args.json_path:
        Path(args.json_path).write_text(json.dumps(rows, indent=2), encoding="utf-8")
        log.info("\nwrote %s", args.json_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
