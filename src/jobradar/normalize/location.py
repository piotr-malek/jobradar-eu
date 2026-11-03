"""Location / work-mode classification against the user's location profile.

Turns a posting's structured fields + location string (+ a few description
cues) into: work mode, countries, region hints, and a section:

    remote     remote, hireable from the user's country / the EU
    home       hybrid / office / unstated mode in the user's country (hybrid and office only within
               the user's radius when one is set)
    elsewhere  hybrid / office / unstated mode in another country the user can work in,
               or remote restricted to residents of one other EU country

Anything else is dropped with an explicit reason (logged for the drop audit).
"""

from __future__ import annotations

import gzip
import math
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from jobradar.normalize.text import fold

EU27 = frozenset(
    "AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE".split()
)
EEA_CH = frozenset("NO IS LI CH".split())
ELIGIBLE = EU27 | EEA_CH

_COUNTRY_ALIASES = {
    "PL": ["poland", "polska", "polen", "pologne", "polonia"],
    "DE": ["germany", "deutschland", "allemagne", "alemania", "niemcy"],
    "AT": ["austria", "osterreich", "autriche"],
    "CH": ["switzerland", "schweiz", "suisse", "svizzera", "szwajcaria"],
    "NL": ["netherlands", "the netherlands", "nederland", "holland", "holandia"],
    "BE": ["belgium", "belgique", "belgie", "belgien"],
    "FR": ["france", "francja", "frankreich"],
    "ES": ["spain", "espana", "hiszpania", "spanien"],
    "PT": ["portugal", "portugalia"],
    "IT": ["italy", "italia", "wlochy", "italien"],
    "IE": ["ireland", "eire", "irlandia"],
    "SE": ["sweden", "sverige", "szwecja"],
    "DK": ["denmark", "danmark", "dania"],
    "NO": ["norway", "norge", "norwegia"],
    "FI": ["finland", "suomi", "finlandia"],
    "CZ": ["czechia", "czech republic", "cesko", "czechy", "ceska republika"],
    "SK": ["slovakia", "slovensko", "slowacja"],
    "HU": ["hungary", "magyarorszag", "wegry"],
    "RO": ["romania", "rumunia"],
    "BG": ["bulgaria"],
    "GR": ["greece", "grecja", "hellas"],
    "HR": ["croatia", "hrvatska", "chorwacja"],
    "SI": ["slovenia", "slovenija", "slowenia"],
    "EE": ["estonia", "eesti"],
    "LV": ["latvia", "latvija", "lotwa"],
    "LT": ["lithuania", "lietuva", "litwa"],
    "LU": ["luxembourg", "luksemburg"],
    "MT": ["malta"],
    "CY": ["cyprus", "cypr"],
    "IS": ["iceland"],
    "LI": ["liechtenstein"],
    "GB": ["united kingdom", "uk", "u k", "great britain", "britain", "england", "scotland", "wales",
           "northern ireland", "wielka brytania"],
    "US": ["united states", "united states of america", "usa", "u s a", "u s", "us", "america"],
    "CA": ["canada"],
    "UA": ["ukraine", "ukraina"],
    "RS": ["serbia"],
    "TR": ["turkey", "turkiye"],
    "IN": ["india"],
    "IL": ["israel"],
    "AE": ["united arab emirates", "uae"],
    "BR": ["brazil", "brasil"],
    "MX": ["mexico"],
    "AR": ["argentina"],
    "AU": ["australia"],
    "NZ": ["new zealand"],
    "SG": ["singapore"],
    "JP": ["japan"],
    "KR": ["korea", "south korea", "republic of korea"],
    "CN": ["china"],
    "ZA": ["south africa"],
    "EG": ["egypt"],
    "PH": ["philippines"],
    "BY": ["belarus"],
    "GE": ["georgia country"],
}

