"""Posting-language detection (stopword heuristic) and language-requirement detection."""

from __future__ import annotations

import re

from jobradar.normalize.text import fold

# Distinctive multi-letter function words per language (accent-folded).
_STOP = {
    "en": "the and of to for with you we our are will your this that have experience team work from about".split(),
    "pl": "sie jest oraz dla nie jak przez pracy doswiadczenie zespolu bedzie lub oferujemy wymagania znajomosc "
    "twoje naszego nasz pracujemy umiejetnosci".split(),
    "de": "und der die das mit fur von ist wir sie auf ein eine den bei oder als ihre deine uns erfahrung "
    "kenntnisse aufgaben bieten".split(),
    "fr": "et le les des du un une pour avec vous nous est dans sur au aux votre notre experience equipe "
    "poste missions".split(),
    "es": "el los las del que con para por una es se nuestro nuestra equipo experiencia trabajo buscamos "
    "ofrecemos".split(),
    "it": "il di che per con una del della sono nel gli nostro esperienza lavoro siamo offriamo".split(),
    "nl": "en het van een voor met je wij ons onze op te bij ervaring werk jouw wat".split(),
    "pt": "os do da com um uma em nosso nossa experiencia trabalho voce sao".split(),
    "sv": "och att det som for med ett ar pa av till vi du har erfarenhet arbete".split(),
    "da": "og at det som for med et er pa af til vi du har erfaring arbejde".split(),
    "no": "og at det som for med et er pa av til vi du har erfaring arbeid".split(),
    "cs": "je na pro do ze jsme prace zkusenosti nabizime".split(),
}
_STOPSETS = {k: set(v) for k, v in _STOP.items()}
_TOKEN = re.compile(r"[a-z]{2,}")


def detect_language(text: str, *, min_hits: int = 8) -> tuple[str | None, dict[str, int]]:
    tokens = _TOKEN.findall(fold(text[:6000]))
    scores = {lang: 0 for lang in _STOPSETS}
    for tok in tokens:
        for lang, words in _STOPSETS.items():
            if tok in words:
                scores[lang] += 1
    best = max(scores, key=scores.get)
    if scores[best] < min_hits:
        return None, scores
    return best, scores


def language_ok(text: str, allowed: set[str]) -> tuple[bool, str | None]:
    """True when the posting is (also) written in an allowed language.

    Bilingual postings (e.g. German intro + English requirements) pass when the
    English share is substantial.
    """
    lang, scores = detect_language(text)
    if lang is None or lang in allowed:
        return True, lang
    best = scores[lang]
    for ok in allowed:
        if scores.get(ok, 0) >= 0.6 * best:
            return True, lang
    return False, lang


LANG_NAMES = {
    "de": r"german|deutsch(?:e|en|kenntnisse)?|niemiecki\w*",
    "fr": r"french|francais|fran[cç]ais|francuski\w*",
    "nl": r"dutch|nederlands|holenderski\w*",
    "es": r"spanish|espanol|espa[nñ]ol|castellano|hiszpanski\w*",
    "it": r"italian|italiano|wloski\w*",
    "pt": r"portuguese|portugues|portugalski\w*",
    "sv": r"swedish|svenska|szwedzki\w*",
    "da": r"danish|dansk|dunski\w*",
    "no": r"norwegian|norsk|norweski\w*",
    "fi": r"finnish|suomi|finski\w*",
    "cs": r"czech|cestina|czeski\w*",
    "hu": r"hungarian|magyar|wegierski\w*",
    "ro": r"romanian|romana|rumunski\w*",
}
_STRONG = (
    r"fluent|fluency|fluently|native|mother tongue|business[- ](?:level|fluent|proficien\w*)|proficien\w*|"
    r"excellent|very good|strong|advanced|full professional|professional working|c1|c2|b2|"
    r"flie(?:ss|ß)end\w*|verhandlungssicher\w*|sehr gut\w*|courant|fluido|fluida|biegl\w*|"
    r"required|must|mandatory|essential|wymagan\w*|obowiazkow\w*"
)
_SOFT = (
    r"nice to have|nice-to-have|is a plus|a plus|plus\b|bonus|advantage|advantageous|preferred|desirable|asset|"
    r"beneficial|helpful|optional|would be great|mile widzian\w*|dodatkowym atutem|atut\w*|deseable|"
    r"von vorteil|wunschenswert|un plus|apprecie"
)
_SOFT_RE = re.compile(_SOFT)


def language_requirements(text: str) -> dict[str, str]:
    """Return {lang: 'required' | 'plus' | 'mentioned'} for non-EN/PL languages.

    Each mention is judged by a ±90-char window: a "nice to have / plus / mile
    widziane" cue makes it 'plus', a "fluent / C1 / required" cue makes it
    'required'. Any required mention wins over plus mentions elsewhere.
    """
    t = fold(text[:12000])
    out: dict[str, str] = {}
    for lang, names in LANG_NAMES.items():
        levels: set[str] = set()
        for m in re.finditer(rf"\b(?:{names})\b", t):
            window = t[max(0, m.start() - 90) : m.end() + 90]
            if _SOFT_RE.search(window):
                levels.add("plus")
            elif re.search(rf"\b(?:{_STRONG})\b", window):
                levels.add("required")
            else:
                levels.add("mentioned")
        if levels:
            out[lang] = "required" if "required" in levels else "plus" if "plus" in levels else "mentioned"
    return out
