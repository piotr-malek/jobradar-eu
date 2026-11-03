#!/usr/bin/env python3
"""Fail if the repository holds personal data: personal words, e-mail addresses, API keys, key-like files or home
directory paths. Rules live in scripts/personal_data_rules.json; gzip files (recorded fixtures) are read unpacked.

    python scripts/check_personal_data.py            # scan the files git would commit
    python scripts/check_personal_data.py --hash WORD  # the hash to add for a new personal word

Personal words are stored as SHA-256 hashes of their folded form (lowercase, no accents), so the rules file doesn't
spell them out. They're checked as single words and as pairs of adjacent words ("first last").
"""

from __future__ import annotations

import argparse
import fnmatch
import gzip
import hashlib
import json
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RULES = Path(__file__).with_name("personal_data_rules.json")

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
SECRETS = {
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "AWS key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "OpenAI/Anthropic key": re.compile(r"(?<![\w-])sk-(?:ant-|proj-)?[A-Za-z0-9_-]{24,}"),
    "GitHub token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_\w{40,})"),
    "Google API key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "Slack token": re.compile(r"\bxox[bpas]-[0-9A-Za-z-]{10,}"),
}
HOME_PATH = re.compile(r"/users/(?!shared/)[a-z0-9._-]+/|/home/(?!runner/)[a-z0-9._-]+/|[a-z]:\\users\\[a-z0-9._-]+",
                       re.I)                                  # lines are scanned lowercased
KEY_FILES = ["*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*", ".env", ".env.*", "*credentials*.json",
             "*service-account*.json", "*.db", "local.db*", "state.db*"]
WORD = re.compile(r"[a-z0-9]+")


def fold(text: str) -> str:
    text = text.replace("ł", "l").replace("Ł", "L").replace("ø", "o").replace("đ", "d")
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)).lower()


def word_hash(word: str) -> str:
    return hashlib.sha256(" ".join(WORD.findall(fold(word))).encode()).hexdigest()


def looks_random(s: str) -> bool:
    """A real key mixes cases and digits; a URL slug ("desk-java-...") doesn't."""
    body = s.split("-", 1)[-1]
    return any(c.isdigit() for c in body) and any(c.isupper() for c in body) and any(c.islower() for c in body)


def repo_files() -> list[str]:
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout
    return sorted(set(out.split()))


def read_text(path: Path) -> str | None:
    data = path.read_bytes()
    if path.suffix == ".gz":
        try:
            data = gzip.decompress(data)
        except OSError:
            return None
    if b"\0" in data[:4096]:
        return None                                       # binary (images, PDFs)
    return data.decode("utf-8", errors="replace")


def matches(rel: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(rel, p) for p in patterns)


def check(files: list[str], rules: dict) -> list[str]:
    words = {w["sha256"]: w["kind"] for w in rules["words"]}
    allow_words = rules["allow_words"]                    # kind → file globs where that kind may appear
    allow_strings = [s.lower() for s in rules["allow_strings"]]
    allow_emails = {e.lower() for e in rules["allow_emails"]}
    allow_domains = tuple(d.lower() for d in rules["allow_email_domains"])
    skip = rules["skip_files"]
    problems: list[str] = []
    for rel in files:
        if matches(rel, skip):
            continue
        name = rel.rsplit("/", 1)[-1]
        if any(fnmatch.fnmatch(name, p) for p in KEY_FILES) and not matches(rel, rules["allow_key_files"]):
            problems.append(f"{rel}: file name looks like a key, credential or database")
        text = read_text(ROOT / rel)
        if text is None:
            continue
        low = text.lower()
        for s in allow_strings:
            low = low.replace(s, " ")
        for n, line in enumerate(low.splitlines(), 1):
            toks = WORD.findall(fold(line))
            for tok in toks + [f"{a} {b}" for a, b in zip(toks, toks[1:])]:
                kind = words.get(hashlib.sha256(tok.encode()).hexdigest())
                if kind and not matches(rel, allow_words.get(kind, [])):
                    problems.append(f"{rel}:{n}: personal word ({kind})")
            for m in EMAIL.finditer(line):
                e = m.group(0).lower()
                dom = e.split("@", 1)[1]
                if e in allow_emails or dom.endswith(allow_domains) or re.search(r"\.(png|jpe?g|svg|webp|gif)$", e):
                    continue
                if not matches(rel, rules["allow_emails_in"]):
                    problems.append(f"{rel}:{n}: e-mail address {e}")
            if not rel.startswith("tests/fixtures/") and HOME_PATH.search(line):
                problems.append(f"{rel}:{n}: home directory path")
        for label, rx in SECRETS.items():
            for m in rx.finditer(text):
                if label != "OpenAI/Anthropic key" or looks_random(m.group(0)):
                    problems.append(f"{rel}: looks like a {label}")
    return sorted(set(problems))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hash", metavar="WORD", help="print the hash for a personal word and exit")
    ap.add_argument("files", nargs="*", help="files to check (default: every file git would commit)")
    a = ap.parse_args(argv)
    if a.hash:
        print(word_hash(a.hash))
        return 0
    rules = json.loads(RULES.read_text(encoding="utf-8"))
    problems = check(a.files or repo_files(), rules)
    for p in problems:
        print(p)
    if problems:
        print(f"\n{len(problems)} problem(s). Remove the data, or if it is public and intended, add an exception to "
              f"{RULES.relative_to(ROOT)}.", file=sys.stderr)
        return 1
    print("no personal data found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
