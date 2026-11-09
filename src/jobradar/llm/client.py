"""JSON chat client for any OpenAI-compatible API, the Anthropic Messages API, and the local Claude Code CLI.

Providers (user/llm.yaml, per step, or env ``JR_<STEP>_LLM_PROVIDER`` / ``_MODEL`` / ``_BASE_URL``):

* ``openai_compatible`` with a preset: openai, gemini, groq, openrouter, mistral, ollama, lmstudio (or any
  ``base_url`` + ``api_key_env``). JSON-schema output where the server supports it, else JSON mode, else the JSON is
  cut out of the text.
* ``anthropic``: Messages API, structured output through a forced tool call.
* ``claude``: the local Claude Code CLI (``claude -p``) on the user's own login. Laptop only, never on Actions.
* ``off``: the step is skipped (Lite works with no LLM).

Steps: ``screen`` (overnight on Actions: API providers only), ``prep`` (map form questions to the bank),
``draft`` (per-job drafts), ``onboarding`` (wizard: CV import, custom role pack). A step with no setting of its own
uses ``default``.

Guardrails: requests-per-minute throttle, a daily request budget per provider persisted in the cache folder, or in
state.db on Actions (``BudgetExhausted`` when spent), one retry on 429/5xx/timeout/bad JSON, ``<think>`` blocks stripped.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from jobradar.settings import get_settings, on_actions

log = logging.getLogger(__name__)

# preset → (base_url, api key env var, default model, rpm, json mode: schema | object)
PRESETS: dict[str, tuple[str, str | None, str, int, str]] = {
    "mistral": ("https://api.mistral.ai/v1", "MISTRAL_API_KEY", "ministral-14b-latest", 8, "schema"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY", "auto", 14, "schema"),
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY", "llama-3.3-70b-versatile", 20, "object"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", "mistralai/mistral-small-3.2-24b-instruct:free",
                   10, "schema"),
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY", "gpt-5-mini", 30, "schema"),
    "ollama": ("http://localhost:11434/v1", None, "qwen3:8b", 0, "schema"),
    "lmstudio": ("http://localhost:1234/v1", None, "local-model", 0, "schema"),
    "cloudflare": ("https://api.cloudflare.com/client/v4/accounts/{CLOUDFLARE_ACCOUNT_ID}/ai/v1", "CLOUDFLARE_API_TOKEN",
                   "@cf/qwen/qwen3-30b-a3b-fp8", 20, "object"),
}
# model "auto": the first of these the key can use (Google retires model ids without notice)
AUTO_MODELS: dict[str, list[str]] = {
    "gemini": ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-flash-lite-latest"],
}
# Free quotas are per model (AI Studio, 2026-11-09: 3.5 and 3.1 Flash-Lite 15 RPM / 250K TPM / 500 RPD each;
# Gemma 4 30 RPM / 16K TPM / 14.4K RPD), so one key can fall back to the next model when one is spent.
DEFAULT_FALLBACKS: dict[str, list[str]] = {
    "gemini": ["gemini:gemini-3.1-flash-lite", "gemini:gemma-4-31b", "gemini:gemma-4-26b"],
}
PRESET_DAILY_BUDGET = {"gemini": 480}
MODEL_RPM = {"gemma-4": 3}     # 16K tokens a minute: about three small batches
QUOTA_SPENT = re.compile(r"per ?day|perday|daily|per ?month|permonth|monthly|exceeded your current quota", re.I)
RETRY_DELAY = re.compile(r'"?retry_?delay"?\s*:\s*"?(\d+(?:\.\d+)?)s', re.I)
LOCAL_PRESETS = ("ollama", "lmstudio")
ANTHROPIC_URL = "https://api.anthropic.com/v1"
ANTHROPIC_DEFAULT_MODEL = "claude-haiku-5-5"
CLAUDE_DEFAULT_MODEL = "claude-sonnet-5-5"
PROVIDERS = ("openai_compatible", "anthropic", "claude", "off")
STEPS = ("screen", "prep", "draft", "onboarding")
CLAUDE_TIMEOUT = 300
_THINK = re.compile(r"<think>.*?</think>", re.S)


class LLMError(RuntimeError):
    pass


class BudgetExhausted(LLMError):
    """The daily request budget is spent; callers stop and fail soft."""


class _Transient(Exception):
    pass


def _env(name: str) -> str | None:
    v = os.environ.get(name)
    return v.strip() if v and v.strip() else None


def step_config(step: str) -> dict[str, Any]:
    """The resolved settings of one step: {provider, preset, model, base_url, api_key_env, rpm, json_mode}."""
    from jobradar import config

    cfg = config.llm_config()
    steps = cfg.get("steps") or {}
    c = dict(cfg.get("default") or {})
    c.update(steps.get(step) or {})
    if step == "draft" and not steps.get("draft"):
        c.update(steps.get("prep") or {})
    up = step.upper()
    for key, env in (("provider", f"JR_{up}_LLM_PROVIDER"), ("model", f"JR_{up}_LLM_MODEL"),
                     ("base_url", f"JR_{up}_LLM_BASE_URL")):
        if _env(env):
            c[key] = _env(env)
    if not c.get("provider") and _env("JR_LLM_PROVIDER"):
        c["provider"] = _env("JR_LLM_PROVIDER")
    prov = (c.get("provider") or "").strip().lower()
    if prov in PRESETS:                      # "provider: mistral" is shorthand for openai_compatible + preset
        c["preset"], prov = prov, "openai_compatible"
    if not prov:
        return {"provider": "off"}
    if prov not in PROVIDERS:
        raise LLMError(f"unknown LLM provider {prov!r} (use {', '.join(PROVIDERS)} or a preset: {', '.join(PRESETS)})")
    c["provider"] = prov
    if prov == "openai_compatible":
        base, key_env, model, rpm, mode = PRESETS.get(c.get("preset") or "", (None, None, None, 0, "schema"))
        c.setdefault("base_url", base)
        c.setdefault("api_key_env", key_env)
        c.setdefault("model", model)
        c.setdefault("rpm", rpm)
        c.setdefault("json_mode", mode)
        if not c.get("base_url"):
            raise LLMError(f"step {step}: openai_compatible needs a preset or base_url")
        c["base_url"] = _fill_env(c["base_url"])
    elif prov == "anthropic":
        c.setdefault("base_url", ANTHROPIC_URL)
        c.setdefault("api_key_env", "ANTHROPIC_API_KEY")
        c.setdefault("model", ANTHROPIC_DEFAULT_MODEL)
        c.setdefault("rpm", 20)
    elif prov == "claude":
        c.setdefault("model", CLAUDE_DEFAULT_MODEL)
    if _env("JR_LLM_RPM"):
        c["rpm"] = int(_env("JR_LLM_RPM") or 0)
    return c


def _fill_env(url: str) -> str:
    """``{NAME}`` in a base URL comes from the environment (Cloudflare's account id)."""
    def sub(m: re.Match[str]) -> str:
        v = _env(m.group(1))
        if not v:
            raise LLMError(f"{m.group(1)} is not set")
        return v
    return re.sub(r"\{([A-Z][A-Z0-9_]+)\}", sub, url)


def step_provider(step: str) -> str:
    try:
        return step_config(step)["provider"]
    except LLMError:
        return ""


def resolve_provider(purpose: str) -> tuple[str, str]:
    c = step_config(purpose)
    return c["provider"], c.get("model") or ""


def optional_client(purpose: str) -> "LLMClient | None":
    """A client for ``purpose`` only when it has settings of its own; otherwise None (callers reuse their main one)."""
    from jobradar import config

    up = purpose.upper()
    own = (config.llm_config().get("steps") or {}).get(purpose) or _env(f"JR_{up}_LLM_PROVIDER") or _env(
        f"JR_{up}_LLM_MODEL")
    return LLMClient(purpose) if own and step_provider(purpose) not in ("off", "") else None


class UsageLedger:
    """Persisted per-day request counter for one provider."""

    def __init__(self, path: Path, budget: int):
        self.path, self.budget = path, budget
        self._lock = threading.Lock()
        self.day, self.count = date.today().isoformat(), 0
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("date") == self.day:
                self.count = int(data.get("count") or 0)
        except (OSError, ValueError):
            pass

    @property
    def remaining(self) -> int:
        return 1_000_000 if self.budget <= 0 else max(0, self.budget - self.count)

    def reserve(self) -> None:
        with self._lock:
            today = date.today().isoformat()
            if today != self.day:
                self.day, self.count = today, 0
            if self.budget > 0 and self.count >= self.budget:
                raise BudgetExhausted(f"daily LLM budget of {self.budget} requests is spent")
            self.count += 1
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps({"date": self.day, "count": self.count}), encoding="utf-8")
            except OSError:
                pass


