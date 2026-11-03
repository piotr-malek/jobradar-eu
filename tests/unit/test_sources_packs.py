"""The source registry is role- and city-free: packs + profile fill the search words, categories, slug tiers and city."""

import re
from pathlib import Path

from jobradar.settings import PACKAGE_DIR
from jobradar.sources.registry import load_specs, selected


def _leftovers(value, path=""):
    """Yield (path, string) for every placeholder that survived expansion."""
    if isinstance(value, str):
        if value.startswith("$") or "{city}" in value or "{country}" in value or "$each" in value:
            yield path, value
    elif isinstance(value, dict):
        for k, v in value.items():
            if k == "$each":
                yield path, k
            yield from _leftovers(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _leftovers(v, f"{path}[{i}]")


def test_every_entry_expands_without_leftovers():
    specs = load_specs(all_sources=True)
    assert len(specs) > 60
    bad = [(sid, p, s) for sid, spec in specs.items() for p, s in _leftovers(spec.params)]
    assert not bad, bad[:5]


def test_every_entry_has_countries_and_label():
    for sid, spec in load_specs(all_sources=True).items():
        assert spec.countries, sid
        assert spec.label, sid


def test_nofluffjobs_categories_and_city_slice():
    listings = load_specs(all_sources=True)["nofluffjobs"].params["listings"]
    assert any("category%3Ddata&" in u for u in listings)
    assert any("category%3Dbusiness-intelligence&" in u for u in listings)
    assert any("category%3Dartificial-intelligence&" in u for u in listings)
    assert any("city%3Dwroclaw" in u for u in listings)
    assert any("city%3Dremote" in u for u in listings)
    assert all(u.endswith("&page={page}") for u in listings)


def test_keyword_sources_carry_the_pack_terms():
    specs = load_specs(all_sources=True)
    for sid, key in (("jooble", "keywords"), ("himalayas", "queries"), ("wttj", "queries"), ("eures", "keywords")):
        kws = specs[sid].params[key]
        assert "data engineer" in kws and "analytics engineer" in kws, sid
    assert len(specs["himalayas"].params["queries"]) == 11
    assert len(specs["jooble"].params["keywords"]) == 10
    assert specs["jooble"].params["locations"] == ["Polska", "wroclaw"]
    assert specs["remotive"].params["categories"] == ["data"]
    assert "gtm engineer" in specs["remotive"].params["queries"] or len(specs["remotive"].params["queries"]) == 3


def test_local_language_boards_add_their_terms():
    kws = load_specs(all_sources=True)["jooble_de"].params["keywords"]
    assert kws[0] == "data engineer"
    assert load_specs(all_sources=True)["arbeitsagentur"].params["searches"][:2] == ["data engineer", "analytics engineer"]


def test_sitemap_prefer_tiers_and_boards():
    specs = load_specs(all_sources=True)
    for sid in ("bulldogjob", "solidjobs", "remoteleaf", "sii_pl", "barcelonajobs"):
        tiers = specs[sid].params["prefer"]
        assert any("analytics-engineer" in t for t in tiers), sid
    assert any(t.startswith("-data") for t in specs["justjoin"].params["prefer"])
    assert specs["rocketjobs"].params["prefer"][-1] == "marketing|crm|automation|revops|analytics"
    assert "https://www.michaelpage.pl/jobs/analytics-engineer" in specs["michaelpage_pl"].params["listing_urls"]
    roles = specs["remoterocketship"].params["roles"]
    assert "data-engineer" in roles and "revenue-operations" in roles and "machine-learning-engineer" in roles
    assert any("remote_in=PL&" in u for u in specs["getdataengineeringjobs"].params["listing_urls"])


def test_linkedin_apify_is_disabled():
    assert load_specs(all_sources=True)["linkedin_apify"].enabled is False
    assert "linkedin_apify" not in load_specs()


def test_selected_by_country():
    specs = load_specs(all_sources=True)
    user = {"countries": ["PT"]}
    picked = {sid for sid, s in specs.items() if selected(s, user)}
    assert {"itjobs_pt", "teamlyzer", "jooble_pt", "remotive", "himalayas", "eures"} <= picked
    assert not picked & {"justjoin", "nofluffjobs", "hays_pl", "michaelpage_pl", "sii_pl", "experis_pl", "jooble",
                         "jooble_de", "arbeitsagentur", "un_careers", "linkedin_apify"}


def test_opt_in_tracks():
    specs = load_specs(all_sources=True)
    for sid in ("hays_pl", "michaelpage_pl", "sii_pl", "experis_pl"):
        assert specs[sid].opt_in == "polish_only"
        assert not selected(specs[sid], {"countries": ["PL"]})
        assert selected(specs[sid], {"countries": ["PL"], "opt_in": ["polish_only"]})
    for sid, s in specs.items():
        if s.track == "purpose":
            assert s.opt_in == "purpose", sid
    assert selected(specs["un_careers"], {"opt_in": ["purpose"]})
    assert not selected(specs["un_careers"], {"countries": ["PT"]})


def test_yaml_text_has_no_role_or_city():
    text = (PACKAGE_DIR / "sources.yaml").read_text(encoding="utf-8")
    assert not re.search(r"analytics engineer|dbt|bigquery|category%3Ddata", text, re.I)


def test_sources_doc_is_current():
    """docs/sources.md is generated: re-run `jr sources --markdown > docs/sources.md` after editing sources.yaml."""
    from jobradar.sources.catalog import markdown

    doc = Path(__file__).parents[2] / "docs" / "sources.md"
    assert doc.read_text(encoding="utf-8") == markdown()
