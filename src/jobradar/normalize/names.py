"""Company / title normalization used for dedup keys."""

from __future__ import annotations

import re
from functools import lru_cache

import yaml

from jobradar.normalize.text import fold
from jobradar.settings import PACKAGE_DIR, user_dir

_LEGAL = (
    r"gmbh(?: & co\.? kg)?|ag|se|kg|ltd\.?|limited|llc|l\.l\.c\.|inc\.?|incorporated|corp\.?|corporation|plc|"
    r"b\.?v\.?|n\.?v\.?|s\.?a\.?|s\.?a\.?s\.?|sarl|s\.?r\.?l\.?|s\.?l\.?|s\.?p\.?a\.?|oy|oyj|ab|as|a/s|aps|"
    r"sp\.? z o\.?o\.?|sp z oo|spolka z ograniczona odpowiedzialnoscia|s\.?k\.?a\.?|s\.?r\.?o\.?|kft\.?|"
    r"group|holding|holdings|co\.?"
)
_LEGAL_RE = re.compile(rf"(?:[,\s]+(?:{_LEGAL}))+\.?$")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")

_GENDER = re.compile(
    r"\(\s*(?:m\s*/\s*w\s*/\s*d|w\s*/\s*m\s*/\s*d|m\s*/\s*f\s*/\s*d|f\s*/\s*m\s*/\s*x|m\s*/\s*f\s*/\s*x|"
    r"m\s*/\s*f|f\s*/\s*m|k\s*/\s*m(?:\s*/\s*n)?|m\s*/\s*k|h\s*/\s*f|f\s*/\s*h|all genders?|d\s*/\s*f\s*/\s*m|"
    r"m\s*/\s*w|w\s*/\s*m|gn\*?|x\s*/\s*f\s*/\s*m)\s*\)",
    re.I,
)
_BRACKETS = re.compile(r"[\(\[\{][^\)\]\}]*(?:remote|hybrid|zdaln|hybryd|onsite|on-site|b2b|uop|full[- ]time|part[- ]time|contract|f/m|m/f)[^\)\]\}]*[\)\]\}]", re.I)
_TRAILING_LOC = re.compile(r"\s+[-–|@]\s+[^-–|@]{2,60}$")
_TRAILING_SLASH = re.compile(r"\s+/\S.{0,40}$")              # "Engineer /GCP/Remote"; "BI / Analytics" has a space after
_MODE_WORDS = re.compile(r"\b(?:remote|hybrid|onsite|on site|hybrydow\w*|zdaln\w*|stacjonarn\w*)\b")  # "…-hybrid", "| Remote"
_SENIOR_ALIASES = [(re.compile(r"\bsr\.?\b"), "senior"), (re.compile(r"\bjr\.?\b"), "junior")]
# title_key / company_key only (dedup), not norm_title: "100% Remoto", "Teletrabajo", "Home office"
_KEY_NOISE = re.compile(r"\b(?:100|remot[oaey]|teletrabajo|h[iy]brid[oaey]?|presencial|home office|telework\w*|fully)\b")
_KEY_STOP = frozenset("a an and at de do dla e en for in mit na of on oraz the to und w with y z".split())   # not "i": "Engineer I"
_KEY_REGIONS = frozenset("emea eu eea europe european cee dach nordics benelux iberia apac latam amer namer usa us uk "
                         "worldwide anywhere".split())
# one-word GeoNames cities (pop ≥ 100k) that are also words in job titles: "Enterprise Architect", "Delta Lake"
_KEY_NOT_CITY = frozenset("enterprise delta hub rest aurora phoenix gold solo dual part name tape logos kilo trident "
                          "terminus monster lion man van mesa vista independence providence paradise surprise orange "
                          "centennial commonwealth concord midland soul mango tango luck shaping reading lander "
                          "pristine franklin gilbert chandler newton kyle mary nancy sophia warren".split())
