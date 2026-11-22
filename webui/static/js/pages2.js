/* pages2.js - the Tier 21 management pages, Settings and the first-run
 * wizard, mirroring ui/workflows_panel.py, ui/memory_panel.py,
 * ui/reminders_panel.py, ui/notes_todos_panel.py, ui/calendar_panel.py,
 * ui/skills_panel.py, ui/performance_panel.py, ui/settings_panel.py and
 * ui/onboarding.py. Shares helpers with app.js through window.WaveApp. */
(function () {
    "use strict";
    const App = window.WaveApp;
    const { el, icon, call } = App;
    const T = () => WaveTheme.current();
    const ui = () => App.ui;
    const toast = (...a) => W.toast(...a);

    function makePage(cls) { return el("section", { class: "page" + (cls ? " " + cls : "") }); }
    const heading = (t, s) => ui().heading(t, s);
    const btn = (...a) => ui().btn(...a);
    const panel = (c, o) => ui().panel(c, o);
    const stretch = () => ui().stretch();
    const space = (px) => ({ marginTop: px + "px" });
    const lineEdit = (ph, extra) => { const i = el("input", { class: "line-edit" + (extra ? " " + extra : ""), type: "text", placeholder: ph || "" }); return i; };

    // A QListWidget: items = [{text, data}], single selection.
    function makeList(cls, opts) {
        opts = opts || {};
        const root = el("div", { class: "list " + (cls || ""), tabindex: "0" });
        const api = {
            root, items: [], selected: -1,
            set(items) {
                api.items = items; api.selected = -1; root.innerHTML = "";
                items.forEach((it, i) => {
                    const node = el("div", { class: "item", draggable: opts.draggable ? "true" : null }, it.text);
                    node.addEventListener("click", () => api.select(i));
                    node.addEventListener("dblclick", () => opts.onDouble && opts.onDouble(it, i));
                    if (opts.draggable) {
                        node.addEventListener("dragstart", (e) => { api.dragFrom = i; e.dataTransfer.effectAllowed = "move"; });
                        node.addEventListener("dragover", (e) => { e.preventDefault(); });
                        node.addEventListener("drop", (e) => {
                            e.preventDefault();
                            const from = api.dragFrom; if (from === undefined || from === i) return;
                            const arr = api.items.slice(); const [m] = arr.splice(from, 1); arr.splice(i, 0, m);
                            api.set(arr); api.select(i); if (opts.onReorder) opts.onReorder();
                        });
                    }
                    root.appendChild(node);
                });
            },
            select(i, silent) {
                api.selected = i;
                Array.from(root.children).forEach((n, k) => n.classList.toggle("selected", k === i));
                if (!silent && opts.onSelect) opts.onSelect(api.items[i], i);
            },
            clearSelection() { api.select(-1, true); },
            current() { return api.selected >= 0 ? api.items[api.selected] : null; },
        };
        root.addEventListener("keydown", (e) => {
            if (!api.items.length) return;
            if (e.key === "ArrowDown") { e.preventDefault(); api.select(Math.min(api.items.length - 1, api.selected + 1)); }
            if (e.key === "ArrowUp") { e.preventDefault(); api.select(Math.max(0, api.selected - 1)); }
        });
        return api;
    }

    // ========================================================= WORKFLOWS
    App.build.workflows = function () {
        const root = makePage("");
        const st = { current: null, dirty: false, workflows: {} };
        const names = makeList("side", { onSelect: (it) => it && selectWorkflow(it.data) });
        const left = el("div", { class: "side-col", style: { width: "260px" } }, [
            heading("WORKFLOWS"), Object.assign(names.root, { style: "flex:1 1 0" }), btn("+ New Workflow", "action", () => newWorkflow()),
        ]);
        const title = el("div", { class: "big-title" }, "New Workflow");
        const steps = makeList("boxed steps", { draggable: true, onReorder: () => markDirty(), onDouble: (it, i) => editStep(i) });
        steps.root.style.flex = "1 1 0";
        const stepInput = lineEdit("e.g. open chrome, or 'what's the weather'...");
        stepInput.addEventListener("keydown", (e) => { if (e.key === "Enter") addStep(); });
        const status = el("div", { class: "small-status" }, "");
        const deleteBtn = btn("Delete", "danger-pill", () => deleteWorkflow());
        const pnl = panel([
            heading("STEPS (runs top to bottom)"), el("div", { class: "vbox", style: Object.assign(space(4), { flex: "1 1 0", minHeight: 0 }) }, steps.root),
            el("div", { class: "hbox", style: Object.assign(space(4), { gap: "6px", alignItems: "center" }) }, [stepInput, btn("Add Step", "action", () => addStep())]),
            el("div", { class: "hbox", style: Object.assign(space(4), { gap: "6px" }) }, [
                btn("↑ Move Up", "action", () => moveStep(-1)), btn("↓ Move Down", "action", () => moveStep(1)), btn("Remove Step", "danger-pill", () => removeStep()), stretch(),
            ]),
        ], { pad: "18px 20px", gap: 6, style: { flex: "1 1 0" } });
        const right = el("div", { class: "vbox", style: { flex: "1 1 0", padding: "28px 32px", gap: "16px", minWidth: 0 } }, [
            el("div", { class: "hbox", style: { alignItems: "center", gap: "6px" } }, [title, stretch(), btn("Rename", "action", () => renameWorkflow())]),
            pnl,
            el("div", { class: "hbox", style: { alignItems: "center", gap: "6px" } }, [status, stretch(), btn("▶︎ Run Now", "action", () => runWorkflow()), deleteBtn, btn("Save Workflow", "action", () => saveWorkflow())]),
        ]);
        root.appendChild(el("div", { class: "hbox", style: { height: "100%" } }, [left, right]));

        async function reload() {
            const r = await call("get_workflows");
            st.workflows = {};
            ((r && r.workflows) || []).forEach((w) => { st.workflows[w.name] = w.actions || []; });
            names.set(Object.keys(st.workflows).map((n) => ({ text: n, data: n })));
        }
        const titleCase = (s) => s.replace(/\w\S*/g, (w) => w[0].toUpperCase() + w.slice(1).toLowerCase());
        function selectWorkflow(name) {
            if (!name || !(name in st.workflows)) return;
            st.current = name; st.dirty = false;
            title.textContent = titleCase(name);
            steps.set(st.workflows[name].map((s) => ({ text: s })));
            deleteBtn.disabled = false;
            status.textContent = `${st.workflows[name].length} step(s) · saved`;
        }
        function newWorkflow() {
            st.current = null; st.dirty = false; title.textContent = "New Workflow";
            steps.set([]); names.clearSelection(); deleteBtn.disabled = true; status.textContent = "Not saved yet";
        }
        function markDirty() {
            st.dirty = true;
            status.textContent = `${steps.items.length} step(s)${st.current ? " · unsaved changes" : " · not saved yet"}`;
        }
        function addStep() { const t = stepInput.value.trim(); if (!t) return; steps.set(steps.items.concat([{ text: t }])); stepInput.value = ""; markDirty(); }
        async function editStep(i) {
            const v = await W.inputBox("Edit Step", "Command:", steps.items[i].text);
            if (v !== null && v.trim()) { const arr = steps.items.slice(); arr[i] = { text: v.trim() }; steps.set(arr); steps.select(i, true); markDirty(); }
        }
        function removeStep() { const i = steps.selected; if (i < 0) return; const arr = steps.items.slice(); arr.splice(i, 1); steps.set(arr); markDirty(); }
        function moveStep(d) {
            const i = steps.selected, j = i + d;
            if (i < 0 || j < 0 || j >= steps.items.length) return;
            const arr = steps.items.slice(); const [m] = arr.splice(i, 1); arr.splice(j, 0, m); steps.set(arr); steps.select(j, true); markDirty();
        }
        async function saveWorkflow() {
            const list = steps.items.map((x) => x.text);
            if (!list.length) { await W.warn("No steps", "Add at least one step before saving."); return; }
            let name = st.current;
            if (!name) { const v = await W.inputBox("Save Workflow", "Workflow name:", ""); if (v === null || !v.trim()) return; name = v.trim(); }
            const r = await call("save_workflow", name, list);
            if (r && r.ok && r.saved) {
                st.current = name.trim().toLowerCase(); st.dirty = false;
                status.textContent = `${list.length} step(s) · saved`;
                await reload();
                const i = names.items.findIndex((x) => x.data === st.current); if (i >= 0) names.select(i, true);
                toast(`Workflow '${st.current}' saved.`, "success");
            } else await W.warn("Couldn't save", "Something went wrong saving that workflow.");
        }
        async function renameWorkflow() {
            if (!st.current) { await W.info("Save first", "Save this workflow before renaming it."); return; }
            const v = await W.inputBox("Rename Workflow", "New name:", st.current);
            if (v === null || !v.trim()) return;
            const r = await call("rename_workflow", st.current, v.trim());
            if (r && r.ok && r.renamed) {
                st.current = v.trim().toLowerCase(); title.textContent = titleCase(st.current);
                await reload(); toast("Workflow renamed.", "success");
            } else await W.warn("Couldn't rename", "That name may already be taken.");
        }
        async function deleteWorkflow() {
            if (!st.current) return;
            if (!(await W.ask("Delete workflow", `Delete '${st.current}'? This can't be undone.`))) return;
            const r = await call("delete_workflow", st.current);
            if (r && r.ok && r.deleted) { toast("Workflow deleted.", "info"); await reload(); newWorkflow(); }
        }
        async function runWorkflow() {
            if (st.dirty) { await W.info("Unsaved changes", "Save the workflow before running it."); return; }
            if (!st.current) { await W.info("Nothing to run", "Save this workflow first."); return; }
            status.textContent = "Running...";
            const r = await call("run_workflow_ui", st.current);
            const result = r && r.ok ? r.result : `Run failed: ${(r && r.error) || ""}`;
            status.textContent = `${steps.items.length} step(s) · saved`;
            toast(String(result), "info", 5000);
            App.addChatBubble(String(result), false);
        }
        newWorkflow(); reload();
        return { root, onShow: () => reload() };
    };

    // ============================================================ MEMORY
    App.build.memory = function () {
        const root = makePage("");
        const BADGE = { explicit: "🔒", auto_consolidated: "✨" };
        const st = { current: null, facts: {}, prov: {} };
        const search = lineEdit("Filter...");
        search.addEventListener("input", () => reload());
        const keys = makeList("side", { onSelect: (it) => it && selectKey(it.data) });
        keys.root.style.flex = "1 1 0";
        const left = el("div", { class: "side-col", style: { width: "280px" } }, [
            heading("REMEMBERED FACTS"), search, keys.root,
            el("div", { class: "hbox", style: { gap: "6px" } }, [btn("+ New Fact", "action grow", () => newFact()), btn("Export", "action grow", () => exportFacts())]),
            el("div", { class: "legend wrap" }, `${BADGE.explicit} you told me  ·  ${BADGE.auto_consolidated} I picked up on my own`),
        ]);
        const title = el("div", { class: "big-title" }, "New Fact");
        const keyIn = lineEdit("e.g. favorite color");
        const valIn = lineEdit("e.g. blue");
        const prov = el("div", { class: "note-faint wrap" }, "");
        const delBtn = btn("Delete", "danger-pill", () => deleteFact());
        const pnl = panel([heading("KEY"), keyIn, heading("VALUE"), valIn, prov, stretch()], { pad: "18px 20px", gap: 10, style: { flex: "1 1 0" } });
        const right = el("div", { class: "vbox", style: { flex: "1 1 0", padding: "28px 32px", gap: "16px", minWidth: 0 } }, [
            title, pnl, el("div", { class: "hbox", style: { gap: "6px" } }, [stretch(), delBtn, btn("Save Fact", "action", () => saveFact())]),
        ]);
        root.appendChild(el("div", { class: "hbox", style: { height: "100%" } }, [left, right]));

        async function reload() {
            const f = search.value.trim().toLowerCase();
            const r = await call("get_memory");
            st.facts = {}; st.prov = {};
            ((r && r.items) || []).forEach((it) => { st.facts[it.key] = it.value; st.prov[it.key] = { source: it.source, updated_at: it.updated_at }; });
            const shown = Object.keys(st.facts).sort().filter((k) => !f || k.toLowerCase().includes(f) || String(st.facts[k]).toLowerCase().includes(f));
            keys.set(shown.map((k) => ({ text: `${BADGE[st.prov[k].source] || BADGE.explicit}   ${k}`, data: k })));
            if (st.current) { const i = shown.indexOf(st.current); if (i >= 0) keys.select(i, true); }
        }
        function fmtProv(p) {
            if (!p) return "";
            const parts = [];
            if (p.source) parts.push({ explicit: "you told me this directly", auto_consolidated: "I picked this up from conversation" }[p.source] || p.source);
            if (p.updated_at) parts.push(`last updated ${p.updated_at}`);
            return parts.length ? parts.join(" · ") : "No provenance recorded for this fact (saved before Tier 20).";
        }
        function selectKey(k) {
            if (!(k in st.facts)) return;
            st.current = k; title.textContent = k; keyIn.value = k; keyIn.disabled = true; valIn.value = st.facts[k];
            delBtn.disabled = false; prov.textContent = fmtProv(st.prov[k]);
        }
        function newFact() {
            st.current = null; title.textContent = "New Fact"; keyIn.value = ""; keyIn.disabled = false; valIn.value = "";
            prov.textContent = ""; keys.clearSelection(); delBtn.disabled = true;
        }
        async function saveFact() {
            const k = keyIn.value.trim(), v = valIn.value.trim();
            if (!k || !v) { await W.warn("Missing info", "Both a key and a value are needed."); return; }
            const r = await call("save_memory", k, v);
            if (r && r.ok && r.saved) {
                st.current = k.toLowerCase() in st.facts ? k.toLowerCase() : k;
                await reload();
                const i = keys.items.findIndex((x) => x.data.trim().toLowerCase() === k.trim().toLowerCase());
                if (i >= 0) { keys.select(i); }
                toast(`Remembered '${k}'.`, "success");
            } else await W.warn("Couldn't save", "Something went wrong saving that fact.");
        }
        async function exportFacts() {
            if (!Object.keys(st.facts).length) { await W.info("Nothing to export", "There are no remembered facts yet."); return; }
            const r = await call("export_memory");
            if (r && r.ok && r.saved) toast(`Exported ${r.count} fact(s) to ${r.path}`, "success");
            else if (r && !r.ok) await W.warn("Couldn't export", `Something went wrong writing that file: ${r.error}`);
        }
        async function deleteFact() {
            if (!st.current) return;
            if (!(await W.ask("Forget this fact", `Forget '${st.current}'? This can't be undone.`))) return;
            const r = await call("delete_memory", st.current);
            if (r && r.ok && r.deleted) { toast(`Forgot '${st.current}'.`, "success"); st.current = null; await reload(); newFact(); }
            else await W.warn("Couldn't delete", "Something went wrong forgetting that fact.");
        }
        newFact(); reload();
        return { root, onShow: () => reload() };
    };

    // ========================================================= REMINDERS
    App.build.reminders = function () {
        const root = makePage("");
        const list = makeList("boxed"); list.root.style.flex = "1 1 0";
        const msg = lineEdit("What should I remind you about? e.g. 'check the oven'");
        const when = lineEdit("When? e.g. 'in 10 minutes', 'tomorrow at 5pm', '7am'");
        const daily = new W.Check("Repeat daily", false, null, "tight");
        daily.root.style.color = "var(--text-main)";
        const status = el("div", { class: "small-status wrap" }, "");
        when.addEventListener("keydown", (e) => { if (e.key === "Enter") add(); });
        const pnl = panel([
            heading("NEW REMINDER"), el("div", { class: "hbox" }, msg),
            el("div", { class: "hbox", style: { gap: "6px", alignItems: "center" } }, [when, daily.root, btn("Add", "action", () => add())]),
            status,
        ], { pad: "18px 20px", gap: 10, style: { flex: "none" } });
        root.appendChild(el("div", { class: "vbox", style: { height: "100%", padding: "28px 32px", gap: "16px" } }, [
            heading("REMINDERS & TIMERS"), list.root,
            el("div", { class: "hbox" }, [stretch(), btn("Cancel Selected", "danger-pill", () => cancelSel())]), pnl,
        ]));
        async function reload() {
            const r = await call("get_reminder_rows");
            const rows = (r && r.rows) || [];
            list.set(rows.length ? rows.map((x) => ({ text: x.label, data: x.id })) : [{ text: "No upcoming reminders or timers.", data: null }]);
        }
        async function cancelSel() {
            const it = list.current(); if (!it || it.data === null || it.data === undefined) return;
            const r = await call("cancel_reminder_id", it.data);
            if (r && r.ok && r.cancelled) { toast("Reminder cancelled.", "success"); reload(); }
            else await W.warn("Couldn't cancel", "That reminder may have already fired.");
        }
        async function add() {
            const message = msg.value.trim() || "Reminder";
            const w = when.value.trim();
            if (!w) { status.textContent = "Enter a time first."; return; }
            const r = await call("add_reminder", message, w, daily.checked ? "daily" : "none");
            const result = (r && r.ok) ? r.result : `Error: ${(r && r.error) || ""}`;
            status.textContent = result;
            const low = String(result).toLowerCase();
            if (r && r.ok && !low.includes("couldn't") && !low.includes("need") && !low.includes("already passed")) {
                msg.value = ""; when.value = ""; daily.set(false); toast(result, "success"); reload();
            }
        }
        reload();
        return { root, onShow: () => reload() };
    };

    // ===================================================== NOTES & TO-DOS
    App.build.notes = function () {
        const root = makePage("");
        const todos = makeList("", { onDouble: (it) => completeTodo(it && it.data) }); todos.root.style.flex = "1 1 0";
        const notes = makeList(""); notes.root.style.flex = "1 1 0";
        const todoIn = lineEdit("New to-do... (double-click an item to complete it)");
        todoIn.addEventListener("keydown", (e) => { if (e.key === "Enter") addTodo(); });
        const noteIn = lineEdit("New quick note...");
        noteIn.addEventListener("keydown", (e) => { if (e.key === "Enter") addNote(); });
        const todoPanel = panel([
            heading("TO-DOS"), todos.root,
            el("div", { class: "hbox", style: { gap: "6px", alignItems: "center" } }, [todoIn, btn("Add", "action", () => addTodo())]),
            el("div", { class: "hbox", style: { gap: "6px" } }, [btn("Complete Selected", "action grow", () => { const it = todos.current(); completeTodo(it && it.data); }),
                btn("Delete Selected", "danger-pill grow", () => deleteTodo())]),
        ], { pad: "18px 20px", gap: 10, style: { flex: "1 1 0" } });
        const notesPanel = panel([
            heading("QUICK NOTES"), notes.root,
            el("div", { class: "hbox", style: { gap: "6px", alignItems: "center" } }, [noteIn, btn("Add", "action", () => addNote())]),
            btn("Delete Selected", "danger-pill", () => deleteNote()),
        ], { pad: "18px 20px", gap: 10, style: { flex: "1 1 0" } });
        root.appendChild(el("div", { class: "hbox", style: { height: "100%", padding: "28px 32px", gap: "24px" } }, [todoPanel, notesPanel]));
        async function reloadTodos() {
            const r = await call("get_todos", false);
            const rows = (r && r.todos) || [];
            todos.set(rows.length ? rows.map((t) => ({ text: t.text + (t.due_text ? `  (due ${t.due_text})` : ""), data: t.id })) : [{ text: "Nothing pending - nice work.", data: null }]);
        }
        async function reloadNotes() {
            const r = await call("get_notes");
            const rows = (r && r.notes) || [];
            notes.set(rows.length ? rows.map((n) => ({ text: n.content, data: n.id })) : [{ text: "No quick notes yet.", data: null }]);
        }
        async function addTodo() {
            const t = todoIn.value.trim(); if (!t) return;
            const r = await call("add_todo_raw", t);
            if (r && r.ok && r.added) { todoIn.value = ""; reloadTodos(); } else await W.warn("Couldn't add", "Something went wrong adding that to-do.");
        }
        async function completeTodo(id) {
            if (id === null || id === undefined) return;
            const r = await call("complete_todo_id", id);
            if (r && r.ok && r.done) { toast("To-do completed.", "success"); reloadTodos(); }
        }
        async function deleteTodo() {
            const it = todos.current(); if (!it || it.data === null) return;
            const r = await call("delete_todo_id", it.data);
            if (r && r.ok && r.deleted) { toast("To-do deleted.", "success"); reloadTodos(); }
        }
        async function addNote() {
            const t = noteIn.value.trim(); if (!t) return;
            const r = await call("add_note_raw", t);
            if (r && r.ok && r.added) { noteIn.value = ""; reloadNotes(); } else await W.warn("Couldn't add", "Something went wrong adding that note.");
        }
        async function deleteNote() {
            const it = notes.current(); if (!it || it.data === null) return;
            const r = await call("delete_note_id", it.data);
            if (r && r.ok && r.deleted) { toast("Note deleted.", "success"); reloadNotes(); }
        }
        reloadTodos(); reloadNotes();
        return { root, onShow: () => { reloadTodos(); reloadNotes(); } };
    };

    // ========================================================== CALENDAR
    App.build.calendar = function () {
        const root = makePage("");
        const list = makeList("boxed"); list.root.style.flex = "1 1 0";
        const title = lineEdit("Title, e.g. 'Team standup'");
        const loc = lineEdit("Location (optional)");
        const when = lineEdit("When? e.g. 'tomorrow at 3pm', 'next Monday 10am'");
        when.addEventListener("keydown", (e) => { if (e.key === "Enter") add(); });
        const rec = new W.Combo(["none", "daily", "weekly"], "none");
        rec.root.style.flex = "none"; rec.root.style.minWidth = "64px";
        const status = el("div", { class: "small-status wrap" }, "");
        const pnl = panel([
            heading("NEW EVENT"),
            el("div", { class: "hbox", style: { gap: "6px" } }, [title, loc]),
            el("div", { class: "hbox", style: { gap: "6px", alignItems: "center" } }, [when, rec.root, btn("Add", "action", () => add())]),
            status,
        ], { pad: "18px 20px", gap: 10, style: { flex: "none" } });
        root.appendChild(el("div", { class: "vbox", style: { height: "100%", padding: "28px 32px", gap: "16px" } }, [
            heading("CALENDAR — NEXT 60 DAYS"), list.root,
            el("div", { class: "hbox" }, [stretch(), btn("Cancel Selected", "danger-pill", () => cancelSel())]), pnl,
        ]));
        async function reload() {
            const r = await call("get_calendar_rows", 60);
            const rows = (r && r.rows) || [];
            list.set(rows.length ? rows.map((x) => ({ text: x.label, data: x.id })) : [{ text: "Nothing on the calendar for the next 60 days.", data: null }]);
        }
        async function cancelSel() {
            const it = list.current(); if (!it || it.data === null || it.data === undefined) return;
            const r = await call("cancel_event_id", it.data);
            if (r && r.ok && r.cancelled) { toast("Event cancelled.", "success"); reload(); }
            else await W.warn("Couldn't cancel", "That event may already be gone.");
        }
        async function add() {
            const t = title.value.trim(), w = when.value.trim();
            if (!t || !w) { status.textContent = "Both a title and a time are needed."; return; }
            const r = await call("add_calendar_event", t, w, null, loc.value.trim(), rec.value);
            const result = (r && r.ok) ? r.message : `Error: ${(r && r.error) || ""}`;
            status.textContent = result;
            const low = String(result).toLowerCase();
            if (r && r.ok && !low.includes("couldn't") && !low.includes("need")) {
                title.value = ""; loc.value = ""; when.value = ""; rec.setValue("none"); toast(result, "success"); reload();
            }
        }
        reload();
        return { root, onShow: () => reload() };
    };

    // ============================================================ SKILLS
    App.build.skills = function () {
        const root = makePage("");
        const search = lineEdit("Filter by name or description...");
        const catalog = makeList("boxed catalog"); catalog.root.style.flex = "2 1 0";
        const status = el("div", { class: "lbl wrap", style: { color: "var(--text-muted)", fontSize: "12px" } }, "");
        const files = makeList("files", { onSelect: (it) => onFileSelected(it && it.data) }); files.root.style.flex = "1 1 0";
        const toggle = btn("Disable", "danger-pill", () => toggleSelected());
        toggle.disabled = true;
        let entries = [];
        search.addEventListener("input", () => renderCatalog());
        const pnl = panel([
            heading("USER_SKILLS/ HOT-RELOAD STATUS"), status, heading("DROP-IN FILES — ENABLE / DISABLE"), files.root,
            el("div", { class: "hbox" }, [stretch(), toggle]),
        ], { pad: "18px 20px", gap: 8, style: { flex: "1 1 0" } });
        root.appendChild(el("div", { class: "vbox", style: { height: "100%", padding: "28px 32px", gap: "16px" } }, [
            el("div", { class: "hbox", style: { alignItems: "center" } }, [heading("SKILLS"), stretch(), btn("Rescan user_skills/", "action", () => rescan())]),
            search, catalog.root, pnl,
        ]));
        function renderCatalog() {
            const f = search.value.trim().toLowerCase();
            const shown = entries.filter((e) => !f || e.name.toLowerCase().includes(f) || (e.description || "").toLowerCase().includes(f));
            catalog.set(shown.length ? shown.map((e) => ({ text: `${e.name}  ·  ${e.source}  —  ${e.description}` }))
                : [{ text: f ? "No skills match that filter." : "No skills registered." }]);
        }
        let statusData = {};
        async function reloadAll() {
            const [c, s] = await Promise.all([call("get_skills_catalog"), call("get_skill_status")]);
            entries = (c && c.entries) || [];
            renderCatalog();
            statusData = (s && s.status) || {};
            if (s && !s.ok) { status.textContent = `Couldn't read user_skills/ status: ${s.error}`; return; }
            const lines = [`Directory: ${statusData.directory || "?"}`, `Watching: ${statusData.watching ? "yes" : "no"}`];
            const loaded = statusData.loaded || {}, errors = statusData.errors || {}, disabled = statusData.disabled || [];
            Object.entries(loaded).forEach(([fn, ns]) => lines.push(`✓ ${fn} → ${ns && ns.length ? ns.join(", ") : "(no skills)"}`));
            Object.entries(errors).forEach(([fn, d]) => lines.push(`✗ ${fn}: ${d}`));
            disabled.forEach((fn) => lines.push(`⏸ ${fn}: disabled`));
            if (!Object.keys(loaded).length && !Object.keys(errors).length && !disabled.length) lines.push("No drop-in skill files found yet.");
            status.textContent = lines.join("\n");
            const names = Array.from(new Set([...Object.keys(loaded), ...Object.keys(errors), ...disabled])).sort();
            files.set(names.map((n) => ({ text: `${disabled.includes(n) ? "⏸" : n in errors ? "✗" : "✓"}   ${n}`, data: n })));
            onFileSelected(null);
        }
        function onFileSelected(fn) {
            if (!fn) { toggle.disabled = true; toggle.querySelector("span").textContent = "Disable"; toggle.className = "btn danger-pill"; return; }
            toggle.disabled = false;
            const isDis = (statusData.disabled || []).includes(fn);
            toggle.querySelector("span").textContent = isDis ? "Enable" : "Disable";
            toggle.className = "btn " + (isDis ? "action" : "danger-pill");
        }
        async function toggleSelected() {
            const it = files.current(); if (!it) return;
            const wasDisabled = (statusData.disabled || []).includes(it.data);
            const r = await call("set_skill_file_disabled", it.data, !wasDisabled);
            if (!r || !r.ok) { await W.warn("Couldn't update skill", (r && r.error) || "failed"); return; }
            await reloadAll();
            toast(`${wasDisabled ? "Enabled" : "Disabled"} ${it.data}.`, "success");
        }
        async function rescan() {
            const r = await call("rescan_skills");
            if (!r || !r.ok) { await W.warn("Rescan failed", (r && r.error) || "failed"); return; }
            await reloadAll();
            toast(r.events ? `Rescanned user_skills/ — ${r.events} change(s).` : "Rescanned user_skills/ — no changes.", "success");
        }
        reloadAll();
        return { root, onShow: () => reloadAll() };
    };

    // ======================================================= PERFORMANCE
    App.build.performance = function () {
        const root = makePage("");
        const mkTable = (cols, widths) => {
            const body = el("tbody");
            const table = el("table", { class: "qtable" }, [el("thead", {}, el("tr", {}, cols.map((c, i) => el("th", { style: { width: widths[i] || undefined, textAlign: "center" } }, c)))), body]);
            return { wrap: el("div", { class: "qtable-wrap" }, table), body };
        };
        const paths = mkTable(["Path", "Turns", "p50", "p90", "p50 1st text", "Target met?"], ["100px", "100px", "100px", "100px", "100px", ""]);
        const langs = mkTable(["Language", "Turns", "p50"], ["100px", "100px", ""]);
        const empty = el("div", { class: "lbl wrap", style: { color: "var(--text-muted)", fontSize: "12px" } }, "");
        const usage = el("div", { class: "lbl wrap", style: { color: "var(--text-muted)", fontSize: "12px" } }, "");
        root.appendChild(el("div", { class: "vbox", style: { height: "100%", padding: "28px 32px", gap: "16px" } }, [
            el("div", { class: "hbox", style: { alignItems: "center" } }, [heading("PERFORMANCE — TIME TO FIRST AUDIO"), stretch(), btn("Refresh", "action", () => reload())]),
            panel([heading("BY PATH (fast path / planner / LLM / ...)"), paths.wrap], { pad: "18px 20px", gap: 10, style: { flex: "2 1 0" } }),
            panel([heading("BY LANGUAGE"), langs.wrap], { pad: "18px 20px", gap: 10, style: { flex: "1 1 0" } }),
            empty,
            panel([heading("USAGE STATS (opt-in, never leaves this device)"), usage], { pad: "18px 20px", gap: 10, style: { flex: "none" } }),
        ]));
        const fill = (t, rows) => { t.body.innerHTML = ""; rows.forEach((r) => t.body.appendChild(el("tr", {}, r.map((v) => el("td", {}, v))))); };
        async function reload() {
            const r = await call("get_performance_report");
            if (!r || !r.ok) { empty.textContent = `Couldn't read the TTFA log: ${(r && r.error) || ""}`; return; }
            fill(paths, r.paths || []); fill(langs, r.languages || []);
            empty.textContent = r.empty_text || ""; usage.textContent = r.usage_text || "";
        }
        reload();
        return { root, onShow: () => reload() };
    };

    // =========================================================== SETTINGS
    const S = {};
    App.settingsWidgets = S;
    const TTS_VOICES = {
        "Neerja — English India (Female)": "en-IN-NeerjaNeural", "Prabhat — English India (Male)": "en-IN-PrabhatNeural",
        "Swara — Hindi (Female)": "hi-IN-SwaraNeural", "Madhur — Hindi (Male)": "hi-IN-MadhurNeural",
        "Aria — English US (Female)": "en-US-AriaNeural", "Guy — English US (Male)": "en-US-GuyNeural",
    };
    // Tier 33: the cloned voice - named after its voice sample ("r1 — Cloned
    // voice") - comes first, and is the default whenever a sample is set.
    const CLONE_ID = "clone";
    let cloneLabel = "Cloned voice";
    const voiceLabels = () => [cloneLabel].concat(Object.keys(TTS_VOICES));
    const voiceId = (label) => (label === cloneLabel ? CLONE_ID : TTS_VOICES[label]);
    const voiceLabel = (id) => (id === CLONE_ID ? cloneLabel : Object.keys(TTS_VOICES).find((k) => TTS_VOICES[k] === id));
    const WAKE_PRESETS ={ "Hey Wave": ["hey wave"], "Hello Wave": ["hello wave"], "Okay Wave": ["okay wave", "ok wave"], "Wave": ["wave"] };
    const WAKE_ENGINES = { "Automatic": "auto", "On-device model (openWakeWord)": "openwakeword", "Speech recognition only": "transcript" };
    const STT_LANGS = ["en-US", "en-IN", "hi-IN", "es-ES", "fr-FR", "de-DE"];
    App.WAKE_PRESETS = WAKE_PRESETS;

    App.build.settings = function () {
        const root = makePage("transparent-panels");
        const cats = ["General", "Appearance", "Voice", "Avatar", "Hotkeys", "Privacy", "Security", "Remote", "Advanced", "About"];
        const pagesBox = el("div", { class: "settings-pages" });
        const builders = [general, appearance, voice, avatar, hotkeys, privacy, security, remote, advanced, about];
        const built = builders.map((f) => { const p = f(); p.classList.add("hidden"); pagesBox.appendChild(p); return p; });
        const catList = el("div", { class: "cat-list" }, cats.map((c, i) => {
            const n = el("div", { class: "cat" }, c);
            n.addEventListener("click", () => switchTo(i));
            return n;
        }));
        function switchTo(i) {
            Array.from(catList.children).forEach((n, k) => n.classList.toggle("selected", k === i));
            built.forEach((p, k) => p.classList.toggle("hidden", k !== i));
            if (i === 6) refreshSecurityEvents();
        }
        switchTo(0);
        root.appendChild(el("div", { class: "settings-root" }, [catList, pagesBox]));
        return { root };
    };
    const sp = (children) => el("div", { class: "std", style: { minHeight: "100%" } }, children);
    const formLbl = (t) => el("div", { class: "lbl" }, t);
    const divider = () => el("div", { class: "divider" });
    const fpanel = (children, gap, style) => panel(children, { pad: "20px 24px", gap: gap === undefined ? 14 : gap, style: Object.assign({ flex: "none" }, style || {}) });

    function general() {
        S.autostart = new W.Check("Start WAVE on system boot", false);
        S.minTray = new W.Check("Minimize to tray on close", false);
        S.startMin = new W.Check("Start minimized", false);
        S.sticky = el("textarea", { class: "sticky", placeholder: "Write your daily goals here...", style: { minHeight: "120px", flex: "1 1 0" } });
        App.refs.weather = el("div", { class: "info-data" }, "Fetching...");
        return sp([
            heading("GENERAL"),
            fpanel([S.autostart.root, S.minTray.root, S.startMin.root], 12),
            panel([heading("QUICK NOTES"), el("div", { class: "vbox", style: Object.assign(space(8), { flex: "1 1 0" }) }, S.sticky)], { pad: "20px 24px", gap: 6, style: { flex: "1 1 0" } }),
            panel([heading("ENVIRONMENT"), el("div", { style: space(8) }, App.refs.weather)], { pad: "20px 24px", gap: 6, style: { flex: "none" } }),
            stretch(),
        ]);
    }

    function appearance() {
        const names = WaveTheme.names();
        S.theme = new W.Combo(names, WaveTheme.current().name, (name) => onThemeChange(name));
        const accentBtn = (i, label) => {
            const b = el("button", { class: "accent-btn" }, label);
            const picker = el("input", { type: "color", style: { position: "absolute", opacity: 0, pointerEvents: "none", width: "1px", height: "1px" } });
            picker.addEventListener("input", () => { const t = WaveTheme.current(); t["accent_" + i] = picker.value.toUpperCase(); WaveTheme.apply(t); refreshAppearance(); });
            b.addEventListener("click", () => { picker.value = toHex6(WaveTheme.current()["accent_" + i]); picker.click(); });
            S["accentBtn" + i] = b;
            return el("div", { style: { flex: "1 1 0", display: "flex", position: "relative" } }, [b, picker]);
        };
        S.blur = new W.Slider(0, 50, 25, (v) => setBlur(v));
        S.opacity = new W.Slider(50, 100, Math.round(WaveTheme.current().page_bg_alpha * 100), (v) => { const t = WaveTheme.current(); t.page_bg_alpha = v / 100; WaveTheme.apply(t); });
        const root = sp([
            heading("APPEARANCE"),
            fpanel([formLbl("Theme:"), S.theme.root, divider(), formLbl("Custom Accent Colors:"),
                el("div", { class: "hbox", style: { gap: "12px" } }, [accentBtn(1, "Primary"), accentBtn(2, "Secondary"), accentBtn(3, "Tertiary")]),
                divider(), formLbl("Backdrop Blur:"), S.blur.root, formLbl("Panel Opacity:"), S.opacity.root,
                btn("Save as Custom Theme", "action", () => saveCustomTheme())]),
            stretch(),
        ]);
        refreshAppearance();
        WaveTheme.onChange(() => refreshAppearance());
        return root;
    }
    function toHex6(c) { const p = WaveTheme.parseColor(c); return "#" + [p.r, p.g, p.b].map((x) => Math.round(x).toString(16).padStart(2, "0")).join(""); }
    function refreshAppearance() {
        const t = WaveTheme.current();
        [1, 2, 3].forEach((i) => { if (S["accentBtn" + i]) S["accentBtn" + i].style.background = t["accent_" + i]; });
        if (S.opacity) S.opacity.set(Math.round(t.page_bg_alpha * 100));
    }
    function setBlur(v) {
        const b = document.getElementById("backdrop");
        if (!b) return;
        b.classList.toggle("blurred", v > 0.5);
        b.style.setProperty("--backdrop-blur", v + "px");
    }
    function onThemeChange(name) {
        WaveTheme.apply(name);
        call("save_settings", { last_theme: name });
    }
    App.setTheme = (name) => { if (S.theme) S.theme.setValue(name, false); onThemeChange(name); };
    async function saveCustomTheme() {
        const r = await call("save_custom_theme", WaveTheme.current());
        if (!r || !r.ok || !r.name) return;
        WaveTheme.addCustom([Object.assign({}, WaveTheme.current(), { name: r.name })]);
        S.theme.addItem(r.name);
        S.theme.setValue(r.name, true);
    }

    function voice() {
        S.tts = new W.Combo(voiceLabels(), cloneLabel, (label) => call("apply_setting", "tts_voice", voiceId(label)).then(refreshCloneStatus));
        S.ttsStatus = el("div", { class: "wrap", style: { color: "var(--text-muted)", fontSize: "11.5px", lineHeight: 1.33 } }, "");
        S.rate = new W.Slider(50, 200, 108, (v) => call("apply_setting", "tts_rate", v));
        S.ttsVol = new W.Slider(0, 100, 100, (v) => call("apply_setting", "tts_volume", v));
        S.wake = new W.Combo(Object.keys(WAKE_PRESETS).concat(["Custom..."]), "Hey Wave", (label) => onWakeWordChange(label));
        S.wakeEngine = new W.Combo(Object.keys(WAKE_ENGINES), "Automatic", (label) => call("apply_setting", "wake_engine", WAKE_ENGINES[label]).then(refreshWakeStatus));
        S.wakeSens = new W.Slider(10, 90, 50, (v) => { S.wakeTouched = true; call("apply_setting", "wake_sensitivity", v).then(refreshWakeStatus); });
        S.wakeStatus = el("div", { class: "wrap", style: { color: "var(--text-muted)", fontSize: "11.5px", lineHeight: 1.33 } }, "");
        S.wakeWords = el("div", { class: "wrap", style: { color: "var(--text-main)", fontSize: "11.5px", lineHeight: 1.33 } }, "");
        S.stt = new W.Combo(STT_LANGS, "en-IN", (v) => call("apply_setting", "stt_lang", v));
        S.localVoice = new W.Check("Download a local voice for offline speech (via Piper)", false, (v) => call("apply_setting", "tts_local_auto_download", v));
        S.localVoiceStatus = el("div", { class: "wrap", style: { color: "var(--text-muted)", fontSize: "11.5px", lineHeight: 1.33 } }, "");
        return sp([
            heading("VOICE SETTINGS"),
            fpanel([formLbl("TTS Voice:"), S.tts.root, S.ttsStatus, divider(), formLbl("Speech Rate:"), S.rate.root, divider(), formLbl("TTS Volume:"), S.ttsVol.root,
                divider(), formLbl("Wake Word:"), S.wake.root, S.wakeWords, formLbl("Wake Word Detection:"), S.wakeEngine.root, formLbl("Wake Sensitivity:"), S.wakeSens.root, S.wakeStatus,
                divider(), formLbl("Speech Recognition Language:"), S.stt.root, divider(), S.localVoice.root, S.localVoiceStatus]),
            stretch(),
        ]);
    }
    // Tier 30: a custom wake word can be several, comma-separated - the first
    // is the main one ("baby, babu, dodo, suar").
    function parseWakeWords(v) {
        const words = [];
        String(v || "").split(/[,\n;]+/).forEach((w) => {
            const t = w.trim().toLowerCase().replace(/\s+/g, " ");
            if (t && !words.includes(t)) words.push(t);
        });
        return words.length ? words : ["hey wave"];
    }
    async function askCustomWakeWords() {
        const current = (App.state.wakePhrases || []).filter((p) => p !== "wake up").join(", ");
        const v = await W.inputBox("Custom Wake Words",
            "What should wake WAVE up? Several are fine - separate them with commas; the first is the main one.", current);
        return v === null ? null : parseWakeWords(v);
    }
    App.parseWakeWords = parseWakeWords;
    App.askCustomWakeWords = askCustomWakeWords;
    async function onWakeWordChange(label) {
        let phrases;
        if (label === "Custom...") {
            phrases = await askCustomWakeWords();
            if (phrases === null) phrases = ["hey wave"];
        } else phrases = WAKE_PRESETS[label] || [label.toLowerCase()];
        if (!phrases.includes("wake up")) phrases = phrases.concat(["wake up"]);
        App.state.wakePhrases = phrases;
        await call("apply_setting", "wake_phrases", phrases);
        refreshWakeStatus();
    }
    App.onWakeWordChange = onWakeWordChange;
    function showWakeWords() {
        if (!S.wakeWords) return;
        const p = App.state.wakePhrases || [];
        S.wakeWords.textContent = p.length ? "Wake words: " + p.map((w, i) => (i === 0 ? `${w} (main)` : w)).join(", ") : "";
    }
    function refreshWakeStatus() {
        showWakeWords();
        return call("get_wake_status").then((r) => { if (r && r.ok && S.wakeStatus) S.wakeStatus.textContent = r.text; });
    }
    // Tier 33: the cloned voice's entry follows its voice sample's name.
    function renameClone(label) {
        if (!label || label === cloneLabel) return;
        const current = S.tts.value;
        cloneLabel = label;
        S.tts.setItems(voiceLabels());   // the old clone entry -> the new one
        if (current !== undefined && S.tts.items.includes(current)) S.tts.setValue(current, false);
    }
    // SettingsPanel.refresh_clone_status(): the line under the voice picker,
    // checked again every few seconds while the cloned voice is loading.
    let cloneStatusTimer = null;
    function refreshCloneStatus() {
        return call("get_clone_status").then((r) => {
            if (!r || !r.ok || !S.ttsStatus) return;
            renameClone(r.clone_label);
            S.ttsStatus.textContent = r.text || "";
            if (r.loading && !cloneStatusTimer) cloneStatusTimer = setTimeout(() => { cloneStatusTimer = null; refreshCloneStatus(); }, 3000);
        });
    }
    App.refreshCloneStatus = refreshCloneStatus;
    async function loadVoiceValues() {
        const r = await call("get_voice_settings");
        if (!r || !r.ok) return;
        renameClone(r.clone_label);
        S.tts.setValue(voiceLabel(r.voice) || Object.keys(TTS_VOICES)[0], false);
        S.ttsStatus.textContent = r.clone_status || "";
        if (r.clone_loading) refreshCloneStatus();
        S.rate.set(r.rate_slider, false); S.ttsVol.set(r.volume_slider, false);
        S.wakeEngine.setValue(Object.keys(WAKE_ENGINES).find((k) => WAKE_ENGINES[k] === r.wake_engine) || "Automatic", false);
        S.wakeSens.set(r.wake_sensitivity, false);
        if (STT_LANGS.includes(r.stt_language)) S.stt.setValue(r.stt_language, false);
        S.localVoice.set(!!r.local_voice, false);
        S.localVoiceStatus.textContent = r.local_voice_status || "";
        S.wakeStatus.textContent = r.wake_status || "";
    }

    function avatar() {
        S.avatarShow = new W.Check("Show the avatar when Wave starts", true);
        S.avatarDash = new W.Check("Also show the full dashboard window on startup", false);
        const photo = lineEdit("A clear, front-facing, well-lit photo of you...", "small");
        const voiceIn = lineEdit("(Optional, for Tier 25's voice cloning) a clean ~20-30s voice sample...", "small");
        const status = el("div", { class: "wrap", style: { color: "var(--text-muted)", fontSize: "11px", lineHeight: 1.33 } }, "No avatar built yet - pick a photo above to get started.");
        if (App.state.bootstrap && App.state.bootstrap.has_avatar_rig) status.textContent = "Avatar already built and ready.";
        const buildBtn = btn("Build avatar", "action", async () => {
            const p = photo.value.trim();
            if (!p) { status.textContent = "Pick a photo first."; return; }
            buildBtn.disabled = true;
            const r = await call("build_avatar", p, voiceIn.value.trim());
            buildBtn.disabled = false;
            const result = r && r.ok ? r.result : `Error: ${(r && r.error) || ""}`;
            status.textContent = result;
            toast(result, String(result).includes("ban gaya") ? "success" : "error");
            if (r && r.ok && window.WaveAvatar && WaveAvatar.reload) WaveAvatar.reload();
            if (r && r.ok && voiceIn.value.trim()) loadVoiceValues();   // Tier 33: the new sample is the voice now
        });
        const browse = (input, kind) => btn("Browse…", "action", async () => { const r = await call("pick_file", kind); if (r && r.ok && r.path) input.value = r.path; });
        // Tier 27: LivePortrait clips for the Dodo page (avatar/live.py)
        const muted = { color: "var(--text-muted)", fontSize: "11px", lineHeight: 1.33 };
        const liveStatus = el("div", { class: "wrap", style: muted }, "Checking…");
        const pct = (p) => Math.floor((100 * (p.done || 0)) / Math.max(1, p.total || 0));
        const liveBtn = btn("Render lifelike animation", "action", async () => {
            liveBtn.disabled = true;
            const r = await call("render_avatar_live");
            if (!r || !r.ok) { liveStatus.textContent = (r && r.error) || "Couldn't start the render."; liveBtn.disabled = false; return; }
            liveStatus.textContent = r.started ? "Rendering started - a few minutes on the GPU…" : "Already rendering…";
        });
        async function refreshLive() {
            const r = await call("get_avatar_live");
            if (!r || !r.ok) { liveStatus.textContent = "Status unavailable."; return; }
            liveBtn.disabled = !r.configured || r.rendering;
            if (r.rendering) liveStatus.textContent = `Rendering… ${pct(r)}%`;
            else if (!r.configured) liveStatus.textContent = r.problem;
            else if (r.available) {
                const m = r.manifest;
                liveStatus.textContent = `Ready - rendered ${String(m.rendered_at || "").replace("T", " ")} in ${Math.max(1, Math.round((m.render_seconds || 0) / 60))} min. Build a new avatar and it renders again.`;
            } else liveStatus.textContent = r.last_error ? `Last render failed: ${r.last_error}` : "Not rendered for this photo yet.";
        }
        wave.on("avatar_live_progress", (p) => { if (p) { liveStatus.textContent = `Rendering… ${pct(p)}% (${p.clip})`; liveBtn.disabled = true; } });
        wave.on("avatar_live_done", (p) => {
            refreshLive();
            if (p) toast(p.ok ? "Lifelike animation ready - open the Dodo page." : `Lifelike animation failed: ${p.error}`, p.ok ? "success" : "error");
        });
        refreshLive();
        return sp([
            heading("AVATAR COMPANION"),
            fpanel([S.avatarShow.root, S.avatarDash.root, el("div", { class: "wrap", style: { color: "var(--text-muted)", fontSize: "11px", lineHeight: 1.33 } },
                "With both left as above, only the avatar is visible - say 'wave ka ui dikhao' (or 'show the UI') any time to open the dashboard, and 'ui chhupao' to hide it again.")]),
            divider(),
            fpanel([heading("BUILD YOUR AVATAR"),
                el("div", { class: "hbox", style: { gap: "6px", alignItems: "center" } }, [photo, browse(photo, "photo")]),
                el("div", { class: "hbox", style: { gap: "6px", alignItems: "center" } }, [voiceIn, browse(voiceIn, "voice")]),
                buildBtn, status], 12),
            divider(),
            fpanel([heading("LIFELIKE ANIMATION (AI)"),
                el("div", { class: "wrap", style: muted },
                    "LivePortrait re-draws your photo on the NVIDIA GPU with real blinks, eye movement, breathing and a mouth that follows Wave's voice. It renders once per photo (a few minutes); the Dodo page then plays it smoothly with no GPU load."),
                liveBtn, liveStatus], 12),
            stretch(),
        ]);
    }

    function hotkeys() {
        const rows = [["Toggle Mic", "ctrl+shift+space"], ["Command Palette", "ctrl+k"], ["Show/Hide Window", "ctrl+alt+w"],
            ["Next Tab", "ctrl+tab"], ["Previous Tab", "ctrl+shift+tab"], ["Clear Chat", "ctrl+l"]].map(([name, def]) => {
            const edit = el("input", { class: "line-edit hotkey", type: "text", readonly: "readonly" });
            edit.value = def;
            const change = btn("Change", "action", () => { edit.value = "Press keys..."; edit.classList.add("capturing"); });
            change.style.width = "80px"; change.style.flex = "none";
            return el("div", { class: "hbox", style: { gap: "12px", alignItems: "center" } }, [
                el("div", { style: { width: "160px", flex: "none", color: "var(--text-main)", fontSize: "12px" } }, name), edit, change, stretch(),
            ]);
        });
        return sp([heading("GLOBAL HOTKEYS"), fpanel(rows), stretch()]);
    }

    function privacy() {
        S.clipboard = new W.Check("Enable clipboard monitoring", true, (v) => call("apply_setting", "clipboard_monitoring", v));
        S.screen = new W.Check("Allow screen capture (OCR/Vision)", true, (v) => call("apply_setting", "screen_capture_allowed", v));
        S.chatHistory = new W.Check("Save chat history locally", true, (v) => call("apply_setting", "save_chat_history", v));
        S.notifications = new W.Check("Desktop notifications", true, (v) => { W.toasts.enabled = v; });
        S.usageStats = new W.Check("Show my local usage stats (never leaves this device)", !!App.state.caps.usage_stats_enabled, (v) => call("apply_setting", "usage_stats_enabled", v));
        const clear = btn("Clear All Local Data", "danger-pill", async () => {
            if (!(await W.ask("Confirm", "Delete ALL local data? (Chat history, remembered preferences, saved settings, the activity log, and Wave's trash)"))) return;
            const r = await call("clear_all_data");
            if (r && r.ok && r.cleared) await W.info("Done", "All local data cleared. Restart the app.");
            else await W.warn("Partial failure", "Settings were cleared, but the local database couldn't be wiped - check wave_system.log for details.");
        });
        return sp([heading("PRIVACY & DATA"), fpanel([S.clipboard.root, S.screen.root, S.chatHistory.root, S.notifications.root, S.usageStats.root, divider(), clear]), stretch()]);
    }

    function security() {
        S.secMode = new W.Combo(["off", "standard", "strict"], "standard", async (v) => {
            const r = await call("set_security_mode", v);
            if (r && r.ok && r.mode !== v) S.secMode.setValue(r.mode, false);
            refreshSecurityEvents();
        });
        S.redact = new W.Check("Redact secrets (API keys, card numbers, etc.) from logs and diagnostics", !!App.state.caps.redact_secrets, (v) => call("apply_setting", "redact_secrets", v));
        S.events = makeList("events"); S.events.root.style.minHeight = "220px"; S.events.root.style.flex = "1 1 0";
        call("get_security_mode").then((r) => { if (r && r.ok) S.secMode.setValue(r.mode, false); });
        return sp([
            heading("SECURITY"),
            fpanel([el("div", { style: { color: "var(--text-main)", fontSize: "12px" } }, "Security mode:"), S.secMode.root,
                el("div", { class: "wrap", style: { color: "var(--text-muted)", fontSize: "11px", lineHeight: 1.33 } },
                    "off: no extra guard beyond Wave's normal per-tool rules.\nstandard: the default tier x taint policy (recommended).\nstrict: tighter - more things need a spoken \"yes\" before running."),
                divider(), S.redact.root]),
            panel([el("div", { class: "hbox", style: { alignItems: "center" } }, [heading("RECENT SECURITY EVENTS"), stretch(), btn("Refresh", "action", () => refreshSecurityEvents())]), S.events.root],
                { pad: "20px 24px", gap: 10, style: { flex: "1 1 0" } }),
            stretch(),
        ]);
    }
    async function refreshSecurityEvents() {
        if (!S.events) return;
        const r = await call("get_security_log", 50);
        const evs = (r && r.events) || [];
        S.events.set(evs.length ? evs.map((e) => ({ text: `[${e.time}] ${e.kind}${e.tool ? " · " + e.tool : ""} — ${e.reason}` })) : [{ text: "No security events yet." }]);
    }

    function remote() {
        S.remoteEnable = new W.Check("Enable remote dashboard (control Wave from your phone)", false, (v) => onRemoteToggled(v));
        S.remotePort = el("input", { class: "spin", type: "number", min: "1024", max: "65535" });
        S.remotePort.value = "8787";
        S.remoteStatus = el("div", { class: "wrap", style: { color: "var(--text-muted)", fontSize: "11px", lineHeight: 1.33 } }, "Off.");
        S.qr = el("div", { class: "qr-box wrap" }, "Start the dashboard to show a pairing QR code here.");
        S.code = el("div", { style: { textAlign: "center", color: "var(--text-main)", fontSize: "18px", fontWeight: 700, letterSpacing: "2px" } }, "");
        S.url = el("div", { style: { textAlign: "center", color: "var(--text-muted)", fontSize: "11px", userSelect: "text" } }, "");
        S.regen = btn("Regenerate pairing code (signs out every paired phone)", "action", async () => { await call("regenerate_remote_pairing"); refreshPairing(); });
        S.regen.disabled = true;
        return sp([
            heading("REMOTE DASHBOARD"),
            fpanel([S.remoteEnable.root,
                el("div", { class: "wrap", style: { color: "var(--warning)", fontSize: "11px", lineHeight: 1.33 } },
                    "While this is on, Wave is reachable from any device on your Wi-Fi/LAN - not just your phone. Never forward this port through your router to the internet."),
                divider(),
                el("div", { class: "hbox", style: { gap: "6px", alignItems: "center" } }, [el("div", { style: { color: "var(--text-main)", fontSize: "12px" } }, "Port:"), S.remotePort, stretch()]),
                S.remoteStatus]),
            fpanel([heading("PAIR A PHONE"), S.qr, S.code, S.url, S.regen], 10),
            stretch(),
        ]);
    }
    async function onRemoteToggled(on) {
        if (on) {
            const r = await call("start_remote_dashboard", Number(S.remotePort.value) || 8787);
            const ok = !!(r && r.ok && r.started);
            S.remoteStatus.textContent = (r && (r.message || r.error)) || "";
            if (!ok) { S.remoteEnable.set(false, false); S.regen.disabled = true; return; }
            S.regen.disabled = false; refreshPairing();
        } else {
            await call("stop_remote_dashboard");
            S.remoteStatus.textContent = "Off.";
            S.qr.innerHTML = ""; S.qr.textContent = "Start the dashboard to show a pairing QR code here.";
            S.code.textContent = ""; S.url.textContent = ""; S.regen.disabled = true;
        }
    }
    async function refreshPairing() {
        const r = await call("remote_pairing_info");
        if (!r || !r.ok || !r.running) return;
        S.code.textContent = r.code; S.url.textContent = r.url;
        S.qr.innerHTML = "";
        if (r.qr) S.qr.appendChild(el("img", { src: r.qr, alt: "pairing QR code" }));
        else S.qr.textContent = "Install 'qrcode[pil]' to show a scannable code - for now, open the URL above on your phone's browser instead.";
    }

    function advanced() {
        const log = new W.Combo(["DEBUG", "INFO", "WARNING", "ERROR"], "INFO");
        S.providerLbl = el("div", { class: "lbl wrap" }, "");
        S.providerStatus = el("div", { class: "lbl wrap" }, "");
        const test = btn("Test connection", "action", async () => {
            test.disabled = true; S.providerStatus.textContent = "Testing...";
            const r = await call("check_provider");
            test.disabled = false;
            if (r && r.ok) S.providerStatus.textContent = `${r.provider_ok ? "OK" : "Problem"}: ${r.message}` + (r.provider_ok ? ` (${r.ms} ms)` : "");
            else S.providerStatus.textContent = `Problem: ${(r && r.error) || ""}`;
        });
        call("provider_info").then((r) => { if (r && r.ok) S.providerLbl.textContent = r.text; });
        return sp([heading("ADVANCED"), fpanel([formLbl("Log Level:"), log.root, divider(), formLbl("AI provider (switch it with LLM_PROVIDER in .env):"),
            S.providerLbl, test, S.providerStatus, divider(), btn("Reset UI Layout", "action")]), stretch()]);
    }

    function about() {
        const b = App.state.bootstrap || {};
        const box = el("div", { class: "about-html" });
        const fill = (brief) => {
            box.innerHTML = "";
            box.append(
                el("h2", {}, b.assistant_name || "Wave AI"),
                el("p", { style: { color: "var(--text-muted)" } }, `Version ${b.version || ""}`),
                el("hr"),
                el("p", { style: { color: "var(--text-main)" } }, "Your intelligent desktop companion — voice-controlled, always ready to help."),
                el("p", { style: { color: "var(--text-muted)", fontSize: "12px" }, html: `Voice: Windows SAPI (TTS) + Google Speech Recognition (STT)<br>Brain: ${String(brief).replace(/[<>&]/g, "")}<br>Vision: Tesseract OCR<br>UI: PyQt6 + qtawesome` }),
                el("hr"),
                el("p", { style: { color: "var(--text-faint)", fontSize: "11px" } }, "Built with ❤️ using Python & Qt"),
            );
        };
        fill(b.provider_brief || "");
        const links = el("div", { class: "hbox", style: { gap: "12px" } }, [["GitHub", "https://github.com"], ["Issues", "https://github.com/issues"], ["Docs", "https://docs.example.com"]]
            .map(([t, u]) => btn(t, "action", () => call("open_url", u))).concat([stretch()]));
        return sp([heading("ABOUT WAVE"), fpanel([box, links]), stretch()]);
    }

    // ---- save / load (MainWindow.save_settings / load_settings) -----------
    App.saveSettings = async function () {
        const data = {
            notes: S.sticky ? S.sticky.value : "",
            focus_mode: App.state.focusMode,
            clipboard_monitoring: S.clipboard.checked, screen_capture_allowed: S.screen.checked,
            save_chat_history: S.chatHistory.checked, notifications_enabled: S.notifications.checked,
            usage_stats_enabled: S.usageStats.checked, security_mode: S.secMode.value, redact_secrets: S.redact.checked,
            tts_voice: voiceId(S.tts.value) || CLONE_ID, tts_rate: S.rate.value, tts_volume: S.ttsVol.value,
            wake_word_label: S.wake.value, wake_phrases: App.state.wakePhrases, stt_lang: S.stt.value,
            wake_engine: S.wakeEngine.value, tts_local_auto_download: S.localVoice.checked,
            remote_dashboard_enabled: S.remoteEnable.checked, remote_dashboard_port: Number(S.remotePort.value) || 8787,
            avatar_show_on_startup: S.avatarShow.checked, avatar_show_dashboard_on_startup: S.avatarDash.checked,
        };
        const vol = await call("get_volume_percent");
        if (vol && vol.ok && vol.percent !== null && vol.percent !== undefined) data.volume = vol.percent;
        if (S.wakeTouched) data.wake_sensitivity = S.wakeSens.value;
        const r = await call("save_settings", data);
        if (r && r.ok) toast("Settings saved", "success"); else toast("Could not save settings", "error");
    };
    App.loadSettings = async function (silent) {
        await loadVoiceValues();
        const r = await call("get_settings_info");
        if (!r || !r.ok) { if (!silent) toast("Could not load settings", "error"); return; }
        const d = r.settings || {};
        if (!r.exists) { if (!silent) toast("No saved settings found", "info"); return; }
        if (S.sticky) S.sticky.value = d.notes || "";
        if ("volume" in d) call("set_volume_percent", d.volume);
        S.clipboard.set(d.clipboard_monitoring !== undefined ? d.clipboard_monitoring : true, true);
        S.screen.set(d.screen_capture_allowed !== undefined ? d.screen_capture_allowed : true, true);
        S.chatHistory.set(d.save_chat_history !== undefined ? d.save_chat_history : true, true);
        S.notifications.set(d.notifications_enabled !== undefined ? d.notifications_enabled : true, true);
        S.usageStats.set(!!d.usage_stats_enabled, true);
        if (d.security_mode) S.secMode.setValue(d.security_mode, true);
        if ("redact_secrets" in d) S.redact.set(d.redact_secrets, true);
        if (d.tts_voice) {
            if (TTS_VOICES[d.tts_voice]) {
                // Saved before Tier 33: the label of the voice that fills in
                // for the cloned voice (the picker had no cloned voice then).
                call("apply_setting", "tts_fallback_voice", TTS_VOICES[d.tts_voice]).then(loadVoiceValues);
            } else if (voiceLabel(d.tts_voice)) {
                S.tts.setValue(voiceLabel(d.tts_voice), true);
            }
        }
        if ("tts_rate" in d) S.rate.set(d.tts_rate, true);
        if ("tts_volume" in d) S.ttsVol.set(d.tts_volume, true);
        if (d.stt_lang) S.stt.setValue(d.stt_lang, true);
        if (d.wake_phrases) { App.state.wakePhrases = d.wake_phrases; call("apply_setting", "wake_phrases", d.wake_phrases); showWakeWords(); }
        if (d.wake_word_label) S.wake.setValue(d.wake_word_label, false);
        if (d.wake_engine) S.wakeEngine.setValue(d.wake_engine, true);
        if ("wake_sensitivity" in d) S.wakeSens.set(Number(d.wake_sensitivity), true);
        refreshWakeStatus();
        if ("tts_local_auto_download" in d) S.localVoice.set(d.tts_local_auto_download, true);
        if ("remote_dashboard_port" in d) S.remotePort.value = String(d.remote_dashboard_port);
        if (d.remote_dashboard_enabled) S.remoteEnable.set(true, true);
        S.avatarShow.set(d.avatar_show_on_startup !== undefined ? d.avatar_show_on_startup : true, false);
        S.avatarDash.set(!!d.avatar_show_dashboard_on_startup, false);
        if (!silent) toast("Settings loaded", "success");
    };

    // ========================================================= ONBOARDING
    App.showOnboarding = function () {
        const steps = [
            ["Welcome", "Hi! I'm WAVE — your desktop AI assistant.\nLet's get you set up in 30 seconds.", "👋"],
            ["AI Brain", "Before anything else, let's confirm your AI\nprovider is configured and actually reachable.", "🧠"],
            ["Microphone", "We need microphone access for voice commands.\nPick your devices and test them.", "🎙"],
            ["Wake Word", "Choose your wake phrase:", "🔊"],
            ["Theme", "Pick your accent color:", "🎨"],
            ["Permissions", "Allow these for the best experience:", "🛡"],
            ["Wake-Word Calibration", "Let's measure your room's background\nnoise so wake-word detection is accurate.", "📏"],
            ["Language", "What language will you mostly speak to me in?", "🌐"],
            ["Local Voice", "Optional: let WAVE speak offline\nusing a local voice (Piper).", "💾"],
            ["Calendar", "Optional: connect an ICS calendar feed\n(you can always add this later in Settings).", "📅"],
            ["Done", "You're all set! Press <b>Ctrl+Shift+Space</b> anytime to wake me.", "✨"],
        ];
        const ch = { wake: "Hey Wave", clipboard: true, screen: true, notifications: true, lang: "en-IN", localVoice: false, calName: "", calUrl: "" };
        const dots = el("div", { class: "dots" }, steps.map(() => el("span", {}, "●")));
        const ic = el("div", { class: "w-icon emoji" }), title = el("div", { class: "w-title" }), desc = el("div", { class: "w-desc" });
        const content = el("div", { class: "w-content" });
        const back = btn("Back", "action", () => show(cur - 1));
        const next = el("button", { class: "btn save" }, "Next");
        next.addEventListener("click", () => (cur < steps.length - 1 ? show(cur + 1) : finish()));
        const box = el("div", { class: "panel wizard", style: { filter: "drop-shadow(0 20px 20px rgba(0,0,0,0.706))" } },
            [dots, ic, title, desc, content, el("div", { class: "w-buttons" }, [back, next])]);
        const overlay = el("div", { class: "overlay" }, box);
        document.body.appendChild(overlay);
        let cur = 0;
        const status = (text, color) => { const s = el("div", { class: "w-status wrap", style: { color: color || "var(--text-muted)" } }, text); return s; };
        async function show(i) {
            cur = i; const [t, d, e] = steps[i];
            ic.textContent = e; title.textContent = t; desc.innerHTML = d.replace(/\n/g, "<br>").replace(/<(?!\/?b>|br>)/g, "&lt;");
            Array.from(dots.children).forEach((n, k) => n.classList.toggle("on", k <= i));
            content.innerHTML = "";
            back.style.visibility = i > 0 ? "visible" : "hidden";
            next.textContent = i === steps.length - 1 ? "Finish" : "Next";
            if (i === 1) {
                const r = await call("provider_info");
                const info = (r && r.info) || {};
                const st = status(info.configured === false ? `⚠ Not usable yet: ${info.problem}` : "Click to verify it's actually reachable...",
                    info.configured === false ? "var(--warning)" : undefined);
                const test = btn("🧠  Test Connection", "action", async () => {
                    test.disabled = true; st.textContent = "Testing..."; st.style.color = "var(--text-muted)";
                    const c = await call("check_provider"); test.disabled = false;
                    const ok = c && c.ok && c.provider_ok;
                    st.textContent = `${ok ? "✓" : "✗"} ${(c && (c.message || c.error)) || ""}` + (ok ? ` (${c.ms} ms)` : "");
                    st.style.color = ok ? "var(--success)" : "var(--danger)";
                });
                content.append(el("div", { class: "w-status", style: { color: "var(--text-faint)", fontSize: "11px" } }, (r && r.text) || ""), test, st);
            } else if (i === 2) {
                const d2 = await call("list_audio_devices");
                const ins = (d2 && d2.inputs) || [], outs = (d2 && d2.outputs) || [];
                const inC = new W.Combo(ins.length ? ins.map((x) => x[1]) : ["No microphone found"]); if (!ins.length) inC.setEnabled(false);
                const outC = new W.Combo(outs.length ? outs.map((x) => x[1]) : ["No speaker found"]); if (!outs.length) outC.setEnabled(false);
                const ms = status("Click to test..."), ss = status("");
                const idxOf = (combo, list) => { const k = combo.items.indexOf(combo.value); return k >= 0 && k < list.length ? list[k][0] : null; };
                content.append(formLbl("Microphone:"), inC.root, btn("🎙  Test Microphone", "action", async () => {
                    ms.textContent = "Listening... speak now"; ms.style.color = "var(--accent-1)";
                    const r = await call("measure_input_level", idxOf(inC, ins), 1.5);
                    const level = (r && r.ok && r.level) || 0;
                    if (level <= 0.5) { ms.textContent = "⚠ Heard nothing - check the mic is selected and unmuted."; ms.style.color = "var(--warning)"; }
                    else { ms.textContent = `✓ Microphone working! (peak level ${level.toFixed(0)}%)`; ms.style.color = "var(--success)"; }
                }), ms, formLbl("Speaker:"), outC.root, btn("🔊  Play Test Sound", "action", async () => {
                    ss.textContent = "Playing..."; ss.style.color = "var(--accent-1)";
                    const r = await call("play_test_tone", idxOf(outC, outs));
                    if (r && r.ok && r.played) { ss.textContent = "✓ Did you hear that?"; ss.style.color = "var(--success)"; }
                    else { ss.textContent = "⚠ Couldn't play - check the speaker is selected."; ss.style.color = "var(--warning)"; }
                }), ss);
            } else if (i === 3) {
                const c = new W.Combo(["Hey Wave", "Hello Wave", "Okay Wave", "Custom..."], ch.wake, (v) => { ch.wake = v; });
                c.root.style.padding = "10px"; c.root.style.fontSize = "13px";
                content.append(c.root);
            } else if (i === 4) {
                const pills = ["Black Glass", "Midnight Blue", "Emerald Dark", "Crimson Dark", "Amber Dark"].map((n) => {
                    const p = btn(n, "pill", () => { pills.forEach((x) => x.classList.toggle("active", x === p)); App.setTheme(n); });
                    if (WaveTheme.current().name === n) p.classList.add("active");
                    return p;
                });
                content.append(...pills);
            } else if (i === 5) {
                const a = new W.Check("Clipboard history (read copied text)", ch.clipboard, (v) => { ch.clipboard = v; }, "tight");
                const b = new W.Check("Screen capture (for OCR / vision)", ch.screen, (v) => { ch.screen = v; }, "tight");
                const c = new W.Check("Desktop notifications", ch.notifications, (v) => { ch.notifications = v; }, "tight");
                content.append(a.root, b.root, c.root);
            } else if (i === 6) {
                const st = status("Stay quiet for a second, then click Calibrate.");
                const cb = btn("📏  Calibrate Now", "action", async () => {
                    cb.disabled = true; st.textContent = "Calibrating..."; st.style.color = "var(--text-muted)";
                    const r = await call("recalibrate_mic"); cb.disabled = false;
                    if (!r || !r.ok || !r.result) { st.textContent = "⚠ Calibration failed - you can retry later in Settings."; st.style.color = "var(--warning)"; return; }
                    st.textContent = `✓ Calibrated. Ambient level ${r.result.baseline_energy.toFixed(0)} -> idle threshold ${r.result.idle_threshold.toFixed(0)}.`;
                    st.style.color = "var(--success)";
                });
                content.append(cb, st);
            } else if (i === 7) {
                const c = new W.Combo(STT_LANGS, ch.lang, (v) => { ch.lang = v; });
                content.append(c.root);
            } else if (i === 8) {
                const cb = new W.Check("Download a local voice for offline speech (via Piper)", ch.localVoice, (v) => { ch.localVoice = v; }, "tight");
                content.append(cb.root, el("div", { class: "wrap", style: { color: "var(--text-muted)", fontSize: "11.5px" } },
                    App.state.caps.piper ? "You can turn this on now, or later in Settings > Voice." : "piper-tts isn't installed - this stays off until it is (`pip install piper-tts`)."));
            } else if (i === 9) {
                const n = lineEdit("Feed name (e.g. 'work')"); n.value = ch.calName; n.addEventListener("input", () => { ch.calName = n.value; });
                const u = lineEdit("ICS feed URL (leave blank to skip)"); u.value = ch.calUrl; u.addEventListener("input", () => { ch.calUrl = u.value; });
                content.append(n, u);
            }
        }
        async function finish() {
            if (ch.wake === "Custom...") {
                let phrases = (await askCustomWakeWords()) || ["hey wave"];
                if (!phrases.includes("wake up")) phrases = phrases.concat(["wake up"]);
                App.state.wakePhrases = phrases; await call("apply_setting", "wake_phrases", phrases); refreshWakeStatus();
            } else if (WAKE_PRESETS[ch.wake]) { S.wake.setValue(ch.wake, true); }
            S.clipboard.set(ch.clipboard, true); S.screen.set(ch.screen, true); S.notifications.set(ch.notifications, true);
            S.stt.setValue(ch.lang, true); S.localVoice.set(ch.localVoice, true);
            await App.saveSettings();
            if (ch.calName.trim() && ch.calUrl.trim()) call("add_ics_feed", ch.calName.trim(), ch.calUrl.trim());
            overlay.remove();
            call("turn_on_mic");
            setTimeout(() => App.refreshDashboardPreviews(), 700);
            toast("Welcome to WAVE! Press Ctrl+Shift+Space to toggle mic.", "success");
        }
        show(0);
    };
})();
