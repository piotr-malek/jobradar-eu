// Job Radar apply app. Every POST carries X-JR: 1 (the server rejects POSTs without it).
function toast(msg, bad) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.className = bad ? "show bad" : "show";
  clearTimeout(t._h);
  t._h = setTimeout(() => (t.className = ""), 3500);
}

async function post(url, body) {
  const r = await fetch(url, {method: "POST", headers: {"Content-Type": "application/json", "X-JR": "1"},
                              body: JSON.stringify(body || {})});
  let data = {};
  try { data = await r.json(); } catch (e) { data = {ok: false, error: r.statusText}; }
  if (!r.ok || data.ok === false) { toast(data.error || `Error ${r.status}`, true); throw new Error(data.error); }
  return data;
}

// A button that is working: disabled, with a spinner and optionally another label until it is done.
function busy(btn, on, label) {
  if (!btn) return;
  if (on && label) { btn.dataset.label = btn.textContent; btn.textContent = label; }
  if (!on && btn.dataset.label) { btn.textContent = btn.dataset.label; delete btn.dataset.label; }
  btn.disabled = on; btn.classList.toggle("busy", on);
}

function esc(s) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }

// Slow actions run on the server as tasks: start one, then show its steps live until it ends.
async function startTask(btn, url, body, onDone) {
  busy(btn, true);
  try {
    const d = await post(url, body);
    if (d.already) toast("Something is already running for this job; showing it");
    watchTask(d.task, onDone);
  } finally { busy(btn, false); }
}

function watchTask(key, onDone) {
  const box = document.getElementById("task");
  box.hidden = false;
  box.className = "task";
  // built once and then updated in place: rebuilding it would restart the progress bar's animation
  box.innerHTML = '<div class="task-head"></div><div class="meter busy"><div></div></div><div class="task-body"></div>';
  const head = box.querySelector(".task-head"), meter = box.querySelector(".meter"), body = box.querySelector(".task-body");
  document.body.classList.add("tasking");
  let t = null, seenAt = 0;
  const secs = s => s < 60 ? `${Math.round(s)} s` : `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`;
  const draw = () => {
    if (!t) { head.innerHTML = "<b>Starting…</b>"; return; }
    const elapsed = t.elapsed + (t.running ? (Date.now() - seenAt) / 1000 : 0);
    const title = t.error ? "Failed" : t.running ? t.what : `${t.what}: done`;
    head.innerHTML = `<b>${esc(title)}</b> <span class="muted">${secs(elapsed)}</span>`
      + (t.total ? ` <span class="muted">· ${t.done}/${t.total}</span>` : "")
      + (t.running ? "" : '<button class="close" onclick="closeTask()">Close</button>');
    box.className = t.error ? "task bad" : "task";
    meter.hidden = !t.running;
    meter.classList.toggle("busy", !t.total);
    meter.firstChild.style.width = t.total ? `${Math.round(100 * t.done / t.total)}%` : "";
    const plain = t.steps.filter(s => !s.kind);
    const steps = t.steps.map(s => {
      const cls = s.kind || (s === plain[plain.length - 1] && t.running ? "current" : "done");
      return `<li class="${cls}">${esc(s.msg)}${s.kind ? "" : ` <span class="muted">${secs(s.t)}</span>`}</li>`;
    }).join("");
    const html = (t.total && t.running && t.now ? `<p class="now">${esc(t.now)}…</p>` : "")
      + (t.error ? `<p class="err">${esc(t.error)}</p>` : "")
      + (steps ? `<ol class="steps">${steps}</ol>` : "");
    if (body.innerHTML !== html) body.innerHTML = html;   // spinner keeps turning when nothing changed
  };
  const timer = setInterval(draw, 500);
  const poll = async () => {
    let fresh = null;
    try { fresh = await (await fetch(`/task/${encodeURIComponent(key)}`)).json(); }
    catch (e) { /* app restarting; keep polling */ }
    if (fresh) { t = fresh; seenAt = Date.now(); }
    if (t && t.missing) { clearInterval(timer); closeTask(); return; }
    draw();
    if (!t || t.running) { setTimeout(poll, 800); return; }
    clearInterval(timer);
    document.body.classList.remove("tasking");
    if (!t.error) (onDone || (() => location.reload()))(t.result || {});
  };
  draw();
  poll();
}

function closeTask() {
  const box = document.getElementById("task");
  box.hidden = true; box.innerHTML = "";
  document.body.classList.remove("tasking");
}

async function prepAll(btn, force) {
  await startTask(btn, "/prep-all", {force}, r => {
    toast(`${r.ready} read and answered · ${r.manual} need the form's questions` + (r.failed ? ` · ${r.failed} failed` : ""));
    setTimeout(() => location.reload(), 1500);
  });
}

