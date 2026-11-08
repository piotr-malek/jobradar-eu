// Setup wizard: each step's form is posted as JSON to /setup/<step>; the reply says what's next or what's wrong.
const WIZ = document.getElementById("wiz");
const STEP = WIZ ? WIZ.dataset.step : "";
const lines = v => (v || "").split(/\n/).map(s => s.trim()).filter(Boolean);

function formData() {
  const f = WIZ, d = {};
  for (const el of f.querySelectorAll("input[name], select[name], textarea[name]")) {
    if (el.name.endsWith("[]")) {
      const k = el.name.slice(0, -2);
      d[k] = d[k] || [];
      if (el.checked) d[k].push(el.value);
    } else if (el.type === "radio") {
      if (el.checked) d[el.name] = el.value;
    } else if (el.type === "checkbox") {
      d[el.name] = el.checked;
    } else if (!["lang_code", "lang_level"].includes(el.name)) {
      d[el.name] = el.value;
    }
  }
  if (STEP === "basics") {
    d.languages = {};
    for (const row of f.querySelectorAll(".lang")) {
      const code = row.querySelector("[name=lang_code]").value.trim();
      if (code) d.languages[code] = row.querySelector("[name=lang_level]").value;
    }
    if ((d.work_setups || []).includes("remote_country") && d.residency_elsewhere) d.work_setups.push("remote_eu");
  }
  if (STEP === "roles") for (const k of ["never", "must_have", "extra_titles"]) d[k] = lines(d[k]);
  if (STEP === "experience") for (const k of ["priority_roles", "target_roles", "strong_stack", "nice_stack", "highlights"]) d[k] = lines(d[k]);
  if (STEP === "countries_sources") d.disable = [...f.querySelectorAll("input.src")].filter(c => !c.checked).map(c => c.value);
  if (STEP === "answer_bank") {
    d.answers = {}; d.approve = [];
    for (const el of f.querySelectorAll(".bank-answer")) d.answers[el.dataset.id] = el.value;
    for (const el of f.querySelectorAll(".bank-ok")) if (el.checked) d.approve.push(el.dataset.id);
  }
  if (STEP === "optional_keys") {
    d.keys = {};
    for (const el of f.querySelectorAll("input.key")) if (el.value.trim()) d.keys[el.name] = el.value.trim();
    const senders = document.getElementById("alert-senders");
    if (senders) d.alert_senders = lines(senders.value);
  }
  return d;
}

async function saveStep(e) {
  e.preventDefault();
  const btn = WIZ.querySelector("button[type=submit]");
  busy(btn, true, STEP === "config_repo" ? "Creating…" : "Saving…");
  try {
    const r = await fetch(`/setup/${STEP}`, {method: "POST", headers: {"Content-Type": "application/json", "X-JR": "1"},
                                             body: JSON.stringify(formData())});
    const d = await r.json();
    if (!d.ok) { toast(d.error || `Error ${r.status}`, true); return; }
    if (STEP === "config_repo" && d.result) {
      document.getElementById("repo-out").innerHTML = `<section><p><b>${esc(d.result.repo)}</b> ready:
        <a href="${esc(d.result.url)}" target="_blank" rel="noopener">open on GitHub ↗</a></p>
        <ul>${d.result.log.map(l => `<li>${esc(l)}</li>`).join("")}</ul></section>`;
      setTimeout(() => (location.href = d.next), 2500);
      return;
    }
    if (d.result && d.result.warning) toast(d.result.warning, true);
    location.href = d.next;
  } finally { busy(btn, false); }
}

async function skipStep(btn) {
  const d = await post(`/setup/${STEP}`, {skip: true});
  location.href = d.next;
}

