PRESERVE_ITEMS = (
    "any difficulties or problems that came up, and how they were handled or resolved",
    "any possibilities, options, or approaches that were raised, tried, or set aside, and why",
    "anything that was asked for, decided, agreed, ruled out, or established as a preference, constraint, or "
    "boundary — stated exactly",
    "exactly where things stand now — what has been covered, settled, or completed so far",
    "anything still open, unresolved, promised, or expected to happen next",
    "specific details that would be hard to reconstruct — names, numbers, dates, exact wording, links or "
    "references — kept exactly",
)

VOICES = (
    "Weight the two voices differently: keep what the user said, asked for, shared, or established carefully and "
    "close to their own words; your own explanations and reasoning can be condensed much further, to what they "
    "concluded or produced — as long as nothing in the six items above is dropped."
)


def preserve_clause() -> str:
    return "; ".join(f"({i}) {item}" for i, item in enumerate(PRESERVE_ITEMS, 1))
