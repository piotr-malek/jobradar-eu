/* Job Radar filler, background: opens the form the app asked for in a new tab of this Chrome and fills it with
filler.js. Nothing attaches to the browser or drives it; the site sees an ordinary tab and the user's own clicks.
The toolbar button fills the current tab again with the last job's answers (a form behind a click, a reload). */
importScripts("filler.js");

const APP = /^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?\//;

function loaded(tabId, ms) {
  return new Promise(resolve => {
    const done = () => { clearTimeout(timer); chrome.tabs.onUpdated.removeListener(onUpdated); resolve(); };
    const onUpdated = (id, info) => { if (id === tabId && info.status === "complete") done(); };
    const timer = setTimeout(done, ms);
    chrome.tabs.onUpdated.addListener(onUpdated);
    chrome.tabs.get(tabId).then(t => { if (t.status === "complete") done(); }, done);
  });
}

// One report row per item: from the frame that filled it, else the most telling miss.
function merge(items, runs) {
  return items.map((it, i) => {
    const rows = runs.map(r => r.report[i]).filter(Boolean);
    return rows.find(r => r.ok) || rows.find(r => r.how !== "field not found") || rows[0]
      || {key: it.key, label: it.label, ok: false, how: "field not found"};
  });
}

async function fill(tabId, payload) {
  const run = (target, waitMs) => chrome.scripting.executeScript(
    {target, world: "MAIN", func: jobRadarFill, args: [{items: payload.items}, {waitMs}]});
  let runs = (await run({tabId}, 15000)).map(r => r.result).filter(Boolean);
  if (!runs.some(r => r.found)) {            // a form embedded in an iframe
    runs = (await run({tabId, allFrames: true}, 4000)).map(r => r.result).filter(Boolean);
  }
  return merge(payload.items, runs);
}

function badge(tabId, report) {
  const ok = report.filter(r => r.ok).length;
  chrome.action.setBadgeBackgroundColor({tabId, color: ok === report.length ? "#2e7d32" : "#ef6c00"});
  chrome.action.setBadgeText({tabId, text: `${ok}/${report.length}`});
}

async function openAndFill(payload, appTab) {
  await chrome.storage.session.set({last: payload});
  const tab = await chrome.tabs.create({url: payload.url, index: appTab.index + 1, openerTabId: appTab.id});
  await loaded(tab.id, 30000);
  const report = await fill(tab.id, payload);
  badge(tab.id, report);
  return {ok: true, job_id: payload.job_id, report};
}

// Chrome adds declared content scripts only to pages loaded after an install or reload: reach open app tabs too.
chrome.runtime.onInstalled.addListener(async () => {
  for (const tab of await chrome.tabs.query({url: ["http://localhost/*", "http://127.0.0.1/*"]})) {
    chrome.scripting.executeScript({target: {tabId: tab.id}, files: ["bridge.js"]}).catch(() => {});
  }
});

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (msg.type !== "fill" || !sender.tab || !APP.test(sender.url || "")) return false;
  openAndFill(msg.payload, sender.tab).then(reply, e => reply({ok: false, error: String((e && e.message) || e)}));
  return true;                               // the reply comes after the fill
});

chrome.action.onClicked.addListener(async tab => {
  const {last} = await chrome.storage.session.get("last");
  try {
    if (!last) throw new Error("no job filled yet");
    badge(tab.id, await fill(tab.id, last));
  } catch (e) {
    chrome.action.setBadgeText({tabId: tab.id, text: "!"});
    chrome.action.setTitle({tabId: tab.id, title: `Job Radar: ${(e && e.message) || e}`});
  }
});
