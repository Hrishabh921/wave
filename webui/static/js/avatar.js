/* avatar.js - the avatar companion ("Dodo"), on its own page.
 *
 * Wave's PyQt6 desktop build gave the avatar its own always-on-top
 * floating window (avatar/window.py). The user's own explicit direction
 * for the web UI (wave_web_start.pyw) was the opposite: no second
 * window. The character first sat at the top of the Chat page; since
 * 2026-11-17 it has its own sidebar page, "Dodo", just above Chat (see
 * App.build.dodo in js/app.js), and lights up the moment the mic is
 * turned on.
 *
 * Two ways of drawing her, best one first:
 *
 * 1. Tier 27 - lifelike clips (createLivePlayer below). LivePortrait
 *    re-drew the photo frame by frame (avatar/live_render.py, rendered
 *    once from Settings -> Avatar): idle, listen, think, and a talk clip
 *    at several mouth openings. This file plays them on a canvas at their
 *    native 25 fps, cross-fades between states starting the new clip at
 *    the frame whose head pose matches, and while Wave speaks picks (and
 *    blends) the mouth opening from the real voice - webui/bridge.py
 *    pushes each utterance's loudness curve ("avatar_speech") just before
 *    it plays. The frames come from avatar/live.py's local file server.
 *
 * 2. Tier 25b - the 5-frame rig (buildRig below), used until/unless a
 *    lifelike render exists. frameForState() mirrors avatar/animation.py's
 *    frame_for_state() exactly and the blink rescheduling mirrors
 *    avatar/window.py's _tick()/_schedule_next_blink() - keep them in sync
 *    by hand, nothing imports the other.
 *
 * Either way the state itself is never guessed here: webui/bridge.py
 * pushes "avatar_state" (idle/listening/thinking/speaking) whenever
 * avatar/state.py's AvatarStateMachine actually changes.
 */
