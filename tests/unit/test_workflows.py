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
            for ctx in re.findall(r"\$\{\{\s*([a-z_]+)", str(value)):
                assert ctx in JOB_ENV_CONTEXTS, f"{job_id}.env.{key} uses `{ctx}`, not available in a job's env"
