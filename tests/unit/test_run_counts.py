"""run_source bookkeeping: a run that only re-saw stored postings or title-filtered everything is not "empty"."""

import httpx

from jobradar.http.client import PoliteClient
from jobradar.models import short_hash
from jobradar.pipeline import run
from jobradar.sources.base import SourceSpec, adapter


class MemoryStore:
    def __init__(self, known):
        self.known, self.rows = known, {}

    def known_versions(self, source):
        return dict(self.known)

    def append(self, table, rows):
        self.rows.setdefault(table, []).extend(rows)


@adapter("_test_seen_only")
def _seen_only(ctx):
    ctx.mark_seen(ctx.spec.id, "stored-1")  # relevant + already stored → not re-yielded
    ctx.skipped += 3  # read but title-filtered before yielding
    return iter(())


def test_seen_and_skipped_records_count_as_fetched(monkeypatch):
    spec = SourceSpec(id="t", adapter="_test_seen_only")
    monkeypatch.setattr(run, "load_specs", lambda **k: {"t": spec})
    monkeypatch.setattr(run, "build_client", lambda *a, **k: PoliteClient(
        "t", transport=httpx.MockTransport(lambda r: httpx.Response(404)), sleep=False))
    store = MemoryStore({short_hash("t", "stored-1"): "vh1"})

    summary = run.run_source("t", store)

    assert summary.status == "success" and summary.fetched == 4
    assert [r["posting_key"] for r in store.rows["posting_sightings"]] == [short_hash("t", "stored-1")]
    assert store.rows["source_runs"][0]["records_fetched"] == 4
    assert not store.rows["posting_versions"]


def test_run_sources_parallel_but_polite(monkeypatch):
    import threading
    import time
    from types import SimpleNamespace

    from jobradar.pipeline import run as run_mod

    modules = {"a1": "boards.a", "a2": "boards.a", "b": "boards.b", "c": "boards.c", "d": "boards.d", "e": "boards.e"}
    specs = {sid: SimpleNamespace(adapter=sid) for sid in modules}
    fns = {sid: type("F", (), {"__module__": mod})() for sid, mod in modules.items()}
    monkeypatch.setattr(run_mod, "load_specs", lambda **k: specs)
    monkeypatch.setattr(run_mod, "get_adapter", lambda name: fns[name])
    monkeypatch.setattr(run_mod.MatchEngine, "load", classmethod(lambda cls: None))
    running, peak, overlap, lock = set(), [0], [], threading.Lock()

    def fake_run_source(sid, store, **kw):
        with lock:
            if modules[sid] in {modules[r] for r in running}:
                overlap.append(sid)
            running.add(sid)
            peak[0] = max(peak[0], len(running))
        time.sleep(0.05)
        with lock:
            running.discard(sid)
        return SimpleNamespace(source_id=sid, store=store)

    monkeypatch.setattr(run_mod, "run_source", fake_run_source)
    stores = []
    out = run_mod.run_sources(list(modules), lambda: stores.append(object()) or stores[-1], workers=3)
    assert [s.source_id for s in out] == list(modules)                  # input order kept
    assert peak[0] == 3 and not overlap                                  # ≤ 3 at once, same module never together
    assert len(stores) == 5 and out[0].store is out[1].store            # one store per group
    assert [s.source_id for s in run_mod.run_sources(["b", "a1"], None, dry_run=True, workers=1)] == ["b", "a1"]
