"""Funny names: <adjective>-<noun>, noun pool split by agent role.

Managers draw from MANAGER_NOUNS (globally-known mythical/fantasy creatures);
workers draw from WORKER_NOUNS (real animals). Disjoint pools make a name's
role readable at a glance and kill cross-role collisions for new rolls. Every
word in all three lists is deliberately simple — CEFR ~B1 / common
international vocabulary, or obvious pop culture ("grumpy-dragon",
"happy-panda") — so names read instantly for a non-native English speaker.
Collision-checked against the active-records set by the caller; 5 re-rolls
then a 2-digit fallback.
"""
import random
from typing import Callable

ADJECTIVES = (
    "happy", "quick", "fast", "slow", "calm", "bold", "brave", "sleepy",
    "lazy", "lucky", "sunny", "clever", "smart", "silly", "goofy", "grumpy",
    "mighty", "sneaky", "tidy", "bouncy", "cozy", "fuzzy", "merry", "angry",
    "hungry", "friendly", "gentle", "noisy", "quiet", "shiny", "golden",
    "crazy", "wild", "proud", "shy", "cool", "fancy", "spicy", "sweet",
    "speedy", "curious", "bright", "cheerful",
)

MANAGER_NOUNS = (
    "dragon", "phoenix", "unicorn", "mermaid", "troll", "ghost", "wizard",
    "witch", "giant", "genie", "fairy", "elf", "dwarf", "goblin", "zombie",
    "vampire", "werewolf", "angel", "demon", "ogre", "yeti", "pegasus",
    "sphinx", "centaur", "cyclops", "titan", "mummy", "skeleton", "gnome",
    "monster", "kraken",
)

WORKER_NOUNS = (
    "cat", "dog", "fox", "wolf", "bear", "lion", "tiger", "panda", "koala",
    "monkey", "rabbit", "mouse", "duck", "frog", "pig", "goat", "sheep",
    "horse", "pony", "donkey", "camel", "zebra", "giraffe", "elephant",
    "hippo", "whale", "dolphin", "shark", "octopus", "crab", "penguin",
    "parrot", "turtle", "snake", "bee", "hamster", "kangaroo", "gorilla",
    "chicken", "llama", "owl", "hedgehog", "capybara", "yak",
)

MAX_REROLLS = 5


def _roll(nouns: tuple[str, ...], rng: random.Random) -> str:
    return f"{rng.choice(ADJECTIVES)}-{rng.choice(nouns)}"


def _roll_unique(
    nouns: tuple[str, ...],
    is_taken: Callable[[str], bool],
    rng: random.Random | None,
) -> str:
    """Roll <adj>-<noun>; retry up to MAX_REROLLS on collision; then fall back
    to <rolled>-<00..99>.

    `is_taken(name)` returns True if the candidate collides with an existing
    active record. Caller is responsible for the live-active-record lookup.
    """
    rng = rng or random.Random()
    candidate = _roll(nouns, rng)
    for _ in range(MAX_REROLLS):
        if not is_taken(candidate):
            return candidate
        candidate = _roll(nouns, rng)
    # Fallback: stick with the last roll plus a 2-digit suffix.
    for _ in range(100):
        suffix = f"{rng.randint(0, 99):02d}"
        fallback = f"{candidate}-{suffix}"
        if not is_taken(fallback):
            return fallback
    # 100 collisions in a row is implausible (would need more active sessions
    # than the ~1300-1900 combos per pool plus 100 suffixes); if we somehow get
    # here, return the last attempt — the caller's active-record write will
    # reject duplicates with a clear error.
    return fallback


def roll_manager_name(
    is_taken: Callable[[str], bool],
    rng: random.Random | None = None,
) -> str:
    return _roll_unique(MANAGER_NOUNS, is_taken, rng)


def roll_worker_name(
    is_taken: Callable[[str], bool],
    rng: random.Random | None = None,
) -> str:
    return _roll_unique(WORKER_NOUNS, is_taken, rng)


# Legacy alias: stale caller code in a long-running process (e.g. the MCP
# server, which holds imported modules in memory across deploys) can still
# reach for this name for the worker roll until restarted.
roll_funny_name = roll_worker_name
