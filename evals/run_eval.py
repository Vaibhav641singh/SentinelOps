"""Retrieval evaluation against the golden set.

    python -m evals.run_eval                 # report
    python -m evals.run_eval --sweep         # threshold sweep
    python -m evals.run_eval --check         # CI gate, exits non-zero on regression
    python -m evals.run_eval --write-baseline
    python -m evals.run_eval --refresh       # re-embed and re-snapshot (costs quota)

Supersedes scripts/measure_hybrid.py, which could not survive a multi-file corpus:
it keyed in-memory points by bare `chunk_index`, so every file's chunk 0 collided
and silently overwrote the others, and its labels carried no `source` at all.

The default run is OFFLINE. Query vectors and a corpus snapshot are cached under
evals/cache/, so a threshold or prompt change is measured against byte-identical
vectors rather than a freshly rolled embedding — and CI needs no API key, no
network and no Qdrant. Only --refresh calls out.

WHAT THIS DOES NOT MEASURE: the pipeline's coverage decision is the floor AND the
judge's `covers_incident`. Only the floor is offline, so the gate numbers here are
the floor alone — an upper bound on refusals and a lower bound on false accepts.
"""

import argparse
import asyncio
import hashlib
import json
import logging
import math
import pathlib
import sys
from collections import defaultdict

import numpy as np
from qdrant_client import AsyncQdrantClient, models

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from copilot.clients import gemini_client, qdrant_client
from copilot.config import COLLECTION, MIN_MATCH_SCORE, SPARSE_VECTOR
from copilot.embeddings import QUERY, embed_async
from copilot.sparse import average_length, encode_document, encode_query

log = logging.getLogger("evals")

EVAL_DIR = pathlib.Path(__file__).resolve().parent
GOLDEN_SET = EVAL_DIR / "golden_set.jsonl"
CACHE_DIR = EVAL_DIR / "cache"
CORPUS_CACHE = CACHE_DIR / "corpus.npz"
QUERY_CACHE = CACHE_DIR / "queries.npz"
BASELINE = EVAL_DIR / "baseline.json"

# Matches pipeline.PREFETCH_DEPTH. Note that with a corpus this small the prefetch
# returns everything, so fusion is ranking the whole collection rather than two
# truncated candidate lists. The depth only starts to bite past ~20 chunks.
PREFETCH_DEPTH = 20
ARMS = ("dense", "bm25", "dbsf")
K_VALUES = (1, 3, 5)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# corpus and query vectors
# --------------------------------------------------------------------------


def snapshot_corpus() -> dict:
    """Read every point back out of the live collection.

    Document vectors are never re-embedded — they are already in Qdrant, so this
    costs nothing but a scroll.
    """
    client = qdrant_client()
    try:
        if not client.collection_exists(COLLECTION):
            sys.exit(f"Collection {COLLECTION!r} is missing. Run: python -m copilot.ingest")
        points, _ = client.scroll(
            COLLECTION, limit=10_000, with_payload=True, with_vectors=True
        )
    finally:
        client.close()

    corpus = []
    for point in points:
        vector = point.vector
        if isinstance(vector, dict):
            # Named vectors: take the dense one, discard the stored sparse — BM25 is
            # recomputed here so the eval exercises copilot.sparse, not a stale copy.
            vector = vector.get("") or next(iter(vector.values()))
        payload = point.payload or {}
        corpus.append(
            {
                "key": f"{payload['source']}#{payload['chunk_index']}",
                "source": payload["source"],
                "chunk_index": int(payload["chunk_index"]),
                "text": payload["text"],
                "dense": np.asarray(vector, dtype=np.float32),
            }
        )
    corpus.sort(key=lambda c: c["key"])
    return corpus


def save_corpus(corpus: list[dict]) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    np.savez_compressed(
        CORPUS_CACHE,
        keys=np.array([c["key"] for c in corpus]),
        texts=np.array([c["text"] for c in corpus]),
        vectors=np.stack([c["dense"] for c in corpus]),
    )


