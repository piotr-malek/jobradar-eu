"""Remote-or-near-home rules (test profile: Wrocław, hybrid within 40 km): the screen's reading of "Remote · <place>",
the one-country fit cap, Jobgether's listing text, and dropping a no-mode copy of a job listed elsewhere as hybrid."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fakes import FakeLLM, screen_reply
from jobradar.apply.screen import DESC_CHARS, screen_jobs, system_prompt
from jobradar.llm.client import LLMClient

NOW = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)
EN = "We are looking for you to join our team and work with the data stack. You will build pipelines with SQL."


def _llm(reply, tmp_path) -> LLMClient:
    return LLMClient("screen", provider="mistral", transport=FakeLLM(reply).transport(), sleep=False,
                     ledger_dir=tmp_path)


def _screen(tmp_path, location: str, fit: int = 5, setup: str = "elsewhere") -> dict:
    job = {"job_id": "JR-1", "title": "Analytics Engineer", "company": "C", "location": location, "description": EN * 6}
    return screen_jobs([job], _llm(screen_reply(fit, (setup,)), tmp_path))["JR-1"]


def test_remote_label_is_remote_whatever_the_model_says(tmp_path):
    out = _screen(tmp_path, "Remote · Warszawa, PL")
    assert (out["fit"], out["work_setup"]) == (5, "remote")
    assert _screen(tmp_path, "Remote · Warszawa; Wrocław", setup="home")["work_setup"] == "home"
    assert (_screen(tmp_path, "Warszawa")["fit"], _screen(tmp_path, "Warszawa")["work_setup"]) == (4, "elsewhere")
    assert _screen(tmp_path, "Hybrid · Oława", setup="elsewhere")["work_setup"] == "home"     # 25 km from home
    assert "\"Remote · <place>\" is a remote job" in system_prompt()
    assert "Wrocław (or a place within 40 km of it)" in system_prompt()


def test_remote_for_one_other_eu_country_tops_out_at_4(tmp_path):
    for loc in ("Remote · France", "Remote · Lisboa", "Remote · Berlin, Germany; London, United Kingdom"):
        assert _screen(tmp_path, loc)["fit"] == 4, loc
    for loc in ("Remote · Poland", "Remote · Europe, European Union, EMEA, Spain", "Remote · Anywhere",
                "Remote · Germany, Portugal", "Remote · Homeoffice"):
        assert _screen(tmp_path, loc)["fit"] == 5, loc
    assert _screen(tmp_path, "Remote · Spain", fit=2)["fit"] == 2


def test_title_only_jobs_top_out_at_4(tmp_path):
    short = {"job_id": "JR-1", "title": "Analytics Engineer", "company": "C", "location": "Remote · Europe",
             "description": "Jobgether listing; the full posting text is not available. Work: Full Remote."}
    out = screen_jobs([short], _llm(screen_reply(5, ("remote",)), tmp_path))["JR-1"]
    assert out["fit"] == 4
    full = {**short, "description": EN * 6}
    assert screen_jobs([full], _llm(screen_reply(5, ("remote",)), tmp_path))["JR-1"]["fit"] == 5


def test_unlabelled_place_with_no_remote_word_counts_as_elsewhere(tmp_path):
    """Stripe "Dublin; Ireland Locations": no mode on any board, the text never says remote → a point off."""
    for setup in ("unclear", "remote", ""):
        out = _screen(tmp_path, "Dublin; Ireland Locations", setup=setup or "hybrid")
        assert (out["fit"], out["work_setup"]) == (4, "elsewhere"), setup
    for loc in ("Barcelona, Spain", "Warszawa"):
        assert _screen(tmp_path, loc, setup="unclear")["fit"] == 4, loc
    for loc in ("Poland", "Wrocław, Poland", "Remote · Europe", "", "Bulgaria; Romania; Poland"):  # Poland may be home
        assert _screen(tmp_path, loc, setup="unclear")["fit"] == 5, loc
    job = {"job_id": "JR-1", "title": "AE", "company": "C", "location": "Dublin, Ireland",
           "description": EN * 6 + " Remote work is possible."}               # remote in the text: the model decides
    out = screen_jobs([job], _llm(screen_reply(5, ("unclear",)), tmp_path))["JR-1"]
    assert (out["fit"], out["work_setup"]) == (5, "unclear")


def test_work_arrangement_past_the_clip_reaches_the_model(tmp_path):
    fake = FakeLLM(screen_reply(5, ("elsewhere",)))
    llm = LLMClient("screen", provider="mistral", transport=fake.transport(), sleep=False, ledger_dir=tmp_path)
    tail = ("In-office expectations. Stripes are expected to spend at least 50% of the time in their local office. "
            "We value curiosity.")
    job = {"job_id": "JR-1", "title": "AE", "company": "C", "location": "Dublin", "description": EN * 60 + " " + tail}
    assert len(job["description"]) > DESC_CHARS
    screen_jobs([job], llm)
    prompt = fake.payloads[-1]["messages"][-1]["content"]
    assert ("Work arrangement (later in the posting): In-office expectations. Stripes are expected to spend at least "
            "50% of the time in their local office.\n") in prompt or prompt.endswith("in their local office.")
    assert "We value curiosity" not in prompt
    short = {**job, "description": EN * 6}
    screen_jobs([short], llm)
    assert "Work arrangement" not in fake.payloads[-1]["messages"][-1]["content"]


def test_screen_profile_states_level_and_no_relocation():
    from jobradar.apply.profile import profile_text

    text = profile_text()
    assert "\nLevel: Mid-level analytics engineer" in text and "not relocating" in text
    assert "hybrid/on-site within 40 km of home" in text
    assert "Polish" in system_prompt() and "German" not in system_prompt()
    assert "Level:" not in profile_text({"summary": "x"})


def test_jobgether_postings_carry_the_listing_fields_as_text():
    from jobradar.http.client import PoliteClient, ReplayTransport
    from jobradar.sources.base import FetchContext, SourceSpec
    from jobradar.sources.boards.jobgether import listing_text
    from jobradar.sources.registry import get_adapter

    spec = SourceSpec(id="jobgether", adapter="jobgether",
                      params={"locations": ["europe", "poland"], "keywords": ["data engineer"], "max_pages": 1})
    fix = Path(__file__).parents[1] / "fixtures" / "jobgether"
    with PoliteClient(spec.id, transport=ReplayTransport(fix), sleep=False, max_requests=50) as http:
        posts = list(get_adapter(spec.adapter)(FetchContext(spec=spec, http=http)))
    assert posts and all(p.description.startswith("Jobgether listing;") and "Work: Full Remote." in p.description
                         for p in posts)
    assert listing_text({"remote": "Full Remote", "location": "Poland, Spain", "experience": "Mid-level (2-5 years)",
                         "jobFunctions": ["Data Engineer"], "experience_ignored": "x"}) == (
        "Jobgether listing; the full posting text is not available. Work: Full Remote. Locations: Poland, Spain. "
        "Experience: Mid-level (2-5 years). Job functions: Data Engineer.")
    assert listing_text({}) == ""


def _version(pk: str, location: str, workplace: str | None, description: str = EN) -> dict:
    return {"posting_key": pk, "source": "justjoin", "source_job_id": pk, "version_hash": f"h{pk}", "run_id": "r",
            "observed_at": (NOW - timedelta(days=1)).isoformat(), "title": "Data Engineer", "company": "Acme",
            "url": f"https://x/{pk}", "location_raw": location, "countries": [], "workplace": workplace,
            "description": description, "posted_at": (NOW - timedelta(days=2)).isoformat()}


def test_delivery_drops_a_no_mode_copy_of_a_hybrid_job_elsewhere(tmp_path):
    from jobradar.match.engine import MatchEngine
    from jobradar.models import Posting
    from jobradar.pipeline.run import process_postings
    from jobradar.pipeline.shortlist import select_items
    from jobradar.sources.base import SourceSpec
    from jobradar.store.sqlite import SQLiteStore

    store = SQLiteStore(tmp_path / "state.db")
    engine = MatchEngine.load()
    now = datetime.now(timezone.utc)

    def post(source, jid, company, title, location, workplace):
        return Posting(source=source, source_job_id=jid, title=title, company=company, url=f"https://x/{source}/{jid}",
                       location_raw=location, workplace=workplace, description=EN, posted_at=now - timedelta(days=1))

    for source, posts in (
            ("justjoin", [post("justjoin", "1", "Acme", "Data Engineer", "Warszawa, PL", "hybrid"),
                          post("justjoin", "2", "Beta", "Data Engineer", "Warszawa, PL", "hybrid"),
                          post("justjoin", "3", "Delta", "Data Engineer", "Barcelona", "onsite")]),
            ("jooble", [post("jooble", "1", "Acme", "Data Engineer", "Warszawa", None),        # same job, no mode
                        post("jooble", "2", "Beta", "Data Engineer", "Wrocław", None),         # names home: kept
                        post("jooble", "3", "Gamma", "Data Engineer", "Gdańsk", None),         # no hybrid sibling
                        post("jooble", "4", "Delta", "Data Engineer", "Barcelona, Spain", None)])):  # on-site sibling
        v, s, m, _ = process_postings(posts, spec=SourceSpec(id=source, adapter=source), engine=engine, known={},
                                      run_id=source)
        for t, rows in (("posting_versions", v), ("posting_sightings", s), ("matches", m)):
            store.append(t, rows)
    items, stats = select_items(store, engine, since=now - timedelta(days=30), now=now)
    assert sorted(it.company for it in items) == ["Beta", "Gamma"] and stats["elsewhere_copies"] == 2
