/* widgets.js - ports of the PyQt6 UI's custom widgets.
 *
 *   CircularGauge / Sparkline / AudioVisualizer / AlbumArtDisc / BrandIcon
 *     -> ui/widgets_basic.py (same paint maths, drawn on <canvas>)
 *   Slider / Combo / Check
 *     -> QSlider / QComboBox / QCheckBox as styled by ui/theme.py's QSS
 *   toast / msgBox / inputBox / contextMenu / tooltip
 *     -> ui/toast.py ToastManager, QMessageBox, QInputDialog, QMenu, QToolTip
 *
 * Everything reads colours from window.WaveTheme.current() at paint time, so
 * a theme switch repaints correctly.
 */
(function () {
    const T = () => window.WaveTheme.current();
    const dpr = () => window.devicePixelRatio || 1;

    function el(tag, props, children) {
        const node = document.createElement(tag);
        Object.entries(props || {}).forEach(([k, v]) => {
            if (v === undefined || v === null || v === false) return;
            if (k === "class") node.className = v;
            else if (k === "html") node.innerHTML = v;
            else if (k === "text") node.textContent = v;
            else if (k === "style" && typeof v === "object") Object.assign(node.style, v);
            else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2).toLowerCase(), v);
            else node.setAttribute(k, v === true ? "" : v);
        });
        (Array.isArray(children) ? children : [children]).forEach((c) => {
            if (c === undefined || c === null || c === false) return;
            node.appendChild(typeof c === "string" || typeof c === "number" ? document.createTextNode(String(c)) : c);
        });
        return node;
    }

    // ---- icons ---------------------------------------------------------
    // ui/icons.py Icon.get(): qtawesome "fa6s.<name>" when that glyph exists,
    // otherwise an emoji rendered into a size x size pixmap - which, as a
    // parentless top-level QLabel, paints Fusion's dark window colour
    // (#1E1E1E) behind the emoji. Both looks are reproduced here.
    const FA = {
        house: "", message: "", "chart-line": "", bolt: "", music: "",
        folder: "", eye: "", clipboard: "", "clock-rotate-left": "",
        "diagram-project": "", brain: "", bell: "", "list-check": "",
        calendar: "", "wand-magic-sparkles": "", gauge: "", gear: "",
        "chevron-left": "", trash: "", paperclip: "", stop: "", xmark: "",
        terminal: "", code: "", lock: "", camera: "", play: "",
        pause: "", plus: "+", image: "", copy: "",
        user: "", // the Dodo page (web only)
    };
    const EMOJI = {
        mic: "🎙", send: "➤", cpu: "🧠", chrome: "⬜", "file-text": "📄", activity: "📈",
        "volume-mute": "🔇", "volume-down": "🔉", "volume-up": "🔊", "skip-back": "⏮", "skip-forward": "⏭",
    };
    function icon(name, color, size) {
        size = size || 18;
        if (FA[name]) {
            // qtawesome draws at round(0.875 * size * scale_factor(1.2)) px
            const px = Math.round(0.875 * size * 1.2);
            return el("span", { class: "fa", style: { fontSize: px + "px", width: size + "px", height: size + "px", lineHeight: size + "px", color: color || "inherit" } }, FA[name]);
        }
        const ch = EMOJI[name] || "⬜";
        return el("span", { class: "emoji qemoji", style: {
            fontSize: size + "px", width: size + "px", height: size + "px", lineHeight: size + "px",
            background: "#1E1E1E", color: color || "inherit", overflow: "hidden", flex: "none",
        } }, ch);
    }

    // ---- canvas helper -------------------------------------------------
    function sizeCanvas(canvas, w, h) {
        const r = dpr();
        const cw = Math.max(1, Math.round(w * r)), ch = Math.max(1, Math.round(h * r));
        if (canvas.width !== cw || canvas.height !== ch) { canvas.width = cw; canvas.height = ch; }
        const ctx = canvas.getContext("2d");
        ctx.setTransform(r, 0, 0, r, 0, 0);
        return ctx;
    }

    // ---- CircularGauge -------------------------------------------------
    class CircularGauge {
        constructor(color, thickness) {
            this.canvas = el("canvas", { class: "gauge" });
            this.value = 0; this.color = color; this.thickness = thickness || 8; this.suffix = "%";
            this.paint();
        }
        setValue(v) {
            v = Number(v); if (!isFinite(v)) v = 0;
            v = Math.max(0, Math.min(100, v));
            if (v !== this.value) { this.value = v; this.paint(); }
        }
        setColor(c) { this.color = c; this.paint(); }
        paint() {
            const w = this.canvas.clientWidth || 110, h = this.canvas.clientHeight || 110;
            const ctx = sizeCanvas(this.canvas, w, h);
            ctx.clearRect(0, 0, w, h);
            const side = Math.min(w, h);
            const margin = this.thickness / 2 + 3;
            const r = (side - 2 * margin) / 2;
            const cx = w / 2, cy = h / 2;
            ctx.lineWidth = this.thickness; ctx.lineCap = "round";
            ctx.strokeStyle = "#2A2A2A";
            ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2); ctx.stroke();
            if (this.value > 0) {
                ctx.strokeStyle = this.color;
                ctx.beginPath();
                const start = -Math.PI / 2;
                ctx.arc(cx, cy, r, start, start + (this.value / 100) * Math.PI * 2);
                ctx.stroke();
            }
            ctx.fillStyle = "#FFFFFF";
            const pt = Math.max(8, Math.floor(side / 7));
            ctx.font = `bold ${Math.round(pt * 96 / 72)}px "Segoe UI"`;
            ctx.textAlign = "center"; ctx.textBaseline = "middle";
            ctx.fillText(`${Math.round(this.value)}${this.suffix}`, cx, cy + 1);
        }
    }

    // ---- Sparkline -----------------------------------------------------
    class Sparkline {
        constructor(color, maxPoints) {
            this.canvas = el("canvas", { class: "spark" });
            this.max = maxPoints || 60; this.data = new Array(this.max).fill(0); this.color = color;
            requestAnimationFrame(() => this.paint());
        }
        addValue(v) {
            v = Number(v); if (!isFinite(v)) v = 0;
            this.data.push(Math.max(0, Math.min(100, v)));
            if (this.data.length > this.max) this.data.shift();
            this.paint();
        }
        paint() {
            const w = this.canvas.clientWidth, h = this.canvas.clientHeight || 36;
            if (!w) return;
            const ctx = sizeCanvas(this.canvas, w, h);
            ctx.clearRect(0, 0, w, h);
            if (this.data.length < 2) return;
            const m = 4, step = (w - 2 * m) / (this.data.length - 1);
            const pts = this.data.map((v, i) => [m + i * step, h - m - (v / 100) * (h - 2 * m)]);
            const P = window.WaveTheme.parseColor(this.color);
            const g = ctx.createLinearGradient(0, h, 0, 0);
            g.addColorStop(0, `rgba(${P.r},${P.g},${P.b},${60 / 255})`);
            g.addColorStop(1, `rgba(${P.r},${P.g},${P.b},0)`);
            ctx.fillStyle = g;
            ctx.beginPath();
            pts.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
            ctx.lineTo(pts[pts.length - 1][0], h - m); ctx.lineTo(pts[0][0], h - m); ctx.closePath(); ctx.fill();
            ctx.strokeStyle = this.color; ctx.lineWidth = 2; ctx.lineCap = "round"; ctx.lineJoin = "round";
            ctx.beginPath();
            pts.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
            ctx.stroke();
        }
    }

    // ---- AudioVisualizer (40ms tick, same modes/targets as Qt) ----------
    const visualizers = new Set();
    class AudioVisualizer {
        constructor(height) {
            this.canvas = el("canvas", { class: "visualizer", style: { height: (height || 40) + "px" } });
            this.mode = "SLEEPING"; this.barW = 3; this.spacing = 3;
            this.heights = []; this.targets = []; this.phase = 0;
            this.level = 0; this.levelAt = -1e9; this.history = [];
            visualizers.add(this);
        }
        setMode(m) { this.mode = m; }
        setLevel(l) { l = Number(l); if (!isFinite(l)) return; this.level = Math.max(0, Math.min(1, l)); this.levelAt = performance.now(); }
        liveLevel() { return performance.now() - this.levelAt > 600 ? null : this.level; }
        tick() {
            const w = this.canvas.clientWidth, h = this.canvas.clientHeight;
            if (!w || !h) return;
            const step = this.barW + this.spacing;
            const n = Math.max(1, Math.floor(w / step));
            if (this.heights.length !== n) { this.heights = new Array(n).fill(3); this.targets = new Array(n).fill(3); }
            this.phase += 0.18;
            const maxH = Math.max(6, h - 6);
            const live = this.mode === "LISTENING" ? this.liveLevel() : null;
            if (live !== null) {
                this.history.push(live); if (this.history.length > 64) this.history.shift();
                const centre = (n - 1) / 2;
                for (let i = 0; i < n; i++) {
                    const age = Math.floor(Math.abs(i - centre));
                    const lvl = age < this.history.length ? this.history[this.history.length - 1 - age] : 0;
                    this.targets[i] = 3 + lvl * (maxH - 3);
                }
            }
            for (let i = 0; i < n; i++) {
                let target;
                if (this.mode === "LISTENING" && live !== null) target = this.targets[i];
                else if (this.mode === "LISTENING") { if (Math.random() < 0.16) this.targets[i] = 5 + Math.random() * (maxH * 0.55 - 5); target = this.targets[i]; }
                else if (this.mode === "SPEAKING") { if (Math.random() < 0.26) this.targets[i] = 8 + Math.random() * (maxH - 8); target = this.targets[i]; }
                else if (this.mode === "PLAYING") { if (Math.random() < 0.22) this.targets[i] = 6 + Math.random() * (maxH * 0.85 - 6); target = this.targets[i]; }
                else target = 3 + 3 * (0.5 + 0.5 * Math.sin(this.phase + i * 0.4));
                this.heights[i] += (target - this.heights[i]) * 0.3;
            }
            this.paint(w, h);
        }
        paint(w, h) {
            const ctx = sizeCanvas(this.canvas, w, h);
            ctx.clearRect(0, 0, w, h);
            const n = this.heights.length; if (!n) return;
            const step = this.barW + this.spacing;
            const total = n * step - this.spacing;
            const sx = (w - total) / 2, mid = h / 2;
            const t = T();
            let c1, c2;
            if (this.mode === "LISTENING") { c1 = t.accent_2; c2 = t.accent_1; }
            else if (this.mode === "SPEAKING") { c1 = t.accent_1; c2 = t.accent_3; }
            else if (this.mode === "PLAYING") { c1 = t.success; c2 = t.accent_2; }
            else { c1 = "#1A1A1A"; c2 = "#2A2A2A"; }
            const A = window.WaveTheme.parseColor(c1), B = window.WaveTheme.parseColor(c2);
            for (let i = 0; i < n; i++) {
                const k = i / Math.max(1, n - 1);
                ctx.fillStyle = `rgb(${Math.floor(A.r + (B.r - A.r) * k)},${Math.floor(A.g + (B.g - A.g) * k)},${Math.floor(A.b + (B.b - A.b) * k)})`;
                const hh = this.heights[i], x = sx + i * step, y = mid - hh / 2;
                roundRect(ctx, x, y, this.barW, hh, 1.5); ctx.fill();
            }
        }
    }
    setInterval(() => { if (document.hidden) return; visualizers.forEach((v) => { if (v.canvas.isConnected && v.canvas.offsetParent !== null) v.tick(); }); }, 40);

    function roundRect(ctx, x, y, w, h, r) {
        r = Math.min(r, w / 2, h / 2);
        ctx.beginPath();
        ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r);
        ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath();
    }

    // ---- AlbumArtDisc (50ms tick, +3deg while spinning) -----------------
    class AlbumArtDisc {
        constructor(size) {
            this.size = size || 76;
            this.canvas = el("canvas", { class: "disc", style: { width: this.size + "px", height: this.size + "px" } });
            this.angle = 0; this.spinning = false;
            setInterval(() => { if (this.spinning && this.canvas.offsetParent !== null) { this.angle = (this.angle + 3) % 360; this.paint(); } }, 50);
            requestAnimationFrame(() => this.paint());
        }
        setSpinning(s) { this.spinning = !!s; }
        paint() {
            const s = this.size, ctx = sizeCanvas(this.canvas, s, s), t = T();
            ctx.clearRect(0, 0, s, s);
            const cx = s / 2, cy = s / 2;
            ctx.save(); ctx.translate(cx, cy); ctx.rotate(this.angle * Math.PI / 180); ctx.translate(-cx, -cy);
            const g = ctx.createLinearGradient(2, 2, s - 2, s - 2);
            g.addColorStop(0, t.accent_1); g.addColorStop(1, t.accent_2);
            ctx.fillStyle = g; ctx.beginPath(); ctx.arc(cx, cy, (s - 4) / 2, 0, Math.PI * 2); ctx.fill();
            ctx.strokeStyle = "rgba(255,255,255,0.118)"; ctx.lineWidth = 1;
            [0.34, 0.25, 0.17].forEach((f) => { ctx.beginPath(); ctx.arc(cx, cy, (s - 4) * f, 0, Math.PI * 2); ctx.stroke(); });
            const cr = (s - 4) * 0.14;
            ctx.fillStyle = t.bg_main; ctx.beginPath(); ctx.arc(cx, cy, cr, 0, Math.PI * 2); ctx.fill();
            ctx.fillStyle = t.text_main; ctx.beginPath(); ctx.arc(cx, cy, cr * 0.3, 0, Math.PI * 2); ctx.fill();
            ctx.restore();
        }
    }

    // ---- BrandIcon (speech bubble with three dots) ----------------------
    function brandIcon(size) {
        size = size || 38;
        const w = size, h = Math.floor(size * 0.86);
        const canvas = el("canvas", { class: "brand-icon", style: { width: w + "px", height: h + "px" } });
        const paint = () => {
            const ctx = sizeCanvas(canvas, w, h), t = T();
            ctx.clearRect(0, 0, w, h);
            const tail = h * 0.22, bh = h - tail;
            const g = ctx.createLinearGradient(0, 0, w, bh);
            g.addColorStop(0, t.accent_1); g.addColorStop(1, t.accent_2);
            ctx.fillStyle = g;
            roundRect(ctx, 0, 0, w, bh, w * 0.26); ctx.fill();
            ctx.beginPath(); ctx.moveTo(w * 0.20, bh - 3); ctx.lineTo(w * 0.40, bh - 3); ctx.lineTo(w * 0.20, h); ctx.closePath(); ctx.fill();
            ctx.fillStyle = "rgba(255,255,255,0.92)";
            const dr = w * 0.045, cy = bh / 2;
            [-0.16, 0, 0.16].forEach((dx) => { ctx.beginPath(); ctx.arc(w / 2 + w * dx, cy, dr, 0, Math.PI * 2); ctx.fill(); });
        };
        requestAnimationFrame(paint);
        window.WaveTheme.onChange(paint);
        return canvas;
    }

    // ---- Slider (QSlider horizontal) -------------------------------------
    class Slider {
        constructor(min, max, value, onChange, opts) {
            this.min = min; this.max = max; this.value = value; this.onChange = onChange; this.opts = opts || {};
            this.root = el("div", { class: "slider" }, [
                this.groove = el("div", { class: "groove" }),
                this.sub = el("div", { class: "sub" }),
                this.handle = el("div", { class: "handle" }),
            ]);
            this.root.tabIndex = 0;
            const setFromEvent = (e) => {
                const r = this.root.getBoundingClientRect();
                const k = Math.max(0, Math.min(1, (e.clientX - r.left - 9) / Math.max(1, r.width - 18)));
                this.set(Math.round(this.min + k * (this.max - this.min)), true);
            };
            this.root.addEventListener("pointerdown", (e) => {
                if (this.disabled) return;
                e.preventDefault(); this.root.setPointerCapture(e.pointerId); this.dragging = true;
                if (this.opts.onPress) this.opts.onPress();
                setFromEvent(e);
            });
            this.root.addEventListener("pointermove", (e) => { if (this.dragging) setFromEvent(e); });
            const end = () => { if (!this.dragging) return; this.dragging = false; if (this.opts.onRelease) this.opts.onRelease(this.value); };
            this.root.addEventListener("pointerup", end);
            this.root.addEventListener("pointercancel", end);
            this.root.addEventListener("keydown", (e) => {
                if (e.key === "ArrowLeft" || e.key === "ArrowDown") { this.set(this.value - 1, true); e.preventDefault(); }
                if (e.key === "ArrowRight" || e.key === "ArrowUp") { this.set(this.value + 1, true); e.preventDefault(); }
            });
            new ResizeObserver(() => this.layout()).observe(this.root);
            this.layout();
        }
        setRange(min, max) { this.min = min; this.max = max; this.set(this.value, false); }
        setEnabled(on) { this.disabled = !on; this.root.classList.toggle("disabled", !on); }
        set(v, fire) {
            v = Math.max(this.min, Math.min(this.max, Math.round(v)));
            const changed = v !== this.value;
            this.value = v; this.layout();
            if (changed && fire && this.onChange) this.onChange(v);
        }
        layout() {
            const w = this.root.clientWidth || 0;
            const span = this.max - this.min;
            const k = span > 0 ? (this.value - this.min) / span : 0;
            const x = 9 + k * Math.max(0, w - 18);
            this.handle.style.left = x + "px";
            this.sub.style.width = Math.max(0, x) + "px";
        }
    }

    // ---- Combo (QComboBox) -----------------------------------------------
    let openPopup = null;
    function closePopup() { if (openPopup) { openPopup.remove(); openPopup = null; } }
    document.addEventListener("pointerdown", (e) => { if (openPopup && !openPopup.contains(e.target)) closePopup(); }, true);
    window.addEventListener("blur", closePopup);
    class Combo {
        constructor(items, current, onChange) {
            this.items = items.slice(); this.value = current !== undefined ? current : items[0]; this.onChange = onChange;
            this.root = el("div", { class: "combo", tabindex: "0" }, this.value);
            this.root.addEventListener("click", () => this.open());
            this.root.addEventListener("keydown", (e) => {
                if (e.key === "Enter" || e.key === " " || e.key === "ArrowDown") { e.preventDefault(); this.open(); }
            });
        }
        setItems(items) { this.items = items.slice(); if (!this.items.includes(this.value)) this.value = this.items[0]; this.root.textContent = this.value || ""; }
        addItem(name) { if (!this.items.includes(name)) this.items.push(name); }
        setEnabled(on) { this.disabled = !on; this.root.classList.toggle("disabled", !on); }
        setValue(v, fire) {
            if (!this.items.includes(v)) return;
            const changed = v !== this.value;
            this.value = v; this.root.textContent = v;
            if (changed && fire && this.onChange) this.onChange(v);
        }
        open() {
            if (this.disabled) return;
            closePopup();
            const r = this.root.getBoundingClientRect();
            const pop = el("div", { class: "combo-popup" }, this.items.map((it) => el("div", {
                class: "opt" + (it === this.value ? " sel" : ""),
                onClick: () => { closePopup(); this.setValue(it, true); this.root.focus(); },
            }, it)));
            pop.style.left = r.left + "px"; pop.style.minWidth = r.width + "px";
            document.body.appendChild(pop);
            const ph = pop.offsetHeight;
            pop.style.top = (r.bottom + ph > window.innerHeight ? Math.max(4, r.top - ph) : r.bottom) + "px";
            openPopup = pop;
        }
    }

    // ---- Check (QCheckBox) -------------------------------------------------
    class Check {
        constructor(label, checked, onToggle, extraClass) {
            this.checked = !!checked; this.onToggle = onToggle;
            this.root = el("div", { class: "check" + (extraClass ? " " + extraClass : "") + (this.checked ? " on" : ""), tabindex: "0" },
                [el("span", { class: "box" }), el("span", { class: "c-label" }, label)]);
            this.root.addEventListener("click", () => this.set(!this.checked, true));
            this.root.addEventListener("keydown", (e) => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); this.set(!this.checked, true); } });
        }
        set(v, fire) {
            v = !!v; const changed = v !== this.checked;
            this.checked = v; this.root.classList.toggle("on", v);
            if (changed && fire && this.onToggle) this.onToggle(v);
        }
    }

    // ---- tooltip (QToolTip) --------------------------------------------------
    const tip = el("div", { id: "tooltip" });
    let tipTimer = null;
    document.addEventListener("DOMContentLoaded", () => document.body.appendChild(tip));
    document.addEventListener("mouseover", (e) => {
        const t = e.target.closest && e.target.closest("[data-tip]");
        clearTimeout(tipTimer); tip.style.display = "none";
        if (!t) return;
        tipTimer = setTimeout(() => {
            tip.textContent = t.getAttribute("data-tip");
            tip.style.display = "block";
            const x = Math.min(window.innerWidth - tip.offsetWidth - 4, lastMouse.x + 12);
            const y = Math.min(window.innerHeight - tip.offsetHeight - 4, lastMouse.y + 20);
            tip.style.left = x + "px"; tip.style.top = y + "px";
        }, 700);
    });
    const lastMouse = { x: 0, y: 0 };
    document.addEventListener("mousemove", (e) => { lastMouse.x = e.clientX; lastMouse.y = e.clientY; }, { passive: true });
    document.addEventListener("mousedown", () => { clearTimeout(tipTimer); tip.style.display = "none"; }, true);

    // ---- context menu (QMenu) --------------------------------------------------
    let openMenu = null;
    function closeMenu() { if (openMenu) { openMenu.remove(); openMenu = null; } }
    document.addEventListener("pointerdown", (e) => { if (openMenu && !openMenu.contains(e.target)) closeMenu(); }, true);
    function contextMenu(x, y, items) {
        closeMenu();
        const m = el("div", { class: "qmenu" }, items.map((it) => it === "-" ? el("div", { class: "sep" })
            : el("div", { class: "mi", onClick: () => { closeMenu(); it[1](); } }, it[0])));
        document.body.appendChild(m);
        m.style.left = Math.min(x, window.innerWidth - m.offsetWidth - 4) + "px";
        m.style.top = Math.min(y, window.innerHeight - m.offsetHeight - 4) + "px";
        openMenu = m;
    }

    // ---- QMessageBox / QInputDialog --------------------------------------------
    function modal(build) {
        return new Promise((resolve) => {
            const overlay = el("div", { class: "overlay" });
            const done = (v) => { overlay.remove(); document.removeEventListener("keydown", onKey, true); resolve(v); };
            const box = build(done);
            overlay.appendChild(box);
            const onKey = (e) => { if (e.key === "Escape") { e.preventDefault(); done(null); } };
            document.addEventListener("keydown", onKey, true);
            document.body.appendChild(overlay);
            const f = box.querySelector("input, .mb-btn.default, .mb-btn");
            if (f) setTimeout(() => f.focus(), 0);
        });
    }
    const MB_ICONS = { warning: "⚠️", question: "❓", info: "ℹ️", critical: "⛔" };
    // buttons: [{text, value, default}] -> resolves to the clicked value (null on Esc)
    function msgBox(title, text, kind, buttons) {
        buttons = buttons || [{ text: "OK", value: true, default: true }];
        return modal((done) => el("div", { class: "msgbox" }, [
            el("div", { class: "mb-title" }, title || "Wave"),
            el("div", { class: "mb-body" }, [
                kind ? el("div", { class: "mb-icon emoji" }, MB_ICONS[kind] || "") : null,
                el("div", { class: "mb-text" }, text),
            ]),
            el("div", { class: "mb-buttons" }, buttons.map((b) => el("button", {
                class: "mb-btn" + (b.default ? " default" : ""), onClick: () => done(b.value),
            }, b.text))),
        ]));
    }
    const ask = (title, text) => msgBox(title, text, "question",
        [{ text: "Yes", value: true, default: true }, { text: "No", value: false }]).then((v) => v === true);
    const warn = (title, text) => msgBox(title, text, "warning");
    const info = (title, text) => msgBox(title, text, "info");
    function inputBox(title, label, value) {
        return modal((done) => {
            const input = el("input", { class: "line-edit small", type: "text" });
            input.value = value || "";
            input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); done(input.value); } });
            return el("div", { class: "msgbox" }, [
                el("div", { class: "mb-title" }, title),
                el("div", { class: "mb-body", style: { paddingBottom: "4px" } }, el("div", { class: "mb-label", style: { padding: 0 } }, label)),
                el("div", { class: "mb-input" }, input),
                el("div", { class: "mb-buttons" }, [
                    el("button", { class: "mb-btn default", onClick: () => done(input.value) }, "OK"),
                    el("button", { class: "mb-btn", onClick: () => done(null) }, "Cancel"),
                ]),
            ]);
        });
    }

    // ---- ToastManager ------------------------------------------------------------
    const toasts = { enabled: true };
    function toast(message, kind, duration) {
        if (!toasts.enabled) return null;
        kind = kind || "info"; duration = duration || 3000;
        let layer = document.getElementById("toasts");
        if (!layer) { layer = el("div", { id: "toasts" }); document.body.appendChild(layer); }
        const icons = { info: "ℹ️", success: "✅", warning: "⚠️", error: "❌" };
        const t = T();
        const colors = { info: t.accent_1, success: t.success, warning: t.warning, error: t.danger };
        const node = el("div", { class: "toast" }, [
            el("span", { class: "t-icon emoji", style: { color: colors[kind] || t.accent_1 } }, icons[kind] || icons.info),
            el("span", { class: "t-msg" }, message),
            el("button", { class: "t-close", onClick: () => remove() }, "✕"),
        ]);
        const remove = () => { if (!node.isConnected) return; node.classList.add("leaving"); setTimeout(() => node.remove(), 150); };
        layer.appendChild(node);
        setTimeout(remove, duration);
        return node;
    }

    window.W = {
        el, icon, FA, EMOJI, sizeCanvas, roundRect,
        CircularGauge, Sparkline, AudioVisualizer, AlbumArtDisc, brandIcon,
        Slider, Combo, Check, contextMenu, closeMenu, closePopup,
        msgBox, ask, warn, info, inputBox, modal, toast, toasts,
    };
})();
