// Setup wizard: each step's form is posted as JSON to /setup/<step>; the reply says what's next or what's wrong.
const WIZ = document.getElementById("wiz");
const STEP = WIZ ? WIZ.dataset.step : "";
const lines = v => (v || "").split(/\n/).map(s => s.trim()).filter(Boolean);
const codes = v => (v || "").split(/[,\s]+/).map(s => s.trim().toUpperCase()).filter(Boolean);

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
    d.work_countries = codes(d.work_countries);
    d.relocate_to = codes(d.relocate_to);
    d.contracts = (d.contracts || "").split(",").map(s => s.trim()).filter(Boolean);
  }
  if (STEP === "roles") for (const k of ["never", "must_have", "extra_titles"]) d[k] = lines(d[k]);
  if (STEP === "experience") for (const k of ["priority_roles", "target_roles", "strong_stack", "nice_stack", "highlights"]) d[k] = lines(d[k]);
  if (STEP === "countries_sources") {
    d.countries = codes(d.countries);
    d.disable = [...f.querySelectorAll("input.src")].filter(c => !c.checked).map(c => c.value);
  }
  if (STEP === "answer_bank") {
    d.answers = {}; d.approve = [];
    for (const el of f.querySelectorAll(".bank-answer")) d.answers[el.dataset.id] = el.value;
    for (const el of f.querySelectorAll(".bank-ok")) if (el.checked) d.approve.push(el.dataset.id);
  }
  if (STEP === "optional_keys") {
    d.keys = {};
    for (const el of f.querySelectorAll("input.key")) if (el.value.trim()) d.keys[el.name] = el.value.trim();
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
    document.getElementById("city-list").innerHTML = hits.map(h =>
      `<option value="${esc(h.name)}" data-cc="${h.country}" data-lat="${h.lat}" data-lon="${h.lon}">${esc(h.name)}, ${h.country}</option>`).join("");
    const hit = hits.find(h => h.name.toLowerCase() === el.value.trim().toLowerCase());
    if (hit) {
      WIZ.elements.country.value = WIZ.elements.country.value || hit.country;
      WIZ.elements.lat.value = hit.lat; WIZ.elements.lon.value = hit.lon;
    }
  }, 200);
}

function addLang() {
  const rows = document.getElementById("langs"), first = rows.querySelector(".lang");
  const row = first.cloneNode(true);
  row.querySelector("input").value = "";
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

async function sourcesFor(el) {
  const rows = await post("/setup/api/sources", {countries: codes(el.value)});
  document.querySelector("#sources tbody").innerHTML = rows.map(s => `<tr><td><input type="checkbox" class="src" value="${esc(s.id)}" checked></td>
    <td>${esc(s.label)}</td><td>${esc(s.countries.join(", "))}</td><td class="muted">${s.keys.length ? (s.has_keys ? "key set" : "free key: add it in Free extras") : ""}${s.local_only ? " · runs from your computer only" : ""}${s.opt_in ? " · " + esc(s.opt_in) : ""}</td></tr>`).join("");
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