REGIONS_OK = {
    "europe": r"europe|european|europa|eu|e u|european union|eea|emea|eu timezone|european timezone",
    "worldwide": r"worldwide|anywhere|global|globally|international|world|remote global|anywhere in the world",
    "cet": r"cet|cest|central european|utc ?\+ ?[0-3]|gmt ?\+ ?[0-3]|utc ?[-−] ?1|gmt ?[-−] ?1|eet|wet",
}
REGIONS_BAD = {
    "americas": r"americas|north america|latin america|latam|south america|n america|namer|amer",
    "apac": r"apac|asia|asia pacific|oceania|anz",
    "mena": r"mena|middle east|gcc|africa|emea excl\w* europe",
    "us_only": r"us only|usa only|u s only|united states only|us based|us-based|must be based in the us",
}
_US_AUTH = re.compile(
    r"(authori[sz]ed|eligible|legally able) to work in the (?:u\.?s\.?a?|united states)|"
    r"(must|need to) (?:be )?(?:located|based|reside|live) in the (?:u\.?s\.?a?|united states)|"
    r"\bus work authori[sz]ation|green card|u\.s\. citizens? only"
)
_US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO",
    "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC",
}
_US_STATE_CODES = set(_US_STATES.values())
_CA_PROVINCES = {"ontario", "quebec", "british columbia", "alberta", "manitoba", "saskatchewan", "nova scotia"}

_REMOTE_STRONG = re.compile(
    r"\b(?:fully[ -]remote|100 ?% remote|remote[- ]first|remote position|remote role|work remotely|"
    r"working remotely|remote work(?:ing)? (?:is )?possible|remote within|remote across|"
    r"praca zdalna|pracy zdalnej|w pelni zdaln\w*|100 ?% zdaln\w*|fully remote)\b"
)
_REMOTE_LOC = re.compile(
    r"\b(?:remote|remotely|zdaln\w*|home ?-?office|teletrabajo|teletrabalho|teletravail|remoto|telework|wfh|"
    r"work from home|thuiswerk\w*|anywhere)\b"
)


_REMOTE_WORD = re.compile(_REMOTE_LOC.pattern.replace("|anywhere", ""))      # "sell anywhere" is not a work mode


def mentions_remote(text: str) -> bool:
    """A remote word anywhere in the text (any of the languages we read): Jooble country sites keep only these."""
    return bool(_REMOTE_WORD.search(fold(text)) or _REMOTE_STRONG.search(fold(text)))