_COMPANY_COUNTRY = re.compile(
    r"(?:\s+(?:poland|polska|polen|spain|espana|iberia|germany|deutschland|france|italy|italia|portugal|netherlands|"
    r"nederland|belgium|austria|switzerland|schweiz|ireland|uk|romania|czech republic|czechia|lithuania|latvia|estonia|"
    r"sweden|denmark|norway|finland|hungary|bulgaria|greece|croatia|europe|emea|cee|global|international|"
    r"technology center|delivery center|tech hub|io|com|ai|app|hq))+$")        # "Constructor.io" → "constructor"


@lru_cache(maxsize=1)
def _aliases() -> dict[str, str]:
    path = user_dir() / "company_aliases.yaml"
    if not path.exists():
        path = PACKAGE_DIR / "company_aliases.yaml"
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    out: dict[str, str] = {}
    for canonical, variants in data.items():
        out[norm_company_raw(canonical)] = norm_company_raw(canonical)
        for v in variants or []:
            out[norm_company_raw(v)] = norm_company_raw(canonical)
    return out


def norm_company_raw(name: str | None) -> str:
    s = fold(name or "").strip()
    s = s.replace("&amp;", "&")
    prev = None
    while prev != s:
        prev = s
        s = _LEGAL_RE.sub("", s).strip(" ,.-")
    s = re.sub(r"^the\s+", "", s)
    return _NON_ALNUM.sub(" ", s).strip()


def norm_company(name: str | None) -> str:
    raw = norm_company_raw(name)
    return _aliases().get(raw, raw)


def clean_title(title: str | None) -> str:
    """Human-readable title without gender markers / noise brackets."""
    t = (title or "").strip()
    t = _GENDER.sub("", t)
    t = re.sub(r"\s{2,}", " ", t)
    return t.strip(" -–|,")


def norm_title(title: str | None, *, strip_location: bool = True) -> str:
    """Dedup key form: folded, no gender/location/mode markers, seniority kept."""
    t = clean_title(title)
    t = _BRACKETS.sub(" ", t)
    if strip_location:
        t = _TRAILING_SLASH.sub("", _TRAILING_LOC.sub("", t))
    t = fold(t)
    for rx, rep in _SENIOR_ALIASES:
        t = rx.sub(rep, t)
    t = _NON_ALNUM.sub(" ", t)
    if strip_location:
        t = _MODE_WORDS.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


def company_key(name: str | None) -> str:
    """Dedup form of a company: `norm_company` without a trailing country/region or domain ("Sii Polska", "Sii Poland"
    → "sii"; "Constructor.io" → "constructor")."""
    c = norm_company(name)
    return _COMPANY_COUNTRY.sub("", c).strip() or c


def title_key(title: str | None) -> str:
    """Dedup form of a title: the words of `norm_title` as a sorted set, without places (GeoNames cities, countries,
    regions), Spanish/Portuguese work-mode words or filler words. Equal `norm_title`s stay equal; on top of that word
    order and where a place sits don't matter ("Data Engineer GCP (Governance)" = "Data Governance Engineer (GCP)",
    "Revenue Operations Manager (EMEA)" = "Revenue Operations Manager - EMEA"). Seniority words stay."""
    from jobradar.normalize.location import _US_STATE_CODES, _city_index, _country_names

    words = _KEY_NOISE.sub(" ", norm_title(title)).split()
    cities, countries = _city_index(), _country_names()
    place: set[int] = set()
    for n in (3, 2, 1):
        for i in range(len(words) - n + 1):
            chunk = " ".join(words[i:i + n])
            city = cities.get(chunk)
            if chunk in countries or chunk in _KEY_REGIONS or (
                    city and (n > 1 or (city[1] >= 100_000 and chunk not in _KEY_NOT_CITY))):
                place.update(range(i, i + n))
    for i, w in enumerate(words):              # "Austin, TX, USA": a state code right after a place
        if i - 1 in place and w.upper() in _US_STATE_CODES:
            place.add(i)
    return " ".join(sorted({w for i, w in enumerate(words) if i not in place and w not in _KEY_STOP}))