class StateLedger(UsageLedger):
    """The same counter kept in state.db (`llm_usage`): on Actions it is the only file that outlives a run."""

    def __init__(self, db_path: Path, provider: str, budget: int):
        import sqlite3

        self.db_path, self.provider, self.budget = db_path, provider, budget
        self._lock = threading.Lock()
        self.day, self.count, self.own = date.today().isoformat(), 0, 0
        try:
            with sqlite3.connect(db_path) as c:
                row = c.execute("SELECT SUM(requests) FROM llm_usage WHERE provider = ? AND day = ?",
                                (provider, self.day)).fetchone()
            self.count = int(row[0] or 0) if row else 0
        except sqlite3.Error:
            pass

    def reserve(self) -> None:
        with self._lock:
            if self.budget > 0 and self.count >= self.budget:
                raise BudgetExhausted(f"daily LLM budget of {self.budget} requests is spent")
            self.count += 1
            self.own += 1

    def flush(self) -> None:
        if not self.own:
            return
        from jobradar.store.sqlite import SQLiteStore

        store = SQLiteStore(self.db_path)
        try:
            store.append("llm_usage", [{"provider": self.provider, "day": self.day, "requests": self.own,
                                        "at": datetime.now(timezone.utc)}])
            self.own = 0
        finally:
            store.close()