def load_corpus() -> list[dict]:
    data = np.load(CORPUS_CACHE, allow_pickle=False)
    corpus = []
    for key, text, vector in zip(data["keys"], data["texts"], data["vectors"]):
        source, _, index = str(key).rpartition("#")
        corpus.append(
            {
                "key": str(key),
                "source": source,
                "chunk_index": int(index),
                "text": str(text),
                "dense": vector.astype(np.float32),
            }
        )
    return corpus


async def query_vectors(queries: list[str], refresh: bool) -> dict:
    """Embed the golden set once, then serve from cache forever.

    This is what lets the eval run in CI. measure_hybrid.py re-embedded on every
    invocation, which is both a quota cost and a source of run-to-run drift.
    """
    cached: dict = {}
    if QUERY_CACHE.exists() and not refresh:
        data = np.load(QUERY_CACHE, allow_pickle=False)
        cached = {str(k): v.astype(np.float32) for k, v in zip(data["keys"], data["vectors"])}

    missing = [q for q in queries if digest(q) not in cached]
    if missing:
        if not refresh:
            log.warning("%d queries not in cache — embedding them now", len(missing))
        gemini = gemini_client()
        # One call per batch of 100; the API rejects very large batches.
        for start in range(0, len(missing), 100):
            batch = missing[start : start + 100]
            vectors = await embed_async(gemini, batch, QUERY)
            for text, vector in zip(batch, vectors):
                cached[digest(text)] = np.asarray(vector, dtype=np.float32)
        CACHE_DIR.mkdir(exist_ok=True)
        keys = sorted(cached)
        np.savez_compressed(
            QUERY_CACHE,
            keys=np.array(keys),
            vectors=np.stack([cached[k] for k in keys]),
        )
    return cached


# --------------------------------------------------------------------------
# retrieval
# --------------------------------------------------------------------------


async def build_index(corpus: list[dict]) -> AsyncQdrantClient:
    """In-memory mirror of the live collection.

    Point ids are the position in the sorted corpus, NOT chunk_index. That is the
    bug that made the old benchmark unusable on a multi-file corpus: eleven files
    all have a chunk 0, so keying on it dropped ten of them on upsert.
    """
    client = AsyncQdrantClient(":memory:")
    await client.create_collection(
        COLLECTION,
        vectors_config=models.VectorParams(
            size=len(corpus[0]["dense"]), distance=models.Distance.COSINE
        ),
        sparse_vectors_config={
            SPARSE_VECTOR: models.SparseVectorParams(modifier=models.Modifier.IDF)
        },
    )
    avg_len = average_length([c["text"] for c in corpus])
    points = []
    for i, chunk in enumerate(corpus):
        indices, values = encode_document(chunk["text"], avg_len)
        points.append(
            models.PointStruct(
                id=i,
                vector={
                    "": chunk["dense"].tolist(),
                    SPARSE_VECTOR: models.SparseVector(indices=indices, values=values),
                },
                payload={"key": chunk["key"]},
            )
        )
    await client.upsert(COLLECTION, points=points)

    stored = (await client.count(COLLECTION)).count
    assert stored == len(corpus), f"index holds {stored} points, corpus has {len(corpus)}"
    return client


