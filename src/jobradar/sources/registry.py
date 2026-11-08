"""Load the source registry (package defaults/sources.yaml + user/sources.yaml) and import every adapter module.

Search parameters in sources.yaml come from the active role packs and the profile, through placeholders that
are expanded when the specs load:

* ``$terms``            every search term of the active packs (English first, then the source's ``langs``)
* ``$terms:N``          the first N of them
* ``$terms_en``         English terms only (``$terms_en:N`` likewise)
* ``$board:<b>.<key>``  a board map value from the packs, e.g. ``$board:nofluffjobs.categories``
* ``$prefer``           slug patterns of the packs (``prefer:`` in each pack), for sitemap title gates
* ``{city}``/``{country}`` inside a string: the home city slug / ISO country code from profile.yaml
* ``$countries`` (``$countries_lower``) the user's countries as ISO codes, home country first; ``$country_names``
  their English names; ``$country_slugs`` the names lowercased with hyphens; ``$country_langs`` their languages
  (also accepted as an entry's ``langs``); ``$alert_senders`` the job-alert senders from user/sources.yaml
* ``default_country: "{country}"`` the home country
* ``{"$each": <list or placeholder>, "as": "x", "template": "...{x}..."}``  one string per item

A list item that expands to a list is spliced in (``queries: [$terms, "extra"]``).

An entry with ``each_country: [codes]`` is a template for one source per country (id ``<id>_<cc>``): ``{CC}``,
``{cc}`` and ``{country_name}`` in its strings become that country, ``countries``/``default_country`` are set to it,
``langs`` to its languages, and a ``{city}`` list item is kept only for the home country.

A source runs for a user when it is enabled and covers one of the user's countries (``countries`` in the entry;
``EU`` = pan-European or remote boards, always on), its ``opt_in`` track is switched on in user/sources.yaml, and
the user did not switch it off, and the setup mode is one of its ``modes`` (if any). ``user/sources.yaml: enable/disable`` override that; ``overrides: {id: {...}}``
deep-merges into one entry.
"""

from __future__ import annotations

import copy
import importlib
import pkgutil
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from jobradar import config
from jobradar.settings import PACKAGE_DIR
from jobradar.sources.base import ADAPTERS, AdapterFn, SourceSpec

PAN_EU = {"EU", "REMOTE", "WORLD"}


def _import_adapters() -> None:
    import jobradar.sources as pkg

    for sub in ("boards", "generic", "ats"):
        mod = importlib.import_module(f"{pkg.__name__}.{sub}")
        for info in pkgutil.iter_modules(mod.__path__):
            importlib.import_module(f"{mod.__name__}.{info.name}")


def _slug(text: str) -> str:
    from jobradar.normalize.text import fold

    return re.sub(r"[^a-z0-9]+", "-", fold(text or "")).strip("-")


def user_countries() -> list[str]:
    """The countries the user searches: home country first, then user/sources.yaml ``countries``."""
    home = ((config.profile().get("location") or {}).get("country") or "").upper()
    picked = [str(c).upper() for c in config.user_sources().get("countries") or []]
    return [c for c in dict.fromkeys([home, *picked]) if c]


def country_langs(countries: list[str]) -> list[str]:
    from jobradar.normalize.location import COUNTRY_LANGS

    return list(dict.fromkeys(lang for c in countries for lang in COUNTRY_LANGS.get(c, [])))


class Expander:
    def __init__(self, langs: list[str] | None = None):
        self.langs = langs or []
        loc = (config.profile().get("location") or {})
        self.city = _slug(loc.get("city") or "")
        self.country = (loc.get("country") or "").upper()
        self.countries = user_countries()

    def _token(self, s: str) -> Any:
        m = re.fullmatch(r"\$terms(_en)?(?::(\d+))?", s)
        if m:
            terms = config.search_terms([] if m.group(1) else self.langs)
            return terms[: int(m.group(2))] if m.group(2) else terms
        if s == "$prefer":            # tier i of every pack joined: strong slug words first, broad ones next
            tiers: list[list[str]] = []
            for pack in config.load_packs():
                for i, pat in enumerate(pack.get("prefer") or []):
                    while len(tiers) <= i:
                        tiers.append([])
                    tiers[i].append(pat)
            return ["|".join(t) for t in tiers]
        m = re.fullmatch(r"\$board:([\w-]+)\.(\w+)", s)
        if m:
            return copy.deepcopy(config.board_map(m.group(1)).get(m.group(2), []))
        if s in ("$countries", "$countries_lower", "$country_names", "$country_slugs", "$country_langs"):
            from jobradar.normalize.location import country_names

            names = country_names()
            if s == "$countries":
                return list(self.countries)
            if s == "$countries_lower":
                return [c.lower() for c in self.countries]
            if s == "$country_langs":
                return country_langs(self.countries)
            full = [names.get(c, c) for c in self.countries]
            return full if s == "$country_names" else [_slug(n) for n in full]
        if s == "$alert_senders":
            return list(config.user_sources().get("alert_senders") or [])
        if s == "$city":
            return self.city
        if s == "$country":
            return self.country
        return s.replace("{city}", self.city).replace("{country}", self.country) if "{" in s else s

    def expand(self, value: Any) -> Any:
        if isinstance(value, str):
            return self._token(value) if (value.startswith("$") or "{city}" in value or "{country}" in value) else value
        if isinstance(value, dict):
            if "$each" in value:
                items = self.expand(value["$each"])
                items = items if isinstance(items, list) else [items]
                name, tpl = value.get("as", "x"), value["template"]
                return [tpl.replace("{" + name + "}", str(i)).replace("{city}", self.city)
                        .replace("{country}", self.country) for i in items]
            return {k: self.expand(v) for k, v in value.items()}
        if isinstance(value, list):
            out: list[Any] = []
            for v in value:
                e = self.expand(v)
                if isinstance(v, (str, dict)) and isinstance(e, list) and (
                        isinstance(v, dict) and "$each" in v or isinstance(v, str) and v.startswith("$")):
                    out += [x for x in e if x not in out]
                else:
                    out.append(e)
            return out
        return value