window.WaveAvatar = (function () {
    // ---- ported constants (avatar/animation.py) --------------------
    const BLINK_MIN_INTERVAL_S = 2.5;
    const BLINK_MAX_INTERVAL_S = 6.0;
    const BLINK_DURATION_S = 0.12;
    const IDLE_SWAY_AFTER_S = 3.0;
    const SWAY_PERIOD_S = 6.0;
    const THINK_SWAY_PERIOD_S = 1.4;
    const SPEAK_FLAP_PERIOD_S = 0.22;
    const FRAME_INTERVAL_MS = 66; // ~15fps - matches the Python side exactly

    function nowS() { return performance.now() / 1000; }

    // Direct port of avatar/animation.py's frame_for_state(). Keep the
    // branch order identical - LISTENING deliberately never checks
    // next_blink_at (see that function's own docstring for why).
    function frameForState(state, secondsInState, now, nextBlinkAt) {
        if (state === "speaking") {
            const phase = Math.floor(secondsInState / SPEAK_FLAP_PERIOD_S) % 2;
            return phase === 0 ? "mouth_open" : "neutral";
        }
        if (state === "thinking") {
            const phase = (secondsInState % THINK_SWAY_PERIOD_S) / THINK_SWAY_PERIOD_S;
            return phase < 0.5 ? "sway_left" : "sway_right";
        }
        if (state === "listening") return "neutral";
        // idle
        if (nextBlinkAt <= now && now < nextBlinkAt + BLINK_DURATION_S) return "blink";
        if (secondsInState > IDLE_SWAY_AFTER_S) {
            const phase = (secondsInState % SWAY_PERIOD_S) / SWAY_PERIOD_S;
            return phase < 0.5 ? "sway_left" : "sway_right";
        }
        return "neutral";
    }

    let panelEl = null, frameEl = null, imgEl = null, progressEl = null;
    let frames = null;            // rig: {frameName: "data:image/png;base64,..."}
    let live = null;              // lifelike player, when there's a render
    let state = "idle";
    let enteredAt = nowS();
    let nextBlinkAt = 0;
    let timer = null;
    let built = false;
    let lastFrameShown = null;
    let micOn = false;

    function scheduleNextBlink() {
        nextBlinkAt = nowS() + BLINK_MIN_INTERVAL_S + Math.random() * (BLINK_MAX_INTERVAL_S - BLINK_MIN_INTERVAL_S);
    }

    function applyFrame(name) {
        if (!imgEl || !frames) return;
        const src = frames[name] || frames.neutral;
        if (!src || name === lastFrameShown) return;
        lastFrameShown = name;
        imgEl.setAttribute("src", src);
    }

    function tick() {
        const now = nowS();
        const secondsInState = now - enteredAt;
        const name = frameForState(state, secondsInState, now, nextBlinkAt);
        // Same re-arm condition as avatar/window.py's _tick(): once the
        // blink window has fully passed (whether or not it was actually
        // shown this frame - e.g. during SPEAKING/THINKING, which never
        // render "blink" at all), roll the next one.
        if (name !== "blink" && now >= nextBlinkAt + BLINK_DURATION_S) scheduleNextBlink();
        applyFrame(name);
    }

    const stateListeners = [];
    function setState(newState) {
        if (!newState || newState === state) return;
        state = newState;
        enteredAt = nowS();
        if (panelEl) panelEl.setAttribute("data-avatar-state", state);
        if (live) live.setState(state);
        stateListeners.forEach((fn) => { try { fn(state); } catch (e) { /* a listener must not stop the animation */ } });
    }
    // The Dodo page's "● Listening" line. Called right away with the current state.
    function onState(fn) {
        stateListeners.push(fn);
        fn(state);
    }

    // Called by app.js's voice-mode handler: the frame glows while the mic
    // is awake (the AWAKE/SLEEPING distinction itself still drives the
    // sidebar's LISTENING/STANDBY label separately).
    function setMicOn(on) {
        micOn = !!on;
        if (panelEl) panelEl.classList.toggle("prominent", micOn);
    }

    function placeholder() {
        const div = document.createElement("div");
        div.className = "avatar-panel avatar-placeholder";
        div.textContent = "Avatar abhi set up nahi hua - Settings → Avatar page se apni photo set karo.";
        return div;
    }

    function makePanel(mountEl, aspect) {
        panelEl = document.createElement("div");
        panelEl.id = "avatar-panel";
        panelEl.className = "avatar-panel" + (micOn ? " prominent" : "");
        panelEl.setAttribute("data-avatar-state", state);
        frameEl = document.createElement("div");
        frameEl.className = "avatar-frame";
        // The frame's own aspect ratio always matches the photo, and what's
        // inside fills it without stretching - the whole photo always shows.
        frameEl.style.aspectRatio = String(aspect || 1);
        panelEl.appendChild(frameEl);
        mountEl.appendChild(panelEl);
        fitFrame(mountEl, aspect || 1);
    }

    // The photo fills the page - as big as the window allows, never cropped
    // or stretched (the owner asked for it full-window, 2026-11-27): the
    // frame is sized to the largest rectangle of the photo's shape that fits
    // the mount, again whenever the window changes size.
    let fitObserver = null;
    function fitFrame(mountEl, aspect) {
        if (fitObserver) fitObserver.disconnect();
        const fit = () => {
            if (!frameEl) return;
            const w = mountEl.clientWidth, h = mountEl.clientHeight;
            if (!w || !h) return;
            let fw = w, fh = w / aspect;
            if (fh > h) { fh = h; fw = h * aspect; }
            frameEl.style.width = Math.floor(fw) + "px";
            frameEl.style.height = Math.floor(fh) + "px";
        };
        fitObserver = new ResizeObserver(fit);
        fitObserver.observe(mountEl);
        fit();
    }

    // ================================================== Tier 27: lifelike
    const LIVE_CLIP = { idle: "idle", listening: "listen", thinking: "think", speaking: "talk" };
    const LOAD_ORDER = ["idle", "talk", "listen", "think"];
    const CROSSFADE_MS = 180;      // state change: blend old clip into new
    const SPEECH_LEAD_MS = 60;     // the audio reaches the speakers a moment after the event
    const CACHE_MAX = 26;          // decoded frames kept (each ~1.6 MB)

    function createLivePlayer(info, frameEl) {
        const m = info.manifest;
        const fps = m.fps || 25;
        const ver = encodeURIComponent(m.rendered_at || "");
        const [CW, CH] = m.canvas;
        const [rx, ry, rw, rh] = m.region_scaled;
        const canvas = document.createElement("canvas");
        canvas.className = "avatar-canvas";
        canvas.width = CW; canvas.height = CH;
        frameEl.appendChild(canvas);
        const ctx = canvas.getContext("2d");
        const packs = {};          // clip -> ArrayBuffer of WebP frames
        const cache = new Map();   // "clip:level:i" -> ImageBitmap | Promise (insertion order = LRU)
        let baseBmp = null, alive = true, raf = 0;
        let want = state;          // avatar state to show
        let cur = null, prev = null;
        let speech = null;         // the loudness curve of the audio playing now
        let lastSig = "";

        const fetchBuf = async (name) => {
            const r = await fetch(`${info.base_url}/${name}?v=${ver}`);
            if (!r.ok) throw new Error(`${name}: HTTP ${r.status}`);
            return r.arrayBuffer();
        };
        (async () => {
            try {
                baseBmp = await createImageBitmap(new Blob([await fetchBuf(m.base)], { type: "image/jpeg" }));
                if (!alive) return;
                ctx.drawImage(baseBmp, 0, 0, CW, CH);
                for (const name of LOAD_ORDER) {
                    if (!alive || !m.clips[name]) continue;
                    packs[name] = await fetchBuf(m.clips[name].file);
                }
            } catch (e) {
                console.warn("Dodo: couldn't load the lifelike clips", e);
            }
        })();

        function bitmap(clip, level, i) {
            const key = `${clip}:${level}:${i}`;
            const hit = cache.get(key);
            if (hit instanceof ImageBitmap) { cache.delete(key); cache.set(key, hit); return hit; }
            if (!hit && packs[clip]) {
                const c = m.clips[clip];
                const [off, len] = c.offsets[level * c.frames + i];
                const p = createImageBitmap(new Blob([new Uint8Array(packs[clip], off, len)], { type: "image/webp" }))
                    .then((bmp) => {
                        if (alive && cache.get(key) === p) { cache.set(key, bmp); trim(); } else bmp.close();
                    })
                    .catch(() => { if (cache.get(key) === p) cache.delete(key); });
                cache.set(key, p);
            }
            return null;
        }
        function trim() {
            let n = 0;
            cache.forEach((v) => { if (v instanceof ImageBitmap) n++; });
            for (const [k, v] of cache) {
                if (n <= CACHE_MAX) break;
                if (v instanceof ImageBitmap) { v.close(); cache.delete(k); n--; }
            }
        }

        const frameOf = (c, now) => {
            const n = m.clips[c.clip].frames;
            return (((c.off + Math.floor((now - c.t0) * fps / 1000)) % n) + n) % n;
        };
        const poseOf = (clip, i) => (m.clips[clip].poses || [])[i] || [0, 0, 0];
        function bestStart(clip, pose) {   // the frame whose head pose is closest - no double image in the fade
            let best = 0, bd = Infinity;
            (m.clips[clip].poses || []).forEach((p, i) => {
                const d = (p[0] - pose[0]) ** 2 + (p[1] - pose[1]) ** 2 + (p[2] - pose[2]) ** 2;
                if (d < bd) { bd = d; best = i; }
            });
            return best;
        }
        function levels() { return (m.clips.talk && m.clips.talk.levels) || 1; }
        // How open the mouth is: ONLY ever from the loudness of audio that is
        // really playing (Tier 29) - no curve, closed lips. There used to be a
        // made-up "chatter" when no curve had arrived yet, which moved her
        // lips through every silent wait for a reply.
        function mouth(now) {              // 0 .. levels-1, may be fractional
            const L = levels();
            if (L < 2 || !speech) return 0;
            const x = (now - speech.t0) * speech.fps / 1000;
            if (x < 0 || x >= speech.env.length) return 0;
            const i = Math.floor(x), f = x - i;
            const a = speech.env[i], b = speech.env[Math.min(i + 1, speech.env.length - 1)];
            return (a + (b - a) * f) * (L - 1);
        }

        // One clip's drawing for this moment, or null if a frame isn't decoded yet.
        function plan(c, now, level, alpha, ahead) {
            const i = frameOf(c, now);
            const n = m.clips[c.clip].frames;
            if (c.clip !== "talk") {
                const A = bitmap(c.clip, 0, i);
                if (ahead) { bitmap(c.clip, 0, (i + 1) % n); bitmap(c.clip, 0, (i + 2) % n); }
                return A ? { sig: `${c.clip}${i}`, draw: () => { ctx.globalAlpha = alpha; ctx.drawImage(A, rx, ry); } } : null;
            }
            const L = levels();
            const lv = Math.max(0, Math.min(L - 1, level));
            const a = Math.floor(lv), f = lv - a, b = Math.min(L - 1, a + 1);
            const A = bitmap("talk", a, i);
            const B = f > 0.03 ? bitmap("talk", b, i) : null;
            if (ahead) {
                const j = (i + 1) % n;
                for (let k = Math.max(0, a - 1); k <= Math.min(L - 1, b + 1); k++) bitmap("talk", k, j);
            }
            if (!A || (f > 0.03 && !B)) return null;
            return {
                sig: `t${i}:${lv.toFixed(2)}`,
                draw: () => {
                    ctx.globalAlpha = alpha; ctx.drawImage(A, rx, ry);
                    if (B) { ctx.globalAlpha = alpha * f; ctx.drawImage(B, rx, ry); }
                },
            };
        }

        function loop() {
            if (!alive) return;
            raf = requestAnimationFrame(loop);
            const now = performance.now();
            // She talks whenever Wave's voice plays - a reply (state
            // "speaking") or anything else spoken aloud (a reminder, the
            // read-aloud button, "Systems active."), for as long as its
            // loudness curve lasts.
            const voiced = speech && now < speech.t0 + (speech.env.length * 1000) / speech.fps + 120;
            const clipName = want === "speaking" || voiced ? "talk" : LIVE_CLIP[want];
            const wanted = packs[clipName] ? clipName : (packs.idle ? "idle" : null);
            if (!wanted || !baseBmp) return;
            if (!cur) cur = { clip: wanted, t0: now, off: 0 };
            else if (cur.clip !== wanted) {
                const i = frameOf(cur, now);
                prev = Object.assign({}, cur, { fadeStart: now, level: cur.clip === "talk" ? mouth(now) : 0 });
                cur = { clip: wanted, t0: now, off: bestStart(wanted, poseOf(cur.clip, i)) };
            }
            if (prev && now - prev.fadeStart >= CROSSFADE_MS) prev = null;
            if (!canvas.isConnected || canvas.offsetParent === null || document.hidden) return;   // page not on screen
            const fade = prev ? (now - prev.fadeStart) / CROSSFADE_MS : 1;
            const parts = [];
            if (prev) {
                const p = plan(prev, now, prev.level, 1, false);
                if (p) parts.push(p);
            }
            const c = plan(cur, now, cur.clip === "talk" ? mouth(now) : 0, prev ? fade : 1, true);
            if (!c) return;                                  // keep what's on screen until the frame is decoded
            parts.push(c);
            const sig = parts.map((p) => p.sig).join("|") + (prev ? fade.toFixed(2) : "");
            if (sig === lastSig) return;
            lastSig = sig;
            ctx.globalAlpha = 1;
            ctx.drawImage(baseBmp, rx, ry, rw, rh, rx, ry, rw, rh);      // restore the photo under the face
            parts.forEach((p) => p.draw());
            ctx.globalAlpha = 1;
        }
        raf = requestAnimationFrame(loop);

        return {
            setState(s) {
                if (want === "speaking" && s !== "speaking") speech = null;   // cut off mid-sentence: lips stop too
                want = s;
            },
            stopSpeech() { speech = null; },
            speech(env, f) {
                if (!Array.isArray(env) || !env.length) return;
                speech = { env, fps: f || fps, t0: performance.now() + SPEECH_LEAD_MS };
            },
            destroy() {
                alive = false;
                cancelAnimationFrame(raf);
                cache.forEach((v) => { if (v instanceof ImageBitmap) v.close(); });
                cache.clear();
                if (baseBmp) baseBmp.close();
                canvas.remove();
            },
        };
    }

    // Render progress from Settings -> Avatar, shown on the frame itself.
    function showProgress(text) {
        if (!frameEl) return;
        if (!progressEl) {
            progressEl = document.createElement("div");
            progressEl.className = "avatar-live-progress";
            frameEl.appendChild(progressEl);
        }
        progressEl.textContent = text;
        progressEl.classList.toggle("hidden", !text);
    }

    // ================================================== Tier 25b: 5-frame rig
    async function buildRig(mountEl) {
        let res;
        try {
            res = await wave.call("get_avatar_rig");
        } catch (e) {
            res = { ok: false };
        }
        if (!res || !res.ok || !res.has_rig) {
            mountEl.appendChild(placeholder());
            return;
        }
        frames = res.frames;
        makePanel(mountEl, (res.width && res.height) ? (res.width / res.height) : 1);
        imgEl = document.createElement("img");
        imgEl.className = "avatar-img";
        imgEl.alt = "Wave";
        frameEl.appendChild(imgEl);
        applyFrame("neutral");
        scheduleNextBlink();
        clearInterval(timer);
        timer = setInterval(tick, FRAME_INTERVAL_MS);
    }

    // Builds the panel once into `mountEl` (the Dodo page's mount;
    // idempotent - a second call does nothing).
    let mountRef = null, listening = false;
    async function build(mountEl) {
        if (built || !mountEl) return;
        built = true;
        mountRef = mountEl;
        if (!listening) {
            listening = true;
            wave.on("avatar_state", (payload) => { if (payload) setState(payload.state); });
            wave.on("avatar_speech", (payload) => { if (live && payload) live.speech(payload.env, payload.fps); });
            // Speech stopped early ("stop", a barge-in): the rest of its curve must not play.
            wave.on("tts_finished", () => { if (live) live.stopSpeech(); });
            wave.on("reply_speech_finished", () => { if (live) live.stopSpeech(); });
            wave.on("avatar_live_progress", (p) => {
                if (p && p.total) showProgress(`Lifelike animation ban rahi hai… ${Math.floor((100 * p.done) / p.total)}%`);
            });
            wave.on("avatar_live_done", (p) => {
                if (p && p.ok) { showProgress(""); reload(); }
                else showProgress(p && p.error ? `Animation nahi bani: ${p.error}` : "");
            });
        }
        let info = null;
        try {
            info = await wave.call("get_avatar_live");
        } catch (e) {
            info = null;
        }
        if (info && info.ok && info.available && info.manifest) {
            const [w, h] = info.manifest.photo || [1, 1];
            makePanel(mountEl, w / h);
            live = createLivePlayer(info, frameEl);
        } else {
            await buildRig(mountEl);
        }
        if (info && info.ok && info.rendering && info.total) {
            showProgress(`Lifelike animation ban rahi hai… ${Math.floor((100 * info.done) / info.total)}%`);
        }
        try {
            const stateRes = await wave.call("get_avatar_state");
            if (stateRes && stateRes.ok) setState(stateRes.state);
        } catch (e) { /* best-effort - the next avatar_state push sets it anyway */ }
        if (live) live.setState(state);
    }

    // Re-reads everything after Settings -> Avatar built a new rig or a
    // lifelike render finished.
    async function reload() {
        if (!mountRef) return;
        clearInterval(timer);
        if (live) live.destroy();
        live = null;
        if (fitObserver) { fitObserver.disconnect(); fitObserver = null; }
        mountRef.innerHTML = "";
        built = false; panelEl = null; frameEl = null; imgEl = null; progressEl = null;
        frames = null; lastFrameShown = null;
        await build(mountRef);
    }

    return { build, setMicOn, reload, onState };
})();
