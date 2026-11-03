/* Job Radar form filler: fills an application form from the app's prepped answers, and STOPS before Submit.

Runs inside the page (chrome.scripting.executeScript, MAIN world, so it can reach widgets such as selectize), in the
person's own Chrome: no automation browser, nothing driving the window. ``jobRadarFill`` is serialised whole by
executeScript, so every helper lives inside it.

- Submit guard: every click goes through ``press``/``tick``, which refuse ``[type=submit]``, buttons without a type
  inside a form (they submit by default) and anything whose text matches submit|apply|send|wyślij|bewerben.
  No key events except one ordinary character to start a typeahead search: never Enter. The final Submit is
  always the user's click.
- Nothing scrolls: values are set on the elements directly, focus uses ``preventScroll``, and a page that
  scrolls itself while being filled (react-select menus) is put back where it was.
- Returns {report: [{key, label, ok, how}], found: <fields found>} for the app's fill report.
*/
function jobRadarFill(payload, opts) {
  opts = opts || {};
  const WAIT_MS = opts.waitMs || 15000, LIST_MS = opts.listMs || 4000, UPLOAD_MS = opts.uploadMs || 15000;
  const RECHECK_MS = opts.recheckMs || 800;
  const SUBMIT_TEXT = /submit|apply|send|wyślij|wyslij|bewerben/i;
  const FIELDS = 'input:not([type=hidden]), select, textarea, [role=combobox], [contenteditable=""], [contenteditable=true]';
  const OPTIONS = '[role=option], .dropdown-location';         // ARIA listboxes; Lever's location list
  const COUNTRY_NAMES = [["poland", "polska", "polen", "pologne", "polonia"]];   // Traffit lists countries in Polish
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const norm = s => (s || "").toLowerCase().split(/\s+/).filter(Boolean).join(" ");
  const fold = s => norm((s || "").normalize("NFKD").replace(/\p{M}/gu, ""));
  const tagOf = el => el.tagName.toLowerCase();
  const typeOf = el => (el.getAttribute("type") || "").toLowerCase();

  class Guard extends Error {}
  class Miss extends Error {}

  // Index of the option meaning `value`: equal, else containing it (4+ letters, so "No" never picks "None"),
  // else same first and last comma part ("Lodz, Poland" → "Lodz, Lodz Voivodeship, Poland"; Lever's
  // "Łódź, Łódzkie, POL" counts too), else the first part alone ("Lodz"). Accents are ignored, so when
  // several options match, the closest spelling wins (an exact accented match beats a different place with the
  // same letters).
  function bestOption(options, value) {
    const v = fold(value), opts = options.map(fold), raw = options.map(o => norm(o.normalize("NFC")));
    const parts = v.split(",").map(p => p.trim()).filter(Boolean), last = parts[parts.length - 1];
    const sameEnd = o => {
      const end = o.split(",").pop().trim();
      return o.endsWith(last) || (end.length <= 3 && end.length > 1 && last.startsWith(end));
    };
    const closest = (test, target) => {
      const t = [...norm(target.normalize("NFC"))];
      const diff = i => [...raw[i]].reduce((n, c, k) => n + (c !== t[k]), Math.abs([...raw[i]].length - t.length));
      return opts.map((o, i) => test(o) ? i : -1).filter(i => i >= 0).reduce((a, b) => a < 0 || diff(b) < diff(a) ? b : a, -1);
    };
    const country = COUNTRY_NAMES.find(g => g.includes(v)) || [];
    let i = closest(o => o === v, value);
    if (i < 0 && v.length >= 4) i = closest(o => o.includes(v), value);
    if (i < 0 && parts.length > 1) i = closest(o => o.startsWith(parts[0]) && sameEnd(o), value);
    if (i < 0 && parts.length > 1) i = closest(o => o === parts[0], value.split(",")[0]);
    if (i < 0) i = closest(o => country.includes(o), value);
    return i;
  }

  function isSubmitLike(el) {
    const tag = tagOf(el), typ = typeOf(el), role = el.getAttribute("role") || "";
    if (typ === "submit" || typ === "image") return true;
    // a <button> submits unless its type is exactly button/reset (missing, empty or invalid type = submit)
    if (tag === "button" && typ !== "button" && typ !== "reset" && el.closest("form")) return true;
    if (typ === "button" && (role === "radio" || role === "checkbox")) return false;   // a toggle (Radix)
    const text = [el.innerText, el.value, el.getAttribute("aria-label"), el.getAttribute("title")].filter(Boolean).join(" ");
    return (tag === "button" || tag === "a" || typ === "button" || role === "button") && SUBMIT_TEXT.test(text);
  }

  // The only clicks: a full pointer sequence (react-select, Lever's list react to mousedown), on non-submit elements.
  function press(el) {
    if (isSubmitLike(el)) throw new Guard(`refusing to click a submit-like element (${tagOf(el)}:${typeOf(el)})`);
    const at = {bubbles: true, cancelable: true, view: window, button: 0};
    for (const t of ["pointerdown", "mousedown", "pointerup", "mouseup"]) {
      el.dispatchEvent(t.startsWith("pointer") ? new PointerEvent(t, at) : new MouseEvent(t, at));
    }
    el.click();
  }

  function tick(box) {
    if (tagOf(box) === "button") {
      if (box.getAttribute("aria-checked") !== "true") press(box);
    } else if (!box.checked) {
      if (isSubmitLike(box)) throw new Guard("refusing to click a submit-like element");
      box.click();                  // toggles it and fires input/change, also when a styled label hides the box
    }
  }

  function setValue(el, value) {
    const proto = {textarea: HTMLTextAreaElement, select: HTMLSelectElement}[tagOf(el)] || HTMLInputElement;
    // the prototype's setter, not el.value: React tracks el.value and would ignore the change
    Object.getOwnPropertyDescriptor(proto.prototype, "value").set.call(el, value);
    el.dispatchEvent(new Event("input", {bubbles: true}));
    el.dispatchEvent(new Event("change", {bubbles: true}));   // Lever: marks it typed, so its CV parser keeps it
  }

  function setFile(el, f) {
    const bytes = Uint8Array.from(atob(f.b64), c => c.charCodeAt(0));
    const dt = new DataTransfer();
    dt.items.add(new File([bytes], f.name, {type: f.mime || "application/pdf"}));
    el.files = dt.files;
    el.dispatchEvent(new Event("input", {bubbles: true}));
    el.dispatchEvent(new Event("change", {bubbles: true}));
  }

  // Lever parses an uploaded CV, then fills its empty fields and hidden selected location from it: wait for the
  // parse (its spinner) before filling the rest, as a person would.
  async function afterUpload() {
    const parsing = () => [...document.querySelectorAll(".resume-upload-working")].some(visible);
    await sleep(100);
    for (let t = 0; t < 60 && parsing(); t++) await sleep(250);
  }

  // Greenhouse's job board (React app, window.ENV.JBEN_URL) uploads files to S3 with fields it fetches only after the
  // form is on screen (/uncacheable_attributes/presigned_fields). A file set before that answer fails with "Cannot
  // read properties of undefined (reading 'uploadFile')". After a good upload the board swaps the input for the file.
  const greenhouseBoard = () => !!(window.ENV && window.ENV.JBEN_URL);
  async function greenhouseUploadsReady() {
    const fetched = () => performance.getEntriesByType("resource").some(e => e.name.includes("/presigned_fields"));
    for (let t = 0; t < UPLOAD_MS / 250 && !fetched(); t++) await sleep(250);
    await sleep(300);                              // the answer is parsed and the uploaders set after it arrives
  }

  async function greenhouseUploaded(el) {
    for (let t = 0; t < UPLOAD_MS / 250 && el.isConnected; t++) await sleep(250);
    return !el.isConnected;
  }

  // Server-rendered React pages (Greenhouse's job board is Remix) show the form before React takes it over. A value
  // set in that gap never reaches React's state, and React wipes it when it attaches (~150 ms after load on
  // Greenhouse): the first answer or two came out empty. Wait until React has claimed the fields.
  const ssrReact = () => !!(window.__remixContext || window.__reactRouterContext || window.__NEXT_DATA__ || window.__next_f);
  const claimed = el => Object.keys(el).some(k => k.startsWith("__reactFiber$") || k.startsWith("__reactProps$"));
  async function reactHydrated(until) {
    while (![...document.querySelectorAll(FIELDS)].some(claimed) && Date.now() < until) await sleep(100);
    await sleep(200);                              // let the hydration commit before the first value goes in
  }

  // Text answers React (or any page script) cleared after they were set: the field and the answer to put back.
  const textEmpty = item => {
    const found = findField(item);
    const el = found && found[0][0];
    return el && ["input", "textarea"].includes(tagOf(el)) && !el.value ? el : null;
  };

  function labelText(el) {
    const parts = [...(el.labels || [])].map(l => l.innerText);
    for (const id of (el.getAttribute("aria-labelledby") || "").split(/\s+/).filter(Boolean)) {
      const l = document.getElementById(id);
      if (l) parts.push(l.innerText);
    }
    parts.push(el.getAttribute("aria-label") || "");
    return parts.map(p => fold(p.replace(/[*✱]/g, ""))).filter(Boolean);
  }

  // Radix widgets (eRecruiter) hide the real <input> right after a <button role=radio|checkbox>: use the button.
  function control(box) {
    const prev = box.previousElementSibling;
    return prev && tagOf(prev) === "button" && ["radio", "checkbox"].includes(prev.getAttribute("role")) ? prev : box;
  }

  function cssAttr(v) { return v.replace(/\\/g, "\\\\").replace(/"/g, '\\"'); }

  // Ashby: [data-field-path] wraps each question; then name, id, label; a substring label match must be unambiguous.
  function findField(item) {
    const key = item.key || "", label = fold((item.label || "").replace(/[*✱]/g, ""));
    if (key) {
      const box = document.querySelector(`[data-field-path="${cssAttr(key)}"]`);
      const inBox = box ? [...box.querySelectorAll(FIELDS)] : [];
      if (inBox.length) return [inBox, "path"];
      const named = [...document.querySelectorAll(`[name="${cssAttr(key)}"]`)].filter(e => typeOf(e) !== "hidden");
      if (named.length) return [named, "name"];
      const byId = document.getElementById(key);
      if (byId) {
        const els = byId.matches(FIELDS) ? [byId] : [...byId.querySelectorAll(FIELDS)];
        if (els.length) return [els, "id"];
      }
    }
    if (label) {
      // a control's own label, else a group's (Recruitee: <fieldset><legend>; ARIA radio groups; Ashby's
      // [data-field-path] wrapper, whose title label points at no element when the form was pasted by hand)
      const all = [...document.querySelectorAll(FIELDS)];
      const groups = [...document.querySelectorAll("fieldset, [role=radiogroup], [role=group], [data-field-path]")]
        .map(g => [g, groupText(g)]).filter(([g, t]) => t && g.querySelector(FIELDS));
      const exact = all.filter(e => labelText(e).includes(label));
      if (exact.length) return [exact, "label"];
      const group = groups.find(([, t]) => t === label);
      if (group) return [[...group[0].querySelectorAll(FIELDS)], "group"];
      const near = all.filter(e => labelText(e).some(t => t.includes(label)));
      if (near.length === 1) return [near, "label~"];
      const nearGroups = groups.filter(([, t]) => t.includes(label));
      if (nearGroups.length === 1) return [[...nearGroups[0][0].querySelectorAll(FIELDS)], "group~"];
    }
    return null;
  }

  function groupText(g) {
    const legend = tagOf(g) === "fieldset" ? [...g.children].find(c => tagOf(c) === "legend")
      : g.hasAttribute("data-field-path") && g.querySelector("label, legend");
    const by = (g.getAttribute("aria-labelledby") || "").split(/\s+/).map(id => document.getElementById(id)).filter(Boolean);
    const text = legend ? legend.innerText : by.length ? by.map(l => l.innerText).join(" ") : g.getAttribute("aria-label");
    return fold((text || "").replace(/[*✱]/g, ""));
  }

  // Some dropdowns (react-select) scroll their menu into view: keep the page where it was while filling.
  async function holdScroll(fn) {
    const x = scrollX, y = scrollY;
    const back = () => { if (scrollX !== x || scrollY !== y) scrollTo(x, y); };
    addEventListener("scroll", back);
    try { return await fn(); } finally { removeEventListener("scroll", back); back(); }
  }

  function visible(el) { return el.getClientRects().length > 0 && getComputedStyle(el).visibility !== "hidden"; }

  async function listOptions(el, waitMs) {
    const owned = id => id && document.getElementById(id);
    for (let t = 0; t <= waitMs; t += 250) {
      const box = owned(el.getAttribute("aria-controls")) || owned(el.getAttribute("aria-owns"));
      const opts = [...(box || document).querySelectorAll(OPTIONS)].filter(visible);
      const texts = opts.map(o => o.innerText.trim());
      if (texts.length && !texts.some(s => s.toLowerCase().startsWith("loading"))) return opts;
      await sleep(250);
    }
    return [];
  }

  // A typeahead (role=combobox, Lever's location): type, wait for the list, press the matching option.
  async function fillTypeahead(el, value, fallbacks) {
    value = value.split(/\s+/).join(" ").replace(/[\s.;!]+$/, "");     // "Łódź, Poland." searches as "Łódź, Poland"
    const queries = [value].concat(value.includes(",") ? [value.split(",")[0].trim()] : [], fallbacks);
    el.focus({preventScroll: true});
    if (el.getAttribute("role") === "combobox") press(el);          // react-select opens its menu on mousedown
    for (const q of queries) {
      setValue(el, "");
      setValue(el, q);
      const key = {key: q.slice(-1), bubbles: true};               // Lever searches on keydown; never Enter
      el.dispatchEvent(new KeyboardEvent("keydown", key));
      el.dispatchEvent(new KeyboardEvent("keyup", key));
      const opts = await listOptions(el, LIST_MS);
      const isFallback = fallbacks.includes(q);
      const hit = bestOption(opts.map(o => o.innerText.trim()), isFallback ? q : value);
      if (hit >= 0) {
        press(opts[hit]);
        await sleep(150);
        el.blur();
        return isFallback ? `combobox fallback '${q}'` : "combobox option";
      }
    }
    throw new Miss("no matching option in the list: pick it by hand");
  }

  // selectize (Traffit) hides the <select> and keeps its options in JS; remote lists (cities) load on a search.
  async function fillSelectize(sz, wants, multi) {
    const {valueField, labelField} = sz.settings;
    const find = w => {
      const opts = Object.values(sz.options);
      const i = bestOption(opts.map(o => String(o[labelField] ?? "")), w);
      return i < 0 ? null : opts[i][valueField];
    };
    const picked = [];
    for (const w of wants) {
      let v = find(w);
      // Traffit's city search knows "Gdansk" but not "Gdansk, Poland"
      for (const q of [w].concat(w.includes(",") ? [w.split(",")[0].trim()] : [])) {
        if (v != null || !sz.settings.load) break;
        sz.onSearchChange(q);
        for (let t = 0; t < LIST_MS / 250 && v == null; t++) { await sleep(250); v = find(w); }
      }
      if (v != null) picked.push(v);
    }
    if (!picked.length) throw new Miss("option not found");
    if (multi) picked.forEach(v => sz.addItem(v)); else sz.setValue(picked[0]);
    sz.close();
    return "select (selectize)";
  }

  async function fillSelect(el, value) {
    const wants = el.multiple ? value.split(";").map(s => s.trim()).filter(Boolean) : [value];
    if (el.selectize) return fillSelectize(el.selectize, wants, el.multiple);
    const opts = [...el.options];
    const hits = wants.map(w => bestOption(opts.map(o => o.text), w)).filter(i => i >= 0);
    if (!hits.length) throw new Miss("option not found");
    if (el.multiple) {
      opts.forEach((o, i) => { o.selected = hits.includes(i); });
      el.dispatchEvent(new Event("input", {bubbles: true}));
      el.dispatchEvent(new Event("change", {bubbles: true}));
    } else {
      setValue(el, opts[hits[0]].value);
    }
    return "select";
  }

  // Ashby's Yes/No: two buttons beside a hidden checkbox.
  function yesNoButtons(el) {
    const box = el.closest("[data-field-path]") || el.parentElement;
    return box ? [...box.querySelectorAll("button[aria-pressed], button[data-option]")] : [];
  }

  function fillChoice(els, el, value) {
    const typ = typeOf(el);
    const group = els.filter(e => typeOf(e) === typ);
    const buttons = yesNoButtons(el);
    if (buttons.length) {
      const want = fold(value.split(",")[0]);
      const b = buttons.find(x => fold(x.innerText) === want);
      if (!b) throw new Miss("option not found");
      if (b.getAttribute("aria-pressed") !== "true") press(b);
      return "yes/no";
    }
    const boxes = group.map(control);
    if (group.length === 1 && typ === "checkbox") {
      if (!["yes", "true", "i agree", "agree", "i consent"].includes(norm(value).split(",")[0])) {
        throw new Miss("single checkbox left as is");
      }
      tick(boxes[0]);
      return "checkbox";
    }
    const labels = group.map(e => (e.labels && e.labels[0] && e.labels[0].innerText) || e.getAttribute("aria-label")
                                  || control(e).getAttribute("aria-label") || e.value || "");
    let hits = [];
    for (const w of value.split(";").map(s => s.trim()).filter(Boolean)) {
      const i = bestOption(labels, w);
      if (i >= 0 && !hits.includes(i)) hits.push(i);
    }
    if (typ === "radio") hits = hits.slice(0, 1);
    if (!hits.length) throw new Miss("option not found");
    hits.forEach(i => tick(boxes[i]));
    return typ;
  }

  async function fillItem(item) {
    const out = {key: item.key, label: item.label, ok: false, how: ""};
    const value = item.option || item.answer || "";
    if (item.type === "file" || item.status === "file") {
      if (!item.file) { out.how = item.status === "file" ? "no CV file (set cv_variants in config/profile.yaml)" : "no file to upload"; return out; }
    } else if (!value) {
      out.how = "no answer";
      return out;
    }
    const found = findField(item);
    if (!found) { out.how = "field not found"; return out; }
    const [els, how] = found;
    const el = els[0], tag = tagOf(el), typ = typeOf(el), role = el.getAttribute("role") || "";
    try {
      let done;
      if (typ === "file") {
        if (!item.file) throw new Miss("no file to upload");
        const greenhouse = greenhouseBoard();
        if (greenhouse) await greenhouseUploadsReady();
        setFile(el, item.file);
        await afterUpload();
        if (greenhouse && !await greenhouseUploaded(el)) throw new Miss("Greenhouse didn't take the file: attach it by hand");
        done = "file";
      } else if (tag === "select") {
        done = await fillSelect(el, value);
      } else if (typ === "radio" || typ === "checkbox") {
        done = fillChoice(els, el, value);
      } else if (role === "combobox" || el.getAttribute("aria-autocomplete") || el.classList.contains("location-input")) {
        // school / discipline lists are short (no Polish universities): "Other" beats an empty required field
        const fallbacks = ["education_school", "education_discipline"].includes(item.answer_id) ? ["Other"] : [];
        done = await fillTypeahead(el, value, fallbacks);
      } else if (tag === "input" || tag === "textarea") {
        setValue(el, value);
        // number/date inputs drop text they can't parse ("8 years", "from January")
        if (!el.value) throw new Miss(`this ${typ || "text"} field didn't take the answer: type it by hand`);
        done = "text";
      } else if (el.isContentEditable) {
        el.focus({preventScroll: true});
        el.textContent = value;
        el.dispatchEvent(new Event("input", {bubbles: true}));
        done = "text";
      } else {
        throw new Miss(`unsupported element <${tag}>`);
      }
      Object.assign(out, {ok: true, how: `${how}: ${done}`});
    } catch (e) {
      out.how = e instanceof Guard || e instanceof Miss ? e.message : e.name || "Error";
    }
    return out;
  }

  if (opts.internals) return {isSubmitLike, bestOption};     // tests

  return (async () => {
    const items = payload.items || [];
    const start = Date.now();
    // SPAs render late; a careers page that embeds the form in an iframe is left to the frame's own run
    const embeds = () => [...document.querySelectorAll("iframe")].some(f => {
      const r = f.getBoundingClientRect();
      return r.width >= 300 && r.height >= 300;
    });
    while (!items.some(findField) && Date.now() - start < WAIT_MS && !(Date.now() - start > 2000 && embeds())) {
      await sleep(250);
    }
    const fields = items.map(findField).filter(Boolean);
    if (fields.length && ssrReact()) await reactHydrated(start + WAIT_MS);
    const report = await holdScroll(async () => {
      const rows = [];
      for (const item of items) {
        rows.push(await fillItem(item));
        await sleep(40);                  // let the page's framework re-render between fields
      }
      // A text answer the page cleared after it was set (a late re-render): put it back once, then report it.
      let typed = rows.map((r, i) => r.ok && r.how.endsWith(": text") ? i : -1).filter(i => i >= 0);
      for (let pass = 0; pass < 2 && typed.length; pass++) {
        await sleep(RECHECK_MS);
        typed = typed.filter(i => {
          const el = textEmpty(items[i]);
          if (!el) return false;
          if (pass === 0) {
            setValue(el, items[i].option || items[i].answer);
            rows[i].how += " (set again: the page cleared it)";
          } else {
            Object.assign(rows[i], {ok: false, how: "the page cleared this field: type it by hand"});
          }
          return true;
        });
      }
      return rows;
    });
    return {report, found: fields.length};
  })();
}