let cityTimer;
function cityLookup(el) {
  clearTimeout(cityTimer);
  cityTimer = setTimeout(async () => {
    const hits = await (await fetch(`/setup/api/cities?q=${encodeURIComponent(el.value)}`)).json();
    const sel = WIZ.elements.country, name = cc => (sel.querySelector(`option[value="${cc}"]`) || {}).textContent || cc;
    document.getElementById("city-list").innerHTML = hits.map(h =>
      `<option value="${esc(h.name)}">${esc(h.name)}, ${esc(name(h.country))}</option>`).join("");
    const hit = hits.find(h => h.name.toLowerCase() === el.value.trim().toLowerCase());
    if (hit) {
      if (!sel.dataset.touched && sel.querySelector(`option[value="${hit.country}"]`)) sel.value = hit.country;
      WIZ.elements.lat.value = hit.lat; WIZ.elements.lon.value = hit.lon;
    }
  }, 200);
}

function addLang() {
  const rows = document.getElementById("langs"), first = rows.querySelector(".lang");
  const row = first.cloneNode(true), taken = [...rows.querySelectorAll("[name=lang_code]")].map(s => s.value);
  const sel = row.querySelector("[name=lang_code]");
  sel.value = "";
  row.querySelector("[name=lang_level]").value = "B2";
  rows.appendChild(row);
}

let previewTimer;
function rolesPreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(async () => {
    const d = formData();
    for (const k of ["never", "must_have", "extra_titles"]) d[k] = lines(WIZ.elements[k].value);
    const r = await post("/setup/api/roles-preview", d);
    const li = (rows, cls) => rows.map(x => `<li class="${cls}" title="${esc(x.why || "")}">${esc(x.title)}</li>`).join("");
    document.getElementById("preview").innerHTML = `<div class="grid"><div><h4>Kept (${r.kept.length})</h4><ul>${li(r.kept, "good")}</ul></div>
      <div><h4>Dropped (${r.dropped.length})</h4><ul>${li(r.dropped.slice(0, 60), "muted")}</ul></div></div>`;
  }, 300);
}

async function packDraft(btn) {
  busy(btn, true, "Drafting…");
  try {
    const d = await post("/setup/api/pack-draft", {description: document.getElementById("pack-desc").value, save: true});
    const out = document.getElementById("pack-out");
    if (!d.ok) { out.innerHTML = `<p class="bad">${esc(d.error || "The draft didn't pass its own checks; try a clearer description.")}</p>`; return; }
    out.innerHTML = `<p>Saved as a custom pack. Matches ${d.keep_ok.length} of its sample titles; ${d.drop_leaked.length} wrong ones leak.
      Reload the page and tick <b>custom</b>.</p>`;
  } finally { busy(btn, false); }
}

async function sourcesFor() {
  const checked = n => [...WIZ.querySelectorAll(`input[name="${n}[]"]:checked`)].map(c => c.value);
  const countries = checked("countries");
  document.getElementById("opt-pl").hidden = !countries.includes("PL");
  if (!countries.includes("PL")) WIZ.querySelector('input[value=polish_only]').checked = false;
  const disable = [...WIZ.querySelectorAll("input.src")].filter(c => !c.checked).map(c => c.value);
  const v = await post("/setup/api/sources", {countries, opt_in: checked("opt_in"), disable});
  const note = s => s.note ? `<span class="muted">${esc(s.note)}</span>` : "";
  document.getElementById("n-ready").textContent = v.ready;
  document.getElementById("sources").innerHTML = v.on.map(s => `<tr><td><input type="checkbox" class="src" value="${esc(s.id)}" ${s.checked ? "checked" : ""} onchange="sourcesFor()"></td>
    <td>${esc(s.label)}</td><td class="muted">${esc(s.covers)}</td><td>${note(s)}</td></tr>`).join("");
  document.getElementById("sources-off").innerHTML = v.off.map(s => `<tr><td>${esc(s.label)}</td><td class="muted">${esc(s.covers)}</td>
    <td class="muted">${esc(s.why)}</td></tr>`).join("");
  document.getElementById("boards-off").hidden = !v.off.length;
}

async function ghInstall(btn) {
  busy(btn, true, "Downloading…");
  try {
    const d = await post("/setup/api/gh-install");
    if (!d.ok) { toast(d.error, true); return; }
    location.reload();
  } finally { busy(btn, false); }
}

