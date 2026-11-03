from datetime import datetime, timedelta, timezone

from jobradar.pipeline.ats import board_tier, plan_boards

NOW = datetime(2026, 10, 24, tzinfo=timezone.utc)


def _s(**kw):
    return {"last_polled_at": NOW - timedelta(days=40), "consecutive_404": 0, "max_eu": 0, "max_matched": 0,
            "ok_polls": 1, **kw}


def test_board_tier_drops_boards_with_no_eu_jobs():
    assert board_tier(None) == "new"
    assert board_tier(_s(max_matched=1, max_eu=1)) == "A"
    assert board_tier(_s(max_eu=3)) == "B"
    assert board_tier(_s(), "lastround") == "dead"
    assert board_tier(_s(), "open_jobs_data") == "dead"
    # hand-picked boards (the maintainer's, the user's own) stay on the slow cadence
    assert board_tier(_s(), "maintainer") == "C"
    assert board_tier(_s(), "user") == "C"
    # a failed poll (timeout, 5xx, budget) is not evidence of "no EU jobs"
    assert board_tier(_s(ok_polls=0), "lastround") == "C"
    assert board_tier(_s(consecutive_404=3), "user") == "dead"


class _Store:
    def __init__(self, registry, polls):
        self.registry, self.polls = registry, polls

    def ats_registry(self):
        return self.registry

    def ats_poll_summary(self):
        return self.polls


def test_plan_boards_skips_dead_us_boards():
    reg = [{"ats_type": "greenhouse", "ats_slug": s, "origin": o}
           for s, o in [("us", "lastround"), ("eu", "lastround"), ("cur", "maintainer"), ("fresh", "lastround")]]
    polls = {("greenhouse", "us"): _s(), ("greenhouse", "eu"): _s(max_eu=2), ("greenhouse", "cur"): _s()}
    slugs = [b["ats_slug"] for b in plan_boards(_Store(reg, polls), now=NOW)]
    assert slugs == ["eu", "fresh", "cur"]


def test_never_working_slug_is_dead_after_one_404():
    assert board_tier(_s(ok_polls=0, last_status=404), "commoncrawl") == "dead"
    assert board_tier(_s(ok_polls=2, last_status=404, max_eu=1), "commoncrawl") == "B"   # worked before: 3 strikes


def test_sweep_order_shipped_tier_then_home_country_then_origin():
    import json

    def row(v, s, o, **extra):
        return {"ats_type": v, "ats_slug": s, "origin": o, "extra": json.dumps(extra)}

    reg = [row("lever", "lr_b", "lastround", shipped_tier="B"),
           row("greenhouse", "lr_a", "lastround", shipped_tier="A"),
           row("personio", "cc_a_pl", "commoncrawl", shipped_tier="A", country_hint=["PL"]),
           row("greenhouse", "cc_b", "commoncrawl", shipped_tier="B"),
           row("greenhouse", "mine", "user")]
    slugs = [b["ats_slug"] for b in plan_boards(_Store(reg, {}), now=NOW)]
    assert slugs == ["cc_a_pl", "lr_a", "cc_b", "lr_b", "mine"]       # the test profile hunts in PL


def test_lite_polls_only_shipped_tier_a():
    import json

    reg = [{"ats_type": "greenhouse", "ats_slug": f"a{i}", "origin": "lastround",
            "extra": json.dumps({"shipped_tier": "A"})} for i in range(400)]
    reg += [{"ats_type": "greenhouse", "ats_slug": "b", "origin": "lastround", "extra": json.dumps({"shipped_tier": "B"})}]
    plan = plan_boards(_Store(reg, {}), now=NOW, lite=True)
    assert len(plan) == 300 and all(b["ats_slug"].startswith("a") for b in plan)


def test_shipped_registry_loads_once_and_keeps_poll_history(tmp_path):
    from jobradar.pipeline.ats import ensure_registry, shipped_registry
    from jobradar.store.sqlite import SQLiteStore

    store = SQLiteStore(tmp_path / "state.db")
    version, rows = shipped_registry()
    assert len(rows) > 5000 and version
    first = ensure_registry(store)
    assert first["added"] > 5000
    store.append("ats_poll_results", [{"ats_type": "greenhouse", "ats_slug": rows[0]["slug"], "polled_at": NOW,
                                       "http_status": 200, "n_eu": 2}])
    assert ensure_registry(store)["added"] == 0                     # same version: nothing re-added
    assert store.ats_poll_summary()[("greenhouse", rows[0]["slug"])]["max_eu"] == 2
