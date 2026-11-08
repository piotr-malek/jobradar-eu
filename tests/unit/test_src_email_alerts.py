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


# ------------------------------------------------------------------ any sender (generic parser)
def _gate(title: str) -> bool:
    return bool(re.search(r"\b(data|analytics)\b", title, re.I))


def test_generic_alert_reads_title_company_place_and_salary():
    from jobradar.sources.boards.email_alerts import parse_generic

    html = (FIX / "generic_cards.html").read_text(encoding="utf-8")
    posts = parse_generic("email_alerts", "Jobagent <jobagent@example.org>", _msg(html), _gate)
    assert [(p.title, p.company, p.location_raw) for p in posts] == [
        ("Data Engineer (m/w/d)", "Donaufeld Analytics GmbH", "Wien"),
        ("Analytics Engineer", "Murinsel Software", "Graz, Steiermark")]
    first = posts[0]
    assert (first.salary_min, first.salary_max, first.salary_currency) == (55000, 70000, "EUR")
    assert "utm_" not in first.url and first.url.endswith("1234567-inline.html")
    assert first.source_job_id.startswith("example.org:") and first.tags == ["alert:example.org"]


def test_generic_alert_ids_are_stable_across_mails():
    from jobradar.sources.boards.email_alerts import parse_generic

    html = (FIX / "generic_cards.html").read_text(encoding="utf-8")
    a = parse_generic("email_alerts", "jobagent@example.org", _msg(html), _gate)
    b = parse_generic("email_alerts", "jobagent@example.org", _msg(html.replace("utm_campaign=jobagent", "x=1")), _gate)
    assert [p.source_job_id for p in a] == [p.source_job_id for p in b]


def test_generic_plain_text_alert():
    from jobradar.sources.boards.email_alerts import parse_generic

    m = EmailMessage()
    m["Subject"], m["Date"] = "New jobs", "Sat, 07 Nov 2026 07:00:00 +0100"
    m.set_content("Your alert\nSenior Data Engineer\nhttps://jobs.example.bg/offer/991?utm_source=mail\n"
                  "Unsubscribe\nhttps://jobs.example.bg/unsubscribe")
    posts = parse_generic("email_alerts", "alerts@example.net", m, _gate)
    assert [(p.title, p.url) for p in posts] == [("Senior Data Engineer", "https://jobs.example.bg/offer/991")]


class _FakeIMAP:
    def __init__(self, mails):
        self.mails, self.searched = mails, []

    def __call__(self, host):
        return self

    def login(self, user, password):
        pass

    def select(self, folder, readonly=False):
        assert readonly

    def search(self, charset, key, value, *rest):
        self.searched.append((key, value.strip('"')))
        hits = [n for n, (sender, _) in self.mails.items() if value.strip('"') in sender]
        return "OK", [b" ".join(hits)]

    def fetch(self, num, what):
        assert "PEEK" in what
        return "OK", [(b"1", self.mails[num][1].as_bytes())]

    def logout(self):
        pass


def test_adapter_reads_every_listed_sender(monkeypatch):
    from jobradar.sources.base import FetchContext, SourceSpec
    from jobradar.sources.boards import email_alerts as ea

    li = _msg((FIX / "linkedin_jobalert.html").read_text(encoding="utf-8"))
    generic = _msg((FIX / "generic_cards.html").read_text(encoding="utf-8"))
    fake = _FakeIMAP({b"1": ("jobalerts-noreply@linkedin.com", li), b"2": ("jobagent@example.org", generic)})
    monkeypatch.setattr(ea.imaplib, "IMAP4_SSL", fake)
    spec = SourceSpec(id="email_alerts", adapter="email_alerts",
                      params={"senders": ["LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>",
                                          "jobagent@example.org", "example.org"]})
    ctx = FetchContext(spec=spec, http=None, wants_detail=_gate, secrets={"JR_IMAP_USER": "u", "JR_IMAP_PASSWORD": "p"})
    posts = list(ea.email_alerts(ctx))
    assert fake.searched[0] == ("TEXT", "jobalerts-noreply@linkedin.com")           # forwarded LinkedIn mails too
    assert ("FROM", "jobagent@example.org") in fake.searched
    boards = {p.source_job_id.split(":")[0] for p in posts}
    assert boards == {"linkedin", "example.org"}
    assert sum(p.source_job_id.startswith("example.org") for p in posts) == 2   # mail 2 read once
