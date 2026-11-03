"""LLM cost estimate per night: screened jobs (the nightly cap) × average prompt size × a price table.

Shown in the wizard, the app's status bar and `jr doctor --estimate`. Prices: defaults/llm_prices.yaml, overridden by
user/llm.yaml `prices: {model: [input, output]}` (USD per million tokens)."""

from __future__ import annotations

from typing import Any

from jobradar import config
from jobradar.settings import PACKAGE_DIR

CHARS_PER_TOKEN = 4
SCREEN_BATCH = 5
SYSTEM_TOKENS = 900          # the screen's instructions
PROFILE_TOKENS = 250
JOB_TOKENS = 1000            # 3,500 characters of posting + title/location lines
OUT_TOKENS_PER_JOB = 130


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


def estimate_night(jobs: int | None = None, model: str | None = None) -> dict[str, Any]:
    from jobradar.llm.client import LLMError, step_config

    if jobs is None:
        jobs = int(config.user_settings().get("shortlist_cap") or 150)
    if model is None:
        try:
            c = step_config("screen")
        except LLMError:
            c = {"provider": "off"}
        model = c.get("model") or ""
        if c.get("provider") == "off":
            return {"screen": "off", "usd_per_night": 0.0, "usd_per_month": 0.0}
    tin, tout = screen_tokens(jobs)
    pin, pout = price_for(model)
    night = tin / 1e6 * pin + tout / 1e6 * pout
    return {"model": model, "jobs": jobs, "tokens_in": tin, "tokens_out": tout, "usd_per_night": round(night, 3),
            "usd_per_month": round(night * 30, 2), "note": "upper bound: most nights screen fewer jobs than the cap, "
                                                           "and free tiers cost nothing within their limits"}
