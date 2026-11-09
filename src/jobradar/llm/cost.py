"""LLM cost estimate per night: screened jobs × average prompt size × a price table, plus how many jobs a night the
provider's free limits and the screen's time budget allow (a big first run is spread over that many nights).

Shown in the wizard, the app's status bar and `jr doctor --estimate`. Prices: defaults/llm_prices.yaml, overridden by
user/llm.yaml `prices: {model: [input, output]}` (USD per million tokens)."""

from __future__ import annotations

from typing import Any

from jobradar import config
from jobradar.settings import PACKAGE_DIR

CHARS_PER_TOKEN = 4
SCREEN_BATCH = 10
SYSTEM_TOKENS = 1000         # the screen's instructions
PROFILE_TOKENS = 250
JOB_TOKENS = 1000            # 3,500 characters of posting + title/location lines
OUT_TOKENS_PER_JOB = 170
TYPICAL_NIGHT = 300          # new jobs on an ordinary night for a flexible search
FREE_PRESETS = ("gemini", "mistral", "groq", "cloudflare")   # free tiers that need no card: no charge at the limit


def prices() -> dict[str, list[float]]:
    table = config.read_yaml(PACKAGE_DIR / "llm_prices.yaml")
    table.update(config.llm_config().get("prices") or {})
    return table


def price_for(model: str) -> tuple[float, float]:
    table = prices()
    row = table.get(model) or next((v for k, v in table.items() if k != "default" and model.startswith(k)), None)
    row = row or table.get("default") or [0.5, 1.5]
    return float(row[0]), float(row[1])


def screen_tokens(jobs: int) -> tuple[int, int]:
    batches = -(-jobs // SCREEN_BATCH)
    tin = batches * (SYSTEM_TOKENS + PROFILE_TOKENS) + jobs * JOB_TOKENS
    return tin, jobs * OUT_TOKENS_PER_JOB


def jobs_per_night(c: dict[str, Any]) -> int:
    """Jobs one night can screen: the daily request budget or the screen minutes at the provider's rate."""
    from jobradar.pipeline.shortlist import screen_minutes

    rpm = int(c.get("rpm") or 0)
    budget = int(c.get("daily_budget") or 400)
    requests = min(budget, int(screen_minutes() * rpm)) if rpm else budget
    return requests * SCREEN_BATCH


def estimate_night(jobs: int | None = None, model: str | None = None) -> dict[str, Any]:
    from jobradar.llm.client import LLMError, step_config

    c: dict[str, Any] = {}
    if jobs is None:
        jobs = int(config.user_settings().get("shortlist_cap") or TYPICAL_NIGHT)
    if model is None:
        try:
            c = step_config("screen")
        except LLMError:
            c = {"provider": "off"}
        model = c.get("model") or ""
        if c.get("provider") == "off":
            return {"screen": "off", "usd_per_night": 0.0, "usd_per_month": 0.0}
    tin, tout = screen_tokens(jobs)
    out: dict[str, Any] = {"model": model, "jobs": jobs, "tokens_in": tin, "tokens_out": tout}
    if c.get("preset") in FREE_PRESETS:
        per_night = jobs_per_night(c)
        return {**out, "free_tier": True, "usd_per_night": 0.0, "usd_per_month": 0.0, "jobs_per_night": per_night,
                "note": f"free tier: no charge; it stops at its limit. About {per_night} jobs a night, so a first "
                        f"run of 3,000 jobs is reviewed over {-(-3000 // max(1, per_night))} nights"}
    pin, pout = price_for(model)
    night = tin / 1e6 * pin + tout / 1e6 * pout
    return {**out, "usd_per_night": round(night, 3), "usd_per_month": round(night * 30, 2),
            "note": "upper bound at paid prices: most nights screen fewer jobs"}
