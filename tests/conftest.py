import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

PROFILES = ROOT / "tests" / "profiles"
EXAMPLE = ROOT / "examples" / "lisbon-backend"

# module/session-scoped fixtures load config before the per-test fixture runs: point them at the test profile too
import tempfile  # noqa: E402

os.environ["JR_USER_DIR"] = str(PROFILES / "pl-data" / "user")
os.environ["JR_HOME"] = tempfile.mkdtemp(prefix="jr_home_")


def use_profile(monkeypatch, user_dir: Path, home: Path | None = None) -> None:
    """Point the config at a user/ folder (and optionally a ~/.job-radar folder) and forget cached config."""
    from jobradar.settings import reset_caches

    monkeypatch.setenv("JR_USER_DIR", str(user_dir))
    if home is not None:
        monkeypatch.setenv("JR_HOME", str(home))
    reset_caches()
    _clear_engine_caches()


def _clear_engine_caches() -> None:
    from jobradar.match import engine
    from jobradar.sources import registry

    engine._load_cfg.cache_clear()
    engine._load_file.cache_clear()
    registry.load_specs.cache_clear()


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """Every test gets an empty ~/.job-radar in tmp, the Polish data test profile as user/, and no real LLM."""
    for name in list(os.environ):
        if name == "JR_LLM_PROVIDER" or (name.startswith("JR_") and "_LLM_" in name) or name in (
                "JR_STATE_DB", "JR_LOCAL_DB", "JR_CACHE_DIR", "JR_OUT_DIR", "GITHUB_ACTIONS"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("JR_RESOLVE_CAP", "0")          # no route look-ups over the network in tests
    home = tmp_path / "jr_home"
    home.mkdir()
    for f in ("experience.md", "draft_style.md", "cv.yaml"):     # the example persona's local-only files
        (home / f).write_bytes((EXAMPLE / "home" / f).read_bytes())
    use_profile(monkeypatch, PROFILES / "pl-data" / "user", home)
    yield
    from jobradar.settings import reset_caches

    reset_caches()
    _clear_engine_caches()
