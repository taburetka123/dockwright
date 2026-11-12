from .base import (INTERACTIVE_MODE, LEG_MODES, MODES, REPLY_MODE, REPORT_MODE, ChildProvider, ChildRound,
                   validate_mode)
from .claude_exec import ClaudeExec
from .claude_tui import ClaudeTui
from .codex_exec import CodexExec

LEGACY_TUI_LABELS = ("codex-tui",)

_REGISTRY: dict[str, ChildProvider] = {}


def register_provider(provider: ChildProvider) -> ChildProvider:
    _REGISTRY[provider.name] = provider
    return provider


def unregister_provider(name: str) -> None:
    _REGISTRY.pop(name, None)


def get_provider(name: str) -> ChildProvider:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise ValueError(f"unknown provider {name!r}; known: {sorted(_REGISTRY)}") from None


def known_provider_names() -> tuple[str, ...]:
    return tuple(_REGISTRY) + LEGACY_TUI_LABELS


def headless_families() -> tuple[str, ...]:
    return tuple(sorted({p.family for p in _REGISTRY.values() if p.headless}))


def headless_provider_for_family(family: str) -> ChildProvider:
    matches = [p for p in _REGISTRY.values() if p.family == family and p.headless]
    if len(matches) == 1:
        return matches[0]
    families = sorted({p.family for p in _REGISTRY.values() if p.headless})
    if not matches:
        raise ValueError(f"no headless provider registered for family {family!r}; known families: {families}")
    raise ValueError(f"family {family!r} has {len(matches)} headless providers ({sorted(p.name for p in matches)}); "
                     "name one with provider=")


for _provider in (CodexExec(), ClaudeExec(), ClaudeTui()):
    register_provider(_provider)

__all__ = ["ChildProvider", "ChildRound", "INTERACTIVE_MODE", "LEG_MODES", "MODES", "REPLY_MODE", "REPORT_MODE",
           "LEGACY_TUI_LABELS",
           "get_provider", "headless_families", "headless_provider_for_family", "known_provider_names", "register_provider",
           "unregister_provider", "validate_mode"]
