"""Setup wizard pages (/setup). The logic lives in jobradar/wizard.py; each step posts JSON and gets back either an
error to show or the next step. `jr setup --step <id>` opens one step; `jr setup --upgrade` switches Lite to Full."""

from __future__ import annotations

from typing import Any

from flask import Blueprint, abort, jsonify, redirect, render_template, request, url_for

from jobradar import config, wizard
from jobradar.settings import local_dir, user_dir

bp = Blueprint("setup", __name__, url_prefix="/setup")

SAVERS = {
    "welcome": wizard.save_welcome,
    "basics": wizard.save_basics,
    "roles": wizard.save_roles,
    "countries_sources": wizard.save_countries_sources,
    "llm": wizard.save_llm,
    "experience": wizard.save_experience,
    "answer_bank": wizard.save_bank,
    "optional_keys": wizard.save_keys,
    "config_repo": wizard.create_config_repo,
}


def _context(step_id: str) -> dict[str, Any]:
    """What each step's page needs to render."""
    st = wizard.load_state()
    p = config.profile()
    ctx: dict[str, Any] = {"st": st, "steps": wizard.steps_for(st["mode"]), "step": step_id,
                           "user_dir": str(user_dir()), "local_dir": str(local_dir()), "profile": p,
                           "settings": config.user_settings()}
    if step_id == "github":
        ctx["gh"] = wizard.github_status()
    elif step_id == "basics":
        ctx["levels"] = wizard.LEVELS
        ctx["level_labels"] = wizard.LEVEL_LABELS
        ctx["lang_names"] = dict(sorted(config.LANG_NAMES.items(), key=lambda kv: kv[1]))
        ctx["country_options"] = wizard.country_options()
    elif step_id == "roles":
        ctx["packs"] = {**config.available_packs(), **config.user_packs()}
        ctx["roles"] = config.roles()
        ctx["seniority"] = config.SENIORITY_LEVELS
    elif step_id == "countries_sources":
        us = config.user_sources()
        countries = us.get("countries") or [((p.get("location") or {}).get("country") or "")]
        ctx["us"] = us
        ctx["countries"] = [c for c in countries if c]
        ctx["country_options"] = wizard.country_options()
    elif step_id == "llm":
        from jobradar.llm.cost import estimate_night

        ctx["presets"] = wizard.LLM_PRESETS
        ctx["llm"] = config.llm_config()
        cur = ctx["llm"].get("default") or {}
        ctx["llm_choice"] = cur.get("preset") or cur.get("provider") or ("gemini" if st["mode"] == "full" else "off")
        ctx["estimate"] = estimate_night()
    elif step_id == "answer_bank":
        from jobradar.local.db import open_local

        db = open_local()
        db.seed_bank()
        ctx["bank"] = db.bank_rows()
    elif step_id == "optional_keys":
        ctx["cards"] = wizard.key_cards(config.user_sources().get("countries") or [])
        ctx["key_labels"] = wizard.KEY_LABELS
        ctx["env"] = {k: bool(v) for k, v in wizard.read_env().items()}
    elif step_id == "extension":
        ctx["extension_dir"] = str(wizard.install_extension())
    elif step_id == "summary":
        from jobradar.doctor import all_green, run_checks

        checks = run_checks()
        ctx["checks"] = checks
        ctx["green"] = all_green(checks)
        ctx["cron"] = wizard.utc_cron(config.user_settings().get("run_at") or "02:00",
                                      config.user_settings().get("timezone") or "UTC")
    return ctx


@bp.get("/")
def index() -> Any:
    if request.args.get("upgrade"):
        wizard.upgrade_to_full()
    return redirect(url_for("setup.step", step_id=wizard.next_step()))


@bp.get("/<step_id>")
def step(step_id: str) -> str:
    if step_id not in wizard.STEP_IDS:
        abort(404)
    return render_template("setup/step.html", **_context(step_id))


@bp.post("/<step_id>")
def save(step_id: str) -> Any:
    if step_id not in wizard.STEP_IDS:
        abort(404)
    data = request.get_json(force=True) or {}
    if data.get("skip"):
        s = next(s for s in wizard.STEPS if s.id == step_id)
        if wizard.load_state()["mode"] not in s.optional_in:
            return jsonify(ok=False, error="this step can't be skipped in this mode"), 400
        wizard.mark(step_id, skipped=True)
        return jsonify(ok=True, next=url_for("setup.step", step_id=wizard.next_step()))
    fn = SAVERS.get(step_id)
    try:
        res = fn(data) if fn else {}
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400
    except Exception as exc:  # noqa: BLE001 - shown on the step
        return jsonify(ok=False, error=f"{type(exc).__name__}: {exc}"[:400]), 500
    if step_id == "github" and not wizard.github_status().get("ok"):
        return jsonify(ok=False, error="Connect GitHub first (the button above)", gh=wizard.github_status()), 400
    if step_id == "extension":
        from jobradar.local.db import open_local

        if not open_local().get_meta("extension_seen"):
            return jsonify(ok=False, error="No sign of the extension yet: open this page in the Chrome where you "
                                           "loaded it, or skip this step"), 400
    pushed = []
    try:
        pushed = wizard.flush_pending()
    except Exception:  # noqa: BLE001 - pushed again after the next step
        pass
    wizard.mark(step_id)
    return jsonify(ok=True, result=res, pushed=pushed, next=url_for("setup.step", step_id=wizard.next_step()))