def _raw_entries(path: Path | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    p = path or PACKAGE_DIR / "sources.yaml"
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return data.get("defaults", {}), data.get("sources", [])


def selected(spec: SourceSpec, user: dict[str, Any] | None = None) -> bool:
    """Does this source run for the user (countries, opt-in tracks, enable/disable)?"""
    user = config.user_sources() if user is None else user
    if spec.id in (user.get("disable") or []):
        return False
    if spec.id in (user.get("enable") or []):
        return True
    if not spec.enabled:
        return False
    if spec.opt_in and spec.opt_in not in (user.get("opt_in") or []):
        return False
    if spec.packs and not set(spec.packs) & set(config.active_pack_names()):
        return False
    if spec.modes and config.mode() not in spec.modes:
        return False
    wanted = {c.upper() for c in user.get("countries") or []}
    covers = {c.upper() for c in spec.countries or ["EU"]}
    if not wanted:
        return True
    return bool(covers & PAN_EU) or bool(covers & wanted)


def per_country(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """An ``each_country`` template → one entry per country; any other entry unchanged."""
    codes = entry.get("each_country")
    if not codes:
        return [entry]
    from jobradar.normalize.location import COUNTRY_LANGS, country_names

    home = ((config.profile().get("location") or {}).get("country") or "").upper()
    names = country_names()
    out = []
    for cc in (str(c).upper() for c in codes):
        def sub(v: Any) -> Any:
            if isinstance(v, str):
                return v.replace("{CC}", cc).replace("{cc}", cc.lower()).replace("{country_name}", names.get(cc, cc))
            if isinstance(v, list):
                return [sub(x) for x in v if cc == home or x != "{city}"]
            if isinstance(v, dict):
                return {k: sub(x) for k, x in v.items()}
            return v

        e = sub({k: v for k, v in entry.items() if k != "each_country"})
        e.update(id=f"{entry['id']}_{cc.lower()}", countries=[cc], default_country=cc,
                 langs=list(COUNTRY_LANGS.get(cc, [])))
        out.append(e)
    return out


@lru_cache(maxsize=4)
def load_specs(path: str | None = None, *, all_sources: bool = False) -> dict[str, SourceSpec]:
    """Specs for the user's sources (``all_sources=True``: every entry, e.g. for `jr sources --all` and tests)."""
    defaults, raw = _raw_entries(Path(path) if path else None)
    entries = [e for entry in raw for e in per_country(entry)]
    user = config.user_sources()
    overrides = user.get("overrides") or {}
    specs: dict[str, SourceSpec] = {}
    for entry in entries:
        merged = {**defaults, **entry}
        merged["politeness"] = {**defaults.get("politeness", {}), **entry.get("politeness", {})}
        if entry["id"] in overrides:
            merged = config.deep_merge(merged, overrides[entry["id"]])
        if merged.get("default_country") == "{country}":
            merged["default_country"] = ((config.profile().get("location") or {}).get("country") or "").upper() or None
        if "$country_langs" in (merged.get("langs") or []):
            merged["langs"] = country_langs(user_countries())
        merged["params"] = Expander(merged.get("langs")).expand(merged.get("params") or {})
        spec = SourceSpec.from_dict(merged)
        if spec.id in specs:
            raise ValueError(f"duplicate source id {spec.id}")
        if all_sources or selected(spec, user):
            specs[spec.id] = spec
    return specs


def spec_for(source_id: str) -> SourceSpec:
    """One source's spec whether or not the user selected it (`jr probe`, ATS ids are not in the registry)."""
    specs = load_specs(all_sources=True)
    return specs[source_id]


def get_adapter(name: str) -> AdapterFn:
    if name not in ADAPTERS:
        _import_adapters()
    try:
        return ADAPTERS[name]
    except KeyError as exc:
        raise KeyError(f"unknown adapter {name!r}; known: {sorted(ADAPTERS)}") from exc


def all_adapters() -> dict[str, AdapterFn]:
    _import_adapters()
    return dict(ADAPTERS)
