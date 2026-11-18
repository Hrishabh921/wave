/* bridge.js - Tier 24
 *
 * Thin wrapper around window.pywebview.api.* (the WaveWebBridge methods
 * in webui/bridge.py). Two jobs:
 *
 *   1. pywebview only attaches window.pywebview after the page's own
 *      "pywebviewready" event fires - calling the api before that throws.
 *      wave.ready() returns a promise that resolves once it's safe.
 *   2. Every bridge event pushed from Python (webui/bridge.py's _push())
 *      calls window.Wave.dispatch(event, payload) - this file owns that
 *      dispatcher and a small pub/sub so app.js can `wave.on('chat_chunk',
 *      fn)` instead of every page wiring its own global function.
 */
(function () {
    const listeners = {};
    let readyResolve;
    const readyPromise = new Promise((resolve) => { readyResolve = resolve; });

    window.addEventListener("pywebviewready", () => readyResolve());
    // Fallback for opening index.html directly in a plain browser tab
    // during development (no pywebview host) - api calls will just fail
    // loudly instead of hanging forever.
    setTimeout(() => { if (!window.pywebview) readyResolve(); }, 2000);

    async function call(method, ...args) {
        await readyPromise;
        if (!window.pywebview || !window.pywebview.api) {
            console.error("pywebview bridge not available - is this running inside the Wave app?");
            return { ok: false, error: "No pywebview bridge in this context." };
        }
        try {
            return await window.pywebview.api[method](...args);
        } catch (e) {
            console.error(`bridge.${method} failed`, e);
            return { ok: false, error: String(e) };
        }
    }

    window.Wave = window.Wave || {};
    window.Wave.dispatch = function (event, payload) {
        (listeners[event] || []).forEach((fn) => {
            try { fn(payload); } catch (e) { console.error(`listener for ${event} failed`, e); }
        });
    };

    window.wave = {
        ready: () => readyPromise,
        call,
        on(event, fn) {
            (listeners[event] = listeners[event] || []).push(fn);
            return () => { listeners[event] = (listeners[event] || []).filter((f) => f !== fn); };
        },
    };
})();