async function ghLogin(btn, refresh) {
  busy(btn, true, "Starting…");
  try {
    const d = await post("/setup/api/gh-login", {refresh});
    if (!d.ok) { toast(d.error, true); return; }
    if (!d.code && !d.done) { toast("GitHub didn't answer; try again in a moment", true); return; }
    btn.hidden = true;
    document.getElementById("gh-code").hidden = false;
    document.getElementById("gh-code-text").textContent = d.code || "";
    if (d.url) document.getElementById("gh-url").href = d.url;
    const poll = async () => {
      const s = await (await fetch("/setup/api/gh-login")).json();
      if (s.done && s.status && s.status.ok) { location.reload(); return; }
      if (s.error) { document.getElementById("gh-wait").textContent = `Sign-in stopped: ${s.error}. Reload the page to try again.`; return; }
      setTimeout(poll, 2000);
    };
    setTimeout(poll, 2000);
  } finally { busy(btn, false); }
}

async function llmTest(btn) {
  busy(btn, true, "Testing…");
  const out = document.getElementById("llm-out");
  try {
    const d = await post("/setup/api/llm-test", formData());
    out.textContent = d.ok ? `Works: ${d.provider} · ${d.model} answered in ${d.seconds}s` : `Failed: ${d.error}`;
    out.className = d.ok ? "good" : "bad";
  } finally { busy(btn, false); }
}

async function cvUpload(btn) {
  const file = document.getElementById("cv-file").files[0], text = document.getElementById("cv-paste").value;
  const fd = new FormData();
  if (file) fd.append("file", file); else fd.append("text", text);
  busy(btn, true, "Reading…");
  try {
    const r = await fetch("/setup/api/cv", {method: "POST", headers: {"X-JR": "1"}, body: fd});
    const d = await r.json();
    if (!d.ok) { toast(d.error, true); return; }
    if (!d.llm) { toast("No AI model set: fill the fields by hand (the CV text is below)"); document.getElementById("cv-paste").value = d.text; return; }
    for (const [k, v] of Object.entries(d.fields)) {
      const el = WIZ.elements[k];
      if (el) el.value = Array.isArray(v) ? v.join("\n") : v;
    }
    WIZ.elements.experience_md.value = d.experience_md || "";
    document.getElementById("cv-sources").innerHTML = "<section><h3>Where each fact came from</h3>" +
      Object.entries(d.sources).map(([k, src]) => `<p><b>${esc(k)}</b>: ${src.map(s => `<q>${esc(s)}</q>`).join(" ")}</p>`).join("") + "</section>";
    toast("Drafted from your CV: check every field");
  } finally { busy(btn, false); }
}

async function keyTest(btn) {
  const card = btn.closest(".keycard"), values = {};
  for (const el of card.querySelectorAll("input.key")) if (el.value.trim()) values[el.name] = el.value.trim();
  busy(btn, true, "Testing…");
  try {
    const d = await post("/setup/api/key-test", {card: card.dataset.card, values});
    const out = card.querySelector(".out");
    out.textContent = d.detail; out.className = "out " + (d.ok ? "good" : "bad");
  } finally { busy(btn, false); }
}

async function findSenders(btn) {
  const card = btn.closest(".keycard"), values = {};
  for (const el of card.querySelectorAll("input.key")) if (el.value.trim()) values[el.name] = el.value.trim();
  busy(btn, true, "Reading the last 30 days of e-mail headers…");
  try {
    const d = await post("/setup/api/alert-senders", {values});
    const out = document.getElementById("sender-out");
    out.innerHTML = `<p class="${d.ok ? "muted" : "bad"}">${esc(d.detail)}</p>` + d.senders.map(s =>
      `<label class="inline"><input type="checkbox" class="sender-pick" value="${esc(s.sender)}"> <b>${esc(s.sender)}</b>
       <span class="muted">${s.mails} e-mails · “${esc(s.subject)}”</span></label>`).join("");
    for (const box of out.querySelectorAll(".sender-pick")) box.addEventListener("change", () => {
      const ta = document.getElementById("alert-senders"), cur = lines(ta.value);
      ta.value = (box.checked ? [...cur, box.value] : cur.filter(x => x !== box.value)).filter((x, i, a) => a.indexOf(x) === i).join("\n");
    });
  } finally { busy(btn, false); }
}