async def retrieve_all(client, dense, sparse_vec, limit: int) -> dict:
    """Run the three arms over one query, concurrently."""
    prefetch = [
        models.Prefetch(query=dense, using="", limit=PREFETCH_DEPTH),
        models.Prefetch(query=sparse_vec, using=SPARSE_VECTOR, limit=PREFETCH_DEPTH),
    ]
    # Queried at PREFETCH_DEPTH, not at `limit`, because this result doubles as the
    # cosine lookup table for whatever chunk fusion picks — exactly as pipeline.py
    # does it. At limit=5 a fused winner outside dense's top 5 got no entry and
    # scored 0.0000, which silently dragged the off-topic floor to zero and made the
    # gate look better than it is.
    dense_q = client.query_points(
        COLLECTION, query=dense, limit=max(limit, PREFETCH_DEPTH), with_payload=True
    )
    bm25_q = client.query_points(
        COLLECTION, query=sparse_vec, using=SPARSE_VECTOR, limit=limit, with_payload=True
    )
    dbsf_q = client.query_points(
        COLLECTION,
        prefetch=prefetch,
        query=models.FusionQuery(fusion=models.Fusion.DBSF),
        limit=limit,
        with_payload=True,
    )
    dense_r, bm25_r, dbsf_r = await asyncio.gather(dense_q, bm25_q, dbsf_q)

    # The floor reads raw dense cosine of the chunk actually SERVED, never the fused
    # score — a fused score is rank-derived, so the top hit scores high by
    # construction however irrelevant it is. See pipeline.retrieve.
    cosine = {p.payload["key"]: p.score for p in dense_r.points}
    out = {}
    for arm, result in (("dense", dense_r), ("bm25", bm25_r), ("dbsf", dbsf_r)):
        # Truncate to `limit` here: the dense arm was over-fetched for the lookup
        # table above, and ranking metrics must score the same depth for every arm.
        ranked = [p.payload["key"] for p in result.points][:limit]
        top = ranked[0] if ranked else None
        out[arm] = {
            "ranked": ranked,
            "top": top,
            "gate_score": float(cosine.get(top, 0.0)) if top else 0.0,
        }
    return out


# --------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------


def success_at(ranked: list[str], relevant: set[str], k: int) -> float:
    """Did ANY correct chunk land in the top k.

    This, not true recall, is the headline: the pipeline serves exactly one chunk,
    so finding either acceptable answer for an ambiguous query is a full success.
    True recall would cap such a query at 0.5 and read as a failure when the
    pipeline did the right thing. Both are reported.
    """
    return 1.0 if relevant & set(ranked[:k]) else 0.0


def recall_at(ranked: list[str], relevant: set[str], k: int) -> float:
    return len(relevant & set(ranked[:k])) / len(relevant) if relevant else 0.0


def ndcg_at(ranked: list[str], relevant: set[str], k: int) -> float:
    dcg = sum(
        1.0 / math.log2(i + 2) for i, key in enumerate(ranked[:k]) if key in relevant
    )
    ideal = sum(1.0 / math.log2(i + 2) for i in range(min(len(relevant), k)))
    return dcg / ideal if ideal else 0.0


def reciprocal_rank(ranked: list[str], relevant: set[str]) -> float:
    for i, key in enumerate(ranked):
        if key in relevant:
            return 1.0 / (i + 1)
    return 0.0


def score_rows(rows: list[dict], arm: str) -> dict:
    positives = [r for r in rows if r["relevant"]]
    metrics = {}
    for k in K_VALUES:
        metrics[f"success@{k}"] = _mean(
            success_at(r["results"][arm]["ranked"], r["relevant"], k) for r in positives
        )
    metrics["recall@5"] = _mean(
        recall_at(r["results"][arm]["ranked"], r["relevant"], 5) for r in positives
    )
    metrics["ndcg@5"] = _mean(
        ndcg_at(r["results"][arm]["ranked"], r["relevant"], 5) for r in positives
    )
    metrics["mrr"] = _mean(
        reciprocal_rank(r["results"][arm]["ranked"], r["relevant"]) for r in positives
    )
    metrics["n"] = len(positives)
    return metrics


def gate_metrics(rows: list[dict], arm: str, threshold: float) -> dict:
    """How the score FLOOR alone behaves. The judge is not simulated here."""
    tp = fp = fn = tn = 0
    for row in rows:
        passes = row["results"][arm]["gate_score"] >= threshold
        if row["relevant"]:
            tp += passes
            fn += not passes
        else:
            fp += passes
            tn += not passes
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "false_refusals": fn,
        "false_accepts": fp,
    }


def _mean(values) -> float:
    values = list(values)
    return sum(values) / len(values) if values else 0.0


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------


def pct(value: float) -> str:
    return f"{value * 100:5.1f}"


