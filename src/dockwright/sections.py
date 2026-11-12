import re
from dataclasses import dataclass

_HEADING = re.compile(r"^## (?:\[([ xX])\] )?(.*?)\s*$")
_FENCE = re.compile(r"(`{3,}|~{3,})(.*)")
_LINE = re.compile(r"[^\n]*\n|[^\n]+\Z")


@dataclass(frozen=True)
class Section:
    text: str
    heading: str
    title: str
    closed: bool


def split(text: str) -> tuple[str, list[Section]]:
    header: list[str] = []
    blocks: list[list[str]] = []
    fence = None
    for line in _LINE.findall(text):
        m = _FENCE.match(line.lstrip())
        if fence is None and m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
            fence = m.group(1)
        elif fence is not None and m and m.group(1).startswith(fence) and not m.group(2).strip():
            fence = None
        elif fence is None and line.startswith("## "):
            blocks.append([])
        (blocks[-1] if blocks else header).append(line)
    return "".join(header), [_section("".join(block)) for block in blocks]


def _section(text: str) -> Section:
    heading = text.split("\n", 1)[0].rstrip("\r")
    m = _HEADING.match(heading)
    return Section(text, heading, m.group(2), m.group(1) in ("x", "X"))