class LLMClient:
    def __init__(self, purpose: str = "screen", *, provider: str | None = None, model: str | None = None,
                 transport: httpx.BaseTransport | None = None, sleep: bool = True, ledger_dir: Path | None = None,
                 settings: dict[str, Any] | None = None):
        c = dict(settings) if settings else step_config(purpose)
        if provider:
            c = {**step_config_for(provider), **({"model": model} if model else {})}
        if model:
            c["model"] = model
        self.purpose, self.provider, self.cfg = purpose, c["provider"], c
        if self.provider == "off":
            raise LLMError(f"no LLM set for {purpose} (user/llm.yaml)")
        self.model = c.get("model") or ""
        self.preset = c.get("preset") or ""
        if on_actions() and (self.provider == "claude" or self.preset in LOCAL_PRESETS):
            raise LLMError(f"{self.provider} {self.preset} runs only on your computer, not on GitHub Actions")
        rpm = int(c.get("rpm") or 0)
        self.min_interval = 60.0 / rpm if rpm > 0 else 0.0
        self.sleep = sleep
        self._next_ok = 0.0
        name = self.preset or self.provider
        if self.model and self.model != "auto" and self.preset in AUTO_MODELS:
            name = f"{name}:{self.model}"         # a separate free quota per model
        budget = int(_env("JR_LLM_DAILY_BUDGET") or c.get("daily_budget") or PRESET_DAILY_BUDGET.get(self.preset)
                     or 400)
        state_db = get_settings().state_db
        self.ledger = (StateLedger(state_db, name, budget) if on_actions() and ledger_dir is None and state_db.is_file()
                       else UsageLedger((ledger_dir or get_settings().cache_dir) / f"llm_usage_{name}.json", budget))
        self.requests = 0
        self.free_tier: bool | None = None
        self._resolved = False
        self._retry_after = 0.0
        self.json_mode = c.get("json_mode") or "schema"
        self._http: httpx.Client | None = None
        if self.provider == "claude":
            self._claude = _env("JR_CLAUDE_BIN") or shutil.which("claude")
            if not self._claude:
                raise LLMError("claude CLI not found (install Claude Code or set JR_CLAUDE_BIN)")
            return
        headers = {"Content-Type": "application/json"}
        key_env = c.get("api_key_env")
        key = _env(key_env) if key_env else None
        if key_env and not key and transport is None:
            raise LLMError(f"{key_env} is not set")
        if self.provider == "anthropic":
            headers.update({"x-api-key": key or "test", "anthropic-version": "2023-06-01"})
        elif key or (key_env and transport is not None):
            headers["Authorization"] = f"Bearer {key or 'test'}"
        local = self.preset in LOCAL_PRESETS
        self._http = httpx.Client(base_url=c["base_url"], headers=headers, timeout=300.0 if local else 90.0,
                                  transport=transport)

    def close(self) -> None:
        if isinstance(self.ledger, StateLedger):
            self.ledger.flush()
        if self._http is not None:
            self._http.close()

    def __enter__(self) -> "LLMClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _throttle(self) -> None:
        if not self.min_interval:
            return
        now = time.monotonic()
        wait = self._next_ok - now
        self._next_ok = max(now, self._next_ok) + self.min_interval
        if wait > 0 and self.sleep:
            time.sleep(wait)

    def _call_claude(self, payload: dict[str, Any]) -> dict[str, Any]:
        """One headless Claude Code call: no tools, no saved session, customizations off; prompt on stdin."""
        msgs = payload["messages"]
        system = next((m["content"] for m in msgs if m["role"] == "system"), None)
        schema = payload["response_format"]["json_schema"]["schema"]
        cmd = [self._claude, "-p", "--safe-mode", "--tools", "", "--no-session-persistence", "--model", self.model,
               "--output-format", "json", "--json-schema", json.dumps(schema)]
        if system:
            cmd += ["--system-prompt", system]
        env = dict(os.environ)
        if _env("JR_CLAUDE_CONFIG_DIR"):
            env["CLAUDE_CONFIG_DIR"] = os.path.expanduser(_env("JR_CLAUDE_CONFIG_DIR") or "")
        env.pop("ANTHROPIC_API_KEY", None)       # always the subscription login, never a stray API key
        cwd = get_settings().cache_dir            # neutral folder: no project CLAUDE.md or settings
        cwd.mkdir(parents=True, exist_ok=True)
        try:
            proc = subprocess.run(cmd, input=msgs[-1]["content"], capture_output=True, text=True, env=env, cwd=cwd,
                                  timeout=CLAUDE_TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            raise _Transient("timeout") from exc
        try:
            out = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise _Transient(f"claude exit {proc.returncode}, no JSON") from exc
        if out.get("is_error"):
            status = out.get("api_error_status")
            if status and int(status) < 500 and int(status) != 429:
                raise LLMError(f"claude HTTP {status}: {str(out.get('result'))[:200]}")
            raise _Transient(f"claude error {status}")
        if isinstance(out.get("structured_output"), dict):
            return out["structured_output"]
        raise _Transient("claude returned no structured output")

    def _check_quota(self, resp: httpx.Response) -> None:
        """A 429 for a spent daily/monthly quota ends tonight's use of this provider; a per-minute one is retried
        after the delay the server asks for."""
        text = resp.text[:4000]
        if "freetier" in text.lower().replace("_", "").replace("-", ""):
            self.free_tier = True
        if QUOTA_SPENT.search(text) and not re.search(r"per ?minute|perminute", text, re.I):
            raise BudgetExhausted(f"{self.label} quota spent: {text[:200]}")
        delay = resp.headers.get("retry-after") or (RETRY_DELAY.search(text) or [None, None])[1]
        try:
            self._retry_after = min(60.0, float(delay)) if delay else 0.0
        except ValueError:
            self._retry_after = 0.0

    @property
    def label(self) -> str:
        return f"{self.preset or self.provider}:{self.model}"

    def _resolve_model(self) -> None:
        """Ask the provider which models this key can use: model "auto" takes the first preferred one, any other
        model name its newest listed version ("gemma-4-31b" → "gemma-4-31b-it")."""
        self._resolved = True
        prefs = AUTO_MODELS.get(self.preset) if self.model == "auto" else [self.model]
        if not prefs:
            return
        self.model = prefs[0]
        if self._http is None:
            return
        try:
            resp = self._http.get("/models")
            ids = {str(m.get("id") or "").removeprefix("models/") for m in (resp.json().get("data") or [])} \
                if resp.status_code == 200 else set()
        except (httpx.HTTPError, ValueError, AttributeError):
            ids = set()
        for want in prefs:
            hit = want if want in ids else next((i for i in sorted(ids) if i.startswith(want + "-")), None)
            if hit:
                self.model = hit
                return

    def _call(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self._resolved and self.preset in AUTO_MODELS:
            self._resolve_model()
        payload["model"] = self.model
        self.ledger.reserve()
        self._throttle()
        self.requests += 1
        if self.provider == "claude":
            return self._call_claude(payload)
        if self.provider == "anthropic":
            return self._call_anthropic(payload)
        assert self._http is not None
        body = dict(payload)
        if self.json_mode == "object":
            schema = payload["response_format"]["json_schema"]["schema"]
            body["response_format"] = {"type": "json_object"}
            body["messages"] = [*payload["messages"][:-1], {"role": "user", "content": payload["messages"][-1]["content"]
                                + "\n\nReply with one JSON object matching this JSON schema:\n" + json.dumps(schema)}]
        try:
            resp = self._http.post("/chat/completions", json=body)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise _Transient(type(exc).__name__) from exc
        if resp.status_code == 400 and self.json_mode == "schema" and "response_format" in resp.text:
            self.json_mode = "object"          # the server has no JSON-schema mode: ask for plain JSON instead
            raise _Transient("no json_schema support")
        if resp.status_code == 429:
            self._check_quota(resp)
        if resp.status_code == 429 or resp.status_code >= 500:
            raise _Transient(f"HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise LLMError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        text = ((resp.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        return _parse_json_text(text)

    def _call_anthropic(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Messages API; the schema is a tool the model must call, so its input is the structured answer."""
        assert self._http is not None
        msgs = payload["messages"]
        system = next((m["content"] for m in msgs if m["role"] == "system"), None)
        rf = payload["response_format"]["json_schema"]
        body: dict[str, Any] = {
            "model": self.model, "max_tokens": payload.get("max_tokens", 2048),
            "messages": [m for m in msgs if m["role"] != "system"],
            "tools": [{"name": rf["name"], "description": "Return the result.", "input_schema": rf["schema"]}],
            "tool_choice": {"type": "tool", "name": rf["name"]},
        }
        if system:
            body["system"] = system
        try:
            resp = self._http.post("/messages", json=body)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise _Transient(type(exc).__name__) from exc
        if resp.status_code == 429:
            self._check_quota(resp)
        if resp.status_code in (429, 529) or resp.status_code >= 500:
            raise _Transient(f"HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise LLMError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        for block in resp.json().get("content") or []:
            if block.get("type") == "tool_use" and isinstance(block.get("input"), dict):
                return block["input"]
        raise _Transient("no tool call in the answer")

    def json(self, prompt: str, schema: dict[str, Any], *, name: str = "result", system: str | None = None,
             max_tokens: int = 2048, temperature: float = 0.1) -> dict[str, Any]:
        """Return the parsed JSON object; raises BudgetExhausted / LLMError after one retry."""
        if self.model.startswith("qwen3"):    # Ollama's Qwen 3 thinks unless told not to
            prompt = prompt + "\n/no_think"
        messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
        payload: dict[str, Any] = {
            "model": self.model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens,
            "response_format": {"type": "json_schema", "json_schema": {"name": name, "schema": schema, "strict": True}},
        }
        for attempt in (1, 2):
            try:
                return self._call(payload)
            except _Transient as exc:
                if attempt == 2:
                    raise LLMError(f"{self.provider} failed twice: {exc}") from exc
                log.warning("llm %s retrying once: %s", self.provider, exc)
                payload["temperature"] = 0.0
                if self.sleep:
                    time.sleep(max(2.0, self._retry_after))
                self._retry_after = 0.0
        raise AssertionError("unreachable")


def _parse_json_text(text: str) -> dict[str, Any]:
    text = _THINK.sub("", text).strip()
    start, end = text.find("{"), text.rfind("}")
    try:
        out = json.loads(text[start:end + 1] if start != -1 and end > start else text)
    except json.JSONDecodeError as exc:
        raise _Transient("bad JSON") from exc
    if not isinstance(out, dict):
        raise _Transient("non-object JSON")
    return out


def step_config_for(provider: str) -> dict[str, Any]:
    """Settings for an explicit provider/preset name, or "preset:model" (tests, fallbacks, `jr llm test`)."""
    prov, _, model_override = provider.lower().partition(":")
    if prov in PRESETS:
        base, key_env, model, rpm, mode = PRESETS[prov]
        model = model_override or model
        rpm = next((r for prefix, r in MODEL_RPM.items() if model.startswith(prefix)), rpm)
        return {"provider": "openai_compatible", "preset": prov, "base_url": _fill_env(base), "api_key_env": key_env,
                "model": model, "rpm": rpm, "json_mode": mode}
    if prov == "anthropic":
        return {"provider": "anthropic", "base_url": ANTHROPIC_URL, "api_key_env": "ANTHROPIC_API_KEY",
                "model": ANTHROPIC_DEFAULT_MODEL, "rpm": 20}
    if prov == "claude":
        return {"provider": "claude", "model": CLAUDE_DEFAULT_MODEL}
    raise LLMError(f"unknown provider {provider!r}")


def test_call(step: str = "screen", settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """One tiny call to check a key and model (the wizard's live test)."""
    started = time.monotonic()
    with LLMClient(step, settings=settings) as c:
        out = c.json("Say hello: return {\"ok\": true, \"word\": \"hello\"}.",
                     {"type": "object", "properties": {"ok": {"type": "boolean"}, "word": {"type": "string"}},
                      "required": ["ok", "word"], "additionalProperties": False}, name="ping", max_tokens=50)
    return {"ok": bool(out.get("ok")), "seconds": round(time.monotonic() - started, 1), "provider": c.provider,
            "model": c.model}