def report(rows: list[dict], threshold: float) -> dict:
    summary = {"threshold": threshold, "arms": {}, "by_category": {}}

    print(f"\n{len(rows)} queries  "
          f"({sum(1 for r in rows if r['relevant'])} on-topic, "
          f"{sum(1 for r in rows if not r['relevant'])} negatives)\n")

    header = f"{'arm':<8}{'S@1':>7}{'S@3':>7}{'S@5':>7}{'R@5':>7}{'nDCG@5':>8}{'MRR':>7}"
    print(header)
    print("-" * len(header))
    for arm in ARMS:
        m = score_rows(rows, arm)
        summary["arms"][arm] = m
        print(f"{arm:<8}{pct(m['success@1']):>7}{pct(m['success@3']):>7}"
              f"{pct(m['success@5']):>7}{pct(m['recall@5']):>7}"
              f"{pct(m['ndcg@5']):>8}{pct(m['mrr']):>7}")

    print(f"\nscore floor at {threshold:.2f} (dense cosine of the chunk DBSF serves)")
    print(f"{'arm':<8}{'prec':>7}{'recall':>8}{'F1':>7}{'false refusals':>16}{'false accepts':>15}")
    for arm in ARMS:
        g = gate_metrics(rows, arm, threshold)
        summary["arms"][arm]["gate"] = g
        print(f"{arm:<8}{pct(g['precision']):>7}{pct(g['recall']):>8}{pct(g['f1']):>7}"
              f"{g['false_refusals']:>16}{g['false_accepts']:>15}")

    print("\nper category (DBSF)")
    print(f"{'category':<22}{'n':>4}{'S@1':>7}{'S@5':>7}{'nDCG@5':>8}")
    by_cat = defaultdict(list)
    for row in rows:
        by_cat[row["category"]].append(row)
    for cat in sorted(by_cat):
        subset = by_cat[cat]
        positives = [r for r in subset if r["relevant"]]
        if not positives:
            refused = sum(
                1 for r in subset if r["results"]["dbsf"]["gate_score"] < threshold
            )
            print(f"{cat:<22}{len(subset):>4}{'  (negatives) ':>22}"
                  f"{refused}/{len(subset)} refused")
            summary["by_category"][cat] = {
                "n": len(subset), "refused": refused, "negatives": True
            }
            continue
        m = score_rows(subset, "dbsf")
        summary["by_category"][cat] = m
        print(f"{cat:<22}{len(positives):>4}{pct(m['success@1']):>7}"
              f"{pct(m['success@5']):>7}{pct(m['ndcg@5']):>8}")

    on = [r["results"]["dbsf"]["gate_score"] for r in rows if r["relevant"]]
    off = [r["results"]["dbsf"]["gate_score"] for r in rows if not r["relevant"]]
    if on and off:
        gap = min(on) - max(off)
        print("\ndense top-1 cosine of the served chunk")
        print(f"  on-topic   {min(on):.4f} .. {max(on):.4f}")
        print(f"  off-topic  {min(off):.4f} .. {max(off):.4f}")
        summary["on_topic_min"] = min(on)
        summary["on_topic_max"] = max(on)
        summary["off_topic_min"] = min(off)
        summary["off_topic_max"] = max(off)
        summary["gap"] = gap
        if gap <= 0:
            print(f"  OVERLAP of {-gap:.4f} - no threshold separates these populations.")
        else:
            print(f"  separated by {gap:.4f}")

    misses = [
        r for r in rows
        if r["relevant"] and not success_at(r["results"]["dbsf"]["ranked"], r["relevant"], 1)
    ]
    if misses:
        print(f"\n{len(misses)} DBSF top-1 misses:")
        for row in misses:
            got = row["results"]["dbsf"]["top"]
            print(f"  {row['id']:<26} wanted {'|'.join(sorted(row['relevant'])):<46} got {got}")

    leaks = [
        r for r in rows
        if not r["relevant"] and r["results"]["dbsf"]["gate_score"] >= threshold
    ]
    if leaks:
        print(f"\n{len(leaks)} negatives passing the floor (the judge must catch these):")
        for row in leaks:
            print(f"  {row['id']:<26} {row['results']['dbsf']['gate_score']:.4f} "
                  f"-> {row['results']['dbsf']['top']}")

    return summary


