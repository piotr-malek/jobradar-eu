"""Every non-Python file under src/jobradar must be listed in pyproject's package-data, or a pip install lacks it."""

import fnmatch
import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_package_data_covers_every_data_file():
    patterns = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]["package-data"]["jobradar"]
    pkg = ROOT / "src" / "jobradar"
    missing = [p.relative_to(pkg).as_posix() for p in pkg.rglob("*")
               if p.is_file() and p.suffix not in (".py", ".pyc") and "__pycache__" not in p.parts
               and not any(fnmatch.fnmatch(p.relative_to(pkg).as_posix(), pat) for pat in patterns)]
    assert missing == [], f"add to [tool.setuptools.package-data] in pyproject.toml: {missing}"
