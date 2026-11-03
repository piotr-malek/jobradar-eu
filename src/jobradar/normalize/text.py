"""Text helpers: HTML → text, accent folding."""

from __future__ import annotations

import html as html_lib
import re
import unicodedata

from bs4 import BeautifulSoup

_WS = re.compile(r"[ \t ]+")
_NL = re.compile(r"\n{3,}")
_SPECIAL_FOLD = str.maketrans({"ł": "l", "Ł": "L", "ß": "ss", "ø": "o", "Ø": "O", "æ": "ae", "đ": "d", "ı": "i"})


def html_to_text(value: str | None) -> str:
    if not value:
        return ""
    text = value
    if "&lt;" in text and "<" not in text:  # HTML-escaped HTML (Greenhouse content)
        text = html_lib.unescape(text)
    if "<" in text and ">" in text:
        soup = BeautifulSoup(text, "lxml")
        for br in soup.find_all(["br", "p", "li", "div", "h1", "h2", "h3", "h4", "tr"]):
            br.append("\n")
        text = soup.get_text()
    text = html_lib.unescape(text)
    text = _WS.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return _NL.sub("\n\n", text).strip()


def fold(value: str | None) -> str:
    """Lowercase and strip diacritics (Zürich → zurich, Łódź → lodz)."""
    if not value:
        return ""
    value = value.translate(_SPECIAL_FOLD)
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return value.lower()


def clip(value: str, n: int) -> str:
    return value if len(value) <= n else value[: n - 1] + "…"
