/* Runs on the local Job Radar app's pages only (they carry <meta name="jr-app">): tells the page the extension is
installed, and passes "fill this job" from the page to the extension and the fill report back. Declared for pages
loaded after an install; background.js also injects it into app tabs already open then (once per extension load). */
if (document.querySelector('meta[name="jr-app"]') && !globalThis.jrBridge) {
  globalThis.jrBridge = true;
  document.documentElement.dataset.jrFiller = chrome.runtime.getManifest().version;
  window.addEventListener("message", e => {
    if (e.source !== window || !e.data || e.data.jr !== "fill") return;
    if (!(chrome.runtime && chrome.runtime.id)) return;    // left over from before a reload: the new copy answers
    const reply = res => window.postMessage({jr: "fill-done", ...res}, location.origin);
    try {
      chrome.runtime.sendMessage({type: "fill", payload: e.data.payload}, res => {
        reply(chrome.runtime.lastError ? {ok: false, error: chrome.runtime.lastError.message} : res);
      });
    } catch (err) {         // extension reloaded since this page opened
      reply({ok: false, error: "Reload this page: the Job Radar extension was updated"});
    }
  });
}
