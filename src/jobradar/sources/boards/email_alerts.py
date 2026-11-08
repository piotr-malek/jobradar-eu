"""Job-alert e-mails read over IMAP: every job in the mails from the senders the user listed in the wizard
(``user/sources.yaml: alert_senders``), for boards we can't or don't fetch (LinkedIn: robots.txt; boards behind
Cloudflare such as pracuj.pl, jobs.bg, eJobs, StepStone). No link in a mail is ever opened.

The adapter reads the last few days of mail from each sender (read-only: ``BODY.PEEK``, nothing is marked or
moved). Senders of a known board get that board's parser: an offer is a block of links to the same offer id (title,
then salary, then company; pracuj: location in ``span.city_text``; theprotocol: "Company - Location" in one link;
LinkedIn: "Company · Location (Remote)"). Any other sender gets the generic parser: each link whose text passes the
matcher's title gate is a job, and the next few lines of the card give company, location and salary. There is no
description, so ``default_country`` (the user's home country) and the matcher's title rules do the rest.

Secrets: ``JR_IMAP_USER`` and ``JR_IMAP_PASSWORD`` (a Gmail app password; IMAP must be on).
Params:
  host       IMAP host (default imap.gmail.com)
  folder     mailbox or Gmail label to read (default INBOX)
  days       look-back window (default 3)
  senders    sender addresses or domains (default: the known boards'); a {board: sender} map is accepted too
  links      {board: regex with a named group ``id``} for the known boards (checked on real alerts 2026-10-28;
             the links are direct, only utm/sendid query params)
"""

from __future__ import annotations

import email
import imaplib
import re
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parseaddr
from html import escape
from typing import Callable, Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup, NavigableString, Tag

from jobradar.models import Posting, short_hash
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
KNOWN_BOARDS = {"linkedin.com": "linkedin", "pracuj.pl": "pracuj", "theprotocol.it": "theprotocol"}
_SKIP_HREF = re.compile(r"^(?:mailto:|tel:|#)|unsubscribe|opt-?out|/preferences|/settings|privacy|abmeld|wypisz|"
                        r"desinscri|désinscri|darse-de-baja", re.I)
_SKIP_TEXT = re.compile(r"unsubscribe|view (?:all|more|in browser|online)|see (?:all|more)|show (?:all|more)|"
                        r"more jobs|all jobs|similar jobs|manage|settings|privacy|terms|download|app store|"
                        r"google play|abmelden|alle (?:jobs|stellen)|weitere|zobacz|wszystkie|voir (?:tou|plus)|"
                        r"ver (?:todas|más)|bekijk|alle vacatures|^apply\b|^save\b|^\d+\s", re.I)
_TRACKING = re.compile(r"^(?:utm_|trk|tracking|refid|ref$|src$|source$|sendid|mc_|_hs|otptoken|midtoken|eid$)", re.I)
_PLACE_SEP = re.compile(r"\s+(?:·|•|\||–|—|-)\s+")
_LI_WORKPLACE = {"remote": "remote", "hybrid": "hybrid", "on-site": "onsite", "onsite": "onsite"}
_CURRENCY = {"zł": "PLN", "pln": "PLN", "€": "EUR", "eur": "EUR", "usd": "USD", "$": "USD", "chf": "CHF", "gbp": "GBP", "£": "GBP"}
_CUR_RX = re.compile(r"zł|\bpln\b|€|\beur\b|\busd\b|\$|\bchf\b|\bgbp\b|£", re.I)
_NUM = re.compile(r"\d{1,3}(?:[\s\u00a0\u202f.,']\d{3})+(?!\d)|\d+")
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


def board_for(sender: str) -> str | None:
    """The known board a sender address or domain belongs to (None: use the generic parser)."""
    s = sender.lower()
    return next((b for dom, b in KNOWN_BOARDS.items() if s.endswith(dom) or f"@{dom}" in s or f".{dom}" in s), None)


def _bare_url(href: str) -> str:
    """The link without tracking query parameters (the job id usually lives in the path or a remaining param)."""
    parts = urlsplit(href.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING.match(k)]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def _following_strings(a: Tag, stop: set[int], limit: int = 6) -> list[str]:
    """Text after ``a`` up to the next job link (at most ``limit`` short strings)."""
    out: list[str] = []
    for el in a.next_elements:
        if isinstance(el, Tag) and id(el) in stop:
            break
        if isinstance(el, NavigableString) and not any(x is a for x in el.parents):
            s = _clean(str(el))
            if s and len(s) <= 100 and not _SKIP_TEXT.search(s) and s not in out:
                out.append(s)
                if len(out) >= limit:
                    break
    return out


