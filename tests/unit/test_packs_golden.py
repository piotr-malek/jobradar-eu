"""Every role pack's own golden titles: `golden.keep` match the pack alone, `golden.drop` don't.

Each pack is tested on its own with the default seniority range (junior..senior), no never words, and a remote EU
posting in English, so only the title decides."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import lru_cache

import pytest

from jobradar import config
from jobradar.match.engine import MatchEngine
from jobradar.models import Posting

NOW = datetime(2026, 10, 22, tzinfo=timezone.utc)
EN = "We are looking for you to join our team. You will work with our engineers and the wider team on our product."
PACKS = config.available_packs()
CASES = [(name, title, True) for name, p in PACKS.items() for title in (p.get("golden") or {}).get("keep", [])]
CASES += [(name, title, False) for name, p in PACKS.items() for title in (p.get("golden") or {}).get("drop", [])]


@lru_cache(maxsize=None)
def _engine(pack: str) -> MatchEngine:
    cfg = config.matching_config(r={"packs": [pack]}, packs=[pack])
    cfg["user_titles"] = {"never": [], "never_unless": [], "keep": [], "must_have": []}
    cfg["seniority_range"] = {"min": "junior", "max": "senior"}
    return MatchEngine.from_config(cfg)


@pytest.mark.parametrize("pack,title,keep", CASES, ids=[f"{c[0]}|{c[1]}" for c in CASES])
def test_pack_golden(pack, title, keep):
    p = Posting(source="t", source_job_id=title, title=title, company="Acme", url="https://x",
                location_raw="Remote - Europe", workplace="remote", description=EN, posted_at=NOW - timedelta(days=1))
    m = _engine(pack).match(p, now=NOW)
    if keep:
        assert m.decision == "match", (m.drop_reason, m.explain.get("title"))
    else:
        assert m.decision == "drop", (m.family, m.explain.get("title"))


@pytest.mark.parametrize("name", sorted(PACKS))
def test_pack_shape(name):
    p = PACKS[name]
    assert p.get("label") and p.get("description")
    assert p.get("families"), "a pack needs at least one family"
    assert p.get("search_terms", {}).get("en"), "English search terms are required"
    golden = p.get("golden") or {}
    assert len(golden.get("keep", [])) + len(golden.get("drop", [])) >= 30, "at least 30 golden titles"
    for fam in p["families"].values():
        assert fam.get("tier") in ("core", "adjacent")
        for pat in fam.get("any", []) + fam.get("context_any", []):
            assert config.safe_regex(pat), pat
