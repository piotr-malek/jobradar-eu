import re
from email.message import EmailMessage
from pathlib import Path

from jobradar.sources.boards.email_alerts import DEFAULT_LINKS, parse_alert, parse_salary

FIX = Path(__file__).parents[1] / "fixtures" / "email_alerts"


def _msg(html: str) -> EmailMessage:
    m = EmailMessage()
    m["Subject"], m["Date"] = "Nowe oferty dla Ciebie", "Sat, 24 Oct 2026 07:00:00 +0200"
    m.set_content("plain")
    m.add_alternative(html, subtype="html")
    return m


def _parse(board: str, name: str) -> list:
    html = (FIX / f"{name}.html").read_text(encoding="utf-8")
    return parse_alert("email_alerts", board, _msg(html), re.compile(DEFAULT_LINKS[board]))


def test_parse_alert_turns_offer_links_into_postings():
    html = """<a href="https://www.pracuj.pl/praca/analytics-engineer-wroclaw,oferta,1004321">Analytics Engineer</a>
              <a href="https://www.pracuj.pl/praca/analytics-engineer-wroclaw,oferta,1004321"><img src="x"></a>
              <a href="https://www.pracuj.pl/praca/data-engineer,oferta,1004322">Data Engineer</a>
              <a href="https://www.pracuj.pl/ustawienia">Zmień ustawienia</a>"""
    posts = parse_alert("email_alerts", "pracuj", _msg(html), re.compile(DEFAULT_LINKS["pracuj"]))
    assert [(p.source_job_id, p.title) for p in posts] == [("pracuj:1004321", "Analytics Engineer"),
                                                          ("pracuj:1004322", "Data Engineer")]
    assert posts[0].posted_at.year == 2026 and posts[0].tags == ["alert:pracuj"]


def test_pracuj_saved_search_alert():
    posts = _parse("pracuj", "pracuj_jobalert")
    assert len(posts) == 11 and all(p.title and p.company and p.location_raw for p in posts)
    assert all(not p.title.startswith("!") and "​" not in p.title for p in posts)  # "!" icon span, zero-width space
    assert all(p.url.startswith("https://www.pracuj.pl/praca/") and "?" not in p.url for p in posts)
    first = posts[0]
    assert (first.source_job_id, first.title, first.company, first.location_raw) == (
        "pracuj:1000000001", "Lead Quality Engineer (Data)", "Northwind Retail", "Wrocław, Krzyki")
    assert (first.salary_min, first.salary_max, first.salary_currency, first.salary_period) == (23000, 30000, "PLN", "month")
    b2b = next(p for p in posts if p.source_job_id == "pracuj:1000000005")
    assert (b2b.salary_min, b2b.salary_max, b2b.salary_period, b2b.tags) == (120, 135, "hour", ["alert:pracuj", "b2b"])
    assert any(p.salary_min is None for p in posts)  # offers without a salary keep the company


def test_pracuj_recommendations():
    posts = _parse("pracuj", "pracuj_rekomendacje")
    assert len(posts) == 5 and all(p.company for p in posts)
    etl = next(p for p in posts if p.source_job_id == "pracuj:1000000103")  # no salary: company is the second link
    assert (etl.company, etl.location_raw, etl.salary_min) == ("Goldfield ITC sp. z o.o.", "Łódź (Śródmieście)", None)


def test_theprotocol_alert():
    posts = _parse("theprotocol", "theprotocol_jobalert")
    assert len(posts) == 8 and all(p.url.startswith("https://theprotocol.it/szczegoly/praca/") for p in posts)
    first = posts[0]
    assert (first.title, first.company, first.location_raw) == ("Data Engineer", "Northwind Retail", "Wrocław, Krzyki")
    assert (first.salary_min, first.salary_max, first.salary_period) == (None, 26000, "month")
    assert {p.company for p in posts} >= {"Medius Poland Sp. z o.o", "OPTIVEUM SPÓŁKA Z OGRANICZONĄ ODPOWIEDZIALNOŚCIĄ"}


def test_parse_salary():
    assert parse_salary("15 000-22 000 zł brutto / mies.") == (15000, 22000, "PLN", "month")
    assert parse_salary("110-120 zł netto (+ VAT) / godz.") == (110, 120, "PLN", "hour")
    assert parse_salary("max 165 zł / hr.") == (None, 165, "PLN", "hour")
    assert parse_salary("12700-21420 zł / mies. (zal. od umowy)") == (12700, 21420, "PLN", "month")
    assert parse_salary("Northwind Retail") == (None, None, None, None)


def test_linkedin_alert():
    """Forwarded "alert created" e-mail (the daily ones share the job card); synthetic fixture."""
    posts = _parse("linkedin", "linkedin_jobalert")
    assert len(posts) == 6
    assert all(p.url == f"https://www.linkedin.com/jobs/view/{p.source_job_id.split(':')[1]}/" for p in posts)
    first = posts[0]
    assert (first.source_job_id, first.title, first.company, first.location_raw) == (
        "linkedin:1000000001", "Data Engineer ID10001", "Acme Engineering", "Wroclaw, Dolnośląskie, Poland")
    assert first.tags == ["alert:linkedin"] and first.workplace is None
    assert posts[-1].title == "Data Engineer - BigData Development" and "easy_apply" in posts[-1].tags
    assert not any(t.startswith(("Actively", "1 connection")) for p in posts for t in (p.company, p.location_raw))


def test_linkedin_single_card_with_work_mode():
    """One job only (block reaches the whole e-mail): title + company line come from the link, not the block."""
    html = """<table><tr><td>Your job alert has been created:</td></tr>
      <tr><td><a href="https://www.linkedin.com/comm/jobs/view/123456789?otpToken=SECRET&trk=x"><img src="l.png"></a></td>
          <td><a href="https://www.linkedin.com/comm/jobs/view/123456789?otpToken=SECRET">GTM Engineer</a>
              <p>CaptivateIQ · European Union (Remote)</p><p>Actively recruiting</p></td></tr></table>"""
    [p] = parse_alert("email_alerts", "linkedin", _msg(html), re.compile(DEFAULT_LINKS["linkedin"]))
    assert (p.title, p.company, p.location_raw, p.workplace) == ("GTM Engineer", "CaptivateIQ", "European Union", "remote")
    assert p.url == "https://www.linkedin.com/jobs/view/123456789/" and "SECRET" not in repr(p)
