/* app.js - the web UI shell + its pages, built to look and behave like the
 * PyQt6 desktop UI (ui/main_window.py MainWindow). Each builder below is
 * named after the MainWindow method it mirrors (_build_home_page ->
 * buildHome, ...) and reproduces that method's layout: the same margins,
 * spacings, widget classes and texts. The Tier 21 management pages and
 * Settings live in pages2.js.
 *
 * Layout notes (why some panels look "hollow"): on the desktop,
 * wrap_scroll() gives the scroll viewport "background: transparent", and a
 * selector-less sheet cascades to every child - so on the System Monitor,
 * Apps & Power, Music, Vision, Clipboard, Activity and Settings pages the
 * Panels keep their rounded borders and drop-shadow halos but lose their
 * fill. Pages built that way get the "transparent-panels" class here.
 *
 * Qt's QBoxLayout hands leftover space to "expanding" items in equal
 * shares (not by their content size), which is what `flex: 1 1 0` does;
 * items that don't expand on the desktop are `flex: none` here.
 */
(function () {
    "use strict";
    const { el, icon } = W;
    const call = (...a) => wave.call(...a);
    const T = () => WaveTheme.current();

    // ui/main_window.py NAV_ITEMS (same order), plus "Dodo" - a web-only
    // page the user asked for, holding Wave's character, just above Chat.
    const NAV = [
        ["Home", "house"], ["Dodo", "user"], ["Chat", "message"], ["System Monitor", "chart-line"], ["Apps & Power", "bolt"],
        ["Music", "music"], ["Files", "folder"], ["Vision", "eye"], ["Clipboard", "clipboard"],
        ["Activity", "clock-rotate-left"], ["Workflows", "diagram-project"], ["Memory", "brain"],
        ["Reminders", "bell"], ["Notes & To-dos", "list-check"], ["Calendar", "calendar"],
        ["Skills", "wand-magic-sparkles"], ["Performance", "gauge"], ["Settings", "gear"],
    ];
    const IDX = { HOME: 0, DODO: 1, CHAT: 2, SYSTEM: 3, APPS: 4, MUSIC: 5, FILES: 6, VISION: 7, CLIPBOARD: 8, ACTIVITY: 9,
        WORKFLOWS: 10, MEMORY: 11, REMINDERS: 12, NOTES: 13, CALENDAR: 14, SKILLS: 15, PERFORMANCE: 16, SETTINGS: 17 };
    // DashboardTile icons: ui/main_window.py ICON_MAP, plus the emoji
    // fallbacks ui/icons.py already defines for the Tier 21 pages.
    const TILE_EMOJI = { house: "🏠", message: "💬", "chart-line": "📊", bolt: "⚡", music: "🎵", folder: "📁", eye: "👁",
        clipboard: "📋", "clock-rotate-left": "🕘", gear: "⚙", "diagram-project": "🧩", brain: "🧠", bell: "🔔",
        "list-check": "✅", calendar: "📅", "wand-magic-sparkles": "🪄", gauge: "⏱", user: "🧑" };
    const QUICK_APPS = [["Terminal", "cmd", "terminal"], ["File Explorer", "file explorer", "folder"], ["Chrome", "chrome", "chrome"],
        ["VS Code", "visual studio code", "code"], ["Notepad", "notepad", "file-text"], ["Task Manager", "task manager", "activity"]];
    const PROACTIVE_ICONS = { battery: "🔋", calendar: "📅", download: "📥", workflow: "▶️", reminder_followup: "🔁",
        morning_briefing: "☀️", idle_break: "🧘", streak: "🔥", habit: "💡", low_disk: "💾" };

    const state = {
        caps: {}, settings: {}, micMode: null, focusMode: false, mediaPlaying: false,
        mediaPosition: 0, mediaDuration: 0, mediaSeeking: false, replyActive: false, wakePhrases: ["hello wave", "wake up", "hey wave"],
    };
    const App = window.WaveApp = { el, icon, call, NAV, IDX, state, build: {}, pages: [], refs: {} };

    // ------------------------------------------------------------ helpers
    const vbox = (gap, children, cls, style) => el("div", { class: "vbox" + (gap ? " g" + gap : "") + (cls ? " " + cls : ""), style }, children);
    const hbox = (gap, children, cls, style) => el("div", { class: "hbox" + (gap ? " g" + gap : "") + (cls ? " " + cls : ""), style }, children);
    const heading = (text, style) => el("div", { class: "heading", style }, text);
    const lbl = (text, cls, style) => el("div", { class: "lbl" + (cls ? " " + cls : ""), style }, text);
    const stretch = () => el("div", { class: "stretch" });
    const space = (px) => ({ marginTop: px + "px" });
    function panel(children, opts) {
        opts = opts || {};
        return el("div", { class: "panel shadow" + (opts.cls ? " " + opts.cls : ""), style: Object.assign({
            display: "flex", flexDirection: opts.row ? "row" : "column", padding: opts.pad || "0", gap: (opts.gap === undefined ? 6 : opts.gap) + "px",
        }, opts.style || {}) }, children);
    }
    function btn(text, cls, onClick, iconName, iconColor, iconSize, tip) {
        const b = el("button", { class: "btn" + (cls ? " " + cls : ""), "data-tip": tip || undefined });
        if (iconName) b.appendChild(icon(iconName, iconColor || "var(--text-muted)", iconSize || 16));
        if (text) b.appendChild(el("span", {}, text));
        if (iconName && text) b.style.gap = "10px";
        if (onClick) b.addEventListener("click", onClick);
        return b;
    }
    App.ui = { vbox, hbox, heading, lbl, stretch, space, panel, btn };

    function makeBackRow(title, subtitle) {
        const back = el("button", { class: "btn icon-btn back-btn", "data-tip": "Back to Home (Esc)" }, [
            icon("chevron-left", "var(--text-muted)", 18), el("span", { class: "arrow" }, "←"),
        ]);
        back.addEventListener("click", () => slideTo(IDX.HOME));
        return el("div", { class: "back-row" }, [
            el("div", { class: "title-row" }, [back, el("div", { class: "page-title" }, title)]),
            subtitle ? el("div", { class: "page-subtitle" }, subtitle) : null,
        ]);
    }
    App.makeBackRow = makeBackRow;

    function fmtTime(sec) {
        sec = Math.max(0, Math.floor(sec || 0));
        const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
        return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
    }
    function nowStamp() {
        const d = new Date(); let h = d.getHours(); const ap = h < 12 ? "AM" : "PM"; h = h % 12 || 12;
        return `${String(h).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")} ${ap}`;
    }
    App.nowStamp = nowStamp;

    // ----------------------------------------------------------- toasts
    const toast = (msg, kind, ms) => W.toast(msg, kind, ms);
    App.toast = toast;

    // ============================================================ SHELL
    let navButtons = [];
    function buildShell() {
        document.body.prepend(el("div", { id: "backdrop" }));
        const nav = el("div", { id: "nav" }, NAV.map(([label, ic], i) => {
            const b = el("button", { class: "nav-item stagger", "data-tip": label, style: { animationDelay: (100 + i * 40) + "ms" } },
                [icon(ic, "var(--text-muted)", 20), el("span", {}, label)]);
            b.firstChild.classList.add("nav-icon");
            b.addEventListener("click", () => slideTo(i));
            return b;
        }));
        navButtons = Array.from(nav.children);
        App.refs.micMini = el("div", { class: "mini-status", style: { whiteSpace: "pre" } }, "●  STANDBY");
        App.refs.health = el("div", { class: "mini-status", style: { whiteSpace: "pre" }, "data-tip": "All background workers running normally." }, "●  SYSTEMS OK");
        const sidebar = el("aside", { id: "sidebar" }, [
            W.brandIcon(38),
            el("div", { class: "wordmark" }, "W . A . V . E ."),
            el("div", { class: "tagline" }, "DESKTOP ASSISTANT"),
            nav,
            el("div", { class: "sidebar-stretch" }),
            el("div", { class: "divider" }),
            App.refs.micMini,
            App.refs.health,
        ]);
        const stackEl = el("main", { id: "stack" });
        document.getElementById("app").append(sidebar, stackEl);
        App.stackEl = stackEl;
    }

    // ===================================================== SLIDING STACK
    const stack = { index: 0, animating: false };
    function setNavChecked(i) { navButtons.forEach((b, k) => b.classList.toggle("checked", k === i)); }
    function slideTo(index) {
        if (stack.animating || index === stack.index || index < 0 || index >= App.pages.length) return;
        stack.animating = true;
        const dir = index > stack.index ? 1 : -1;
        const oldP = App.pages[stack.index].root, newP = App.pages[index].root;
        setNavChecked(index);
        newP.classList.add("sliding", dir > 0 ? "slide-in-right" : "slide-in-left");
        oldP.classList.add(dir > 0 ? "slide-out-left" : "slide-out-right");
        const done = () => {
            oldP.classList.remove("current", "sliding", "slide-out-left", "slide-out-right");
            newP.classList.remove("sliding", "slide-in-right", "slide-in-left");
            newP.classList.add("current");
            stack.index = index; stack.animating = false;
            onIndexChanged(index);
        };
        setTimeout(done, 330);
    }
    App.slideTo = slideTo;
    App.currentIndex = () => stack.index;
    function onIndexChanged(i) {
        setNavChecked(i);
        if (i === IDX.HOME) setTimeout(refreshDashboardPreviews, 80);
        if (i === IDX.ACTIVITY) setTimeout(() => App.refreshActivity && App.refreshActivity(), 0);
        const p = App.pages[i];
        if (p && p.onShow) p.onShow();
        resizeVisibleCanvases();
    }
    function resizeVisibleCanvases() {
        requestAnimationFrame(() => {
            (App.sparks || []).forEach((s) => s.paint());
            (App.gauges || []).forEach((g) => g.paint());
        });
    }

    // horizontal wheel: _handle_wheel_delta (threshold 90, 450ms cooldown)
    let wheelAccum = 0, wheelCooldown = false;
    window.addEventListener("wheel", (e) => {
        const dx = e.deltaX || 0;
        if (!dx || Math.abs(dx) < Math.abs(e.deltaY)) return;
        if (document.querySelector(".overlay")) return;
        e.preventDefault();
        if (stack.animating || wheelCooldown || drag.active) return;
        wheelAccum += dx;
        if (Math.abs(wheelAccum) >= 90) {
            const next = wheelAccum > 0;
            wheelAccum = 0; wheelCooldown = true;
            setTimeout(() => { wheelCooldown = false; wheelAccum = 0; }, 450);
            slideTo(stack.index + (next ? 1 : -1));
        }
    }, { passive: false });

    // mouse drag between pages (SlidingStack.mousePress/Move/Release) -
    // only from non-interactive spots, exactly where Qt lets the press fall
    // through to the stack (labels, panels, backgrounds).
    const drag = { active: false, startX: 0, dx: 0 };
    const INTERACTIVE = "button, input, textarea, select, a, .slider, .combo, .check, .list, .tree-wrap, .chat-scroll, .qtable-wrap, .b-text, [contenteditable], .dash-tile, .cat-list, .scroll-y.no-drag";
    function initDrag() {
        const s = App.stackEl;
        s.addEventListener("pointerdown", (e) => {
            if (e.button !== 0 || stack.animating || e.target.closest(INTERACTIVE)) return;
            drag.active = true; drag.startX = e.clientX; drag.dx = 0; drag.moved = false;
        });
        window.addEventListener("pointermove", (e) => {
            if (!drag.active) return;
            drag.dx = e.clientX - drag.startX;
            if (!drag.moved && Math.abs(drag.dx) < 6) return;
            drag.moved = true;
            const w = s.clientWidth, cur = App.pages[stack.index].root;
            cur.style.transform = `translateX(${drag.dx}px)`;
            const nb = drag.dx < 0 ? stack.index + 1 : stack.index - 1;
            App.pages.forEach((p, k) => { if (k !== stack.index && k !== nb) { p.root.classList.remove("sliding"); p.root.style.transform = ""; } });
            if (nb >= 0 && nb < App.pages.length) {
                const n = App.pages[nb].root;
                n.classList.add("sliding");
                n.style.transform = `translateX(${(drag.dx < 0 ? w : -w) + drag.dx}px)`;
            }
        });
        window.addEventListener("pointerup", () => {
            if (!drag.active) return;
            drag.active = false;
            if (!drag.moved) return;
            const w = s.clientWidth, dx = drag.dx, cur = App.pages[stack.index].root;
            const nb = dx < 0 ? stack.index + 1 : stack.index - 1;
            const finish = Math.abs(dx) >= w * 0.2 && nb >= 0 && nb < App.pages.length;
            const animateTo = (node, x, ms) => { node.style.transition = `transform ${ms}ms cubic-bezier(0.33,1,0.68,1)`; node.style.transform = `translateX(${x}px)`; };
            stack.animating = true;
            if (finish) {
                const n = App.pages[nb].root;
                animateTo(cur, dx < 0 ? -w : w, 220); animateTo(n, 0, 220);
                setTimeout(() => {
                    [cur, n].forEach((x) => { x.style.transition = ""; x.style.transform = ""; });
                    cur.classList.remove("current"); n.classList.remove("sliding"); n.classList.add("current");
                    stack.index = nb; stack.animating = false; onIndexChanged(nb);
                }, 230);
            } else {
                animateTo(cur, 0, 200);
                if (nb >= 0 && nb < App.pages.length) animateTo(App.pages[nb].root, dx < 0 ? w : -w, 200);
                setTimeout(() => {
                    App.pages.forEach((p, k) => { p.root.style.transition = ""; p.root.style.transform = ""; if (k !== stack.index) p.root.classList.remove("sliding"); });
                    stack.animating = false;
                }, 210);
            }
        });
    }

    function makePage(cls) { return el("section", { class: "page" + (cls ? " " + cls : "") }); }

    // QVBoxLayout's extra-space rule (qGeomCalc): every "expanding" item gets
    // an EQUAL share of the column's height - never less than its own content
    // - while fixed items keep their natural size. A CSS grid with `Nfr` rows
    // for the expanding children and `auto` rows for the rest computes exactly
    // that, which `flex-grow` can't (it adds the leftover on top of padding).
    function qtColumns(root) {
        root.querySelectorAll(".std").forEach((col) => {
            const rows = Array.from(col.children).map((c) => {
                if (c.classList.contains("stretch")) return "minmax(0, 1fr)";
                const f = parseFloat(String(c.style.flex || "").split(" ")[0]);
                return f > 0 ? `${f}fr` : "auto";
            });
            Object.assign(col.style, { display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gridTemplateRows: rows.join(" ") });
        });
    }

    // ============================================================= HOME
    const tiles = [];
    App.build.home = function () {
        const root = makePage("page-bg");
        const grid = el("div", { class: "home-grid" });
        NAV.slice(1).forEach(([label, ic], k) => {
            const idx = k + 1;
            const preview = el("div", { class: "tile-preview" });
            const tile = el("div", { class: "dash-tile" }, [
                el("div", { class: "tile-title" }, [el("span", { class: "emoji" }, TILE_EMOJI[ic] || "⬜"), el("span", { class: "tl" }, label)]),
                preview,
            ]);
            tile.addEventListener("click", () => slideTo(idx));
            grid.appendChild(tile);
            tiles.push({ tile, preview, idx });
        });
        const dash = el("div", { class: "home-dash" }, [heading("QUICK ACCESS"), grid]);
        root.appendChild(el("div", { class: "home-wrap" }, el("div", { class: "scroll-y" }, dash)));
        new ResizeObserver(sizeTiles).observe(grid);
        return { root };
    };
    function sizeTiles() {
        tiles.forEach((t) => {
            const w = t.tile.clientWidth;
            if (w) t.preview.style.height = Math.max(116, Math.round(w * 0.625)) + "px";
        });
    }
    // _refresh_dashboard_previews(): a live, scaled-down render of each page.
    function refreshDashboardPreviews() {
        const sw = App.stackEl.clientWidth, sh = App.stackEl.clientHeight;
        if (!sw || !sh) return;
        tiles.forEach(({ preview, idx }) => {
            const src = App.pages[idx] && App.pages[idx].root;
            if (!src) return;
            const pw = preview.clientWidth, ph = preview.clientHeight;
            if (!pw || !ph) return;
            const clone = src.cloneNode(true);
            clone.querySelectorAll("[id]").forEach((n) => n.removeAttribute("id"));
            clone.removeAttribute("id");
            const srcCanvases = src.querySelectorAll("canvas"), dstCanvases = clone.querySelectorAll("canvas");
            dstCanvases.forEach((c, i) => {
                const s = srcCanvases[i];
                if (!s || !s.width || !s.height) return;
                c.width = s.width; c.height = s.height;
                try { c.getContext("2d").drawImage(s, 0, 0); } catch (e) { /* tainted - skip */ }
            });
            clone.querySelectorAll("textarea, input").forEach((n, i) => { n.setAttribute("tabindex", "-1"); });
            clone.classList.remove("slide-in-right", "slide-in-left", "slide-out-left", "slide-out-right", "sliding");
            clone.classList.add("current");
            const scale = Math.max(pw / sw, ph / sh);
            Object.assign(clone.style, {
                inset: "auto", position: "absolute", width: sw + "px", height: sh + "px", display: "block",
                transform: `translate(${-sw * scale / 2}px, ${-sh * scale / 2}px) scale(${scale})`,
                transformOrigin: "0 0", left: "50%", top: "50%", pointerEvents: "none",
            });
            clone.setAttribute("aria-hidden", "true"); clone.classList.add("clone");
            preview.innerHTML = ""; preview.appendChild(clone);
        });
    }
    App.refreshDashboardPreviews = refreshDashboardPreviews;

    // ============================================================= CHAT
    // =============================================================== DODO
    // Web-only page: Wave's character (the Tier 25b avatar) used to sit at
    // the top of the Chat panel; the user moved it here, its own sidebar
    // entry just above Chat. js/avatar.js still draws and animates it.
    const DODO_STATES = { idle: ["Idle", "var(--text-muted)"], listening: ["Listening", "var(--accent-2)"],
        thinking: ["Thinking", "var(--warning)"], speaking: ["Speaking", "var(--accent-1)"] };
    const dodo = { mount: null, state: null };
    App.dodo = dodo;
    App.build.dodo = function () {
        const root = makePage("page-bg");
        dodo.mount = el("div", { class: "dodo-mount" });
        dodo.state = el("div", { class: "dodo-state" });
        const stage = el("div", { class: "panel shadow dodo-stage" }, [dodo.mount, dodo.state]);
        root.appendChild(el("div", { class: "dodo-wrap" }, [makeBackRow("Dodo", "W.A.V.E.'s character — reacts while you talk to Wave"), stage]));
        return { root };
    };
    function showDodoState(name) {
        const [text, color] = DODO_STATES[name] || DODO_STATES.idle;
        dodo.state.textContent = "●  " + text;
        dodo.state.style.color = color;
    }

    // =============================================================== CHAT
    const chat = { list: null, scroll: null, input: null, sendBtn: null, micBtn: null, status: null, banner: null,
        visualizer: null, attachChip: null, pending: null, reply: null, captionRow: null, captionLabel: null,
        captionSeq: 0, captionClosedAt: -1e9, bannerGen: 0, turnSeq: 0 };
    App.chat = chat;
    App.build.chat = function () {
        const root = makePage("page-bg");
        chat.status = el("div", { class: "status-message" });
        const clearBtn = btn(null, "icon-btn", () => clearChat(), "trash", "var(--text-muted)", 18, "Clear chat (Ctrl+L)");
        const header = el("div", { class: "chat-header" }, [
            el("div", { class: "avatar-w" }, "W"),
            el("div", { class: "name-col" }, [el("div", { class: "contact-name" }, "W.A.V.E. Assistant"), chat.status]),
            el("div", { class: "stretch" }),
            clearBtn,
        ]);
        chat.banner = el("div", { class: "activity-banner hidden" });
        chat.list = el("div", { class: "chat-list" });
        chat.scroll = el("div", { class: "chat-scroll" }, chat.list);
        chat.scroll.addEventListener("contextmenu", (e) => {
            e.preventDefault();
            W.contextMenu(e.clientX, e.clientY, [["Clear Chat", clearChat], ["Copy Last AI Response", copyLastAi], ["Export Chat", exportChat]]);
        });
        chat.visualizer = new W.AudioVisualizer(40);
        // attachment chip
        chat.attachThumb = el("div", { class: "ac-thumb" });
        chat.attachName = el("div", { class: "ac-name" });
        chat.attachMeta = el("div", { class: "ac-meta" });
        chat.attachChip = el("div", { class: "attach-chip hidden" }, [
            chat.attachThumb,
            el("div", { class: "vbox", style: { flex: "1 1 auto", gap: "1px", minWidth: 0 } }, [chat.attachName, chat.attachMeta]),
            btn(null, "icon-btn", () => clearAttachment(), "xmark", "var(--text-muted)", 14, "Remove attachment"),
        ]);
        const attachBtn = el("button", { class: "btn mic-inline", "data-tip": "Attach an image or document" }, icon("paperclip", "var(--accent-1)", 18));
        attachBtn.addEventListener("click", pickAttachment);
        chat.micBtn = el("button", { class: "btn mic-inline", "data-tip": "Toggle voice listening (Ctrl+Shift+Space)" }, icon("mic", "var(--accent-1)", 20));
        chat.micBtn.addEventListener("click", () => toggleMic());
        chat.input = el("textarea", { class: "chat-input", placeholder: "Message W.A.V.E... (Enter to send, Shift+Enter for new line)", rows: "1" });
        chat.input.addEventListener("keydown", (e) => {
            if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMessage(); }
        });
        chat.input.addEventListener("input", autoGrow);
        chat.sendBtn = el("button", { class: "btn send", "data-tip": "Send (Enter)" }, icon("send", "#000000", 20));
        chat.sendBtn.addEventListener("click", () => (chat.reply ? stopClicked() : sendMessage()));
        const inputBar = el("div", { class: "input-bar" }, [
            chat.visualizer.canvas, chat.attachChip,
            el("div", { class: "input-row" }, [attachBtn, chat.micBtn, chat.input, chat.sendBtn]),
        ]);
        const chatPanel = el("div", { class: "panel shadow chat-panel" }, [header, chat.banner, chat.scroll, inputBar]);
        root.appendChild(el("div", { class: "chat-wrap" }, [makeBackRow("Chat", "Talk to W.A.V.E., your desktop assistant"), chatPanel]));
        setStatus("Online", T().success);
        return { root };
    };
    function autoGrow() {
        const ta = chat.input;
        ta.style.height = "44px";
        ta.style.height = Math.max(44, Math.min(132, ta.scrollHeight + 2)) + "px";
    }
    function setStatus(text, dot) {
        if (!chat.status) return;
        chat.status.innerHTML = "";
        chat.status.append(el("span", { style: { color: dot } }, "●"), el("span", { style: { color: "var(--text-muted)", whiteSpace: "pre" } }, "  " + text));
    }
    App.setStatus = setStatus;
    function greeting() { const h = new Date().getHours(); return h < 12 ? "Good Morning" : h < 18 ? "Good Afternoon" : "Good Evening"; }
    function bubbleMaxWidth(text) {
        const ratio = (text || "").includes("```") ? 0.88 : 0.68;
        const w = chat.scroll ? chat.scroll.clientWidth : 0;
        if (!w) return 420;
        return Math.round(Math.min(720, Math.max(260, w * ratio)));
    }
    function scrollChatToBottom(delay) {
        setTimeout(() => { if (chat.scroll) chat.scroll.scrollTop = chat.scroll.scrollHeight; }, delay === undefined ? 80 : delay);
    }

    // --- markdown -> sanitised HTML (rendered by the bridge with the same
    // python-markdown + Pygments monokai setup ChatBubble._format_message
    // asks for; the result is filtered here so LLM-provided HTML can never
    // run script or navigate the app window).
    const ALLOWED = new Set(["P", "BR", "STRONG", "B", "EM", "I", "U", "DEL", "S", "CODE", "PRE", "SPAN", "DIV", "UL", "OL", "LI",
        "H1", "H2", "H3", "H4", "H5", "H6", "TABLE", "THEAD", "TBODY", "TR", "TH", "TD", "BLOCKQUOTE", "A", "HR", "SUP", "SUB"]);
    function sanitize(html) {
        const doc = new DOMParser().parseFromString(`<div>${html}</div>`, "text/html");
        const walk = (node) => {
            Array.from(node.childNodes).forEach((c) => {
                if (c.nodeType === 3) return;
                if (c.nodeType !== 1 || !ALLOWED.has(c.tagName)) {
                    if (c.nodeType === 1 && !["SCRIPT", "STYLE", "IFRAME", "OBJECT", "EMBED", "SVG", "MATH", "TEMPLATE", "NOSCRIPT"].includes(c.tagName)) {
                        const frag = doc.createDocumentFragment();
                        Array.from(c.childNodes).forEach((g) => frag.appendChild(g));
                        walk(frag); c.replaceWith(frag);
                    } else c.remove();
                    return;
                }
                Array.from(c.attributes).forEach((a) => {
                    const n = a.name.toLowerCase();
                    if (n === "href" && c.tagName === "A" && /^https?:\/\//i.test(a.value)) return;
                    if (n === "style" && /^[\s#a-z0-9:;%.,()-]*$/i.test(a.value) && !/url|expression|@import/i.test(a.value)) return;
                    if (n === "class" && /^[a-z0-9 _-]*$/i.test(a.value)) return;
                    if ((n === "colspan" || n === "rowspan" || n === "align") && /^[a-z0-9]+$/i.test(a.value)) return;
                    c.removeAttribute(a.name);
                });
                walk(c);
            });
        };
        const root = doc.body.firstChild;
        walk(root);
        return root.innerHTML;
    }
    function escapeHtml(s) { return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }
    let mdSeq = 0;
    async function renderInto(node, text) {
        const my = ++mdSeq; node.dataset.mdseq = my;
        node.innerHTML = escapeHtml(text).replace(/\n/g, "<br>");
        const r = await call("render_markdown", text);
        if (node.dataset.mdseq !== String(my)) return;
        if (r && r.ok && typeof r.html === "string") node.innerHTML = sanitize(r.html);
    }
    document.addEventListener("click", (e) => {
        const a = e.target.closest && e.target.closest(".b-text a[href]");
        if (!a) return;
        e.preventDefault();
        call("open_url", a.getAttribute("href"));
    });

    // --- ChatBubble ------------------------------------------------------
    const REACTIONS = ["👍", "❤️", "😂", "😮", "😢", "😡"];
    function tailSvg(isUser, color) {
        const pts = isUser ? "0,0 17,10 0,18" : "18,0 1,10 18,18";
        const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        svg.setAttribute("class", "tail"); svg.setAttribute("width", "18"); svg.setAttribute("height", "19");
        svg.setAttribute("viewBox", "0 0 18 19");
        const p = document.createElementNS("http://www.w3.org/2000/svg", "polygon");
        p.setAttribute("points", pts); p.style.fill = color;
        svg.appendChild(p);
        return svg;
    }
    function addChatBubble(text, isUser, attachment) {
        text = text || "";
        const row = el("div", { class: "bubble-row " + (isUser ? "user" : "ai") });
        const bubble = el("div", { class: "bubble " + (isUser ? "user" : "ai") });
        bubble.style.maxWidth = bubbleMaxWidth(text) + "px";
        bubble.appendChild(el("div", { class: "b-shape" }, [el("div", { class: "body" }), tailSvg(isUser, isUser ? "var(--bubble-sent)" : "var(--bubble-recv)")]));
        if (attachment) bubble.appendChild(attachmentWidget(attachment, isUser));
        const textNode = el("div", { class: "b-text" });
        if (text.trim()) renderInto(textNode, text);
        else if (isUser) textNode.classList.add("hidden");
        bubble.appendChild(textNode);
        const stamp = el("span", { class: "stamp" }, nowStamp());
        const stampRow = el("div", { class: "stamp-row" });
        if (!isUser) {
            REACTIONS.forEach((r) => stampRow.appendChild(el("button", { class: "react emoji", onClick: () => toast(`Reacted with ${r}`, "info") }, r)));
            stampRow.appendChild(stamp);
            const replay = el("button", { class: "replay emoji", "data-tip": "Read aloud" }, "🔊");
            replay.addEventListener("click", () => startSpeaking(bubble._text));
            stampRow.appendChild(replay);
        } else {
            stampRow.append(stamp, el("span", { class: "ticks" }, "✓✓"));
        }
        bubble.appendChild(stampRow);
        const copy = el("button", { class: "copy emoji" }, "📋");
        copy.addEventListener("click", () => call("copy_to_clipboard", bubble._text));
        bubble.appendChild(copy);
        bubble._text = text; bubble._textNode = textNode; bubble.isUser = isUser;
        row.appendChild(bubble);
        if (chat.captionRow && chat.captionRow.parentNode === chat.list) chat.list.insertBefore(row, chat.captionRow);
        else chat.list.appendChild(row);
        scrollChatToBottom();
        return bubble;
    }
    App.addChatBubble = addChatBubble;
    function setBubbleText(bubble, text) {
        bubble._text = text || "";
        bubble.style.maxWidth = bubbleMaxWidth(text) + "px";
        bubble._textNode.classList.toggle("hidden", !bubble._text.trim() && bubble.isUser);
        if (bubble._text.trim()) renderInto(bubble._textNode, bubble._text); else bubble._textNode.innerHTML = "";
    }
    function attachmentWidget(att, isUser) {
        if (att.kind === "image") {
            if (att.thumb) return el("img", { class: "attach-img", src: att.thumb, alt: att.name || "image" });
            return el("div", { class: "lbl" }, "🖼 (image unavailable)");
        }
        const kb = (att.size_bytes || 0) / 1024;
        const size = kb > 1024 ? `${(kb / 1024).toFixed(1)} MB` : `${kb.toFixed(0)} KB`;
        return el("div", { class: "attach-doc" }, [
            el("span", { class: "emoji", style: { fontSize: "18px" } }, (att.name || "").toLowerCase().endsWith(".pdf") ? "📕" : "📄"),
            el("div", { class: "vbox" }, [el("div", { class: "ad-name" }, att.name || "file"), el("div", { class: "ad-meta" }, size)]),
        ]);
    }
    function clearChat() {
        cancelReply();
        chat.list.innerHTML = "";
        chat.captionRow = null; chat.captionLabel = null;
        addChatBubble("Chat cleared. Fresh start!", false);
    }
    App.clearChat = clearChat;
    function copyLastAi() {
        const bubbles = Array.from(chat.list.querySelectorAll(".bubble.ai"));
        const last = bubbles[bubbles.length - 1];
        if (!last) return;
        call("copy_to_clipboard", last._text).then((r) => {
            if (r && r.ok) toast("Copied last AI response", "success");
            else toast((r && r.error) || "pyperclip not installed", "warning");
        });
    }
    function exportChat() {
        const lines = Array.from(chat.list.querySelectorAll(".bubble")).map((b) => (b.isUser ? "You: " : "WAVE: ") + (b._textNode.innerText || ""));
        call("export_chat", lines.join("\n\n") + (lines.length ? "\n\n" : "")).then((r) => { if (r && r.ok && r.saved) toast("Chat exported", "success"); });
    }

    // --- attachments -----------------------------------------------------
    async function pickAttachment() {
        const r = await call("pick_attachment");
        if (!r || !r.ok) { if (r && r.error) toast(r.error, r.level || "warning"); return; }
        if (!r.attachment) return;
        chat.pending = r.attachment;
        showAttachPreview();
    }
    function showAttachPreview() {
        const att = chat.pending;
        if (!att) { chat.attachChip.classList.add("hidden"); return; }
        chat.attachThumb.innerHTML = "";
        if (att.kind === "image" && att.thumb) chat.attachThumb.appendChild(el("img", { src: att.thumb }));
        else chat.attachThumb.appendChild(el("span", { class: "emoji" }, "📄"));
        const kb = (att.size_bytes || 0) / 1024;
        const size = kb > 1024 ? `${(kb / 1024).toFixed(1)} MB` : `${kb.toFixed(0)} KB`;
        chat.attachName.textContent = att.name;
        chat.attachMeta.textContent = `${size}${att.truncated ? " (truncated)" : ""} - will be sent with your message`;
        chat.attachChip.classList.remove("hidden");
    }
    function clearAttachment() { chat.pending = null; chat.attachChip.classList.add("hidden"); }

    // --- live captions (Tier 12) -------------------------------------------
    function onPartialText(text) {
        text = (text || "").trim();
        if (!text || performance.now() - chat.captionClosedAt < 500) return;
        if (!chat.captionLabel) {
            chat.captionLabel = el("div", { class: "live-caption" });
            chat.captionRow = el("div", { class: "caption-row" }, chat.captionLabel);
            chat.list.appendChild(chat.captionRow);
        }
        chat.captionLabel.style.maxWidth = bubbleMaxWidth(text) + "px";
        chat.captionLabel.textContent = "🎙 " + text + " …";
        const seq = ++chat.captionSeq;
        setTimeout(() => clearCaption(seq), 4000);
        scrollChatToBottom();
    }
    function clearCaption(seq) {
        if (seq !== undefined && seq !== chat.captionSeq) return;
        if (seq === undefined) chat.captionClosedAt = performance.now();
        if (chat.captionRow) chat.captionRow.remove();
        chat.captionRow = null; chat.captionLabel = null;
    }

    // --- activity banner (Tier 21) -----------------------------------------
    function onToolActivity(desc) {
        chat.banner.textContent = `⚙  ${desc}…`;
        chat.banner.style.whiteSpace = "pre";
        chat.banner.classList.remove("hidden");
        const gen = ++chat.bannerGen;
        setTimeout(() => { if (gen === chat.bannerGen) chat.banner.classList.add("hidden"); }, 4000);
    }

    // --- voice state ----------------------------------------------------------
    function showIdleVoiceState() {
        if (state.micMode === "AWAKE") { chat.visualizer.setMode("LISTENING"); setStatus("Listening...", "var(--accent-1)"); }
        else { chat.visualizer.setMode("SLEEPING"); setStatus("Online", T().success); }
    }
    App.showIdleVoiceState = showIdleVoiceState;
    function stopSpeaking() { call("stop_speaking"); showIdleVoiceState(); }
    function startSpeaking(text) {
        if (!text) return;
        call("stop_speaking");
        chat.visualizer.setMode("SPEAKING");
        setStatus("Speaking...", T().accent_3);
        call("speak", text);
    }
    App.startSpeaking = startSpeaking;
    function handleWakeWordMode(mode) {
        state.micMode = mode;
        const t = T();
        if (mode === "AWAKE") {
            chat.micBtn.classList.add("awake");
            chat.micBtn.style.setProperty("--glow", WaveTheme.withAlpha(t.accent_1, 150 / 255));
            App.refs.micMini.textContent = "●  LISTENING";
            App.refs.micMini.style.color = t.accent_1;
            chat.visualizer.setMode("LISTENING");
            setStatus("Listening...", t.accent_1);
        } else {
            chat.micBtn.classList.remove("awake");
            App.refs.micMini.textContent = "●  STANDBY";
            App.refs.micMini.style.color = "var(--text-muted)";
            chat.visualizer.setMode("SLEEPING");
            setStatus("Online", t.success);
        }
        if (window.WaveAvatar) WaveAvatar.setMicOn(mode === "AWAKE");
    }
    function toggleMic() { call("toggle_listening"); }
    App.toggleMic = toggleMic;

    // --- sending / streaming ----------------------------------------------------
    function setSendStopMode(isStop) {
        chat.sendBtn.innerHTML = "";
        chat.sendBtn.appendChild(isStop ? icon("stop", "#000000", 16) : icon("send", "#000000", 20));
        chat.sendBtn.setAttribute("data-tip", isStop ? "Stop" : "Send (Enter)");
    }
    function sendMessage(fromVoice) {
        const text = chat.input.value.trim();
        const att = chat.pending;
        if (!text && !att) return;
        cancelReply();
        stopSpeaking();
        addChatBubble(text, true, att);
        chat.input.value = ""; autoGrow();
        clearAttachment();
        setStatus("Thinking...", T().warning);
        const bubble = addChatBubble("…", false);
        // Tier 29: every reply carries its own turn number, echoed back on
        // chat_chunk/chat_done - a cancelled turn finishing late can never
        // again end (or blank out) the NEXT reply's bubble.
        const turn = ++chat.turnSeq;
        chat.reply = { bubble, text: "", done: false, renderPending: false, cancelled: false, turn };
        setSendStopMode(true);
        call("send_message", text, att ? { kind: att.kind, path: att.path, name: att.name, size_bytes: att.size_bytes,
            text_content: att.text_content || null, truncated: !!att.truncated } : null, !!fromVoice, turn);
    }
    // Events from an older (cancelled) turn are dropped; events with no turn
    // number (an older bridge) still go to the current reply.
    const forCurrentTurn = (turn) => !!chat.reply && (turn === undefined || turn === null || turn === chat.reply.turn);
    App.sendMessage = sendMessage;
    function cancelReply() {
        const r = chat.reply;
        if (!r) return false;
        r.cancelled = true;
        chat.reply = null;
        setSendStopMode(false);
        call("cancel_reply");
        finishReply(r, null);
        return true;
    }
    App.cancelReply = cancelReply;
    function stopClicked() {
        const had = cancelReply();
        call("stop_speaking");
        showIdleVoiceState();
        if (had) toast("⏹ Stopped", "info");
    }
    function applyReplyText(view, text) {
        const follow = chat.scroll.scrollTop >= chat.scroll.scrollHeight - chat.scroll.clientHeight - 48;
        setBubbleText(view.bubble, text);
        if (follow) scrollChatToBottom(30);
    }
    function onReplyChunk(piece) {
        const v = chat.reply;
        if (!v || v.done || !piece) return;
        const first = !v.text;
        v.text += piece;
        if (first) { chat.visualizer.setMode("SPEAKING"); setStatus("Speaking...", T().accent_3); }
        if (!v.renderPending) {
            v.renderPending = true;
            setTimeout(() => { v.renderPending = false; if (!v.done) applyReplyText(v, v.text || "…"); }, 60);
        }
    }
    function finishReply(v, fullText) {
        if (!v || v.done) return;
        v.done = true;
        let text = fullText || v.text;
        if (v.cancelled) text = (text.trim() ? text.trimEnd() + "\n\n" : "") + "⏹ *Stopped*";
        else if (!text.trim()) text = "Hmm, no answer came back that time - please ask me again.";
        v.text = text;
        applyReplyText(v, text);
    }
    function onReplyDone(fullText) {
        const v = chat.reply;
        if (!v) return;
        chat.reply = null;
        setSendStopMode(false);
        finishReply(v, fullText);
    }
    function displayAiResponse(response) {
        if (!response) return;
        addChatBubble(response, false);
        call("speech_text", response).then((r) => startSpeaking((r && r.text) || response));
    }
    App.displayAiResponse = displayAiResponse;
    // MainWindow._run_action(): run a backend action, show + speak its result.
    function runAction(name, args, announce) {
        return call("run_action", name, args || []).then((r) => {
            if (!r || !r.ok) { toast(`Error: ${(r && r.error) || "failed"}`, "error"); return r; }
            if (announce !== false) displayAiResponse(r.result);
            return r;
        });
    }
    App.runAction = runAction;

    function emergencyStop() {
        cancelReply();
        call("emergency_stop");
        showIdleVoiceState();
        toast("Emergency stop - all actions halted.", "error");
        addChatBubble("🛑 Emergency stop — all actions halted.", false);
    }

    // ===================================================== SYSTEM MONITOR
    App.sparks = []; App.gauges = [];
    const sys = {};
    App.build.system = function () {
        const root = makePage("transparent-panels");
        const t = T();
        const cells = [["CPU", t.accent_2, "cpu"], ["MEMORY", t.accent_1, "ram"], ["DISK", t.accent_3, "disk"], ["BATTERY", t.success, "bat"]].map(([title, color, key]) => {
            const g = new W.CircularGauge(color); const s = new W.Sparkline(color, 60);
            App.gauges.push(g); App.sparks.push(s);
            sys[key + "Gauge"] = g; sys[key + "Spark"] = s;
            sys[key + "Detail"] = el("div", { class: "gauge-detail" }, "—");
            return el("div", { class: "gauge-cell" }, [g.canvas, s.canvas, el("div", { class: "gauge-caption" }, title), sys[key + "Detail"]]);
        });
        const gaugePanel = panel(cells, { row: true, pad: "28px 30px", gap: 26, style: { flex: "none" } });
        sys.dl = el("div", { class: "info-data" }, "⬇️ DL: 0.00 MB/s");
        sys.ul = el("div", { class: "info-data" }, "⬆️ UL: 0.00 MB/s");
        sys.ip = el("div", { class: "lbl", style: { color: "var(--text-muted)", fontSize: "11px" } }, "Local IP: …");
        sys.dlSpark = new W.Sparkline(t.accent_1, 60); sys.ulSpark = new W.Sparkline(t.accent_2, 60);
        App.sparks.push(sys.dlSpark, sys.ulSpark);
        const netPanel = panel([
            heading("NETWORK"), el("div", { style: space(10) }, sys.dl), sys.ul, el("div", { style: space(6) }, sys.ip),
            el("div", { class: "net-sparks" }, [lbl("DL"), sys.dlSpark.canvas, lbl("UL"), sys.ulSpark.canvas]), stretch(),
        ], { pad: "22px 26px", style: { flex: "1 1 0" } });
        sys.hw = el("div", { class: "lbl wrap", style: { color: "var(--text-main)", fontSize: "12px" } }, "Tap below to fetch your PC's hardware details.");
        const hwBtn = btn("Get Hardware Info", "action", fetchHardwareInfo, "cpu", t.accent_1, 16);
        const infoPanel = panel([heading("HARDWARE INFO"), el("div", { style: space(10) }, sys.hw), el("div", { style: Object.assign(space(10), { display: "flex", flexDirection: "column" }) }, hwBtn), stretch()],
            { pad: "22px 26px", style: { flex: "1 1 0" } });
        sys.procBody = el("tbody");
        const tree = el("table", { class: "tree" }, [
            el("thead", {}, el("tr", {}, ["Name", "CPU %", "Memory", "PID"].map((h, i) => el("th", { style: { width: ["", "100px", "120px", "100px"][i] || undefined } }, h)))),
            sys.procBody,
        ]);
        const procPanel = panel([heading("TOP PROCESSES"), el("div", { class: "tree-wrap", style: space(10) }, tree)], { pad: "22px 26px", style: { flex: "1 1 0", minHeight: "120px" } });
        const page = el("div", { class: "std" }, [
            makeBackRow("System Monitor", "Live hardware usage, refreshed every second"),
            gaugePanel, el("div", { class: "cols-2", style: { flex: "none" } }, [netPanel, infoPanel]), procPanel,
        ]);
        root.appendChild(el("div", { class: "scroll-y" }, page));
        WaveTheme.onChange((th) => {
            [["cpu", th.accent_2], ["ram", th.accent_1], ["disk", th.accent_3], ["bat", th.success]].forEach(([k, c]) => {
                if (k !== "bat") sys[k + "Gauge"].setColor(c);
                sys[k + "Spark"].color = c; sys[k + "Spark"].paint();
            });
            sys.dlSpark.color = th.accent_1; sys.ulSpark.color = th.accent_2; sys.dlSpark.paint(); sys.ulSpark.paint();
        });
        return { root, onShow: () => refreshProcesses() };
    };
    function fetchHardwareInfo() {
        sys.hw.textContent = "Fetching...";
        call("get_hardware_info").then((r) => { sys.hw.textContent = r && r.ok ? String(r.text || "").replace(/\*\*/g, "") : `Error: ${(r && r.error) || ""}`; });
    }
    function refreshProcesses() {
        call("get_top_processes", 12).then((r) => {
            if (!r || !r.ok) return;
            sys.procBody.innerHTML = "";
            (r.processes || []).forEach((p) => sys.procBody.appendChild(el("tr", {}, [
                el("td", {}, p.name), el("td", {}, (p.cpu || 0).toFixed(1)), el("td", {}, `${(p.memory_mb || 0).toFixed(0)} MB`), el("td", {}, String(p.pid)),
            ])));
        });
    }
    let lastNet = null;
    async function updateSysStats() {
        if (document.hidden) return;
        const r = await call("get_system_snapshot");
        if (!r || !r.ok) return;
        const s = r.stats || {}, t = T();
        const cpu = s.cpu_percent || 0, ram = s.ram_percent || 0;
        sys.cpuGauge.setValue(cpu); sys.cpuDetail.textContent = `${cpu.toFixed(0)}% load`; sys.cpuSpark.addValue(cpu);
        sys.ramGauge.setValue(ram); sys.ramDetail.textContent = `${s.ram_used_gb ?? "—"}/${s.ram_total_gb ?? "—"} GB`; sys.ramSpark.addValue(ram);
        let bat = s.battery_percent, bcolor, btext;
        if (typeof bat === "number") {
            bcolor = bat <= 20 ? t.danger : bat <= 50 ? t.warning : t.success;
            btext = s.is_plugged ? "Plugged In" : "On Battery";
        } else { bat = 0; bcolor = t.text_faint; btext = "No Battery"; }
        sys.batGauge.setValue(bat); sys.batGauge.setColor(bcolor); sys.batDetail.textContent = btext; sys.batSpark.addValue(bat);
        if (r.disk) { sys.diskGauge.setValue(r.disk.percent); sys.diskDetail.textContent = `${r.disk.free_gb.toFixed(1)} GB free`; sys.diskSpark.addValue(r.disk.percent); }
        if (r.net) {
            const now = performance.now() / 1000;
            if (lastNet) {
                const el2 = Math.max(0.25, now - lastNet.t);
                const dl = (r.net.recv - lastNet.recv) / 1048576 / el2, ul = (r.net.sent - lastNet.sent) / 1048576 / el2;
                sys.dl.textContent = `⬇️ DL: ${dl.toFixed(2)} MB/s`; sys.ul.textContent = `⬆️ UL: ${ul.toFixed(2)} MB/s`;
                sys.dlSpark.addValue(Math.min(100, dl * 10)); sys.ulSpark.addValue(Math.min(100, ul * 10));
            }
            lastNet = { t: now, recv: r.net.recv, sent: r.net.sent };
        }
    }

    // ======================================================= APPS & POWER
    const appPills = {};
    App.build.apps = function () {
        const root = makePage("transparent-panels");
        const t = T();
        const grid = el("div", { style: { display: "grid", gridTemplateColumns: "repeat(3, minmax(0, 1fr))", gap: "10px" } });
        QUICK_APPS.forEach(([name, target, ic]) => {
            const b = btn(name, "pill", () => launchApp(target, name), ic, "var(--text-muted)", 18);
            appPills[name] = b; grid.appendChild(b);
        });
        const search = el("input", { class: "line-edit", type: "text", placeholder: "Open any app by name..." });
        const openSearched = () => { const n = search.value.trim(); if (n) { runAction("open_app", [n]); search.value = ""; } };
        search.addEventListener("keydown", (e) => { if (e.key === "Enter") openSearched(); });
        const appsPanel = panel([
            heading("QUICK APPS"), el("div", { style: space(14) }, grid), el("div", { class: "divider", style: space(16) }),
            el("div", { style: space(14) }, heading("OR OPEN ANY OTHER APP")),
            el("div", { class: "hbox g10", style: Object.assign(space(10), { alignItems: "center" }) }, [search, btn("Open", "action", openSearched)]),
            el("div", { class: "note-faint wrap", style: space(6) }, "Tip: you can also just type \"open spotify\" (or any app) straight into the chat."),
            stretch(),
        ], { pad: "24px 28px", style: { flex: "1 1 0" } });
        App.refs.focusBtn = btn("Focus Mode: OFF", "pill", toggleFocus);
        const pcGrid = el("div", { style: { display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: "10px" } }, [
            btn("Lock", "pill", () => runAction("lock_pc", [], false), "lock", "var(--text-muted)", 18),
            btn("Screenshot", "pill", () => runAction("take_screenshot"), "camera", "var(--text-muted)", 18),
            btn("Delete Last SS", "pill", () => runAction("delete_latest_screenshot"), "trash", "var(--text-muted)", 18),
            btn("Mute OS", "pill", () => call("press_key", "volumemute"), "volume-mute", "var(--text-muted)", 18),
        ]);
        App.refs.focusBtn.style.gridColumn = "1 / span 2";
        pcGrid.appendChild(App.refs.focusBtn);
        const powerPanel = panel([heading("PC STATE"), el("div", { style: space(14) }, pcGrid)], { pad: "24px 28px", style: { flex: "none" } });
        const dangerHead = heading("DANGER ZONE", { flex: "1 1 0", display: "flex", alignItems: "center" });
        const dangerPanel = panel([dangerHead, el("div", { class: "hbox g10", style: space(12) }, [
            btn("Restart PC", "danger-pill grow", () => confirmPower("restart")),
            btn("Shutdown PC", "danger-pill grow", () => confirmPower("shutdown")),
        ])], { pad: "24px 28px", style: { flex: "1 1 auto" } });
        const row = el("div", { class: "cols-2", style: { flex: "1 1 0" } }, [appsPanel, el("div", { class: "vbox g20", style: { minWidth: 0 } }, [powerPanel, dangerPanel])]);
        const page = el("div", { class: "std" }, [makeBackRow("Apps & Power", "Launch apps instantly and control your PC's power state"), row, stretch()]);
        root.appendChild(el("div", { class: "scroll-y" }, page));
        return { root };
    };
    function launchApp(target, displayName) {
        Object.entries(appPills).forEach(([n, b]) => b.classList.toggle("active", n === displayName));
        runAction("open_app", [target]);
    }
    function toggleFocus() {
        state.focusMode = !state.focusMode;
        call("press_key", "volumemute");
        App.refs.focusBtn.querySelector("span").textContent = `Focus Mode: ${state.focusMode ? "ON" : "OFF"}`;
        App.refs.focusBtn.classList.toggle("active", state.focusMode);
        addChatBubble(state.focusMode ? "🧘 Focus Mode ON: System muted. Distractions minimized." : "🔔 Focus Mode OFF: System audio restored.", false);
    }
    App.toggleFocus = toggleFocus;
    async function confirmPower(kind) {
        const title = kind === "restart" ? "Restart PC" : "Shutdown PC";
        const msg = `Are you sure you want to ${kind === "restart" ? "restart" : "shut down"} your PC right now?\nAny unsaved work will be lost.`;
        const v = await W.msgBox(title, msg, "warning", [{ text: "Yes, continue", value: true }, { text: "Cancel", value: false, default: true }]);
        if (v !== true) return;
        const r = await call("power_command", kind);
        if (r && r.ok) {
            addChatBubble(`⚠️ ${title} confirmed — executing in 5 seconds.`, false);
            toast(`${title} scheduled in 5s`, "warning");
        } else toast(`Error: ${(r && r.error) || "failed"}`, "error");
    }

    // ============================================================== MUSIC
    const music = {};
    App.build.music = function () {
        const root = makePage("transparent-panels");
        const t = T();
        music.disc = new W.AlbumArtDisc(76);
        music.title = el("div", { style: { color: "var(--text-main)", fontSize: "16px", fontWeight: "700", lineHeight: 1.33 } }, "System Media Player");
        music.sub = el("div", { class: "wrap", style: { color: "var(--text-muted)", fontSize: "12px", lineHeight: 1.33 } }, "Works with Spotify, YouTube & more — press play to control");
        const infoRow = el("div", { class: "hbox g18", style: { gap: "18px", alignItems: "center", flex: "1 1 0", minHeight: "76px" } }, [
            music.disc.canvas, el("div", { class: "vbox", style: { gap: "4px", flex: "1 1 auto" } }, [music.title, music.sub]),
        ]);
        music.viz = new W.AudioVisualizer(34);
        music.elapsed = el("div", { style: { color: "var(--text-muted)", fontSize: "11px" } }, "0:00");
        music.duration = el("div", { style: { color: "var(--text-muted)", fontSize: "11px" } }, "0:00");
        music.progress = new W.Slider(0, 0, 0, null, {
            onPress: () => { state.mediaSeeking = true; },
            onRelease: (v) => { const target = v / 1000; state.mediaPosition = target; music.elapsed.textContent = fmtTime(target); call("seek_media", target); state.mediaSeeking = false; },
        });
        const winsdk = !!state.caps.winsdk;
        music.progress.setEnabled(winsdk);
        const prev = el("button", { class: "btn round-sm round52" }, icon("skip-back", "var(--text-main)", 22));
        prev.addEventListener("click", () => call("press_key", "prevtrack"));
        const next = el("button", { class: "btn round-sm round52" }, icon("skip-forward", "var(--text-main)", 22));
        next.addEventListener("click", () => call("press_key", "nexttrack"));
        const back10 = btn("-10s", "round-sm seek", () => skipSeconds(-10));
        const fwd10 = btn("+10s", "round-sm seek", () => skipSeconds(10));
        if (!winsdk) { back10.classList.add("hidden"); fwd10.classList.add("hidden"); }
        music.play = el("button", { class: "btn play-big keep-bg" });
        music.play.addEventListener("click", togglePlayPause);
        stylePlayButton();
        const nowItems = [
            heading("NOW PLAYING"), el("div", { style: Object.assign(space(18), { display: "flex", flex: "1 1 0" }) }, infoRow),
            el("div", { style: space(22) }, music.viz.canvas),
            el("div", { class: "hbox g10", style: Object.assign(space(14), { alignItems: "center" }) }, [music.elapsed, music.progress.root, music.duration]),
            el("div", { class: "transport", style: space(8) }, [prev, back10, music.play, fwd10, next]),
        ];
        if (!winsdk) nowItems.push(el("div", { class: "wrap", style: { color: "var(--text-muted)", fontSize: "11px" } }, "Install 'winsdk' package for real song title/progress — currently media-key controls only."));
        const nowPanel = panel(nowItems, { pad: "30px 34px 32px 34px", style: { flex: "1 1 0" } });

        let volRow;
        if (state.caps.pycaw) {
            music.volVal = el("div", { class: "info-data" }, "50%");
            music.vol = new W.Slider(0, 100, 50, (v) => { music.volVal.textContent = `${v}%`; call("set_volume_percent", v); });
            volRow = el("div", { class: "hbox", style: { gap: "6px", alignItems: "center" } }, [el("span", { class: "lbl emoji" }, "🔊"), music.vol.root, music.volVal]);
        } else {
            volRow = el("div", { class: "hbox g10" }, [["volumedown", "volume-down"], ["volumemute", "volume-mute"], ["volumeup", "volume-up"]].map(([key, ic]) => {
                const b = el("button", { class: "btn round-sm", style: { width: "40px", height: "40px" } }, icon(ic, "var(--text-main)", 18));
                b.addEventListener("click", () => call("press_key", key)); return b;
            }).concat([stretch()]));
        }
        const volPanel = panel([heading("VOLUME"), el("div", { style: space(14) }, volRow), stretch()], { pad: "22px 26px", style: { flex: "1 1 0" } });
        music.brightVal = el("div", { class: "info-data" }, "50%");
        music.bright = new W.Slider(0, 100, 50, (v) => { music.brightVal.textContent = `${v}%`; call("set_brightness_percent", v); });
        const brightPanel = panel([heading("DISPLAY BRIGHTNESS"), el("div", { class: "hbox", style: Object.assign(space(14), { gap: "6px", alignItems: "center" }) },
            [el("span", { class: "lbl" }, "☀︎"), music.bright.root, music.brightVal]), stretch()], { pad: "22px 26px", style: { flex: "1 1 0" } });
        music.time = el("div", { class: "clock-big" }, "00:00:00");
        music.date = el("div", { class: "lbl" }, "YYYY-MM-DD");
        const clockPanel = panel([el("div", { class: "vbox", style: { gap: "2px" } }, [heading("LOCAL TIME"), music.time, music.date]), stretch()],
            { row: true, pad: "18px 28px", style: { flex: "none" } });
        const page = el("div", { class: "std" }, [
            makeBackRow("Music", "Control whatever's playing on your PC, plus display brightness"),
            nowPanel, el("div", { class: "cols-2", style: { flex: "1 1 0" } }, [volPanel, brightPanel]), clockPanel, stretch(),
        ]);
        root.appendChild(el("div", { class: "scroll-y" }, page));
        return { root, onShow: () => loadVolumeBrightness() };
    };
    function loadVolumeBrightness() {
        if (music.vol) call("get_volume_percent").then((r) => { if (r && r.ok && r.percent !== null && r.percent !== undefined) { music.vol.set(r.percent); music.volVal.textContent = `${r.percent}%`; } });
        call("get_brightness_percent").then((r) => { if (r && r.ok && r.percent !== null && r.percent !== undefined) { music.bright.set(r.percent); music.brightVal.textContent = `${r.percent}%`; } });
    }
    function stylePlayButton() {
        music.play.innerHTML = "";
        music.play.appendChild(icon(state.mediaPlaying ? "pause" : "play", "#000000", 28));
    }
    function togglePlayPause() {
        call("press_key", "playpause");
        if (!state.caps.winsdk) {
            state.mediaPlaying = !state.mediaPlaying;
            stylePlayButton();
            music.disc.setSpinning(state.mediaPlaying);
            music.viz.setMode(state.mediaPlaying ? "PLAYING" : "SLEEPING");
            music.sub.textContent = state.mediaPlaying ? "Playing — controlling your active media player" : "Paused — press play to resume";
        }
    }
    function skipSeconds(delta) {
        if (!state.caps.winsdk) return;
        const target = Math.max(0, Math.min(state.mediaDuration, state.mediaPosition + delta));
        state.mediaPosition = target;
        music.progress.set(Math.round(target * 1000));
        music.elapsed.textContent = fmtTime(target);
        call("seek_media", target);
    }
    function updateNowPlaying(info) {
        if (!music.play) return;
        if (!info || !Object.keys(info).length) {
            if (state.caps.winsdk) {
                music.title.textContent = "Nothing playing";
                music.sub.textContent = "Play something on Spotify, YouTube, etc.";
                music.progress.setRange(0, 0); music.progress.set(0);
                music.elapsed.textContent = "0:00"; music.duration.textContent = "0:00";
            }
            state.mediaPlaying = false; stylePlayButton(); music.disc.setSpinning(false); music.viz.setMode("SLEEPING");
            return;
        }
        state.mediaPlaying = !!info.is_playing;
        state.mediaDuration = info.duration || 0;
        music.title.textContent = info.title || "Unknown Title";
        music.sub.textContent = info.artist || (state.mediaPlaying ? "Playing" : "Paused");
        if (!state.mediaSeeking) {
            state.mediaPosition = info.position || 0;
            const total = Math.max(1, Math.round((info.duration || 0) * 1000));
            music.progress.setRange(0, total);
            music.progress.set(Math.min(total, Math.round((info.position || 0) * 1000)));
            music.elapsed.textContent = fmtTime(info.position || 0);
            music.duration.textContent = fmtTime(info.duration || 0);
        }
        stylePlayButton(); music.disc.setSpinning(state.mediaPlaying); music.viz.setMode(state.mediaPlaying ? "PLAYING" : "SLEEPING");
    }
    function updateClock() {
        if (!music.time || document.hidden) return;
        const d = new Date(); let h = d.getHours(); const ap = h < 12 ? "AM" : "PM"; h = h % 12 || 12;
        const p2 = (n) => String(n).padStart(2, "0");
        music.time.textContent = `${p2(h)}:${p2(d.getMinutes())}:${p2(d.getSeconds())} ${ap}`;
        const months = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];
        music.date.textContent = `${p2(d.getDate())} ${months[d.getMonth()]} ${d.getFullYear()}`;
    }

    // ============================================================== FILES
    const files = {};
    App.build.files = function () {
        const root = makePage("page-bg");
        const t = T();
        files.path = el("div", { class: "info-data", style: { flex: "1 1 auto", minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" } }, "");
        const toolbar = el("div", { class: "hbox g10", style: { alignItems: "center", flex: "none" } }, [
            files.path,
            btn("Desktop", "action", () => filesNavigate(state.defaultPath), "house", "var(--text-muted)", 16),
            btn("New Folder", "action", () => createNew("folder"), "plus", "var(--text-muted)", 16),
            btn("New File", "action", () => createNew("file"), "file-text", "var(--text-muted)", 16),
        ]);
        files.body = el("tbody");
        const table = el("table", { class: "tree" }, [
            el("thead", {}, el("tr", {}, [el("th", { style: { width: "320px", paddingLeft: "25px" } }, "Name"), el("th", { style: { width: "100px" } }, "Size"), el("th", { style: { width: "100px" } }, "Type"), el("th", {}, "Date Modified")])),
            files.body,
        ]);
        table.style.tableLayout = "fixed";
        const pnl = panel(el("div", { class: "tree-wrap" }, table), { pad: "6px", style: { flex: "1 1 0" } });
        root.appendChild(el("div", { class: "std files" }, [makeBackRow("Files", "Browse your PC, and create new folders or files"), toolbar, pnl]));
        return { root };
    };
    function fileRow(entry, depth) {
        const tr = el("tr", { "data-path": entry.path });
        const twisty = el("span", { class: "twisty" }, entry.is_dir ? "▸" : "");
        const ic = el("span", { class: "ficon emoji" }, entry.kind === "drive" ? "💽" : entry.is_dir ? "📁" : "📄");
        const nameTd = el("td", { style: { paddingLeft: (5 + depth * 20) + "px" } }, [twisty, ic, el("span", {}, entry.name)]);
        tr.append(nameTd, el("td", {}, entry.size_text || ""), el("td", {}, entry.type || ""), el("td", {}, entry.modified || ""));
        tr._entry = entry; tr._depth = depth; tr._expanded = false;
        tr.addEventListener("click", () => { files.body.querySelectorAll("tr.selected").forEach((x) => x.classList.remove("selected")); tr.classList.add("selected"); });
        tr.addEventListener("dblclick", () => {
            if (entry.is_dir) filesNavigate(entry.path);
            else call("open_path", entry.path).then((r) => { if (r && !r.ok) toast(`Error opening file: ${r.error}`, "error"); });
        });
        twisty.addEventListener("click", (e) => { e.stopPropagation(); if (entry.is_dir) toggleExpand(tr); });
        tr.addEventListener("contextmenu", (e) => { e.preventDefault(); fileContextMenu(e, entry); });
        return tr;
    }
    async function toggleExpand(tr) {
        const entry = tr._entry;
        if (tr._expanded) {
            let n = tr.nextSibling;
            while (n && n._depth > tr._depth) { const nx = n.nextSibling; n.remove(); n = nx; }
            tr._expanded = false; tr.querySelector(".twisty").textContent = "▸"; return;
        }
        const r = await call("list_dir", entry.path);
        if (!r || !r.ok) return;
        tr._expanded = true; tr.querySelector(".twisty").textContent = "▾";
        let after = tr;
        (r.entries || []).forEach((e2) => { const row = fileRow(e2, tr._depth + 1); after.after(row); after = row; });
    }
    async function filesNavigate(path) {
        const r = await call("list_dir", path);
        if (!r || !r.ok) { if (r && r.error) toast(r.error, "error"); return; }
        state.filesPath = r.path;
        files.path.textContent = r.path;
        files.body.innerHTML = "";
        (r.entries || []).forEach((e) => files.body.appendChild(fileRow(e, 0)));
    }
    App.filesNavigate = filesNavigate;
    function createNew(kind) {
        const path = state.filesPath;
        runAction(kind === "folder" ? "create_folder" : "create_file", kind === "folder" ? ["New Folder", path] : ["New File.txt", "", path]);
        setTimeout(() => filesNavigate(path), 300);
    }
    function fileContextMenu(e, entry) {
        const items = entry.is_dir ? [
            ["Open in Terminal", () => runAction("open_terminal_at", [entry.path])],
            ["Open in VS Code", () => runAction("open_in_vscode", [entry.path])],
        ] : [
            ["Open", () => call("open_path", entry.path)],
            ["Open With...", () => call("open_with", entry.path)],
            ["Copy Path", () => call("copy_to_clipboard", entry.path)],
            ["Delete", () => deleteFile(entry)],
        ];
        items.push("-", ["Refresh", () => filesNavigate(state.filesPath)]);
        W.contextMenu(e.clientX, e.clientY, items);
    }
    async function deleteFile(entry) {
        const yes = await W.ask("Delete", `Delete ${entry.name}?`);
        if (!yes) return;
        const r = await call("delete_path", entry.path);
        if (!r || !r.ok) { toast(`Error: ${(r && r.error) || "failed"}`, "error"); return; }
        toast(r.message, r.level || "success");
    }

    // ============================================================= VISION
    const vision = {};
    App.build.vision = function () {
        const root = makePage("transparent-panels");
        const t = T();
        vision.result = el("textarea", { class: "sticky", readonly: "readonly", placeholder: "Extracted text will appear here...", style: { minHeight: "220px", flex: "1 1 0" } });
        const pnl = panel([
            heading("SCREEN TEXT READER (OCR)"),
            el("div", { class: "hbox g10", style: space(12) }, [
                btn("Capture & Read Text", "action grow", runOcrCapture, "image", "var(--text-muted)", 16),
                btn("Take Photo", "action grow", takeWebcamPhoto, "camera", "var(--text-muted)", 16),
            ]),
            el("div", { class: "vbox", style: Object.assign(space(16), { flex: "1 1 0" }) }, vision.result),
            el("div", { class: "note-faint wrap", style: space(10) }, "Needs Tesseract-OCR installed on your PC for text reading to work."),
        ], { pad: "26px 30px", style: { flex: "1 1 0" } });
        const page = el("div", { class: "std" }, [makeBackRow("Vision", "Capture your screen and let W.A.V.E. read what's on it"), pnl, stretch()]);
        root.appendChild(el("div", { class: "scroll-y" }, page));
        return { root };
    };
    async function runOcrCapture() {
        vision.result.value = "Capturing screen and reading text...";
        const r = await call("ocr_capture");
        vision.result.value = r && r.ok ? (r.text || "") : `Error: ${(r && r.error) || ""}`;
        if (r && r.ok) toast("OCR complete", "success");
    }
    App.runOcrCapture = runOcrCapture;
    async function takeWebcamPhoto() {
        vision.result.value = "Opening webcam...";
        const r = await call("vision_take_photo");
        vision.result.value = r && r.ok ? (r.message || "") : `Error: ${(r && r.error) || ""}`;
    }

    // ========================================================== CLIPBOARD
    const clip = {};
    App.build.clipboard = function () {
        const root = makePage("transparent-panels");
        const items = [makeBackRow("Clipboard History", "Everything you copy shows up here — click Copy to grab it again")];
        if (!state.caps.pyperclip) {
            items.push(panel(lbl("Clipboard monitoring needs the 'pyperclip' package.\nInstall it with: pip install pyperclip", "wrap"), { pad: "22px 26px", style: { flex: "none" } }));
        }
        clip.list = el("div", { class: "vbox g8" }, clipEmpty());
        items.push(panel([heading("RECENT ITEMS"), el("div", { style: space(12) }, clip.list), stretch()], { pad: "24px 26px", style: { flex: "1 1 0" } }));
        root.appendChild(el("div", { class: "scroll-y" }, el("div", { class: "std" }, items)));
        return { root };
    };
    function clipEmpty() { return el("div", { class: "empty-note wrap clip-empty" }, "Nothing copied yet this session. Copy some text anywhere and it'll show up here."); }
    function onClipChanged(text) {
        const empty = clip.list.querySelector(".clip-empty"); if (empty) empty.remove();
        const preview = text.length <= 90 ? text : text.slice(0, 87) + "...";
        const copyBtn = btn("Copy", "action", () => call("copy_to_clipboard", text), "copy", "var(--accent-1)", 14);
        copyBtn.style.width = "70px"; copyBtn.style.flex = "none"; copyBtn.style.padding = "10px 6px";
        const entry = el("div", { class: "entry" }, [el("div", { class: "e-title wrap", style: { flex: "1 1 auto", fontWeight: 400 } }, preview), copyBtn]);
        clip.list.prepend(entry);
        while (clip.list.children.length > 20) clip.list.lastChild.remove();
    }

    // =========================================================== ACTIVITY
    const act = {};
    App.build.activity = function () {
        const root = makePage("transparent-panels");
        act.undoLast = btn("Undo last", "action", undoLast);
        act.emptyTrash = btn("Empty trash", "action", emptyTrash);
        act.note = el("div", { class: "note-faint wrap" }, "");
        act.list = el("div", { class: "vbox g8" });
        const pnl = panel([
            el("div", { class: "hbox", style: { gap: "6px", alignItems: "center" } }, [heading("RECENT ACTIONS"), stretch(), act.undoLast, act.emptyTrash]),
            act.note, el("div", { style: space(8) }, act.list), stretch(),
        ], { pad: "24px 26px", style: { flex: "1 1 0" } });
        root.appendChild(el("div", { class: "scroll-y" }, el("div", { class: "std" }, [makeBackRow("Activity", "Everything Wave did, newest first — file changes can be undone with one click"), pnl])));
        return { root };
    };
    async function refreshActivity() {
        const r = await call("get_activity", 60);
        if (!r || !r.ok) return;
        const t = T();
        act.list.innerHTML = "";
        const rows = r.actions || [];
        if (!rows.length) act.list.appendChild(el("div", { class: "empty-note wrap" }, "Nothing yet. Ask Wave to create a file or open an app and it will show up here."));
        rows.forEach((rec) => {
            const [mark, color] = { ok: ["✓", t.success], failed: ["✗", t.danger], blocked: ["⛔", t.warning] }[rec.status] || ["•", t.text_faint];
            const detail = (rec.result || "").length <= 110 ? (rec.result || "") : rec.result.slice(0, 107) + "...";
            const right = rec.reversible ? (() => { const b = btn("Undo", "action", () => undoAction(rec.id), null, null, null, rec.undo_label || "Undo this action"); return b; })()
                : rec.undone_at ? el("div", { class: "note-faint" }, "Undone") : null;
            act.list.appendChild(el("div", { class: "entry" }, [
                el("div", { class: "mark" + (mark === "⛔" ? " emoji" : ""), style: { color } }, mark),
                el("div", { class: "vbox", style: { gap: "2px", flex: "1 1 auto", minWidth: 0 } }, [
                    el("div", { class: "e-title wrap" }, rec.summary),
                    el("div", { class: "e-sub wrap" }, `${rec.time}  ·  ${rec.source}` + (detail ? `  ·  ${detail}` : "")),
                ]),
                right,
            ]));
        });
        act.undoLast.disabled = !rows.some((x) => x.reversible);
        act.note.textContent = r.trash_note || "";
    }
    App.refreshActivity = refreshActivity;
    function undoAction(id) {
        call("undo_action", id).then((r) => {
            if (!r || !r.ok) { toast(`Error: ${(r && r.error) || "failed"}`, "error"); return; }
            toast(r.message, r.success ? "success" : "warning", 5000);
            refreshActivity();
        });
    }
    function undoLast() {
        call("undo_last").then((r) => {
            if (r && r.ok && !r.success && /nothing to undo/i.test(r.message || "")) { toast("There's nothing to undo right now.", "info"); return; }
            if (!r || !r.ok) { toast(`Error: ${(r && r.error) || "failed"}`, "error"); return; }
            toast(r.message, r.success ? "success" : "warning", 5000);
            refreshActivity();
        });
    }
    async function emptyTrash() {
        const yes = await W.ask("Empty trash", "Permanently delete everything in Wave's trash? Earlier deletions can't be undone afterwards.");
        if (!yes) return;
        const r = await call("empty_trash");
        if (r && r.ok) { toast(`Trash emptied (${r.count} item${r.count !== 1 ? "s" : ""}).`, "success"); refreshActivity(); }
    }

    // ===================================================== COMMAND PALETTE
    function showCommandPalette() {
        if (document.querySelector(".palette")) return;
        const actions = {
            "Dodo": () => slideTo(IDX.DODO),
            "New Chat": () => slideTo(IDX.CHAT), "System Monitor": () => slideTo(IDX.SYSTEM), "Open App...": () => slideTo(IDX.APPS),
            "Music Controls": () => slideTo(IDX.MUSIC), "File Browser": () => slideTo(IDX.FILES), "Screen OCR": () => runOcrCapture(),
            "Clipboard History": () => slideTo(IDX.CLIPBOARD), "Activity & Undo": () => slideTo(IDX.ACTIVITY), "Workflows": () => slideTo(IDX.WORKFLOWS),
            "Memory": () => slideTo(IDX.MEMORY), "Reminders": () => slideTo(IDX.REMINDERS), "Notes & To-dos": () => slideTo(IDX.NOTES),
            "Calendar": () => slideTo(IDX.CALENDAR), "Skills": () => slideTo(IDX.SKILLS), "Performance": () => slideTo(IDX.PERFORMANCE),
            "Settings": () => slideTo(IDX.SETTINGS), "Toggle Mic": toggleMic, "Clear Chat": clearChat,
            "Take Screenshot": () => runAction("take_screenshot"), "Lock PC": () => runAction("lock_pc", [], false),
            "Focus Mode": toggleFocus, "Save Settings": () => App.saveSettings && App.saveSettings(), "Load Settings": () => App.loadSettings && App.loadSettings(false),
        };
        QUICK_APPS.forEach(([name, target]) => { actions[`Open ${name}`] = () => runAction("open_app", [target]); });
        const input = el("input", { class: "p-input", placeholder: "Type a command, app, or action... (Ctrl+K to close)" });
        const list = el("div", { class: "p-list" });
        let sel = 0, shown = [];
        const overlay = el("div", { class: "overlay top" });
        const close = () => { overlay.remove(); document.removeEventListener("keydown", onKey, true); };
        const activate = (name) => { close(); if (actions[name]) actions[name](); };
        const render = () => {
            const q = input.value.toLowerCase().trim();
            shown = Object.keys(actions).filter((n) => !q || n.toLowerCase().includes(q));
            sel = Math.min(sel, Math.max(0, shown.length - 1));
            list.innerHTML = "";
            shown.forEach((n, i) => { const it = el("div", { class: "p-item" + (i === sel ? " sel" : "") }, n); it.addEventListener("click", () => activate(n)); list.appendChild(it); });
            const cur = list.children[sel]; if (cur) cur.scrollIntoView({ block: "nearest" });
        };
        const onKey = (e) => {
            if (e.key === "Escape") { e.preventDefault(); close(); }
            else if (e.key === "ArrowDown") { e.preventDefault(); sel = Math.min(sel + 1, shown.length - 1); render(); }
            else if (e.key === "ArrowUp") { e.preventDefault(); sel = Math.max(sel - 1, 0); render(); }
            else if (e.key === "Enter") { e.preventDefault(); if (shown[sel]) activate(shown[sel]); }
            else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); close(); }
        };
        input.addEventListener("input", () => { sel = 0; render(); });
        overlay.addEventListener("pointerdown", (e) => { if (e.target === overlay) close(); });
        overlay.appendChild(el("div", { class: "panel palette", style: { filter: "drop-shadow(0 20px 20px rgba(0,0,0,0.706))" } }, [input, list]));
        document.body.appendChild(overlay);
        document.addEventListener("keydown", onKey, true);
        render(); input.focus();
    }
    App.showCommandPalette = showCommandPalette;
    document.addEventListener("keydown", (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k" && !document.querySelector(".palette")) { e.preventDefault(); showCommandPalette(); }
    });

    // ================================================ backend push events
    function wireEvents() {
        wave.on("chat_chunk", ({ piece, turn }) => { if (forCurrentTurn(turn)) onReplyChunk(piece); });
        wave.on("chat_done", ({ text, turn }) => { if (forCurrentTurn(turn)) onReplyDone(text); });
        wave.on("reply_speech_finished", ({ success }) => {
            if (success === false) toast("Couldn't play voice reply - check your audio output device", "warning");
            showIdleVoiceState();
        });
        wave.on("tts_started", () => { chat.visualizer.setMode("SPEAKING"); setStatus("Speaking...", T().accent_3); });
        wave.on("tts_finished", ({ success }) => {
            if (success === false) toast("Couldn't play voice reply - check your audio output device", "warning");
            showIdleVoiceState();
        });
        wave.on("voice_mode", ({ mode }) => handleWakeWordMode(mode));
        wave.on("voice_level", ({ level }) => chat.visualizer.setLevel(level));
        wave.on("voice_partial", ({ text }) => onPartialText(text));
        wave.on("voice_utterance_done", () => clearCaption());
        wave.on("voice_command", ({ text }) => { chat.input.value = text; sendMessage(true); });
        wave.on("voice_interrupt", () => { cancelReply(); stopSpeaking(); });
        wave.on("voice_stop", ({ cancelled_confirmation, had_activity }) => {
            cancelReply(); showIdleVoiceState();
            if (cancelled_confirmation) toast("Okay, cancelled - I won't do that.", "info");
            else if (had_activity) toast("⏹ Stopped", "info");
        });
        wave.on("voice_recognition_failed", () => toast("Didn't catch that - try again?", "warning"));
        wave.on("reminder_due", ({ message }) => { toast(`⏰ ${message}`, "warning", 6000); addChatBubble(`⏰ ${message}`, false); });
        wave.on("reminder_upcoming", ({ message }) => { toast(`⏳ ${message}`, "info", 5000); addChatBubble(`⏳ ${message}`, false); });
        wave.on("proactive_nudge", ({ kind, title, message }) => {
            const ic = PROACTIVE_ICONS[kind] || "📅";
            toast(`${ic} ${title}: ${message}`, "info", 6000);
            addChatBubble(`${ic} ${message}`, false);
            startSpeaking(message);
        });
        wave.on("scheduled_workflow_result", ({ text }) => addChatBubble(String(text), false));
        wave.on("tool_activity", ({ description }) => onToolActivity(description));
        wave.on("audit_changed", () => { if (stack.index === IDX.ACTIVITY) refreshActivity(); });
        wave.on("clip_changed", ({ text }) => onClipChanged(text));
        wave.on("thread_health", ({ level, text, tip }) => {
            const t = T();
            App.refs.health.textContent = text;
            App.refs.health.style.color = level === "down" ? t.danger : level === "recovered" ? t.warning : "var(--text-muted)";
            App.refs.health.setAttribute("data-tip", tip || "");
        });
        wave.on("ollama_event", ({ event, message }) => {
            if (event === "problem") toast(`⚠️ Ollama: ${message}`, "warning", 6000);
            else if (event === "server_started" || event === "model_ready") toast(`🦙 ${message}`, "success");
        });
        wave.on("health_check", ({ results }) => (results || []).forEach((r) => {
            if (r.status === "problem") toast(`⚠️ ${r.name}: ${r.message}`, "error", 8000);
            else if (r.status === "warn") toast(`${r.name}: ${r.message}`, "warning", 6000);
        }));
        wave.on("skill_event", ({ kind, message }) => {
            const ic = { loaded: "success", reloaded: "success", unloaded: "info", error: "warning" }[kind] || "info";
            toast(message, ic, kind === "error" ? 6000 : 3500);
        });
        wave.on("weather", ({ text }) => { if (App.refs.weather) App.refs.weather.textContent = text; });
        wave.on("hotkey", ({ name }) => {
            if (name === "mic_toggle") toggleMic();
            else if (name === "palette") showCommandPalette();
            else if (name === "emergency_stop") emergencyStop();
        });
        wave.on("remote_dashboard", (p) => App.onRemoteResult && App.onRemoteResult(p));
    }

    // =================================================================== BOOT
    async function boot() {
        await wave.ready();
        const b = await call("get_bootstrap");
        state.caps = (b && b.capabilities) || {};
        state.settings = (b && b.settings) || {};
        state.defaultPath = (b && b.default_path) || "";
        state.localIp = (b && b.local_ip) || "Unavailable";
        state.bootstrap = b || {};
        WaveTheme.addCustom((b && b.custom_themes) || []);
        WaveTheme.apply((b && b.last_theme) || "Black Glass");
        W.toasts.enabled = state.settings.notifications_enabled !== false;
        buildShell();
        const order = ["home", "dodo", "chat", "system", "apps", "music", "files", "vision", "clipboard", "activity",
            "workflows", "memory", "reminders", "notes", "calendar", "skills", "performance", "settings"];
        order.forEach((key) => {
            const p = App.build[key]();
            App.pages.push(p);
            App.stackEl.appendChild(p.root);
        });
        App.pages.forEach((p) => qtColumns(p.root));
        App.pages[0].root.classList.add("current");
        setNavChecked(0);
        initDrag();
        wireEvents();
        sys.ip.textContent = `Local IP: ${state.localIp}`;
        addChatBubble(`${greeting()}! I'm W.A.V.E., your AI assistant. Systems are online and ready to help.`, false);
        if (window.WaveAvatar) {
            WaveAvatar.onState(showDodoState);
            WaveAvatar.build(dodo.mount);
        }
        filesNavigate(state.defaultPath);
        refreshActivity();
        App.loadSettings && App.loadSettings(true);
        setInterval(updateSysStats, 1000); updateSysStats();
        setInterval(updateClock, 1000); updateClock();
        setInterval(() => { if (!document.hidden) call("get_now_playing").then((r) => { if (r && r.ok) updateNowPlaying(r.info); }); }, 1000);
        sizeTiles();
        call("start_background_services");
        if (b && b.first_run) setTimeout(() => App.showOnboarding && App.showOnboarding(), 500);
        else { setTimeout(() => call("turn_on_mic"), 1000); setTimeout(refreshDashboardPreviews, 700); }
        window.addEventListener("resize", () => { resizeVisibleCanvases(); sizeTiles(); });
    }
    document.addEventListener("DOMContentLoaded", boot);
})();
