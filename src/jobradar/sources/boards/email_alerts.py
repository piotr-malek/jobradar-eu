"""Job-alert e-mails read over IMAP — for boards we can't fetch (pracuj.pl, theprotocol.it: Cloudflare;
LinkedIn: robots.txt), LinkedIn's forwarded from another account to the same mailbox.

The boards send saved-search alerts to a mailbox; this adapter reads the last few days of those
messages (read-only: ``BODY.PEEK``, nothing is marked or moved) and turns each offer into a Posting.
An offer is a block of links to the same offer id: title, then salary (if shown), then company
(pracuj: location in ``span.city_text``; theprotocol: "Company - Location" in one link). There is no
description (detail pages are behind Cloudflare), so ``default_country: PL`` and the matcher's title
rules do the rest.

Secrets: ``JR_IMAP_USER`` and ``JR_IMAP_PASSWORD`` (a Gmail app password; IMAP must be on).
Params:
  host       IMAP host (default imap.gmail.com)
  folder     mailbox or Gmail label to read (default INBOX)
  days       look-back window (default 3)
  senders    {board: sender domain}; wysylka.pracuj.pl sends saved searches (jobalert@) and
             recommendations (rekomendacje@), while application-status mails (aplikacje.pracuj.pl) are left out
  links      {board: regex with a named group ``id``} matched against every href in the e-mail
             (checked on real alerts 2026-10-28; the links are direct, only utm/sendid query params)
"""

from __future__ import annotations

import email
import imaplib
import re
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.message import Message
from typing import Iterator

from bs4 import BeautifulSoup, Tag

from jobradar.models import Posting
from jobradar.sources.base import FetchContext, adapter, to_float

DEFAULT_SENDERS = {"pracuj": "wysylka.pracuj.pl", "theprotocol": "mailing.theprotocol.it",
                   "linkedin": "jobalerts-noreply@linkedin.com"}
DEFAULT_LINKS = {
    "pracuj": r"https?://(?:www\.)?pracuj\.pl/praca/[^\s\"'?#]*,oferta,(?P<id>\d+)",
    "theprotocol": r"https?://(?:www\.)?theprotocol\.it/szczegoly/praca/[^\s\"'?#]*,oferta,(?P<id>[0-9a-f-]+)",
    "linkedin": r"https?://(?:[a-z]+\.)?linkedin\.com/(?:comm/)?jobs/view/(?P<id>\d+)",
}
# Boards searched with IMAP TEXT (header + body) instead of FROM: LinkedIn alerts come from another
# account by Gmail auto-forward (original From kept) or by hand (From is the forwarder, the address
# is in the quoted header), and TEXT finds both.
DEFAULT_TEXT_SEARCH = ["linkedin"]
_LI_WORKPLACE = {"remote": "remote", "hybrid": "hybrid", "on-site": "onsite", "onsite": "onsite"}
_CURRENCY = {"zł": "PLN", "pln": "PLN", "€": "EUR", "eur": "EUR", "usd": "USD", "$": "USD", "chf": "CHF", "gbp": "GBP", "£": "GBP"}
_CUR_RX = re.compile(r"zł|\bpln\b|€|\beur\b|\busd\b|\$|\bchf\b|\bgbp\b|£", re.I)
_NUM = re.compile(r"\d{1,3}(?:[   ]\d{3})+|\d+")
_PERIOD = re.compile(r"/\s*(mies|mth|month|godz|hr|hour|h\b|rok|year|yr|dzie|day)", re.I)
_PERIODS = {"mies": "month", "mth": "month", "month": "month", "godz": "hour", "hr": "hour", "hour": "hour",
            "h": "hour", "rok": "year", "year": "year", "yr": "year", "dzie": "day", "day": "day"}


def parse_salary(text: str) -> tuple[float | None, float | None, str | None, str | None]:
    """'23 000-30 000 zł brutto / mies.' → (23000, 30000, 'PLN', 'month'); 'max 26 000 zł / mth.' → (None, 26000, ...)."""
    cur = _CUR_RX.search(text)
    nums = [to_float(re.sub(r"\D", "", n)) for n in _NUM.findall(text.split("(")[0])]
    if not cur or not nums:
        return None, None, None, None
    per = _PERIOD.search(text)
    period = _PERIODS[per.group(1).lower()] if per else None
    lo, hi = nums[0], nums[1] if len(nums) > 1 else None
    if hi is None and text.strip().lower().startswith(("max", "do ", "up to")):
        lo, hi = None, lo
    return lo, hi, _CURRENCY[cur.group(0).lower()], period


def _clean(text: str) -> str:
    """Link text without zero-width characters, pracuj's "!" icon and doubled spaces."""
    text = re.sub(r"[​-‍﻿]", "", text)
    return " ".join(re.sub(r"^!\s*", "", text.strip()).split())


def _offer_block(a: Tag, oid: str, link_rx: re.Pattern[str]) -> Tag:
    """Largest ancestor of ``a`` whose offer links all point to ``oid``."""
    node = a
    while isinstance(node.parent, Tag) and node.parent.name not in ("body", "[document]"):
        ids = {m.group("id") for x in node.parent.find_all("a", href=True) if (m := link_rx.search(x["href"]))}
        if ids - {oid}:
            break
        node = node.parent
    return node


def _html(msg: Message) -> str:
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_type() == "text/html":
            payload = part.get_payload(decode=True) or b""
            return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    return ""


