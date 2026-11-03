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


_MOJIBAKE = re.compile("[\u00c2\u00c3\u00e2][\u0080-\u00bf\u20ac\u201a-\u203a\u2122\u0152\u0153\u0160\u0161\u0178\u017d\u017e]")


def fix_mojibake(value: str) -> str:
    """Repair UTF-8 text that was decoded as Latin-1 / Windows-1252 somewhere upstream ("Sothebyâ€™s" → "Sotheby’s").
    Only text with the telltale byte pairs is touched, and only when re-decoding works."""
    if not value or not _MOJIBAKE.search(value):
        return value
    for enc in ("cp1252", "latin-1"):
        try:
            return value.encode(enc).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
    return value


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