async function prepOne(btn) {
  await startTask(btn, `/job/${JOB_ID}/prep`);
}

async function saveAnswer(btn, scope) {
  const q = btn.closest(".q");
  const text = q.querySelector("textarea").value.trim();
  if (!text) { toast("Write an answer first", true); return; }
  busy(btn, true);
  try {
    await post(`/job/${JOB_ID}/answer`, {key: q.dataset.key, text, scope});
    toast(scope === "bank" ? "Saved to the bank (Approved)" : "Used for this application");
    location.reload();
  } finally { busy(btn, false); }
}

async function chooseOption(btn) {
  const q = btn.closest(".q");
  await post(`/job/${JOB_ID}/answer`, {key: q.dataset.key, option: q.querySelector("select").value, scope: "once"});
  location.reload();
}

async function chooseCv(btn) {
  busy(btn, true);
  try {
    const d = await post(`/job/${JOB_ID}/cv`, {variant: document.getElementById("cv-variant").value});
    toast(`CV for this job: ${d.variant}`);
    location.reload();
  } finally { busy(btn, false); }
}

// Auto-fill runs in this browser through the Job Radar extension (extension/): it opens the form in a new tab,
// fills it and replies with a report. Nothing here or there ever submits.
function extensionFill(payload) {
  return new Promise(resolve => {
    const timer = setTimeout(() => done({ok: false, error: "No answer from the Job Radar extension"}), 120000);
    const on = e => { if (e.source === window && e.data && e.data.jr === "fill-done") done(e.data); };
    const done = res => { clearTimeout(timer); removeEventListener("message", on); resolve(res); };
    addEventListener("message", on);
    window.postMessage({jr: "fill", payload}, location.origin);
  });
}

// Auto-fill needs the extension, which is in Chrome. Clicked in another browser (Cursor's built-in one, where the app
// usually is), it opens this job page in Chrome with ?autofill=1 and the fill starts there by itself.
const NO_EXTENSION = "The Job Radar extension isn't on in this Chrome: chrome://extensions → Load unpacked → jobradar/extension";
const inChrome = () => !/Electron\//.test(navigator.userAgent)
  && ((navigator.userAgentData || {}).brands || []).some(b => b.brand === "Google Chrome");

async function fill(btn) {
  if (!document.documentElement.dataset.jrFiller) {
    if (inChrome()) { toast(NO_EXTENSION, true); return; }
    busy(btn, true, "Opening Chrome…");
    try {
      await post("/open-in-chrome", {path: `${location.pathname}?autofill=1`});
      toast("Opened in Chrome: the form fills there");
    } finally { busy(btn, false); }
    return;
  }
  busy(btn, true, "Filling…");
  try {
    const {payload} = await post(`/job/${JOB_ID}/fill`);
    const res = await extensionFill(payload);
    if (!res.ok) { toast(res.error || "The extension couldn't fill the form", true); return; }
    const r = await post(`/job/${JOB_ID}/fill-report`, {report: res.report});
    toast(`Filled ${r.filled}/${r.total}. Check the form's tab, then click Submit there yourself.`);
    setTimeout(() => location.reload(), 1500);
  } finally { busy(btn, false); }
}

async function markApplied(id) {
  if (!confirm("Did you click Submit in the form? This marks the job applied.")) return;
  const btn = event.target; busy(btn, true, "Saving…");
  try { await post(`/job/${id}/applied`); } catch (e) { busy(btn, false); return; }
  toast("Marked applied");
  setTimeout(() => (location.href = "/?tab=applied"), 800);
}

// Pick / skip / later / a later stage. In the list the card leaves; on a job page the page reloads.
// A pick opens the posting in a new tab.
async function decide(el, status, openPosting) {
  const card = el.closest ? el.closest(".job") : null;
  const id = card ? card.dataset.id : (typeof JOB_ID !== "undefined" ? JOB_ID : null);
  if (!id) return;
  if (openPosting && card && card.dataset.url) window.open(card.dataset.url, "_blank", "noopener");
  try { await post("/decide", {job_id: id, status}); } catch (e) { return; }
  if (card && TAB !== "applied") {
    const next = card.nextElementSibling;
    card.remove();
    if (next) next.focus();
    toast(status === "picked" ? "Picked" : status === "skipped" ? "Skipped" : status);
  } else {
    toast(`Status: ${status}`);
    setTimeout(() => location.reload(), 400);
  }
}

