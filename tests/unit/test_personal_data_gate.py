"""scripts/check_personal_data.py: what it flags and what it lets through."""

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).parents[2]
AT = "@"  # sample addresses are built at runtime so this file passes the gate itself
spec = importlib.util.spec_from_file_location("gate", ROOT / "scripts" / "check_personal_data.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def _rules(**extra):
    rules = {"words": [{"kind": "name", "sha256": gate.word_hash("Zorblax")},
                       {"kind": "project", "sha256": gate.word_hash("quiet harbor")}],
             "allow_words": {"name": ["NOTICE"]}, "allow_strings": ["zorblax/jobradar-eu"],
             "allow_emails": ["bot" + AT + "users.noreply.github.com"], "allow_email_domains": ["example.com"],
             "allow_emails_in": [], "allow_key_files": [], "skip_files": []}
    return {**rules, **extra}


def _check(tmp_path, monkeypatch, files: dict[str, str], **rules):
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text, encoding="utf-8")
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    return gate.check(sorted(files), _rules(**rules))


def test_flags_words_pairs_emails_keys_and_home_paths(tmp_path, monkeypatch):
    found = _check(tmp_path, monkeypatch, {
        "a.py": "# written by Zórblax\n",
        "b.txt": "project id quiet-harbor-123\n",
        "c.txt": "mail me: someone" + AT + "realmail.eu\n",
        "d.txt": "key = s" + "k-proj-Ab3dEf6hIj9kLm2nOp5qRs8t\n",
        "e.txt": "open /" + "Users/someone/notes.txt\n",
        ".env": "X=1\n",
    })
    assert any(p.startswith("a.py:1: personal word (name)") for p in found)
    assert any(p.startswith("b.txt:1: personal word (project)") for p in found)
    assert any("someone" + AT + "realmail.eu" in p for p in found)
    assert any(p.startswith("d.txt: looks like a OpenAI/Anthropic key") for p in found)
    assert any(p.startswith("e.txt:1: home directory path") for p in found)
    assert any(p.startswith(".env: file name") for p in found)


def test_lets_through_allowed_and_lookalike_text(tmp_path, monkeypatch):
    found = _check(tmp_path, monkeypatch, {
        "NOTICE": "Copyright Zorblax\n",
        "README.md": f"https://github.com/zorblax/jobradar-eu\nbot{AT}users.noreply.github.com jobs{AT}example.com\n",
        "slug.txt": f"https://magnet.me/desk-java-eiland-netherlands-developer-amsterdam\nimg{AT}2x.png\n",
        "harbor.txt": "a quiet port; harbor master\n",
    })
    assert found == []


def test_repository_is_clean():
    rules = json.loads((ROOT / "scripts" / "personal_data_rules.json").read_text(encoding="utf-8"))
    assert gate.check(gate.repo_files(), rules) == []