def _card_fields(title: str, strings: list[str]) -> tuple[str, str, str]:
    """(company, location, salary) from the lines under a job title."""
    from jobradar.normalize.location import extract_places

    strings = [s for s in strings if s.lower() != title.lower()]
    salary = next((s for s in strings if _CUR_RX.search(s) and _NUM.search(s)), "")
    rest = [s for s in strings if s != salary]
    for s in rest[:3]:
        parts = _PLACE_SEP.split(s, maxsplit=1)
        if len(parts) == 2 and any(extract_places(parts[1])[:3:2]):
            return parts[0], parts[1], salary
    place = next((s for s in rest[:4] if any(extract_places(s)[:3:2])), "")
    company = next((s for s in rest[:3] if s != place), "")
    return company, place, salary


def parse_generic(source: str, sender: str, msg: Message, wants_detail: Callable[[str], bool]) -> list[Posting]:
    """Any job-alert mail: every link whose text looks like a wanted job title is one job."""
    received = email.utils.parsedate_to_datetime(msg["Date"]) if msg["Date"] else None
    subject = str(make_header(decode_header(msg["Subject"] or "")))
    html = _html(msg)
    if not html:
        lines = _plain(msg)
        html = "".join(f'<p>{escape(line)}</p>' if not re.match(r"https?://", line) else
                       f'<a href="{escape(line)}">{escape(prev)}</a>' for prev, line in zip([""] + lines, lines))
    soup = BeautifulSoup(html, "lxml")
    jobs: list[tuple[Tag, str]] = []
    for a in soup.find_all("a", href=True):
        text = _clean(a.get_text(" ", strip=True))
        if (3 <= len(text) <= 120 and not _SKIP_HREF.search(a["href"]) and not _SKIP_TEXT.search(text)
                and re.match(r"https?://", a["href"].strip()) and wants_detail(text)):
            jobs.append((a, text))
    stop = {id(a) for a, _ in jobs}
    domain = (parseaddr(sender)[1] or sender).split("@")[-1].lower()
    out: dict[str, Posting] = {}
    for a, title in jobs:
        company, location, salary = _card_fields(title, _following_strings(a, stop - {id(a)}))
        key = f"{domain}:{short_hash(title.lower(), company.lower(), location.lower())}"
        if key in out:
            continue
        smin, smax, cur, period = parse_salary(salary)
        out[key] = Posting(
            source=source, source_job_id=key, title=title, company=company, url=_bare_url(a["href"]),
            location_raw=location, posted_at=received, salary_min=smin, salary_max=smax, salary_currency=cur,
            salary_period=period, tags=[f"alert:{domain}"], raw={"subject": subject, "sender": domain},
        )
    return list(out.values())


def _plain(msg: Message) -> list[str]:
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_type() == "text/plain":
            payload = part.get_payload(decode=True) or b""
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            return [x.strip() for x in text.splitlines() if x.strip()]
    return []


def _senders(value: object) -> list[str]:
    if isinstance(value, dict):
        return [str(v) for v in value.values()]
    out = [parseaddr(str(v))[1] or str(v).strip() for v in value or [] if str(v).strip()]
    return list(dict.fromkeys(out)) or list(DEFAULT_SENDERS.values())


@adapter("email_alerts")
def email_alerts(ctx: FetchContext) -> Iterator[Posting]:
    p = ctx.params
    user, password = ctx.secrets.get("JR_IMAP_USER"), ctx.secrets.get("JR_IMAP_PASSWORD")
    links = {b: re.compile(rx) for b, rx in (p.get("links") or DEFAULT_LINKS).items()}
    since = (datetime.now(timezone.utc) - timedelta(days=int(p.get("days", 3)))).strftime("%d-%b-%Y")
    text_search = set(p.get("text_search", DEFAULT_TEXT_SEARCH))
    done: set[bytes] = set()
    imap = imaplib.IMAP4_SSL(p.get("host", "imap.gmail.com"))
    try:
        imap.login(user, password)
        imap.select(f'"{p.get("folder", "INBOX")}"', readonly=True)
        for i, sender in enumerate(_senders(p.get("senders"))):
            board = board_for(sender)
            key = "TEXT" if board in text_search else "FROM"
            _, data = imap.search(None, key, f'"{sender}"', "SINCE", since)
            ids = [n for n in (data[0] or b"").split() if n not in done]
            ctx.notes.append(f"{board or f'sender {i + 1}'}: {len(ids)} alert e-mails")
            for num in ids[-int(p.get("max_messages", 40)):]:
                done.add(num)
                _, parts = imap.fetch(num, "(BODY.PEEK[])")
                raw = next((x[1] for x in parts if isinstance(x, tuple)), None)
                if raw is None:
                    continue
                msg = email.message_from_bytes(raw)
                posts = (parse_alert(ctx.spec.id, board, msg, links[board]) if board in links
                         else parse_generic(ctx.spec.id, sender, msg, ctx.wants_detail))
                for post in posts:
                    if ctx.is_known(post.source, post.source_job_id):
                        ctx.mark_seen(post.source, post.source_job_id)
                        continue
                    yield post
    finally:
        try:
            imap.logout()
        except (imaplib.IMAP4.error, OSError):
            pass