_HYBRID = re.compile(r"\b(?:hybrid\w*|hybryd\w*|hybride|hibrido|ibrido)\b")
_ONSITE = re.compile(
    r"\b(?:on[- ]?site only|100 ?% on[- ]?site|fully on[- ]?site|office[- ]based|in[- ]office only|"
    r"stacjonarn\w*|vor ort|presencial|sur site)\b"
)
_ONSITE_LOC = re.compile(r"\b(?:on[- ]?site|onsite|office|stacjonarn\w*|in office)\b")
# "hybrid" in a description is a work arrangement only in these phrasings ("hybrid cloud/environments" is not)
_HYBRID_WORK = re.compile(
    r"\bhybrid\w* (?:work\w*|model|mode|set ?-?up|role|position|basis|schedule|arrangement|policy|approach|format|"
    r"way of working|office|from)\b|\b(?:work\w*|model|mode|set ?-?up|role|position|job) (?:is |will be )?hybrid\b|"
    r"\bon a hybrid\b|\b\d(?: ?[-–] ?\d)? days? (?:a |per |each |every )?week (?:in|at|from) (?:the |our )?office\b|"
    r"\b\d(?: ?[-–] ?\d)? days? (?:in|at) (?:the |our )?office\b|"
    r"\b(?:prac\w*|model\w*|tryb\w*|system\w*|form\w*) hybrydow\w*|\bhybrydow\w* (?:prac\w*|model\w*|tryb\w*)|"
    r"\bhybrydowo\b|\bhybryda\b|\b(?:trabajo|modelo|modalidad|regime|regimen) h[iy]brid\w*|\bmodo h[iy]brido\b|"
    r"\btravail hybride\b|\bhybrides? arbeiten\b|\bhybride arbeit\w*|li-hybrid\b|\((?:hybrid|hybryd\w*|h[ií]brido)\)|"
    r"\b(?:location|work(?:ing)? (?:mode|model|style|setup|arrangement)|location type|workplace(?: type)?|tryb pracy|"
    r"model (?:pracy|wspolpracy)|(?:service )?delivery model)\s*[:\-–|]?\s*(?:[\w.]+,? ){0,3}?(?:hybrid\w*|hybryd\w*)\b|"
    r"\bhybrid (?:in|from|at) (?!nature)[a-z]+|\b(?:full|part)[- ]time,? hybrid\b|\bhybrid: [a-z]+"
)
# a hybrid/office posting that still offers remote work: kept, the screen reads the text
_REMOTE_OPTION = re.compile(
    r"\b(?:remote or hybrid|hybrid or remote|remote ?/ ?hybrid|hybrid ?/ ?remote|fully[ -]remote|100 ?% remote|"
    r"remote[- ]first|full remote|(?:possibility|option|options|opportunity|ability|choice) (?:of|for|to) "
    r"(?:\w+ ){0,2}(?:remote|work(?:ing)? (?:fully )?remotely|work(?:ing)? from home)|remote work(?:ing)? (?:is )?(?:also )?"
    r"(?:possible|available|an option|negotiable|allowed)|(?:can|could) (?:also )?be (?:done|performed) remotely|"
    r"(?:office|on-?site),? or (?:fully )?remotely|"
    r"(?:mostly|primarily|predominantly|largely|mainly) (?:\w+ ){0,2}remote(?:ly)?|remote(?:ly)? \(?(?:most|the majority) "
    r"of the time|remote, hybrid|location: remote|"
    r"praca zdalna|mozliwosc pracy zdalnej|opcja pracy zdalnej|w pelni zdaln\w*|100 ?% zdaln\w*|lokalizacja: zdalnie|"
    r"zdaln\w*,? (?:przez |lub |albo |i )?hybrydow\w*|hybrydow\w*,? (?:lub |albo |i )?zdaln\w*|"
    r"wiekszosc (?:zadan|czasu|pracy) (?:\w+ ){0,2}zdalnie|totalmente remoto|100 ?% remoto|teletrabajo 100)\b"
)


@dataclass
class LocationResult:
    section: str | None
    work_mode: str
    countries: list[str]
    regions: list[str]
    flags: list[str] = field(default_factory=list)
    drop_reason: str | None = None
    evidence: dict = field(default_factory=dict)


# ---------------------------------------------------------------- gazetteer
_DATA = Path(__file__).with_name("data")


@lru_cache(maxsize=1)
def _country_names() -> dict[str, str]:
    """folded country name → ISO2 (GeoNames countryInfo + multilingual aliases)."""
    names: dict[str, str] = {}
    for line in (_DATA / "countries.tsv").read_text(encoding="utf-8").splitlines():
        iso, name = line.split("\t")
        names[fold(name)] = iso
    for iso, aliases in _COUNTRY_ALIASES.items():
        for a in aliases:
            names[fold(a)] = iso
    for ambiguous in ("georgia", "jersey", "chad", "guinea"):
        names.pop(ambiguous, None)
    return names


@lru_cache(maxsize=1)
def _city_index() -> dict[str, tuple[str, int, float | None, float | None]]:
    """folded city name → (country, population, lat, lon). Vendored from GeoNames cities15000 (CC BY 4.0).

    On name clashes the larger city wins, with a 1.5x boost for EU/EEA cities
    because the postings we see are overwhelmingly European.
    """
    index: dict[str, tuple[str, int, float | None, float | None]] = {}
    with gzip.open(_DATA / "cities.tsv.gz", "rt", encoding="utf-8") as fh:
        for line in fh:
            key, cc, pop_s, lat, lon = line.rstrip("\n").split("\t")
            pop = int(pop_s)
            cur = index.get(key)
            score = pop * (1.5 if cc in ELIGIBLE else 1.0)
            if cur is None or score > cur[1] * (1.5 if cur[0] in ELIGIBLE else 1.0):
                index[key] = (cc, pop, float(lat) if lat else None, float(lon) if lon else None)
    for word in ("remote", "hybrid", "office", "home", "anywhere", "europe", "global", "data", "online",
                 "mobile", "central", "nice", "split", "best", "deal", "most", "sale", "bar", "ede", "team",
                 "lead", "senior", "junior", "engineer", "manager", "flexible", "multiple", "various", "north",
                 "south", "east", "west", "union", "victoria", "hybrid remote", "mitte", "center", "centre"):
        index.pop(word, None)
    return index