async function dryRun(btn) {
  busy(btn, true, "Fetching a few boards (about a minute)…");
  try {
    const d = await post("/setup/api/dry-run");
    document.getElementById("dry-out").innerHTML = `<section><p>${d.sources.map(s => `${esc(s.id)}: ${s.fetched} read, ${s.matched} matched`).join(" · ")}</p>
      <p><b>${d.would_shortlist || 0}</b> jobs would be on tonight's list${d.preview.length ? ":" : "."}</p>
      <ul>${d.preview.slice(0, 20).map(p => `<li>${esc(p.title)} <span class="muted">${esc(p.company)} · ${esc(p.location)}</span></li>`).join("")}</ul></section>`;
  } finally { busy(btn, false); }
}

async function firstRun(btn) {
  busy(btn, true, "Starting…");
  try {
    const d = await post("/setup/api/first-run");
    const out = document.getElementById("run-out");
    out.innerHTML = `<p>Started: <a href="${esc(d.actions)}" target="_blank" rel="noopener">follow it on GitHub ↗</a> · <span id="run-state">queued</span></p>`;
    const poll = async () => {
      const s = await (await fetch("/setup/api/first-run")).json();
      const run = (s.run || {});
      document.getElementById("run-state").textContent = `${run.status || "…"} ${run.conclusion || ""}`;
      if (run.status === "completed") {
        if (run.conclusion === "success") { await post("/sync"); location.href = "/"; }
        return;
      }
      setTimeout(poll, 15000);
    };
    setTimeout(poll, 10000);
  } finally { busy(btn, false); }
}

async function makeShortcut(btn) {
  const d = await post("/setup/api/shortcut");
  toast(d.ok ? `Shortcut: ${d.path}` : d.error, !d.ok);
}

function chromiumShow(d) {
  const el = document.getElementById("chromium-status"), btn = document.getElementById("chromium-btn");
  const fix = document.getElementById("chromium-fix");
  if (!el) return;
  btn.hidden = true; fix.hidden = true;
  if (d.browser) { el.textContent = "Ready ✓"; el.className = "check good"; return; }
  el.className = "check warn";
  if (!d.package) {
    el.textContent = "The PDF package isn't installed. Run this in a terminal, then reload:";
    fix.textContent = d.fix; fix.hidden = false; return;
  }
  el.textContent = d.error ? `Download failed: ${d.error}` : "Chromium not downloaded yet";
  btn.hidden = false;
}

async function chromiumInstall(btn) {
  busy(btn, true, "Downloading… (a few minutes)");
  try { chromiumShow(await post("/setup/api/chromium")); } finally { busy(btn, false); }
}

if (STEP === "experience" && document.getElementById("chromium-status")) {
  document.addEventListener("DOMContentLoaded", async () => {
    chromiumShow(await (await fetch("/setup/api/chromium")).json());
  });
}
if (STEP === "roles") document.addEventListener("DOMContentLoaded", rolesPreview);
if (STEP === "countries_sources") document.addEventListener("DOMContentLoaded", sourcesFor);
if (STEP === "basics") document.addEventListener("DOMContentLoaded", () => {
  WIZ.elements.country.addEventListener("change", e => (e.target.dataset.touched = "1"));
});
if (STEP === "basics") document.addEventListener("DOMContentLoaded", () => {
  const tz = document.getElementById("tz");
  if (tz && !tz.value) tz.value = Intl.DateTimeFormat().resolvedOptions().timeZone || "";
});
if (STEP === "extension") {
  const check = async () => {
    const d = await post("/setup/api/extension");
    const el = document.getElementById("ext-status");
    if (d.seen) { el.textContent = "Connected ✓"; el.className = "check good"; return; }
    setTimeout(check, 3000);
  };
  document.addEventListener("DOMContentLoaded", check);
}