# ---------------------------------------------------------------- live checks and helpers
@bp.get("/api/cities")
def cities() -> Any:
    from jobradar.normalize.location import _city_index
    from jobradar.normalize.text import fold

    q = fold(request.args.get("q", "")).strip()
    if len(q) < 2:
        return jsonify([])
    best: dict[tuple[str, int], str] = {}          # one name per place: the shortest of its alias names
    for k, v in _city_index().items():
        if k.startswith(q) and v[2] is not None:
            cur = best.get((v[0], v[1]))
            if cur is None or len(k) < len(cur):
                best[(v[0], v[1])] = k
    idx = _city_index()
    hits = sorted(((k, idx[k]) for k in best.values()), key=lambda kv: -kv[1][1])[:12]
    return jsonify([{"name": k.title(), "country": v[0], "population": v[1], "lat": v[2], "lon": v[3]}
                    for k, v in hits])


@bp.post("/api/roles-preview")
def roles_preview() -> Any:
    return jsonify(wizard.roles_preview(request.get_json(force=True) or {}))


@bp.post("/api/pack-draft")
def pack_draft() -> Any:
    from jobradar.match.packdraft import draft_pack, save_pack

    data = request.get_json(force=True) or {}
    res = draft_pack(data.get("description") or "", name=data.get("name") or "custom")
    if res.get("ok") and data.get("save"):
        res["saved"] = str(save_pack(res["pack"], data.get("name") or "custom"))
    return jsonify(res)


@bp.post("/api/sources")
def sources_for() -> Any:
    d = request.get_json(force=True) or {}
    return jsonify(wizard.countries_sources_view(d.get("countries") or [], d.get("opt_in") or [], d.get("disable")))


@bp.post("/api/llm-test")
def llm_test() -> Any:
    return jsonify(wizard.test_llm(request.get_json(force=True) or {}))


@bp.post("/api/cv")
def cv_upload() -> Any:
    """CV file or pasted text → (with an LLM) a drafted profile with the CV line behind each fact."""
    f = request.files.get("file")
    text = wizard.cv_text(f.filename or "cv.txt", f.read()) if f else (request.form.get("text") or "")
    if not text.strip():
        return jsonify(ok=False, error="no text found in the CV"), 400
    from jobradar.llm.client import step_provider

    if step_provider("onboarding") in ("off", ""):
        return jsonify(ok=True, llm=False, text=text[:20000])
    try:
        return jsonify(ok=True, llm=True, **wizard.draft_profile(text))
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=f"{type(exc).__name__}: {exc}"[:300], text=text[:20000])


@bp.post("/api/key-test")
def key_test() -> Any:
    data = request.get_json(force=True) or {}
    return jsonify(wizard.test_key(data.get("card") or "", data.get("values") or {}))


@bp.get("/api/github")
def github() -> Any:
    return jsonify(wizard.github_status())


@bp.post("/api/dry-run")
def dry_run() -> Any:
    return jsonify(wizard.dry_run_preview())


@bp.post("/api/first-run")
def first_run() -> Any:
    try:
        return jsonify(ok=True, **wizard.start_first_run())
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc)[:300]), 400


@bp.get("/api/first-run")
def first_run_status() -> Any:
    try:
        return jsonify(ok=True, run=wizard.first_run_status())
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc)[:300])


@bp.post("/api/extension")
def extension_status() -> Any:
    from jobradar.local.db import open_local

    return jsonify(seen=open_local().get_meta("extension_seen"))


@bp.post("/api/gh-install")
def gh_install() -> Any:
    from jobradar import ghcli

    return jsonify(ghcli.install())


@bp.post("/api/gh-login")
def gh_login() -> Any:
    from jobradar import ghcli

    return jsonify(ghcli.start_login(refresh=bool((request.get_json(silent=True) or {}).get("refresh"))))


@bp.get("/api/gh-login")
def gh_login_state() -> Any:
    from jobradar import ghcli

    st = ghcli.login_state()
    if st.get("done"):
        st["status"] = wizard.github_status()
    return jsonify(st)


@bp.get("/api/chromium")
def chromium_status() -> Any:
    return jsonify(ok=True, **wizard.chromium_status())


@bp.post("/api/chromium")
def chromium_install() -> Any:
    out = wizard.install_chromium()
    return jsonify(ok=bool(out.get("browser")), **out)


@bp.post("/api/shortcut")
def shortcut() -> Any:
    from jobradar.shortcut import create_shortcut

    try:
        return jsonify(ok=True, path=str(create_shortcut()))
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc)[:300]), 400