_SPLIT = re.compile(r"[,;/|()\[\]•·–—]+|\s+-\s+|\s+or\s+|\s+and\s+|\s+&\s+|\s+lub\s+")


@lru_cache(maxsize=50_000)
def extract_places(text: str) -> tuple[frozenset[str], frozenset[str], tuple[str, ...]]:
    """Return (countries, regions_ok/bad labels, cities) found in a location string."""
    countries: set[str] = set()
    regions: set[str] = set()
    cities: list[str] = []
    if not text:
        return frozenset(), frozenset(), ()
    raw_segments = [s.strip() for s in _SPLIT.split(text) if s and s.strip()]
    folded_all = fold(text)
    for label, rx in {**REGIONS_OK, **REGIONS_BAD}.items():
        if re.search(rf"(?<![a-z])(?:{rx})(?![a-z])", folded_all):   # grouped: "e u" must not match "remote us"
            regions.add(label)
    names = _country_names()
    city_idx = _city_index()
    us_hint = False
    pending_codes: list[str] = []
    for seg in raw_segments:
        f = fold(seg).strip(" .")
        if not f:
            continue
        if re.fullmatch(r"[A-Z]{2}", seg.strip()):
            pending_codes.append(seg.strip())
            continue
        if f in names:
            countries.add(names[f])
            continue
        if f in _US_STATES or f in _CA_PROVINCES:
            countries.add("US" if f in _US_STATES else "CA")
            us_hint = True
            continue
        if f in city_idx:
            cc = city_idx[f][0]
            countries.add(cc)
            cities.append(seg.strip())
            us_hint |= cc in ("US", "CA")
            continue
        # multi-word segment: look for country names / big cities inside it
        found = False
        for name, iso in names.items():
            if len(name) >= 4 and re.search(rf"(?<![a-z]){re.escape(name)}(?![a-z])", f):
                countries.add(iso)
                found = True
        words = f.split()
        for n in (3, 2, 1):
            for i in range(len(words) - n + 1):
                chunk = " ".join(words[i : i + n])
                hit = city_idx.get(chunk)
                if hit and (n > 1 or hit[1] >= 100_000):
                    countries.add(hit[0])
                    cities.append(chunk)
                    us_hint |= hit[0] in ("US", "CA")
                    found = True
        if not found and re.search(r"(?<![a-z])(?:usa?|u\.s(?:\.a)?\.?)(?![a-z])", f):
            countries.add("US")
    for code in pending_codes:
        if us_hint and code in _US_STATE_CODES:
            countries.add("US")
        elif code == "UK":
            countries.add("GB")
        elif code in ELIGIBLE or code in {"US", "GB", "CA", "IN", "BR", "MX", "AU", "SG", "IL", "UA", "AE"}:
            countries.add(code)
    return frozenset(countries), frozenset(regions), tuple(cities)


def single_country_remote(places: str, home_country: str | None = None) -> str | None:
    """The one EU/EEA country (not the user's) a remote job's place text limits it to ("France" → FR), as the
    matcher's residency flag reads it; None for the home country, a region (Europe, EMEA, anywhere), several
    countries or nothing."""
    countries, regions, _ = extract_places(places)
    if (home_country and home_country in countries) or regions & {"europe", "worldwide", "cet"}:
        return None
    eligible = countries & ELIGIBLE
    return next(iter(eligible)) if len(eligible) == 1 else None


# ---------------------------------------------------------------- work mode
def detect_work_mode(workplace: str | None, title: str, location: str, description: str) -> tuple[str, str]:
    if workplace in ("remote", "hybrid", "onsite"):
        return workplace, "structured"
    head = fold(f"{title} | {location}")
    if _HYBRID.search(head):
        return "hybrid", "title/location"
    if _REMOTE_LOC.search(head):
        return "remote", "title/location"
    desc = fold(description[:5000])
    if _HYBRID.search(desc):
        return "hybrid", "description"
    if _REMOTE_STRONG.search(desc):
        return "remote", "description"
    if _ONSITE.search(desc) or _ONSITE_LOC.search(head):
        return "onsite", "description" if _ONSITE.search(desc) else "title/location"
    return "unknown", "none"


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(h))


