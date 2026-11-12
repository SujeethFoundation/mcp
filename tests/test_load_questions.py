"""The load script's question picker: fixed questions, or new wordings so caches cannot answer them."""
import importlib.util
import os
import random

SPEC = importlib.util.spec_from_file_location("load_mcp", os.path.join(os.path.dirname(os.path.abspath(__file__)), "load_mcp.py"))
load = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(load)


def sample(share, n=2000, seed=1):
    rng = random.Random(seed)
    return [load.pick_question(share, rng) for _ in range(n)]


def test_with_no_variation_only_the_fixed_questions_are_asked():
    asked = set(sample(0.0))
    assert asked <= set(load.QUESTIONS) and len(asked) > 30


def test_full_variation_makes_almost_every_question_unique():
    asked = sample(1.0)
    assert len(set(asked)) / len(asked) > 0.99
    assert all(q not in load.QUESTIONS for q in asked)


def test_a_partial_share_mixes_new_wordings_with_repeats():
    asked = sample(0.7)
    new = [q for q in asked if q not in load.QUESTIONS]
    assert 0.62 < len(new) / len(asked) < 0.78
    assert len(set(asked)) < len(asked)                      # the rest repeat, as real traffic does


def test_a_varied_question_still_contains_its_original_words():
    rng = random.Random(5)
    for _ in range(200):
        q = load.pick_question(1.0, rng)
        base = next(b for b in load.QUESTIONS if q.startswith(b.rstrip("?.")))
        assert q.endswith(tuple(str(d) for d in range(10))) and " ref " in q and base.rstrip("?.") in q


def test_the_same_seed_gives_the_same_questions():
    assert sample(0.5, 50, seed=9) == sample(0.5, 50, seed=9)
    assert sample(0.5, 50, seed=9) != sample(0.5, 50, seed=10)