// keyboard: j/k move, p pick, s skip, l later, o open the posting, Enter details
document.addEventListener("keydown", e => {
  if (typeof TAB === "undefined" || ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)) return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const cards = [...document.querySelectorAll(".job")];
  if (!cards.length) return;
  let cur = document.activeElement.closest ? document.activeElement.closest(".job") : null;
  if (!cur) { cur = cards[0]; if ("jk".includes(e.key)) { cur.focus(); return; } }
  const i = cards.indexOf(cur);
  const keys = {
    j: () => (cards[i + 1] || cur).focus(), k: () => (cards[i - 1] || cur).focus(),
    p: () => decide(cur, "picked", true), s: () => decide(cur, "skipped"), l: () => decide(cur, "later"),
    o: () => window.open(cur.dataset.url, "_blank", "noopener"), Enter: () => cur.querySelector("a.title").click(),
  };
  if (keys[e.key]) { e.preventDefault(); keys[e.key](); }
});

async function syncNow(btn) {
  busy(btn, true, "Syncing…");
  try {
    const d = await post("/sync");
    toast(d.ok ? `Synced (${(d.push || {}).pushed || 0} decisions pushed)` : (d.pull.error || "Sync failed"), !d.ok);
    if (d.ok) setTimeout(() => location.reload(), 600);
  } finally { busy(btn, false); }
}

// push what is left when the page closes
addEventListener("pagehide", () => {
  try { navigator.sendBeacon && fetch("/close", {method: "POST", keepalive: true, headers: {"X-JR": "1"}}); } catch (e) {}
});

// the extension's bridge marks the page: tell the app (the setup wizard waits for it)
document.addEventListener("DOMContentLoaded", () => {
  setTimeout(() => {
    if (document.documentElement.dataset.jrFiller) post("/ext/seen").catch(() => {});
  }, 800);
});

async function saveNotes(el) {
  await post(`/job/${JOB_ID}/notes`, {notes: el.value});
  toast("Notes saved");
}

async function saveBank(btn, status) {
  const row = btn.closest(".bankrow");
  busy(btn, true);
  try {
    const aliases = row.querySelector(".alias-text");
    await post(`/bank/${row.dataset.id}`, {answer: row.querySelector(".answer-text").value,
                                           short: row.querySelector(".short-text").value, status,
                                           aliases: aliases ? aliases.value : undefined});
    toast("Saved");
    if (status) location.reload();
  } finally { busy(btn, false); }
}

async function deleteBank(btn) {
  const row = btn.closest(".bankrow");
  if (!confirm("Delete this answer from the bank?")) return;
  await post(`/bank/${row.dataset.id}`, {delete: true});
  row.remove();
  toast("Deleted");
}

async function addJob(e) {
  e.preventDefault();
  const f = e.target, btn = f.querySelector("button[type=submit]"), out = document.getElementById("added");
  busy(btn, true, "Reading the posting…");
  try {
    const r = await fetch("/add", {method: "POST", headers: {"Content-Type": "application/json", "X-JR": "1"},
                                   body: JSON.stringify(Object.fromEntries(new FormData(f)))});
    let d = {};
    try { d = await r.json(); } catch (err) { d = {ok: false, error: r.statusText}; }
    if (!r.ok || !d.ok) {
      toast(d.error || `Error ${r.status}`, true);
      for (const k of ["company", "title"]) {             // keep what was read; the empty one gets the focus
        if (d.found && d.found[k] && !f.elements[k].value) f.elements[k].value = d.found[k];
      }
      const empty = ["company", "title"].find(k => !f.elements[k].value);
      if (empty) f.elements[empty].focus();
      return;
    }
    if (f.elements.queue.value === "apply") { location.href = d.url; return; }
    const text = d.posting_text ? `${d.posting_text.toLocaleString()} characters of posting text` : "no posting text";
    out.innerHTML = `<p><b>Added</b> to New: <a href="${esc(d.url)}">${esc(d.title)} — ${esc(d.company)}</a>
      · ${esc(d.job_id)} · route ${esc(d.route)} · ${text}.</p>`;
    out.hidden = false;
    toast("Added");
    f.reset();
  } finally { busy(btn, false); }
}

async function pasteQuestions(btn, clear) {
  const text = clear ? "" : document.getElementById("pasted").value.trim();
  if (!clear && !text) { toast("Paste the form's questions first", true); return; }
  await startTask(btn, `/job/${JOB_ID}/questions`, {text});
}

