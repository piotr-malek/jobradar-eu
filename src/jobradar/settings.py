"""Paths and runtime settings.

Three places hold configuration, read in this order of precedence (highest first):

* ``USER_DIR`` (``$JR_USER_DIR``): the ``user/`` folder of the user's private config repo: profile summary, roles,
  sources, LLM settings and matching overrides. Safe to push: it never holds contact details or keys.
  On GitHub Actions the workflow points it at the checkout; on the laptop it is ``~/.job-radar/user``.
* ``LOCAL_DIR`` (``$JR_HOME``, default ``~/.job-radar``): local only, never pushed: ``.env`` with keys, the answer
  bank and picks (``local.db``), the pulled ``state.db``, ``experience.md``, ``cv.yaml``, ``draft_style.md``, PDFs.
* ``PACKAGE_DIR``: defaults shipped with the package (``jobradar/defaults``: role packs, sources, base matching rules,
  the company ATS registry).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

PACKAGE_DIR = Path(__file__).resolve().parent / "defaults"


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def local_dir() -> Path:
    return Path(_env("JR_HOME", str(Path.home() / ".job-radar"))).expanduser()


def user_dir() -> Path:
    return Path(_env("JR_USER_DIR", str(local_dir() / "user"))).expanduser()


# keys the overnight run needs: the wizard uploads these as Actions secrets, the workflow hands them over as JSON
SECRET_NAMES = re.compile(r"(MISTRAL|GEMINI|GROQ|OPENROUTER|OPENAI|ANTHROPIC)_API_KEY|CLOUDFLARE_(?:API_TOKEN|ACCOUNT_ID)|"
                          r"JOOBLE_[A-Z]{2}_API_KEY|"
                          r"ADZUNA_APP_(?:ID|KEY)|THEIR_STACK_API_KEY|ITJOBS_PT_API_KEY|JR_IMAP_\w+|SMTP_\w+|EMAIL_TO|"
                          r"NOTION_TOKEN|NOTION_JOBS_DB|NOTION_PARENT_PAGE|NOTION_JOBS_URL")


def load_env() -> None:
    """Load ``~/.job-radar/.env`` (keys typed into the wizard) and, on Actions, the repo secrets the workflow passes as
    ``JR_SECRETS_JSON``. Real environment variables win."""
    load_dotenv(local_dir() / ".env", override=False)
    try:
        secrets = json.loads(os.environ.get("JR_SECRETS_JSON") or "{}")
    except ValueError:
        secrets = {}
    for name, value in (secrets if isinstance(secrets, dict) else {}).items():
        if SECRET_NAMES.fullmatch(name) and isinstance(value, str) and value and not os.environ.get(name):
            os.environ[name] = value


load_env()


def on_actions() -> bool:
    return os.environ.get("GITHUB_ACTIONS") == "true"


@dataclass(frozen=True)
class Settings:
    runner: str
    timezone: str
    state_db: Path
    local_db: Path
    cache_dir: Path
    out_dir: Path
    smtp_host: str
    smtp_port: int
    smtp_user: str | None
    smtp_password: str | None
    email_to: str | None
    extra: dict[str, str] = field(default_factory=dict)

    def secret(self, name: str) -> str | None:
        return _env(name)

    def has_secrets(self, names: list[str] | tuple[str, ...]) -> bool:
        return all(_env(n) for n in names)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    home = local_dir()
    from jobradar.config import user_settings     # late import: config reads settings paths

    us = user_settings()
    return Settings(
        runner=_env("JR_RUNNER", "github" if on_actions() else "local"),
        timezone=_env("JR_TIMEZONE", us.get("timezone") or "UTC"),
        state_db=Path(_env("JR_STATE_DB", str(home / "state.db"))).expanduser(),
        local_db=Path(_env("JR_LOCAL_DB", str(home / "local.db"))).expanduser(),
        cache_dir=Path(_env("JR_CACHE_DIR", str(home / "cache"))).expanduser(),
        out_dir=Path(_env("JR_OUT_DIR", str(home / "out"))).expanduser(),
        smtp_host=_env("SMTP_HOST", "smtp.gmail.com"),
        smtp_port=int(_env("SMTP_PORT", "587")),
        smtp_user=_env("SMTP_USER"),
        smtp_password=_env("SMTP_PASSWORD"),
        email_to=_env("EMAIL_TO"),
    )


def reset_caches() -> None:
    """Forget cached settings and config (tests and the wizard change files and env at run time)."""
    get_settings.cache_clear()
    from jobradar import config

    config.clear_caches()
