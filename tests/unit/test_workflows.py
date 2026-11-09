"""GitHub rejects a workflow file before running anything when a job's `env` uses a context that isn't available
there (a 0-second "workflow file issue" failure). Catch that here; CI also runs actionlint."""

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[2]
WORKFLOWS = sorted([*ROOT.glob(".github/workflows/*.yml"), *ROOT.glob("template/.github/workflows/*.yml")])
JOB_ENV_CONTEXTS = {"github", "needs", "strategy", "matrix", "vars", "secrets", "inputs", "env"}


@pytest.mark.parametrize("path", WORKFLOWS, ids=[p.relative_to(ROOT).as_posix() for p in WORKFLOWS])
def test_job_env_uses_only_allowed_contexts(path):
    wf = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert wf.get("jobs"), "no jobs"
    for job_id, job in wf["jobs"].items():
        for key, value in (job.get("env") or {}).items():
            for expr in re.findall(r"\$\{\{(.*?)\}\}", str(value)):
                for ctx in re.findall(r"(?<![\w.])([A-Za-z_]\w*)(?!\w|\s*\()", expr):
                    assert ctx in JOB_ENV_CONTEXTS, f"{job_id}.env.{key} uses `{ctx}`, not available in a job's env"


def test_template_hands_every_secret_to_the_run():
    wf = yaml.safe_load((ROOT / "template/.github/workflows/jobradar.yml").read_text(encoding="utf-8"))
    env = wf["jobs"]["run"]["env"]
    assert env["JR_SECRETS_JSON"].replace(" ", "") == "${{toJSON(secrets)}}"
    assert not [k for k in env if k.endswith(("_API_KEY", "_PASSWORD"))], "secrets go through JR_SECRETS_JSON"


def test_load_env_reads_known_secrets_from_json(monkeypatch, tmp_path):
    import json

    from jobradar import settings

    for k in ("JOOBLE_IT_API_KEY", "ADZUNA_APP_ID", "SMTP_HOST", "github_token"):
        monkeypatch.setenv(k, "")                 # empty counts as unset; monkeypatch restores whatever load_env sets
    monkeypatch.setenv("JR_HOME", str(tmp_path))
    monkeypatch.setenv("GEMINI_API_KEY", "from-env")
    monkeypatch.setenv("JR_SECRETS_JSON", json.dumps({
        "github_token": "ghs_x", "JOOBLE_IT_API_KEY": "j", "ADZUNA_APP_ID": "a", "SMTP_HOST": "smtp.example.com",
        "GEMINI_API_KEY": "from-secret", "EMPTY": ""}))
    settings.load_env()
    import os

    assert os.environ["JOOBLE_IT_API_KEY"] == "j" and os.environ["ADZUNA_APP_ID"] == "a"
    assert os.environ["SMTP_HOST"] == "smtp.example.com"
    assert os.environ["GEMINI_API_KEY"] == "from-env"
    assert os.environ["github_token"] == ""
