import random

import pytest

from dockwright import names


def test_noun_pools_are_disjoint():
    assert set(names.MANAGER_NOUNS) & set(names.WORKER_NOUNS) == set()


@pytest.mark.parametrize("pool", [names.ADJECTIVES, names.MANAGER_NOUNS, names.WORKER_NOUNS])
def test_pool_words_are_simple_lowercase_tokens(pool):
    # Names are hyphen-joined and the collision fallback appends "-NN", so a
    # word containing a hyphen or space would corrupt every name.split("-")
    # consumer. Duplicates would silently shrink the collision space.
    assert all(word.isascii() and word.isalpha() and word.islower() for word in pool)
    assert len(set(pool)) == len(pool)


def test_pools_are_large_enough_for_unique_rolls():
    assert len(names.ADJECTIVES) >= 30
    assert len(names.MANAGER_NOUNS) >= 25
    assert len(names.WORKER_NOUNS) >= 25


def test_roll_manager_name_draws_from_manager_pool():
    name = names.roll_manager_name(is_taken=lambda _: False, rng=random.Random(0))
    adj, noun = name.split("-", 1)
    assert adj in names.ADJECTIVES
    assert noun in names.MANAGER_NOUNS


def test_roll_worker_name_draws_from_worker_pool():
    name = names.roll_worker_name(is_taken=lambda _: False, rng=random.Random(0))
    adj, noun = name.split("-", 1)
    assert adj in names.ADJECTIVES
    assert noun in names.WORKER_NOUNS


def test_roll_retries_on_collision():
    """A simulated collision must force a re-roll, not return the taken name."""
    first = names.roll_manager_name(is_taken=lambda _: False, rng=random.Random(42))
    rerolled = names.roll_manager_name(is_taken=lambda n: n == first, rng=random.Random(42))
    assert rerolled != first


def test_roll_falls_back_to_suffix_when_exhausted():
    """If every roll collides for MAX_REROLLS, the next attempt gets a 2-digit suffix."""
    call_count = {"n": 0}

    def is_taken(n: str) -> bool:
        call_count["n"] += 1
        return call_count["n"] <= names.MAX_REROLLS

    name = names.roll_worker_name(is_taken=is_taken, rng=random.Random(1))
    parts = name.split("-")
    assert len(parts) == 3
    assert parts[2].isdigit() and len(parts[2]) == 2


def test_roll_funny_name_is_worker_alias():
    # Old hooks.py copies import roll_funny_name for the worker roll; the alias
    # keeps the file-copy deployment window safe.
    assert names.roll_funny_name is names.roll_worker_name
