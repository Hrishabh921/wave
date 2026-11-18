/* theme.js - the web UI's copy of ui/theme.py's ThemeColors + ThemeManager.
 *
 * The PyQt6 desktop UI (wave_start.pyw) is the visual reference for this
 * web UI: every value below is copied 1:1 from ui/theme.py's
 * _load_builtin_themes(), so both UIs render the same palette and share the
 * same "last_theme" key in wave_settings.json. Custom themes saved from
 * either UI land in <DATA_DIR>/themes/*.json (the bridge lists them).
 *
 * qt8(): Qt's stylesheet parser reads an 8-digit hex colour as #AARRGGBB,
 * not #RRGGBBAA. ui/theme.py builds several colours as "{accent}40"-style
 * strings, so on the desktop they come out as a *different colour* than the
 * code suggests (e.g. the olive-green active nav item). The web UI applies
 * the same rule so it looks exactly like what the desktop actually draws.
 */
(function () {
    const DEFAULTS = {
        name: "Black Glass",
        bg_main: "#000000",
        bg_panel: "rgba(20,20,20,0.95)",
        bg_surface: "rgba(30,30,30,0.98)",
        bg_surface2: "#1A1A1A",
        border: "rgba(255,255,255,0.15)",
        border_soft: "rgba(255,255,255,0.08)",
        text_main: "#FFFFFF",
        text_muted: "#B0B0B0",
        text_faint: "#808080",
        accent_1: "#00D4AA",
        accent_2: "#FF6B9D",
        accent_3: "#FFD60A",
        success: "#00E676",
        warning: "#FFD60A",
        danger: "#FF3D5A",
        bubble_sent: "#00D4AA",
        bubble_recv: "#1A1A1A",
        page_bg_alpha: 0.95,
        panel_top_alpha: 0.98,
        panel_bottom_alpha: 0.92,
        surface_hover: "rgba(40,40,40,1)",
        surface_pressed: "rgba(10,10,10,1)",
        surface_header: "rgba(30,30,30,1)",
        dialog_bg: "#0A0A0A",
        page_bg_rgb: "20,20,20",
    };

    function make(overrides) { return Object.assign({}, DEFAULTS, overrides); }

    const BUILTIN = [
        make({}),
        make({ name: "Midnight Blue", bg_main: "#050A1A", bg_panel: "rgba(15,20,40,0.95)", bg_surface: "rgba(20,25,50,0.98)",
            bg_surface2: "#101830", border: "rgba(100,150,255,0.2)", border_soft: "rgba(100,150,255,0.1)",
            accent_1: "#00B4D8", accent_2: "#90E0EF", accent_3: "#FFD60A", bubble_sent: "#00B4D8", bubble_recv: "#101830" }),
        make({ name: "Emerald Dark", bg_main: "#001008", bg_panel: "rgba(10,30,20,0.95)", bg_surface: "rgba(15,40,25,0.98)",
            bg_surface2: "#0A2518", border: "rgba(0,255,150,0.2)", border_soft: "rgba(0,255,150,0.1)",
            accent_1: "#00FF96", accent_2: "#00D4AA", accent_3: "#FFD60A", bubble_sent: "#00FF96", bubble_recv: "#0A2518" }),
        make({ name: "Crimson Dark", bg_main: "#1A0008", bg_panel: "rgba(40,10,20,0.95)", bg_surface: "rgba(50,15,25,0.98)",
            bg_surface2: "#2A0A18", border: "rgba(255,60,120,0.2)", border_soft: "rgba(255,60,120,0.1)",
            accent_1: "#FF3C6E", accent_2: "#FF6B9D", accent_3: "#FFD60A", bubble_sent: "#FF3C6E", bubble_recv: "#2A0A18" }),
        make({ name: "Amber Dark", bg_main: "#1A1000", bg_panel: "rgba(40,30,10,0.95)", bg_surface: "rgba(50,40,15,0.98)",
            bg_surface2: "#2A200A", border: "rgba(255,180,0,0.2)", border_soft: "rgba(255,180,0,0.1)",
            accent_1: "#FFB400", accent_2: "#FFD60A", accent_3: "#FF6B9D", bubble_sent: "#FFB400", bubble_recv: "#2A200A" }),
        make({ name: "Slate Dark", bg_main: "#0A0C10", bg_panel: "rgba(22,26,34,0.95)", bg_surface: "rgba(28,33,42,0.98)",
            bg_surface2: "#181C24", border: "rgba(150,170,200,0.2)", border_soft: "rgba(150,170,200,0.1)",
            accent_1: "#7B9FE0", accent_2: "#A0B8F0", accent_3: "#FFD60A", bubble_sent: "#7B9FE0", bubble_recv: "#181C24" }),
        make({ name: "Violet Dark", bg_main: "#120818", bg_panel: "rgba(35,15,45,0.95)", bg_surface: "rgba(45,20,58,0.98)",
            bg_surface2: "#2A1038", border: "rgba(190,120,255,0.2)", border_soft: "rgba(190,120,255,0.1)",
            accent_1: "#B478FF", accent_2: "#D9A8FF", accent_3: "#FFD60A", bubble_sent: "#B478FF", bubble_recv: "#2A1038" }),
        make({ name: "Daylight", bg_main: "#F4F5F7", bg_panel: "rgba(255,255,255,0.95)", bg_surface: "rgba(248,249,251,0.98)",
            bg_surface2: "#FFFFFF", border: "rgba(0,0,0,0.12)", border_soft: "rgba(0,0,0,0.06)",
            text_main: "#1A1C1E", text_muted: "#5B5F66", text_faint: "#8A8E96",
            accent_1: "#0A84FF", accent_2: "#FF6B9D", accent_3: "#E0A500", success: "#1EA672", warning: "#B8860B", danger: "#D92D4C",
            bubble_sent: "#0A84FF", bubble_recv: "#EAEBEF", surface_hover: "rgba(225,227,232,1)", surface_pressed: "rgba(205,208,215,1)",
            surface_header: "rgba(240,241,244,1)", dialog_bg: "#FFFFFF", page_bg_rgb: "244,245,247" }),
        make({ name: "Paper Light", bg_main: "#F7F2E9", bg_panel: "rgba(255,252,245,0.95)", bg_surface: "rgba(252,248,239,0.98)",
            bg_surface2: "#FFFDF7", border: "rgba(90,70,40,0.15)", border_soft: "rgba(90,70,40,0.08)",
            text_main: "#2A2316", text_muted: "#6B5E45", text_faint: "#9A8C6E",
            accent_1: "#B4651E", accent_2: "#C94E4E", accent_3: "#2F7A4F", success: "#2F7A4F", warning: "#B4651E", danger: "#C94E4E",
            bubble_sent: "#B4651E", bubble_recv: "#EEE4D0", surface_hover: "rgba(232,224,205,1)", surface_pressed: "rgba(214,204,180,1)",
            surface_header: "rgba(242,235,220,1)", dialog_bg: "#FFFCF5", page_bg_rgb: "247,242,233" }),
        make({ name: "Ocean Light", bg_main: "#EEF5F8", bg_panel: "rgba(255,255,255,0.95)", bg_surface: "rgba(246,251,253,0.98)",
            bg_surface2: "#FFFFFF", border: "rgba(0,80,120,0.12)", border_soft: "rgba(0,80,120,0.06)",
            text_main: "#0D2430", text_muted: "#4A6A78", text_faint: "#7D98A3",
            accent_1: "#0088B3", accent_2: "#00B4D8", accent_3: "#E0A500", success: "#188F5A", warning: "#B8860B", danger: "#D92D4C",
            bubble_sent: "#0088B3", bubble_recv: "#DCEDF3", surface_hover: "rgba(216,233,238,1)", surface_pressed: "rgba(192,216,224,1)",
            surface_header: "rgba(230,242,246,1)", dialog_bg: "#FFFFFF", page_bg_rgb: "238,245,248" }),
        make({ name: "Mint Light", bg_main: "#EFF8F3", bg_panel: "rgba(255,255,255,0.95)", bg_surface: "rgba(247,253,250,0.98)",
            bg_surface2: "#FFFFFF", border: "rgba(0,100,70,0.12)", border_soft: "rgba(0,100,70,0.06)",
            text_main: "#0C2A1D", text_muted: "#4A6B5A", text_faint: "#7E9A8C",
            accent_1: "#00A878", accent_2: "#00D4AA", accent_3: "#E0A500", success: "#00A878", warning: "#B8860B", danger: "#D92D4C",
            bubble_sent: "#00A878", bubble_recv: "#DCF0E6", surface_hover: "rgba(214,237,226,1)", surface_pressed: "rgba(190,220,206,1)",
            surface_header: "rgba(228,245,237,1)", dialog_bg: "#FFFFFF", page_bg_rgb: "239,248,243" }),
    ];

    // name -> theme. Custom themes (from <DATA_DIR>/themes) are merged in by
    // addCustom(), exactly like ThemeManager._load_custom_themes().
    const themes = {};
    BUILTIN.forEach((t) => { themes[t.name] = t; });
    let current = themes["Black Glass"];
    const listeners = [];

    function addCustom(list) {
        (list || []).forEach((data) => {
            if (!data || !data.name) return;
            const t = make({});
            Object.keys(DEFAULTS).forEach((k) => { if (data[k] !== undefined) t[k] = data[k]; });
            themes[t.name] = t;
        });
    }

    // ---- colour helpers ------------------------------------------------
    function parseColor(str) {
        // -> {r,g,b,a} (a in 0..1). Accepts #RGB, #RRGGBB, rgb(), rgba().
        if (!str) return { r: 0, g: 0, b: 0, a: 1 };
        str = String(str).trim();
        if (str[0] === "#") {
            let h = str.slice(1);
            if (h.length === 3) h = h.split("").map((c) => c + c).join("");
            if (h.length === 8) return qt8parts(h);
            const n = parseInt(h, 16);
            return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255, a: 1 };
        }
        const m = str.match(/rgba?\(([^)]+)\)/i);
        if (m) {
            const p = m[1].split(",").map((s) => parseFloat(s));
            return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
        }
        return { r: 0, g: 0, b: 0, a: 1 };
    }
    function qt8parts(h8) {
        const n = (i) => parseInt(h8.slice(i, i + 2), 16);
        return { a: n(0) / 255, r: n(2), g: n(4), b: n(6) };
    }
    // "{accent}40" the way Qt reads it: #AARRGGBB.
    function qt8(hex6, suffix) {
        const h = (String(hex6).replace("#", "") + suffix).slice(0, 8);
        const c = qt8parts(h);
        return `rgba(${c.r},${c.g},${c.b},${+c.a.toFixed(4)})`;
    }
    function rgba(c, alphaOverride) {
        const a = alphaOverride === undefined ? c.a : alphaOverride;
        return `rgba(${Math.round(c.r)},${Math.round(c.g)},${Math.round(c.b)},${+(+a).toFixed(4)})`;
    }
    function withAlpha(str, a) { return rgba(parseColor(str), a); }

    // ---- apply ---------------------------------------------------------
    function apply(themeOrName) {
        const t = typeof themeOrName === "string" ? (themes[themeOrName] || themes["Black Glass"]) : themeOrName;
        current = t;
        const s = document.documentElement.style;
        const set = (k, v) => s.setProperty(k, v);
        [
            "bg_main", "bg_panel", "bg_surface", "bg_surface2", "border", "border_soft", "text_main", "text_muted",
            "text_faint", "accent_1", "accent_2", "accent_3", "success", "warning", "danger", "bubble_sent",
            "bubble_recv", "surface_hover", "surface_pressed", "surface_header", "dialog_bg",
        ].forEach((k) => set("--" + k.replace(/_/g, "-"), t[k]));
        set("--page-bg", `rgba(${t.page_bg_rgb},${t.page_bg_alpha})`);
        set("--panel-top", `rgba(30,30,30,${t.panel_top_alpha})`);
        set("--panel-bottom", `rgba(20,20,20,${t.panel_bottom_alpha})`);
        // Qt #AARRGGBB quirk colours (see header comment).
        set("--nav-checked-c0", qt8(t.accent_1, "40"));
        set("--nav-checked-c1", qt8(t.accent_2, "20"));
        set("--nav-checked-border", qt8(t.accent_1, "80"));
        set("--danger-pill-border", qt8(t.danger, "50"));
        set("--accent-1-q30", qt8(t.accent_1, "30"));
        set("--accent-1-q25", qt8(t.accent_1, "25"));
        set("--accent-1-q18", qt8(t.accent_1, "18"));
        // selection colour used by QTextBrowser bubbles ({accent_1}80)
        set("--accent-1-q80", qt8(t.accent_1, "80"));
        // backdrop blobs
        set("--blob-1-a", withAlpha(t.accent_1, 60 / 255));
        set("--blob-1-b", withAlpha(t.accent_1, 20 / 255));
        set("--blob-1-c", withAlpha(t.accent_1, 0));
        set("--blob-2-a", withAlpha(t.accent_2, 60 / 255));
        set("--blob-2-b", withAlpha(t.accent_2, 20 / 255));
        set("--blob-2-c", withAlpha(t.accent_2, 0));
        set("--blob-3-a", withAlpha(t.accent_3, 60 / 255));
        set("--blob-3-b", withAlpha(t.accent_3, 20 / 255));
        set("--blob-3-c", withAlpha(t.accent_3, 0));
        const light = parseColor(t.bg_main);
        document.documentElement.setAttribute("data-theme-tone",
            (light.r * 0.299 + light.g * 0.587 + light.b * 0.114) > 140 ? "light" : "dark");
        listeners.forEach((fn) => { try { fn(t); } catch (e) { console.error(e); } });
        return t;
    }

    window.WaveTheme = {
        DEFAULTS,
        names: () => Object.keys(themes),
        get: (name) => themes[name],
        current: () => current,
        apply,
        addCustom,
        onChange: (fn) => listeners.push(fn),
        qt8,
        parseColor,
        withAlpha,
        rgba,
    };
})();
