import math

from copilot.embeddings import l2_normalize


def test_normalizes_to_unit_length():
    # The whole point: gemini-embedding-001 only returns a unit vector at its
    # native 3072 dims, so truncated output must be rescaled or cosine scores
    # are silently wrong.
    out = l2_normalize([3.0, 4.0])
    assert math.isclose(math.sqrt(sum(v * v for v in out)), 1.0)
    assert math.isclose(out[0], 0.6)
    assert math.isclose(out[1], 0.8)


def test_preserves_direction():
    out = l2_normalize([1.0, 2.0, 2.0])
    assert out[1] > out[0]
    assert math.isclose(out[1], out[2])


def test_already_normalized_is_unchanged():
    out = l2_normalize([1.0, 0.0, 0.0])
    assert math.isclose(out[0], 1.0)


def test_zero_vector_does_not_divide_by_zero():
    assert l2_normalize([0.0, 0.0]) == [0.0, 0.0]


def test_empty_vector_is_safe():
    assert l2_normalize([]) == []
