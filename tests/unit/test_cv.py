import pytest

from jobradar.apply.cv import CVError, render_html, resolve

SRC = {
    "name": "Ann Example",
    "contact": [["Town", "ann@example.com"]],
    "variants": {
        "core": {"headline": "Data", "summary": "S core", "skills": ["a", "b"], "projects": ["p1"],
                 "bullets": {"r1": ["y", "x"]}},
        "ai": {"headline": "AI", "summary": "S ai", "skills": ["b"], "projects": ["p1"]},
    },
    "skills": {"a": {"label": "A", "items": "one"}, "b": {"label": "B", "items": {"default": "two", "ai": "two for ai"}}},
    "experience": [{"company": "Co", "where": "", "roles": [
        {"id": "r1", "title": "Eng", "dates": "2024", "default_bullets": ["x"],
         "bullets": {"x": {"default": "x plain", "ai": "x for ai"}, "y": "y text"}}]}],
    "projects": {"p1": {"name": "P", "text": {"default": "p plain", "ai": "p ai"}}},
}


def test_variant_picks_orders_and_rewords():
    core = resolve(SRC, "core")
    assert [g["label"] for g in core["skills"]] == ["A", "B"]
    assert [g["items"] for g in core["skills"]] == ["one", "two"]
    assert core["experience"][0]["roles"][0]["bullets"] == ["y text", "x plain"]
    ai = resolve(SRC, "ai")
    assert [g["items"] for g in ai["skills"]] == ["two for ai"]
    assert ai["experience"][0]["roles"][0]["bullets"] == ["x for ai"]  # default_bullets, variant wording
    assert ai["projects"] == [{"name": "P", "text": "p ai"}]


def test_unknown_ids_fail_loudly():
    bad = {**SRC, "variants": {"core": {**SRC["variants"]["core"], "bullets": {"r1": ["nope"]}}}}
    with pytest.raises(CVError, match="nope"):
        resolve(bad, "core")
    with pytest.raises(CVError):
        resolve(SRC, "devrel")


def test_html_escapes_and_contains_content():
    html = render_html({**SRC, "name": "Ann <b>"}, "core")
    assert "Ann &lt;b&gt;" in html and "y text" in html and "ann@example.com" in html


PROFILE = {
    "cv_variants": {"default": "/cv/core.pdf", "core": "/cv/core.pdf", "ai": "/cv/ai.pdf", "gtm": "/cv/gtm.pdf",
                    "devrel": ""},
    "cv_title_keywords": {"devrel": ["devrel"], "ai": ["ai", "genai"], "gtm": ["marketing", "rev ?ops"]},
}


@pytest.mark.parametrize("family,title,variant,why", [
    ("gtm", "RevOps Engineer", "gtm", "job family gtm"),             # specialty family from the matcher wins
    ("core", "AI Data Engineer", "ai", "title mentions “ai”"),        # core title with a specialty keyword
    ("core", "Marketing Analytics Engineer", "gtm", "title mentions “marketing”"),
    ("core", "Data Engineer, Maintenance", "core", "job family core"),  # "ai" only as part of a word
    ("review", "Data Specialist", "core", "default"),                # review → default, named after its variant
    (None, "Senior Data Engineer, GenAI", "ai", "title mentions “genai”"),
    ("devrel", "DevRel Engineer", "core", "default"),                # devrel has no PDF yet → falls through
])
def test_cv_choice(family, title, variant, why):
    from jobradar.apply.profile import cv_choice

    c = cv_choice(family, title, PROFILE)
    assert (c["variant"], c["why"]) == (variant, why) and c["path"] == f"/cv/{variant}.pdf"


def test_cv_choice_without_config():
    from jobradar.apply.profile import cv_choice, cv_options

    assert cv_choice("core", "x", {"cv_variants": {}})["path"] == ""
    assert cv_options(PROFILE) == {"core": "/cv/core.pdf", "ai": "/cv/ai.pdf", "gtm": "/cv/gtm.pdf"}
