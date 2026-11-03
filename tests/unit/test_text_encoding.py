"""Text file I/O must name its encoding: without it Windows uses cp1252 and breaks on any non-ASCII text."""

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2]


def _offenders():
    for root in ("src", "tests", "scripts"):
        for p in (ROOT / root).rglob("*.py"):
            for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.Call) or "encoding" in {k.arg for k in node.keywords}:
                    continue
                f = node.func
                name = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")
                if name in ("read_text", "write_text"):
                    yield f"{p.relative_to(ROOT)}:{node.lineno}: {name}() without encoding"
                elif name == "open" and isinstance(f, ast.Name):
                    mode = node.args[1] if len(node.args) > 1 else next((k.value for k in node.keywords if k.arg == "mode"), None)
                    if not (isinstance(mode, ast.Constant) and "b" in str(mode.value)):
                        yield f"{p.relative_to(ROOT)}:{node.lineno}: open() in text mode without encoding"


def test_text_io_names_its_encoding():
    assert list(_offenders()) == []
