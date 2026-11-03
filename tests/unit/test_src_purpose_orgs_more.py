"""ESA, ICRC, UNIDO: SuccessFactors Career Site Builder sites read by the shared sf_csb adapter (fixtures replayed)."""
from pathlib import Path

import pytest

from jobradar.http.client import PoliteClient, ReplayTransport
from jobradar.sources.base import FetchContext, SourceSpec
from jobradar.sources.registry import get_adapter

FIX = Path(__file__).parents[1] / "fixtures"
CASES = {"esa": "jobs.esa.int", "icrc": "careers.icrc.org", "unido": "careers.unido.org"}


def _run(sid):
    spec = SourceSpec(id=sid, adapter="sf_csb", params={"host": CASES[sid], "org": sid.upper(), "max_details": 6, "title_gate": False})
    with PoliteClient(sid, transport=ReplayTransport(FIX / sid), sleep=False, max_requests=40) as http:
        ctx = FetchContext(spec=spec, http=http)
        return list(get_adapter("sf_csb")(ctx)), ctx


@pytest.mark.parametrize("sid", list(CASES))
def test_parse(sid):
    posts, ctx = _run(sid)
    assert len(posts) >= 4
    assert all(p.title and p.company == sid.upper() and p.description and "<" not in p.description for p in posts)
    assert all(p.url.startswith(f"https://{CASES[sid]}/job/") and p.location_raw for p in posts)
    assert not any("internship" in p.title.lower() for p in posts)
