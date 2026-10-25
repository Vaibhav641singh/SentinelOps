"""BM25 sparse vectors, for the half of retrieval that dense embedding is bad at.

Dense embedding blurs an exact token into its neighbourhood: `wal_keep_segments`
becomes "some Postgres WAL setting". That is the wrong behaviour here, because the
judge's whole job is to reason about exact parameter and command names. Measured over
11 scored queries, dense retrieval got 9 right and BM25 got 11 — and both of dense's
failures were exact-token lookups it had blurred. See scripts/measure_hybrid.py.

Only the term-frequency half of BM25 is computed here. Qdrant applies IDF itself when
the sparse vector is configured with `Modifier.IDF`, because IDF depends on the whole
collection and the collection is what Qdrant has. Writing it here would mean keeping a
second copy of the corpus statistics in sync with the one Qdrant already maintains.

This is a deliberate ~60 lines rather than a dependency. fastembed is the obvious
alternative and provides stemming and stopword handling that this does not, but it
pulls in onnxruntime, which is ~200MB in an image deployed on a free plan and which
segfaults on this project's development machine.
"""

import re
import zlib

# Keep `_` and `.` inside tokens. This is the entire point: `wal_keep_segments`,
# `recovery.conf` and `pg_resetxlog` have to survive as single terms, or BM25 offers
# nothing that dense retrieval did not already do better. Splitting them also measurably
# changed nothing for the better when tested with --subtokens.
TOKEN = re.compile(r"[a-z0-9_.]+")

# Standard Okapi BM25 constants. k1 bounds how much repeating a term can help; b is how
# hard to penalise a long document. Untuned on purpose — the corpus is too small for
# fitting these to mean anything, and the defaults are defaults for good reason.
K1 = 1.5
B = 0.75


def tokenize(text: str) -> list[str]:
    return TOKEN.findall(text.lower())


def term_id(token: str) -> int:
    """Map a token to a sparse-vector index.

    crc32 rather than Python's hash(), which is salted per process and would hand the
    same token a different index on every restart — documents and queries would stop
    agreeing. Collisions map two unrelated terms onto one dimension; at 2^31 buckets
    and a corpus this size the chance is negligible, and the cost of one is a slightly
    wrong score rather than a failure.
    """
    return zlib.crc32(token.encode()) & 0x7FFFFFFF


def average_length(corpus: list[str]) -> float:
    """Mean token count, which BM25 needs to tell a long document from a verbose one."""
    if not corpus:
        return 0.0
    return sum(len(tokenize(doc)) for doc in corpus) / len(corpus)


def encode_document(text: str, avg_len: float) -> tuple[list[int], list[float]]:
    """Term-frequency weights for a stored chunk. Qdrant multiplies in the IDF."""
    tokens = tokenize(text)
    length = len(tokens)
    norm = 1 - B + B * (length / avg_len if avg_len else 1.0)

    counts: dict[int, int] = {}
    for token in tokens:
        counts[term_id(token)] = counts.get(term_id(token), 0) + 1

    indices = list(counts)
    values = [freq * (K1 + 1) / (freq + K1 * norm) for freq in counts.values()]
    return indices, values


def encode_query(text: str) -> tuple[list[int], list[float]]:
    """Presence weights for a query.

    Deliberately not term-frequency weighted: an error log that repeats the same line
    forty times should not have that line count forty times as much. IDF still does the
    discriminating, so a rare token like `wal_keep_segments` outweighs `the` regardless.
    """
    indices = sorted({term_id(token) for token in tokenize(text)})
    return indices, [1.0] * len(indices)
