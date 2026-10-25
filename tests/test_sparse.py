from copilot.sparse import (
    average_length,
    encode_document,
    encode_query,
    term_id,
    tokenize,
)


def test_exact_tokens_survive_tokenization():
    # The whole reason BM25 is here. If these split, it offers nothing dense
    # retrieval did not already do better.
    assert "wal_keep_segments" in tokenize("SHOW wal_keep_segments;")
    assert "recovery.conf" in tokenize("promote via recovery.conf on the standby")
    assert "pg_resetxlog" in tokenize("pg_resetxlog: command not found")


def test_tokenizer_is_case_insensitive():
    assert tokenize("VACUUM FULL") == tokenize("vacuum full")


def test_term_ids_are_stable_across_processes():
    # Python's hash() is salted per process, which would leave documents indexed
    # under one id and queries looking under another.
    assert term_id("wal_keep_segments") == 213026591
    assert term_id("wal_keep_segments") != term_id("wal_keep_size")


def test_query_weights_ignore_repetition():
    # An error log repeating one line 40 times must not weight it 40x.
    once, weights_once = encode_query("No space left on device")
    many, weights_many = encode_query("No space left on device " * 40)
    assert once == many
    assert set(weights_once) == set(weights_many) == {1.0}


def test_document_weights_saturate_with_repetition():
    # BM25's k1 bounds the payoff of repeating a term: twice is worth more than
    # once, but nothing like twice as much.
    avg = 10.0
    _, one = encode_document("alpha beta", avg)
    _, two = encode_document("alpha alpha beta", avg)
    assert max(two) > max(one)
    assert max(two) < 2 * max(one)


def test_longer_documents_are_penalized():
    # Same single occurrence, more padding around it: b>0 means it scores lower.
    avg = 20.0
    short_idx, short_val = encode_document("alpha", avg)
    long_idx, long_val = encode_document("alpha " + "filler " * 50, avg)
    target = term_id("alpha")
    assert short_val[short_idx.index(target)] > long_val[long_idx.index(target)]


def test_average_length_of_empty_corpus_does_not_divide_by_zero():
    assert average_length([]) == 0.0
    # And an avg_len of 0 must not blow up document encoding either.
    indices, values = encode_document("alpha beta", 0.0)
    assert len(indices) == len(values) == 2


def test_document_and_query_agree_on_term_ids():
    # The two encoders are separate functions; if they ever disagree on ids,
    # retrieval silently returns nothing.
    doc_idx, _ = encode_document("wal_keep_segments was removed", 5.0)
    qry_idx, _ = encode_query("wal_keep_segments")
    assert set(qry_idx) <= set(doc_idx)