def city_point(name: str) -> tuple[str, float, float] | None:
    """(country, lat, lon) of a city name from the gazetteer, or None."""
    hit = _city_index().get(fold(name).strip())
    if not hit or hit[2] is None:
        return None
    return hit[0], hit[2], hit[3]


@dataclass
class LocationProfile:
    """Where the user lives and which work setups they take (from profile.yaml via config.location_rules)."""

    home_country: str | None = None
    home_city: str = ""
    home_point: tuple[float, float] | None = None
    radius_km: float | None = 30.0         # hybrid/office kept within this distance of home; None = anywhere
    home_aliases: tuple[str, ...] = ()
    remote_eu: bool = True                 # remote limited to residents of another EU country is kept (flagged)
    remote: bool = True
    hybrid: bool = True
    onsite: bool = False
    relocate_to: frozenset[str] = frozenset()
    work_countries: frozenset[str] = frozenset()
    _home_rx: re.Pattern[str] | None = None

    @classmethod
    def from_config(cls, c: dict) -> "LocationProfile":
        home = (c.get("home_country") or "").upper() or None
        point = None
        if c.get("home_lat") is not None and c.get("home_lon") is not None:
            point = (float(c["home_lat"]), float(c["home_lon"]))
        elif c.get("home_city"):
            hit = city_point(c["home_city"])
            point = (hit[1], hit[2]) if hit else None
        radius = c.get("hybrid_radius_km", 30)
        names = [fold(n) for n in [c.get("home_city") or "", *(c.get("home_aliases") or [])] if n]
        work = frozenset(c.get("work_countries") or []) or (ELIGIBLE | ({home} if home else frozenset()))
        return cls(
            home_country=home, home_city=c.get("home_city") or "", home_point=point,
            radius_km=None if radius in (None, "", "any") else float(radius),
            home_aliases=tuple(names), remote_eu=bool(c.get("remote_eu", True)),
            remote=bool(c.get("remote_eu", True) or c.get("remote_country", True)),
            hybrid=bool(c.get("hybrid", True)), onsite=bool(c.get("onsite", False)),
            relocate_to=frozenset(c.get("relocate_to") or []), work_countries=frozenset(work),
            _home_rx=re.compile(r"(?<![a-z])(?:" + "|".join(re.escape(n) for n in names) + r")(?![a-z])") if names else None,
        )

    @property
    def eligible(self) -> frozenset[str]:
        return self.work_countries or ELIGIBLE

    @property
    def limits_hybrid(self) -> bool:
        return self.radius_km is not None and (self.home_point is not None or self._home_rx is not None)

    def names_home_area(self, text: str, cities: tuple[str, ...] | None = None) -> bool:
        """The text names the home city (or an alias) or a city within the radius."""
        f = fold(text)
        if self._home_rx and self._home_rx.search(f):
            return True
        if self.home_point is None or self.radius_km is None:
            return False
        for c in cities if cities is not None else extract_places(text)[2]:
            hit = city_point(c)
            if hit and haversine_km(self.home_point, (hit[1], hit[2])) <= self.radius_km:
                return True
        return False


DEFAULT_PROFILE = LocationProfile()