async function editWithClaude(btn) {
  const keys = [...document.querySelectorAll(".q input.pick:checked")].map(c => c.closest(".q").dataset.key);
  if (!keys.length) { toast("Tick the questions you want to work on first", true); return; }
  busy(btn, true);
  try {
    const d = await post(`/job/${JOB_ID}/claude`, {keys});
    if (d.opened) toast(`Opened your editor with ${keys.length} question(s). Saved answers show up here.`);
    else if (d.url) location.href = d.url;
    else {
      try { await navigator.clipboard.writeText(d.prompt); toast("Prompt copied: paste it into your AI assistant"); }
      catch (e) { prompt("Paste this into your AI assistant:", d.prompt); }
    }
    watchForChanges();
  } finally { busy(btn, false); }
}

// After a Claude session is opened, notice answers it saves with `jr answer` (they change the cached prep).
function watchForChanges() {
  if (window._watching) return;
  window._watching = true;
  const tick = async () => {
    try {
      const d = await (await fetch(`/job/${JOB_ID}/stamp`)).json();
      if (d.stamp > STAMP) {
        const n = document.createElement("div");
        n.className = "notice";
        n.innerHTML = 'Claude saved new answers. <button onclick="location.reload()">Reload</button>';
        document.querySelector("main").prepend(n);
        return;
      }
    } catch (e) { /* app restarting; keep polling */ }
    setTimeout(tick, 3000);
  };
  setTimeout(tick, 3000);
}

async function copyLink(btn) {
  try { await navigator.clipboard.writeText(btn.dataset.url); toast("Posting link copied"); }
  catch (e) { prompt("Copy the posting link:", btn.dataset.url); }
}

async function useApplyUrl(btn) {
  const url = document.getElementById("apply-url").value.trim();
  if (!url) { toast("Paste the application form's URL first", true); return; }
  await startTask(btn, `/job/${JOB_ID}/apply-url`, {url}, r => {
    if (r.prep === "manual form") toast(`Still can't read this form (${r.vendor}); paste its questions instead`, true);
    setTimeout(() => location.reload(), r.prep === "manual form" ? 2500 : 0);
  });
}

// The cover letter as a PDF (what the box shows now, edits included), saved by the browser.
async function coverLetterPdf(btn) {
  const q = btn.closest(".q");
  busy(btn, true, "Making the PDF…");
  try {
    const r = await fetch(`/job/${JOB_ID}/cover-letter`, {method: "POST",
      headers: {"Content-Type": "application/json", "X-JR": "1"},
      body: JSON.stringify({key: q.dataset.key, text: q.querySelector("textarea").value})});
    if (!r.ok) { let e = {}; try { e = await r.json(); } catch (x) { /* not JSON */ } toast(e.error || `Error ${r.status}`, true); return; }
    const name = (r.headers.get("Content-Disposition") || "").match(/filename="?([^";]+)"?/);
    const a = document.createElement("a");
    a.href = URL.createObjectURL(await r.blob());
    a.download = name ? name[1] : "Cover_Letter.pdf";
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 10000);
    toast("PDF downloaded");
  } finally { busy(btn, false); }
}

// A task started before this page loaded (Prep all, a prep, a fill) is still running: show it.
document.addEventListener("DOMContentLoaded", () => {
  if (typeof RUNNING !== "undefined" && RUNNING) watchTask(typeof JOB_ID !== "undefined" ? JOB_ID : "__all__");
});

// Opened by Auto-fill in another browser: wait for the extension's bridge (a content script, it can arrive after this
// script runs), then fill once. The flag leaves the address first, so a reload doesn't fill again.
async function autoFillOnOpen() {
  const params = new URLSearchParams(location.search);
  if (!params.has("autofill")) return;
  params.delete("autofill");
  history.replaceState(null, "", location.pathname + (params.toString() ? `?${params}` : "") + location.hash);
  const btn = document.getElementById("autofill");
  if (!btn) return;
  for (let i = 0; i < 50 && !document.documentElement.dataset.jrFiller; i++) await new Promise(r => setTimeout(r, 100));
  if (document.documentElement.dataset.jrFiller) fill(btn);
  else toast(NO_EXTENSION, true);                 // never back to /open-in-chrome from here: no loop between browsers
}
document.addEventListener("DOMContentLoaded", autoFillOnOpen);

// Copy what the answer box shows now: an edited draft's textarea, the chosen option, or the stored answer.
async function copyAnswer(btn) {
  const q = btn.closest(".q");
  const box = q.querySelector(":scope > textarea") || q.querySelector(":scope > select") || q.querySelector(":scope > .answer");
  const text = (box.value !== undefined ? box.value : box.innerText).trim();
  try { await navigator.clipboard.writeText(text); }
  catch (e) { prompt("Copy the answer:", text); return; }
  btn.textContent = "Copied";
  setTimeout(() => (btn.textContent = "Copy"), 1500);
}