def sweep(rows: list[dict]) -> None:
    print("\nthreshold sweep (DBSF-served chunk, dense cosine)\n")
    print(f"{'floor':>7}{'prec':>8}{'recall':>8}{'F1':>8}{'false refusals':>16}{'false accepts':>15}")
    best = None
    for step in range(40, 71):
        threshold = step / 100
        g = gate_metrics(rows, "dbsf", threshold)
        marker = ""
        if best is None or g["f1"] > best[1]["f1"]:
            best = (threshold, g)
        if abs(threshold - MIN_MATCH_SCORE) < 1e-9:
            marker = "   <- configured"
        print(f"{threshold:>7.2f}{pct(g['precision']):>8}{pct(g['recall']):>8}{pct(g['f1']):>8}"
              f"{g['false_refusals']:>16}{g['false_accepts']:>15}{marker}")
    print(f"\nbest F1 at {best[0]:.2f} ({best[1]['f1'] * 100:.1f})")
    print(
        "\nF1 is not the whole argument. A false refusal throws away an answer the\n"
        "library does contain, and the judge never gets the chance to overturn it.\n"
        "A false accept still meets covers_incident. The costs are asymmetric, so the\n"
        "floor belongs BELOW the lowest on-topic score rather than at peak F1."
    )


# --------------------------------------------------------------------------


async def measure(refresh: bool) -> list[dict]:
    golden = [json.loads(line) for line in GOLDEN_SET.read_text(encoding="utf-8").splitlines() if line.strip()]

    if refresh or not CORPUS_CACHE.exists():
        corpus = snapshot_corpus()
        save_corpus(corpus)
        log.info("snapshotted %d chunks from the live collection", len(corpus))
    else:
        corpus = load_corpus()

    vectors = await query_vectors([g["query"] for g in golden], refresh)

    client = await build_index(corpus)
    try:
        rows = []
        for item in golden:
            dense = vectors[digest(item["query"])].tolist()
            indices, values = encode_query(item["query"])
            sparse_vec = models.SparseVector(indices=indices, values=values)
            results = await retrieve_all(
                client, dense, sparse_vec, limit=max(K_VALUES)
            )
            rows.append(
                {
                    "id": item["id"],
                    "category": item["category"],
                    "relevant": {
                        f"{e['source']}#{e['chunk_index']}" for e in item["expected"]
                    },
                    "results": results,
                }
            )
        return rows
    finally:
        await client.close()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true",
                        help="re-embed queries and re-snapshot the corpus (costs quota)")
    parser.add_argument("--sweep", action="store_true", help="sweep the score floor")
    parser.add_argument("--check", action="store_true",
                        help="compare against baseline.json; exit 1 on regression")
    parser.add_argument("--write-baseline", action="store_true")
    parser.add_argument("--tolerance", type=float, default=0.02,
                        help="allowed drop before --check fails (default 0.02)")
    parser.add_argument("--json", dest="json_path")
    args = parser.parse_args()

    rows = asyncio.run(measure(args.refresh))
    summary = report(rows, MIN_MATCH_SCORE)

    if args.sweep:
        sweep(rows)

    if args.json_path:
        pathlib.Path(args.json_path).write_text(json.dumps(summary, indent=2), encoding="utf-8")

    if args.write_baseline:
        BASELINE.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"\nwrote baseline -> {BASELINE}")
        return 0

    if args.check:
        if not BASELINE.exists():
            print("\nno baseline.json — run --write-baseline first", file=sys.stderr)
            return 1
        before = json.loads(BASELINE.read_text(encoding="utf-8"))
        failures = []
        for metric in ("success@5", "recall@5", "ndcg@5"):
            was = before["arms"]["dbsf"][metric]
            now = summary["arms"]["dbsf"][metric]
            if now < was - args.tolerance:
                failures.append(f"dbsf {metric}: {was:.3f} -> {now:.3f}")
        was_f1 = before["arms"]["dbsf"]["gate"]["f1"]
        now_f1 = summary["arms"]["dbsf"]["gate"]["f1"]
        if now_f1 < was_f1 - args.tolerance:
            failures.append(f"gate F1: {was_f1:.3f} -> {now_f1:.3f}")
        if failures:
            print("\nREGRESSION:", file=sys.stderr)
            for failure in failures:
                print(f"  {failure}", file=sys.stderr)
            return 1
        print("\nno regression against baseline.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
