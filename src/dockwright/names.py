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
    rng = rng or random.Random()
    candidate = _roll(nouns, rng)
    for _ in range(MAX_REROLLS):
        if not is_taken(candidate):
            return candidate
        candidate = _roll(nouns, rng)
    for _ in range(100):
        suffix = f"{rng.randint(0, 99):02d}"
        fallback = f"{candidate}-{suffix}"
        if not is_taken(fallback):
            return fallback
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


roll_funny_name = roll_worker_name
