"""Deterministic matcher: title families → exclusions → freshness → location → language → tags/priority.

No AI anywhere. Every decision carries an ``explain`` dict and, when dropped, a
``drop_reason`` so false negatives can be audited (``v_drop_audit``).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from jobradar.models import MatchResult, Posting
from jobradar.normalize.lang import language_ok, language_requirements
from jobradar.normalize.location import classify_location
from jobradar.normalize.names import clean_title, norm_company
from jobradar.normalize.text import fold
from jobradar.config import SENIORITY_LEVELS
from jobradar.normalize.location import LocationProfile

TITLE_DATA_CONTEXT = re.compile(
    r"\b(?:data|analytics|analytical|dbt|warehouse|etl|elt|bi|ai|ml|llm|genai|lakehouse|bigquery|snowflake|databricks)\b"
)
STRONG_DATA_TERMS = re.compile(
    r"\b(?:dbt|data warehous\w*|snowflake|bigquery|databricks|redshift|etl|elt|data pipelines?|"
    r"analytics engineering|data platforms?|data engineering|lakehouse|looker|airflow|fivetran|airbyte|"
    r"data modell?ing|semantic layer|reverse etl)\b"
)


def has_data_context(title_folded: str, description: str) -> bool:
    """DevRel/solutions titles count only for data-tool contexts (data term in title, or 2+ stack terms)."""
    if TITLE_DATA_CONTEXT.search(title_folded):
        return True
    return len(set(STRONG_DATA_TERMS.findall(fold(description[:8000])))) >= 2


def _alt(patterns: list[str]) -> re.Pattern[str]:
    body = "|".join(f"(?:{p})" for p in patterns)
    return re.compile(rf"(?<![a-z0-9])(?:{body})(?![a-z0-9])")


@dataclass
class TitleVerdict:
    tier: str
    family: str | None
    rule: str | None
    drop_reason: str | None


class MatchEngine:
    def __init__(self, cfg: dict[str, Any], rule_version: str):
        self.cfg = cfg
        self.rule_version = rule_version
        t = cfg["title"]
        # per family: (pattern, regex, needs_context); context patterns count only when the description has
        # `context_min` distinct `context_terms` (the pre-gate lets them through to fetch the description)
        self.families: list[tuple[str, dict, list[tuple[str, re.Pattern[str], bool]]]] = []
        for name, fam in (t.get("families") or {}).items():
            compiled = [(p, _alt([p]), False) for p in fam.get("any") or []]
            compiled += [(p, _alt([p]), True) for p in fam.get("context_any", [])]
            fam = {**fam, "pregate_rx": _alt(fam["pregate_any"]) if fam.get("pregate_any") else None,
                   "context_rx": _alt(fam["context_terms"]) if fam.get("context_terms") else None,
                   "exclude_rx": _alt(fam["exclude"]) if fam.get("exclude") else None,
                   "exclude_ok_rx": _alt(fam["exclude_ok"]) if fam.get("exclude_ok") else None}
            self.families.append((name, fam, compiled))
        self.family_tier = {name: fam.get("tier", "core") for name, fam, _ in self.families}
        self.family_labels: dict[str, str] = cfg.get("family_labels") or {}
        ex = t.get("exclude") or {}
        self.manager_rx = _alt(ex.get("manager") or ["$^"])
        self.manager_ok = {name for name, fam, _ in self.families if fam.get("manager_ok")}
        self.discipline_hard = _alt(ex.get("discipline_hard") or ["$^"])
        self.discipline_ok = _alt(ex.get("discipline_ok_if") or ["$^"])
        rv = t.get("review") or {}
        self.review_on = bool(rv.get("enabled", False)) and bool(rv.get("qualifiers"))
        self.review_q = _alt(rv.get("qualifiers") or ["$^"])
        self.review_h = _alt(rv.get("heads") or ["$^"])
        sr = rv.get("stack_rescue") or {}
        self.rescue_min = int(sr.get("min_score", 0))
        self.rescue_ignore = set(sr.get("ignore", []))
        self.rescue_never = _alt(sr.get("never", ["$^"]))
        pu = cfg.get("purpose") or {}
        self.purpose_any = _alt(pu.get("any", ["$^"]))
        self.purpose_noise = _alt(pu.get("noise", ["$^"]))
        self.purpose_grades = _alt(pu.get("drop_grades", ["$^"]))
        self.purpose_categories = {c.lower() for c in pu.get("drop_categories", [])}
        self.purpose_text = _alt(pu.get("drop_text", ["$^"]))
        sen = dict(cfg.get("seniority") or {})
        self.level_range = _alt(sen.pop("range_patterns", None) or ["$^"])
        self.levels = [(lv, _alt(sen[lv])) for lv in SENIORITY_LEVELS[::-1] if sen.get(lv)]   # principal first
        self.seniority = {lv: rx for lv, rx in self.levels if lv != "intern"}
        rng = cfg.get("seniority_range") or {"min": "junior", "max": "senior"}
        self.level_min = SENIORITY_LEVELS.index(rng.get("min", "junior"))
        self.level_max = SENIORITY_LEVELS.index(rng.get("max", "senior"))
        ut = cfg.get("user_titles") or {}
        self.never = _alt(ut["never"]) if ut.get("never") else None
        self.never_unless = _alt(ut["never_unless"]) if ut.get("never_unless") else None
        self.keep_titles = _alt(ut["keep"]) if ut.get("keep") else None
        self.must_have = _alt(ut["must_have"]) if ut.get("must_have") else None
        self.contract = {k: _alt(v) for k, v in (cfg.get("contract") or {}).items()}
        st = cfg.get("stack") or {}
        self.stack_weights: dict[str, int] = st.get("weights") or {}
        self.stack_patterns = {k: re.compile(v) for k, v in (st.get("patterns") or {}).items()}
        lang = cfg.get("language") or {}
        self.allowed_langs = set(lang.get("allowed_posting_languages") or ["en"])
        self.required_drop = set(lang.get("required_drop") or [])
        self.plus_tags: dict[str, str] = lang.get("plus_tag") or {}
        self.location = LocationProfile.from_config(cfg.get("location") or {})
        self.max_age_days = int((cfg.get("location") or {}).get("max_age_days", 30))
        comp = cfg.get("companies", {})
        self.muted = {norm_company(c) for c in comp.get("mute", [])}
        self.agency = _alt([re.escape(fold(a)) for a in comp.get("agency_markers", [])]) if comp.get("agency_markers") else None
        self.agency_sources = set(comp.get("agency_sources", []))
        self.prio = cfg["priority"]
        self.title_bonus = [(_alt([p]), pts) for p, pts in (self.prio.get("title_bonus") or {}).items()]

    # ------------------------------------------------------------------ load
    @classmethod
    def load(cls, path: Path | None = None) -> "MatchEngine":
        """The user's matcher (package base + role packs + profile + user overrides); `path`: one YAML file."""
        if path is not None:
            return _load_file(str(path))
        from jobradar import config

        cfg = config.matching_config()
        return _load_cfg(json.dumps(cfg, sort_keys=True, default=str))

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> "MatchEngine":
        from jobradar import config

        return cls(cfg, config.rule_version(cfg))

    def family_label(self, family: str | None) -> str:
        return self.family_labels.get(family or "", "") if family else ""

    # ----------------------------------------------------------------- title
    def title_level(self, title_folded: str) -> str | None:
        """The title's seniority word, highest first; "Mid/Senior" counts as the lower level."""
        if self.level_range.search(title_folded):
            for lv in ("junior", "mid"):
                if self.seniority.get(lv) and self.seniority[lv].search(title_folded):
                    return lv
            return "mid"
        for lv, rx in self.levels:
            if rx.search(title_folded):
                return lv
        return None

    def _family_for(self, t: str, description: str, pregate: bool) -> tuple[str | None, dict | None, str | None]:
        for name, fam, compiled in self.families:
            for pattern, rx, needs_context in compiled:
                m = rx.search(t)
                if not m:
                    continue
                if fam.get("requires_data_context") and not has_data_context(t, description) and not (
                        pregate and fam["pregate_rx"] and fam["pregate_rx"].search(t)):
                    continue
                if needs_context and not pregate and not self._has_context(fam, description):
                    continue
                exm = fam["exclude_rx"].search(t) if fam["exclude_rx"] else None
                if exm:
                    remainder = (t[: exm.start()] + " " + t[exm.end():]).strip()
                    if not (fam["exclude_ok_rx"] and fam["exclude_ok_rx"].search(remainder)):
                        break                  # this family's words are there, but so is another discipline's
                return name, fam, pattern
        return None, None, None

    def title_verdict(self, title: str, description: str = "", *, purpose: bool = False,
                      pregate: bool = False) -> TitleVerdict:
        """`purpose`: UN/EU/intergovernmental posting (tag "purpose"), tried against the purpose vocabulary first.
        `pregate`: title-only check before a detail fetch, so context-dependent families are let through to be
        decided once the description is known."""
        t = fold(clean_title(title))
        family, fam_cfg, rule = None, None, None
        if purpose:
            if self.purpose_noise.search(t):
                return TitleVerdict("excluded", None, None, "title: purpose noise")
            m = self.purpose_any.search(t)
            if m:
                family, fam_cfg, rule = "purpose", {"tier": "adjacent"}, m.group(0)
        if not family:
            family, fam_cfg, rule = self._family_for(t, description, pregate)
        kept = bool(self.keep_titles and self.keep_titles.search(t))
        rescued = bool(self.never_unless and self.never_unless.search(t))
        level = self.title_level(t)
        if level == "intern" and self.level_min > 0:
            return TitleVerdict("excluded", family, rule, "title: internship/student level")
        hard = self.discipline_hard.search(t)
        if hard and not (family and self.discipline_ok.search(t[: hard.start()] + " " + t[hard.end():])):
            return TitleVerdict("excluded", family, rule, f"title: other discipline ({hard.group(0)})")
        if (self.manager_rx.search(t) and family not in self.manager_ok and self.level_max < SENIORITY_LEVELS.index("lead")
                and not kept):
            return TitleVerdict("excluded", family, rule, "title: people manager")
        if self.never and not kept and not rescued:
            m = self.never.search(t)
            if m:
                return TitleVerdict("excluded", family, rule, f"title: never word ({m.group(0)})")
        if level and not kept and not rescued:
            idx = SENIORITY_LEVELS.index(level)
            if idx > self.level_max:
                return TitleVerdict("excluded", family, rule, f"title: above your level ({level})")
            if idx < self.level_min:
                return TitleVerdict("excluded", family, rule, f"title: below your level ({level})")
        if kept and not family:
            return TitleVerdict("core", "custom", "keep_titles", None)
        if family:
            return TitleVerdict(fam_cfg.get("tier", "core"), family, rule, None)
        if (self.review_on or purpose) and self.review_q.search(t) and self.review_h.search(t):
            return TitleVerdict("review", "review", "qualifier+head", None)
        return TitleVerdict("excluded", None, None, "title: not a target role")

    @staticmethod
    def _has_context(fam: dict, description: str) -> bool:
        rx = fam["context_rx"]
        hits = {m.group(0) for m in rx.finditer(fold(description[:8000]))} if rx else set()
        return len(hits) >= int(fam.get("context_min", 1))

    def wants_detail(self, title: str, purpose: bool = False) -> bool:
        """Cheap title pre-gate used by adapters before fetching detail pages."""
        return self.title_verdict(title, purpose=purpose, pregate=True).tier != "excluded"

    def purpose_drop(self, p: Posting) -> str | None:
        """Grade/category/eligibility filter for purpose postings (senior grades, national officers, internal-only)."""
        grades = [tg.split(":", 1)[1] for tg in p.tags if tg.startswith("grade:")]
        for g in grades or [p.title]:
            m = self.purpose_grades.search(fold(g))
            if m:
                return f"purpose: grade {m.group(0).upper()}"
        cats = {tg.split(":", 1)[1].lower() for tg in p.tags if tg.startswith("category:")}
        if cats & self.purpose_categories:
            return f"purpose: category {','.join(sorted(cats & self.purpose_categories)).upper()}"
        m = self.purpose_text.search(fold(f"{p.title}\n{p.description[:1500]}"))
        if m:
            return f"purpose: {m.group(0)}"
        return None

    def unstated_elsewhere(self, r: dict[str, Any]) -> bool:
        """A matched listing (row with work_mode) that states no work mode and names no place in the user's hybrid
        area: when another listing of the same job is hybrid outside that area, this copy is that job too."""
        if not self.location.limits_hybrid or (r.get("work_mode") or "unknown") != "unknown":
            return False
        return not self.location.names_home_area(f"{r.get('title') or ''} | {r.get('location_raw') or ''} | "
                                                 f"{r.get('remote_scope') or ''}")

    # ----------------------------------------------------------------- match
    def match(
        self,
        p: Posting,
        *,
        default_country: str | None = None,
        default_workplace: str | None = None,
        now: datetime | None = None,
    ) -> MatchResult:
        now = now or datetime.now(timezone.utc)
        explain: dict[str, Any] = {}
        flags: list[str] = []
        purpose = "purpose" in p.tags
        tv = self.title_verdict(p.title, p.description, purpose=purpose)
        text = f"{p.title}\n{p.description}"
        text_f = fold(text)

        loc = classify_location(
            workplace=p.workplace,
            title=p.title,
            location_raw=p.location_raw,
            remote_scope=p.remote_scope,
            countries_structured=p.countries,
            description=p.description,
            default_country=default_country,
            default_workplace=default_workplace,
            profile=self.location,
        )
        explain["location"] = {"mode": loc.work_mode, "countries": loc.countries, "regions": loc.regions,
                               **loc.evidence}
        flags.extend(loc.flags)
        lang_ok, posting_lang = language_ok(p.description, self.allowed_langs) if p.description else (True, None)
        reqs = language_requirements(text) if p.description else {}
        explain["language"] = {"posting_lang": posting_lang, "requirements": reqs}

        seniority = self.title_level(fold(clean_title(p.title)))
        contract = [k for k, rx in self.contract.items() if rx.search(text_f)]
        stack_hits = [k for k, rx in self.stack_patterns.items() if rx.search(text_f)]
        stack_score = sum(self.stack_weights.get(k, 0) for k in stack_hits)
        if (tv.drop_reason == "title: not a target role" and self.review_on and self.rescue_min
                and sum(self.stack_weights.get(k, 0) for k in stack_hits if k not in self.rescue_ignore) >= self.rescue_min
                and not self.rescue_never.search(fold(p.title))):
            tv = TitleVerdict("review", "review", "stack rescue", None)
        explain["title"] = {"tier": tv.tier, "family": tv.family, "rule": tv.rule}
        company_norm = norm_company(p.company)
        if (self.agency and self.agency.search(fold(p.company))) or p.source in self.agency_sources:
            flags.append("agency")
        for lang, level in reqs.items():
            if level == "plus" and lang in self.plus_tags:
                flags.append(self.plus_tags[lang])

        age_days = None
        if p.posted_at:
            posted = p.posted_at if p.posted_at.tzinfo else p.posted_at.replace(tzinfo=timezone.utc)
            age_days = max(0.0, (now - posted).total_seconds() / 86400)

        drop_reason = tv.drop_reason
        if not drop_reason and purpose:
            drop_reason = self.purpose_drop(p)
        if not drop_reason and age_days is not None and age_days > self.max_age_days:
            drop_reason = f"stale: posted {int(age_days)} days ago"
        if not drop_reason and loc.drop_reason:
            drop_reason = loc.drop_reason
        if not drop_reason and not lang_ok:
            drop_reason = f"posting language: {posting_lang}"
        if not drop_reason:
            blocking = sorted(lang for lang, lvl in reqs.items() if lvl == "required" and lang in self.required_drop)
            if blocking:
                drop_reason = f"requires language: {','.join(blocking)}"
        if not drop_reason and company_norm in self.muted:
            drop_reason = "company muted"

        decision = "drop" if drop_reason else "match"
        family = "purpose" if purpose else tv.family      # purpose jobs share one family whatever their title
        section = loc.section if decision == "match" else None
        priority = 0.0
        if decision == "match":
            pr = self.prio
            priority += pr["family"].get(family or "review", 0)
            priority += pr["section"].get(section, 0)
            priority += pr["stack_point"] * stack_score
            if age_days is not None:
                for days, bonus in sorted((float(d), b) for d, b in pr["fresh_days"].items()):
                    if age_days <= days:
                        priority += bonus
                        break
            for f in flags:
                priority += pr["flags"].get(f, 0)
            title_f = fold(clean_title(p.title))
            priority += sum(pts for rx, pts in self.title_bonus if rx.search(title_f))
            tp = pr["top_pick"]
            if (
                self.family_tier.get(family or "", "") in tp.get("tiers", ["core"])
                and stack_score >= tp["min_stack_score"]
                and (age_days is None or age_days <= tp["max_age_days"])
            ):
                flags.append("top_pick")
        explain["stack_score"] = stack_score
        return MatchResult(
            posting_key=p.posting_key,
            rule_version=self.rule_version,
            tier=tv.tier,
            family=family,
            section=section,
            decision=decision,
            drop_reason=drop_reason,
            work_mode=loc.work_mode,
            countries=loc.countries,
            flags=flags,
            stack_hits=stack_hits,
            seniority=seniority,
            contract=contract,
            posting_lang=posting_lang,
            priority=round(priority, 2),
            explain=explain,
        )


@lru_cache(maxsize=4)
def _load_file(path: str) -> MatchEngine:
    raw = Path(path).read_bytes()
    cfg = yaml.safe_load(raw)
    version = f"{cfg.get('label', 'v')}-{hashlib.sha256(raw).hexdigest()[:8]}"
    return MatchEngine(cfg, version)


@lru_cache(maxsize=4)
def _load_cfg(cfg_json: str) -> MatchEngine:
    return MatchEngine.from_config(json.loads(cfg_json))