def _card_strings(a: Tag, oid: str, link_rx: re.Pattern[str], limit: int = 8) -> list[str]:
    """Text after ``a`` up to the next job's link (at most ``limit`` non-empty strings)."""
    out: list[str] = []
    for el in a.next_elements:
        if isinstance(el, Tag):
            if el.name == "a" and (m := link_rx.search(el.get("href", ""))) and m.group("id") != oid:
                break
        elif el.parent is not a and (t := _clean(str(el))):
            out.append(t)
            if len(out) >= limit:
                break
    return out


def _linkedin_offer(source: str, oid: str, a: Tag, link_rx: re.Pattern[str], received, subject: str) -> Posting:
    """LinkedIn job card: the title link, then a "Company · Location (Remote)" line, then extras
    ("Actively recruiting", "3 connections", "Easy Apply"). The e-mail's links carry tracking and a
    one-time sign-in token (otpToken), so only the bare /jobs/view/<id>/ URL is kept."""
    title = _clean(a.get_text(" ", strip=True))
    after = _card_strings(a, oid, link_rx)
    line = next((t for t in after[:3] if " · " in t), "")
    company, _, location = (s.strip() for s in line.partition(" · "))
    workplace = None
    if wm := re.search(r"\((remote|hybrid|on-?site)\)\s*$", location, re.I):
        workplace = _LI_WORKPLACE[wm.group(1).lower()]
        location = location[: wm.start()].strip()
    tags = ["alert:linkedin"] + (["easy_apply"] if "Easy Apply" in after else [])
    return Posting(
        source=source, source_job_id=f"linkedin:{oid}", title=title, company=company,
        url=f"https://www.linkedin.com/jobs/view/{oid}/", location_raw=location, workplace=workplace,
        posted_at=received, tags=tags, raw={"subject": subject},
    )


def parse_alert(source: str, board: str, msg: Message, link_rx: re.Pattern[str]) -> list[Posting]:
    """Offers in one alert e-mail → Postings (first block per offer id wins)."""
    soup = BeautifulSoup(_html(msg), "lxml")
    received = email.utils.parsedate_to_datetime(msg["Date"]) if msg["Date"] else None
    subject = str(make_header(decode_header(msg["Subject"] or "")))
    out: dict[str, Posting] = {}
    for a in soup.find_all("a", href=True):
        m = link_rx.search(a["href"])
        if not m or m.group("id") in out or not _clean(a.get_text(" ", strip=True)):
            continue
        oid = m.group("id")
        if board == "linkedin":
            out[oid] = _linkedin_offer(source, oid, a, link_rx, received, subject)
            continue
        block = _offer_block(a, oid, link_rx)
        links = [x for x in ([block] if block.name == "a" else block.find_all("a", href=True))
                 if (lm := link_rx.search(x.get("href", ""))) and lm.group("id") == oid]
        texts = list(dict.fromkeys(t for x in links if (t := _clean(x.get_text(" ", strip=True)))))
        salary = next((t for t in texts[1:] if _CUR_RX.search(t) and _NUM.search(t)), "")
        company = next((t for t in texts[1:] if t != salary), "")
        city = block.select_one(".city_text") if block.name != "a" else None
        location = _clean(city.get_text(" ", strip=True)) if city else ""
        if not city and " - " in company:
            company, location = (s.strip() for s in company.rsplit(" - ", 1))
        smin, smax, cur, period = parse_salary(salary)
        out[oid] = Posting(
            source=source, source_job_id=f"{board}:{oid}", title=texts[0], company=company,
            url=m.group(0), location_raw=location, posted_at=received,
            salary_min=smin, salary_max=smax, salary_currency=cur, salary_period=period,
            tags=[f"alert:{board}"] + (["b2b"] if "vat" in salary.lower() else []),
            raw={"subject": subject, "salary": salary} if salary else {"subject": subject},
        )
    return list(out.values())


@adapter("email_alerts")
def email_alerts(ctx: FetchContext) -> Iterator[Posting]:
    p = ctx.params
    user, password = ctx.secrets.get("JR_IMAP_USER"), ctx.secrets.get("JR_IMAP_PASSWORD")
    senders = p.get("senders") or DEFAULT_SENDERS
    links = {b: re.compile(rx) for b, rx in (p.get("links") or DEFAULT_LINKS).items()}
    since = (datetime.now(timezone.utc) - timedelta(days=int(p.get("days", 3)))).strftime("%d-%b-%Y")
    imap = imaplib.IMAP4_SSL(p.get("host", "imap.gmail.com"))
    try:
        imap.login(user, password)
        imap.select(f'"{p.get("folder", "INBOX")}"', readonly=True)
        text_search = set(p.get("text_search", DEFAULT_TEXT_SEARCH))
        for board, domain in senders.items():
            key = "TEXT" if board in text_search else "FROM"
            _, data = imap.search(None, key, f'"{domain}"', "SINCE", since)
            ids = (data[0] or b"").split()
            ctx.notes.append(f"{board}: {len(ids)} alert e-mails")
            for num in ids[-int(p.get("max_messages", 40)):]:
                _, parts = imap.fetch(num, "(BODY.PEEK[])")
                raw = next((x[1] for x in parts if isinstance(x, tuple)), None)
                if raw is None or board not in links:
                    continue
                for post in parse_alert(ctx.spec.id, board, email.message_from_bytes(raw), links[board]):
                    if ctx.is_known(post.source, post.source_job_id):
                        ctx.mark_seen(post.source, post.source_job_id)
                        continue
                    yield post
    finally:
        try:
            imap.logout()
        except (imaplib.IMAP4.error, OSError):
            pass