def classify_location(
    *,
    workplace: str | None,
    title: str,
    location_raw: str,
    remote_scope: str | None,
    countries_structured: list[str],
    description: str,
    default_country: str | None = None,
    default_workplace: str | None = None,
    profile: LocationProfile | None = None,
) -> LocationResult:
    """Sort a posting into remote / home / elsewhere for this user, or drop it with a reason.

    A hybrid (or accepted on-site) job named only outside the user's area is dropped when the evidence is explicit
    (structured field, title/location, or a hybrid work-arrangement phrase in the text) and the text offers no
    remote option; unnamed places and unstated modes are kept."""
    prof = profile or DEFAULT_PROFILE
    home = prof.home_country
    mode, mode_src = detect_work_mode(workplace or None, title, location_raw, description)
    if mode == "unknown" and default_workplace:
        mode, mode_src = default_workplace, "source default"
    loc_countries, regions, cities = extract_places(f"{location_raw} ; {remote_scope or ''}")
    countries = {c.upper() for c in countries_structured if c} | loc_countries
    if "GB" in countries and "UK" in countries:
        countries.discard("UK")
    if not countries and default_country and mode != "remote":
        countries.add(default_country)
    evidence = {"mode_source": mode_src, "cities": list(cities[:5])}
    desc_f = fold(description[:8000])
    us_auth = bool(_US_AUTH.search(desc_f))
    eligible = countries & prof.eligible
    res = LocationResult(section=None, work_mode=mode, countries=sorted(countries), regions=sorted(regions),
                         evidence=evidence)

    if mode == "remote":
        if not prof.remote:
            res.drop_reason = "remote (you asked for hybrid/office jobs only)"
            return res
        ok_region = regions & {"europe", "worldwide", "cet"}
        bad_region = regions & {"americas", "apac", "us_only", "mena"}
        if ok_region == {"worldwide"} and ((countries and not eligible) or bad_region):
            ok_region = set()      # "Anywhere in the World, United States", "Brazil; International", "Anywhere in LATAM"
        if regions & {"mena"} and not regions & {"europe"} and not eligible:
            res.drop_reason = "remote region not EU (mena)"
            return res
        if (home and home in countries) or len(eligible) >= 2 or (ok_region and "us_only" not in regions):
            if ok_region and not countries and us_auth:
                res.drop_reason = "remote: US work authorization required"
                return res
            res.section = "remote"
            if not eligible and "worldwide" in ok_region:
                res.flags.append("worldwide")
            return res
        if len(eligible) == 1:
            c = next(iter(eligible))
            if not prof.remote_eu:
                res.drop_reason = f"remote for residents of {c} only"
                return res
            res.section = "elsewhere"
            res.flags.append(f"residency:{c}")
            return res
        if countries:
            res.drop_reason = f"remote outside EU ({','.join(sorted(countries))[:30]})"
            return res
        if bad_region:
            res.drop_reason = f"remote region not EU ({','.join(sorted(bad_region))})"
            return res
        if us_auth:
            res.drop_reason = "remote: US work authorization required"
            return res
        res.section = "remote"
        res.flags.append("scope?")
        return res

    if mode == "onsite" and not prof.onsite:
        res.drop_reason = "on-site only"
        return res
    if mode == "hybrid" and not (prof.hybrid or prof.onsite):
        if _REMOTE_OPTION.search(desc_f):
            res.flags.append("remote option?")
        else:
            res.drop_reason = "hybrid (you asked for remote jobs only)"
            return res
    if mode in ("hybrid", "onsite") and prof.limits_hybrid and "remote option?" not in res.flags:
        place_text = f"{title} | {location_raw} | {remote_scope or ''}"
        if prof.names_home_area(place_text, cities):
            pass
        elif _REMOTE_OPTION.search(desc_f):
            res.flags.append("remote option?")
            evidence["remote_option"] = _REMOTE_OPTION.search(desc_f).group(0)
        elif mode_src == "description" and not _HYBRID_WORK.search(desc_f):
            pass                                  # "hybrid" in the text may not be the work setup
        elif countries & prof.relocate_to:
            res.flags.append("relocation")
        elif cities or (countries and home not in countries):
            where = ", ".join(cities[:2]) or ",".join(sorted(countries))[:30]
            area = prof.home_city or "your area"
            res.drop_reason = f"{mode} outside {area} ({where})"
            return res
    if mode == "unknown":
        res.flags.append("mode?")
    if home and home in countries:
        res.section = "home"
        return res
    if eligible:
        res.section = "elsewhere"
        return res
    if countries:
        res.drop_reason = f"location outside your work countries ({','.join(sorted(countries))[:30]})"
        return res
    if regions & {"europe"}:
        res.section = "elsewhere"
        res.flags.append("location?")
        return res
    res.drop_reason = "location unknown"
    return res
