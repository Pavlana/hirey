// getjo dashboard enhancements: sortable tables, filtering, forms without a reload,
// toasts with undo, the Applications panels (application or job offer) and their refresh. Every
// page and form works without this file; it only adds to them.
// No external requests: fetch goes to this page's own origin only (relative URLs).
"use strict";

(() => {
  // --- remembered view ------------------------------------------------------------
  // The Applications page's left column shows the pipeline or New offers (?view=). A bare "/"
  // (the nav link, a bookmark) opens the view last picked with the switch in this browser. Any
  // other URL says what to show (a message's job link, a filter), so it is never redirected.
  // The jobs ticked for the Applications Assistant box are in the URL (?sel=a:3&sel=j:…&sel=cv) and kept here too, so a
  // bare "/" brings them back with the view.
  const VIEW_KEY = "apps-view";
  const SEL_KEY = "apps-sel";
  if (location.pathname === "/" && !location.search && document.querySelector(".view-switch")) {
    let view = null;
    let sel = [];
    try {
      view = localStorage.getItem(VIEW_KEY);
      sel = JSON.parse(localStorage.getItem(SEL_KEY) || "[]");
    } catch { /* storage blocked: the default view, nothing ticked */ }
    const query = new URLSearchParams();
    if (view === "offers") query.set("view", "offers");
    if (Array.isArray(sel)) sel.slice(0, 4).forEach((k) => { if (typeof k === "string") query.append("sel", k); });
    if (query.toString()) { location.replace(`/?${query}`); return; }
  }
  document.addEventListener("click", (e) => {
    const tab = e.target.closest(".view-switch a[data-view]");
    if (tab) try { localStorage.setItem(VIEW_KEY, tab.dataset.view); } catch { /* not remembered */ }
  });
  document.documentElement.classList.add("has-js");

  const toasts = document.querySelector(".toasts");

  // --- Copy (a draft reply: its body, and an email's subject) ------------------------
  // Copies the named element's text exactly as shown; nothing is sent anywhere.
  document.addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-copy]");
    if (!btn) return;
    const el = document.getElementById(btn.dataset.copy);
    if (!el) return;
    const text = el.textContent;
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const area = document.createElement("textarea"); // no clipboard API (plain http on another host)
      area.value = text;
      document.body.append(area);
      area.select();
      document.execCommand("copy");
      area.remove();
    }
    const label = btn.textContent;
    btn.textContent = "Copied";
    setTimeout(() => { btn.textContent = label; }, 1500);
  });

  // --- toasts -------------------------------------------------------------------
  // Text is always set with textContent: server messages can echo form input.
  function toast(message, { error = false, action = null, ms = 5000 } = {}) {
    if (!toasts) return;
    const el = document.createElement("div");
    el.className = "toast" + (error ? " toast-error" : "");
    el.setAttribute("role", error ? "alert" : "status");
    const text = document.createElement("span");
    text.textContent = message;
    el.append(text);
    const close = () => {
      el.classList.add("is-leaving");
      setTimeout(() => el.remove(), 200);
    };
    if (action) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = action.label;
      btn.addEventListener("click", () => { close(); action.run(); });
      el.append(btn);
    }
    toasts.append(el);
    setTimeout(close, action ? ms * 1.6 : ms);
  }

  // A server-rendered notice (after a no-JS style redirect) becomes a toast.
  const flash = document.querySelector("[data-flash]");
  if (flash) {
    toast(flash.textContent);
    flash.remove();
    const url = new URL(location.href);
    url.searchParams.delete("msg"); // keep the rest of the query (filters, ?app=)
    history.replaceState(null, "", url);
  }

  // --- sortable tables ------------------------------------------------------------
  function cellValue(row, index, kind) {
    const text = (row.cells[index]?.textContent || "").trim();
    if (!text || text === "–") return null; // empty sorts last in both directions
    if (kind === "num") {
      const n = parseFloat(text);
      return Number.isNaN(n) ? null : n;
    }
    return kind === "date" ? text : text.toLowerCase();
  }

  function sortBy(table, th) {
    const index = th.cellIndex;
    const kind = th.dataset.sort;
    const dir = th.getAttribute("aria-sort") === "ascending" ? "descending" : "ascending";
    table.querySelectorAll("th[aria-sort]").forEach((other) => other.removeAttribute("aria-sort"));
    th.setAttribute("aria-sort", dir);
    const sign = dir === "ascending" ? 1 : -1;
    const body = table.tBodies[0];
    const rows = Array.from(body.rows).filter((r) => !r.classList.contains("no-results"));
    rows.sort((a, b) => {
      const x = cellValue(a, index, kind);
      const y = cellValue(b, index, kind);
      if (x === null || y === null) return (x === null) - (y === null);
      return (x < y ? -1 : x > y ? 1 : 0) * sign;
    });
    body.append(...rows);
  }

  // Also run on the part of the Applications page that refreshApps swaps in.
  function makeSortable(root) {
    root.querySelectorAll("th[data-sort]").forEach((th) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "sort";
      btn.textContent = th.textContent;
      btn.setAttribute("aria-label", `Sort by ${th.textContent}`);
      th.textContent = "";
      th.append(btn);
      btn.addEventListener("click", () => sortBy(th.closest("table"), th));
    });
  }
  makeSortable(document);

  // --- filtering --------------------------------------------------------------------
  // Matches against the visible cell text, skipping cells that hold a form.
  function rowText(row) {
    return Array.from(row.cells)
      .filter((td) => !td.querySelector("form"))
      .map((td) => td.textContent)
      .join(" ")
      .toLowerCase();
  }

  function applyFilter(input) {
    const table = document.getElementById(input.dataset.filter);
    if (!table) return;
    const query = input.value.trim().toLowerCase();
    const body = table.tBodies[0];
    let shown = 0;
    for (const row of body.rows) {
      if (row.classList.contains("no-results")) continue;
      row.hidden = query !== "" && !rowText(row).includes(query);
      if (!row.hidden) shown += 1;
    }
    let none = body.querySelector("tr.no-results");
    if (shown === 0 && !none) {
      none = body.insertRow();
      none.className = "no-results";
      const td = none.insertCell();
      td.colSpan = table.tHead.rows[0].cells.length;
    }
    if (none) {
      none.hidden = shown !== 0;
      none.cells[0].textContent = `Nothing matches “${input.value.trim()}”.`;
    }
  }

  const filters = document.querySelectorAll("input[data-filter]");
  filters.forEach((input) => {
    if (!document.getElementById(input.dataset.filter)) return; // empty state: nothing to filter
    input.hidden = false;
    input.addEventListener("input", () => applyFilter(input));
    input.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { input.value = ""; applyFilter(input); }
    });
  });

  // "/" focuses the first filter box (New offers' search on Applications), as in many web apps.
  document.addEventListener("keydown", (e) => {
    if (e.key !== "/" || e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.target.closest("input, select, textarea, [contenteditable]")) return;
    const input = document.querySelector('form[data-live-filters] input[type="search"]')
      || Array.from(filters).find((f) => !f.hidden);
    if (input) { e.preventDefault(); input.focus(); }
  });

  // --- New offers filters ------------------------------------------------------------
  // The filter bar is a GET form, so every view has a URL. Selects and checkboxes reload with the
  // new query; the search narrows the loaded rows (company or role) as you type and keeps the URL
  // (and the forms' return fields) in step. Widening a search needs rows that were not loaded, so
  // it reloads after a pause and puts the cursor back in the box. Listeners sit on the document:
  // the list is swapped in again after each change (refreshApps).
  const FOCUS_KEY = "offers-focus";
  let searchTimer = null;
  const liveForm = (el) => el.closest?.("form[data-live-filters]");
  const submitForm = (form) => (form.requestSubmit ? form.requestSubmit() : form.submit());
  document.addEventListener("change", (e) => {
    const form = liveForm(e.target);
    if (form && e.target.type !== "search") submitForm(form);
  });
  document.addEventListener("input", (e) => {
    const form = liveForm(e.target);
    if (!form || e.target.name !== "q") return;
    const q = e.target.value.trim().toLowerCase();
    clearTimeout(searchTimer);
    // The rows on the page were loaded for the search the server rendered (the box's defaultValue).
    if (!q.includes(e.target.defaultValue.trim().toLowerCase())) { // wider than that: ask the server
      searchTimer = setTimeout(() => {
        try { sessionStorage.setItem(FOCUS_KEY, "q"); } catch { /* storage blocked: lose focus only */ }
        submitForm(form);
      }, 500);
      return;
    }
    document.querySelectorAll("#offer-list tbody tr").forEach((row) => {
      row.hidden = q !== "" && !(row.dataset.company || "").includes(q) && !(row.dataset.role || "").includes(q);
    });
    const url = new URL(location.href);
    url.searchParams.delete("msg");
    if (e.target.value.trim()) url.searchParams.set("q", e.target.value.trim()); else url.searchParams.delete("q");
    history.replaceState(null, "", url);
    document.querySelectorAll('input[name="return"]').forEach((i) => { i.value = url.pathname + url.search; });
  });
  let refocus = null;
  try { refocus = sessionStorage.getItem(FOCUS_KEY); sessionStorage.removeItem(FOCUS_KEY); } catch { /* none */ }
  const box = refocus && document.querySelector('form[data-live-filters] input[name="q"]');
  if (box) { box.focus(); box.setSelectionRange(box.value.length, box.value.length); }

  // --- Applications Assistant box --------------------------------------------------------------------------
  // One conversation under the pipeline list. While a message waits for the agent, or the open
  // panel's application has queued agent work (research, CV fit, tailored CV: data-busy), poll
  // /applications/thread every few seconds, then (after a few minutes) every half minute until it is
  // done or Stop is pressed: a slow reply still shows. When it is done, refresh the list, the panel
  // and the conversation: the agent may have moved the stage, added tasks or comments. Only one poll
  // runs; opening another panel or closing it restarts it.
  const POLL_MS = 4000;
  const POLL_FOR_MS = 5 * 60 * 1000;
  const SLOW_POLL_MS = 30 * 1000;
  const CHAT_KEY = "agent-filter";
  let poll = null;
  let refreshApps = () => location.reload(); // the Applications block below swaps the parts instead
  let showPanelAt = (url) => { location.href = url; return Promise.resolve(); }; // likewise, without a reload
  let chatFilter = "all";
  try { chatFilter = localStorage.getItem(CHAT_KEY) === "sel" ? "sel" : "all"; } catch { /* default */ }

  // --- ticked jobs (sel) ---------------------------------------------------------------
  const MAX_SEL = 4;
  const currentSel = () => new URL(location.href).searchParams.getAll("sel");
  // `url` with the ticked jobs of this page (or `sel`) instead of its own.
  function withSel(url, sel = currentSel()) {
    const u = new URL(url, location.href);
    u.searchParams.delete("sel");
    sel.forEach((k) => u.searchParams.append("sel", k));
    return u.pathname + u.search;
  }
  // What a panel URL opens, as a tick key: an application, a job offer or My CV.
  function keyOf(url) {
    const q = new URL(url, location.href).searchParams;
    if (q.get("cv") === "1") return "cv";
    if (q.get("app")) return `a:${q.get("app")}`;
    if (q.get("job")) return `j:${q.get("job")}`;
    return null;
  }
  function rememberSel(sel) {
    try { localStorage.setItem(SEL_KEY, JSON.stringify(sel)); } catch { /* not remembered */ }
  }
  // The server names the ticked jobs as they are now (a job marked Interested since is its
  // application; unknown ids are dropped): its About line is the truth, so the URL follows it.
  function adoptServerSel() {
    const about = document.querySelector("#agent .agent-about");
    if (!about) return;
    const keys = Array.from(about.querySelectorAll('input[name="about"]'), (i) => i.value);
    const now = currentSel();
    if (keys.length !== now.length || keys.some((k, i) => k !== now[i])) {
      history.replaceState(null, "", withSel(location.href, keys));
    }
  }
  adoptServerSel();

  function stopPoll() {
    if (poll) clearTimeout(poll.timer);
    poll = null;
  }

  // All, or Selected: the messages about any ticked job (a message lists its targets in
  // data-targets; a comparison has several). The switch shows while something is ticked.
  function applyChatFilter() {
    const box = document.getElementById("agent");
    if (!box) return;
    const sel = new Set(currentSel());
    const only = chatFilter === "sel" && sel.size > 0;
    box.querySelector(".agent-filter").hidden = sel.size === 0;
    box.querySelectorAll("[data-filter-chat]").forEach((b) => b.classList.toggle("is-active", b.dataset.filterChat === (only ? "sel" : "all")));
    box.querySelectorAll("li.msg").forEach((li) => {
      li.hidden = only && !(li.dataset.targets || "").split(" ").some((k) => sel.has(k));
    });
    const msgs = box.querySelector(".msgs");
    if (msgs) msgs.scrollTop = msgs.scrollHeight; // the newest at the bottom, in view
  }
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-filter-chat]");
    if (!btn) return;
    chatFilter = btn.dataset.filterChat;
    try { localStorage.setItem(CHAT_KEY, chatFilter); } catch { /* not remembered */ }
    applyChatFilter();
  });

  function watchThread() {
    stopPoll();
    const thread = document.querySelector("#agent [data-thread]");
    const panel = document.getElementById("panel");
    if (!thread || !window.fetch) return;
    if (!thread.hasAttribute("data-pending") && !panel?.hasAttribute("data-busy")) return;
    const me = { timer: null, started: Date.now() };
    const app = panel?.dataset.app;
    const job = panel?.dataset.job;
    const query = app ? `?app=${encodeURIComponent(app)}` : job ? `?job=${encodeURIComponent(job)}` : "";
    const before = thread.querySelectorAll(".msg").length;
    poll = me;
    const tick = async () => {
      if (poll !== me) return;
      try {
        const res = await fetch(`/applications/thread${query}`, {
          headers: { Accept: "application/json" }, credentials: "same-origin",
        });
        const reply = await res.json();
        if (poll !== me) return; // the panel changed while waiting
        showResend(reply.failed);
        if (reply.ok && !reply.pending) {
          poll = null;
          const replied = reply.messages !== before;
          await refreshApps();
          toast(replied ? "The Applications Assistant replied." : "The assistant's work is in.");
          return;
        }
      } catch { /* the server restarted or is busy: try again */ }
      const slow = Date.now() - me.started > POLL_FOR_MS;
      if (slow && !me.slow) {
        me.slow = true; // the text only: Stop stays on the line
        const text = document.querySelector("#agent .thread-state > span:not(.spinner)");
        if (text) text.textContent = "Still waiting. The reply shows here when it comes (checked every 30 s); Stop cancels it.";
      }
      me.timer = setTimeout(tick, slow ? SLOW_POLL_MS : POLL_MS);
    };
    me.timer = setTimeout(tick, POLL_MS);
  }
  applyChatFilter();
  watchThread();

  // A job description without a brief is folded to a few lines: Show all opens it.
  function setupClamp(root) {
    root.querySelectorAll(".description[data-clamp]").forEach((el) => {
      if (el.nextElementSibling?.classList.contains("show-all") || el.scrollHeight <= el.clientHeight + 4) return;
      el.classList.add("is-folded"); // only text that is cut off fades out
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn btn-ghost btn-sm show-all";
      btn.textContent = "Show all";
      btn.setAttribute("aria-expanded", "false");
      btn.addEventListener("click", () => {
        const open = el.classList.toggle("is-open");
        btn.textContent = open ? "Show less" : "Show all";
        btn.setAttribute("aria-expanded", String(open));
      });
      el.after(btn);
    });
  }
  setupClamp(document);

  // Cmd/Ctrl+Enter sends the message.
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" || !(e.metaKey || e.ctrlKey)) return;
    const form = e.target.closest?.("form.ask");
    if (!form) return;
    e.preventDefault();
    form.requestSubmit ? form.requestSubmit() : form.submit();
  });

  // --- split between the left column and the panel -------------------------------------------
  // The divider (.apps-split, a window splitter) moves the boundary: drag it, arrow keys (Shift for
  // bigger steps), Home / End to the limits, double-click or Enter for half and half. The left
  // column's share is kept per browser; storage may be blocked, then it is 50/50 on every load.
  // The share goes into --split-l / --split-r as fr, so the divider's own width comes out of the
  // rest. Both sides keep a minimum width; where both cannot fit the split stays at half and half.
  // CSS shows the divider only from 801px wide while a panel is open. The Learn page's card view has
  // the same divider; each one names its own storage key (data-split-key) and its two sides
  // (data-split-names, for the screen reader text).
  document.querySelectorAll(".apps-split[data-split-key]").forEach((split) => {
    const SPLIT_KEY = split.dataset.splitKey;
    const [LEFT_NAME, RIGHT_NAME] = (split.dataset.splitNames || "List,panel").split(",");
    const MIN_LEFT = 360;
    const MIN_PANEL = 420;
    const container = split.parentElement;
    let share = 0.5; // the left column's share, as picked (the shown one may be clamped)
    try {
      const saved = Number(localStorage.getItem(SPLIT_KEY));
      if (saved > 0.05 && saved < 0.95) share = saved;
    } catch { /* not remembered: half and half */ }

    const room = () => container.clientWidth - (split.offsetWidth || 16);
    function limits() {
      const width = room();
      if (width < MIN_LEFT + MIN_PANEL) return [0.5, 0.5];
      return [MIN_LEFT / width, 1 - MIN_PANEL / width];
    }
    function paint() {
      const [lo, hi] = limits();
      const shown = Math.min(hi, Math.max(lo, share));
      container.style.setProperty("--split-l", `${(shown * 100).toFixed(2)}fr`);
      container.style.setProperty("--split-r", `${((1 - shown) * 100).toFixed(2)}fr`);
      const pct = Math.round(shown * 100);
      split.setAttribute("aria-valuenow", String(pct));
      split.setAttribute("aria-valuemin", String(Math.ceil(lo * 100)));
      split.setAttribute("aria-valuemax", String(Math.floor(hi * 100)));
      split.setAttribute("aria-valuetext", `${LEFT_NAME} ${pct}%, ${RIGHT_NAME} ${100 - pct}%`);
      split.classList.toggle("is-fixed", lo === hi);
      return shown;
    }
    function save() {
      try {
        if (Math.abs(share - 0.5) < 0.005) localStorage.removeItem(SPLIT_KEY);
        else localStorage.setItem(SPLIT_KEY, share.toFixed(4));
      } catch { /* not remembered */ }
    }
    function setShare(next, { keep = true } = {}) {
      share = next;
      share = paint(); // what is shown is what is kept (within the limits)
      if (keep) save();
    }

    split.addEventListener("pointerdown", (e) => {
      if (e.button !== 0 || split.classList.contains("is-fixed")) return;
      e.preventDefault(); // no text selection while dragging
      split.focus({ preventScroll: true }); // then the arrow keys go on from here
      split.setPointerCapture(e.pointerId);
      split.classList.add("is-dragging");
      document.body.classList.add("is-resizing");
    });
    split.addEventListener("pointermove", (e) => {
      if (!split.hasPointerCapture(e.pointerId)) return;
      const box = container.getBoundingClientRect();
      setShare((e.clientX - box.left - split.offsetWidth / 2) / room(), { keep: false });
    });
    const endDrag = (e) => {
      if (!split.hasPointerCapture(e.pointerId)) return;
      split.releasePointerCapture(e.pointerId);
      split.classList.remove("is-dragging");
      document.body.classList.remove("is-resizing");
      save();
    };
    split.addEventListener("pointerup", endDrag);
    split.addEventListener("pointercancel", endDrag);
    // Where both sides cannot fit their minimum the split is fixed; the share picked on a wider
    // window is left alone.
    split.addEventListener("dblclick", () => { if (!split.classList.contains("is-fixed")) setShare(0.5); });
    split.addEventListener("keydown", (e) => {
      if (e.metaKey || e.ctrlKey || e.altKey || split.classList.contains("is-fixed")) return;
      const step = e.shiftKey ? 0.1 : 0.02;
      const [lo, hi] = limits();
      const next = { ArrowLeft: share - step, ArrowRight: share + step, Home: lo, End: hi, Enter: 0.5 }[e.key];
      if (next === undefined) return;
      e.preventDefault();
      e.stopPropagation();
      setShare(next);
    });
    // A narrower window may push the shown split into a limit; the picked share is kept.
    let resizeFrame = 0;
    window.addEventListener("resize", () => {
      cancelAnimationFrame(resizeFrame);
      resizeFrame = requestAnimationFrame(paint);
    });
    paint();
  });

  // --- Learn: the Notes editor and Your answer --------------------------------------------------
  // Both are forms posting to /learn/notes (prep/learnnotes.py files), saved by autosaving(): 1.5 s
  // after the last change, on blur and on Cmd/Ctrl-S (or :w with vim keys). Each save names the stamp
  // of the text it started from. A refused save (the file changed meanwhile) that is only an addition
  // at the end (an agent append) is merged: the addition goes below the user's text and the save
  // runs again. Any other change pauses autosave and shows the text as it is now; the next explicit
  // save replaces it. The Notes editor adds a line-number gutter and optional vim keys (vim.js, off by
  // default, the status line toggle remembered per browser); Your answer grows as it is typed.
  function autosaving(form, { onText = () => {}, savedLabel = "autosaved" } = {}) {
    const area = form.querySelector("textarea");
    const savedEl = form.querySelector("[data-ed-saved]");
    const saveBtn = form.querySelector("[data-ed-save]");
    const AUTOSAVE_MS = 1500;
    const trimEnd = (t) => t.replace(/\s+$/, "");
    let base = area.value; // the text as the server has it
    let dirty = false;
    let conflict = Boolean(form.querySelector(".ed-current"));
    let saving = null;
    let again = false;
    let timer = 0;
    if (saveBtn) saveBtn.hidden = !conflict;

    const hhmm = () => new Date().toTimeString().slice(0, 5);
    const status = (text) => { savedEl.textContent = text; };
    const extras = () => form.querySelectorAll(".ed-current, .warning");

    function clearConflict() {
      extras().forEach((el) => el.remove());
      conflict = false;
      if (saveBtn) saveBtn.hidden = true;
    }

    function showConflict(reply) {
      const tpl = document.createElement("template");
      tpl.innerHTML = reply.html; // built and escaped by the server
      extras().forEach((el) => el.remove());
      const anchor = form.querySelector("[data-ed-anchor]");
      tpl.content.querySelectorAll(".warning, .ed-current").forEach((el) => anchor.before(el));
      form.elements.stamp.value = reply.stamp;
      base = reply.text;
      conflict = true;
      if (saveBtn) saveBtn.hidden = false;
      status("not saved: compare, then save");
    }

    async function send(how) {
      const text = area.value;
      const res = await fetch(form.action, {
        method: "POST",
        body: new URLSearchParams(new FormData(form)),
        headers: { Accept: "application/json" },
        credentials: "same-origin",
      });
      let reply = null;
      if ((res.headers.get("content-type") || "").includes("application/json")) reply = await res.json();
      if (!reply) throw new Error(`Not saved (HTTP ${res.status}).`);
      if (reply.ok) {
        form.elements.stamp.value = reply.stamp;
        base = text;
        dirty = area.value !== text;
        if (conflict) clearConflict();
        status(`${how === "auto" ? savedLabel : "saved"} ${hhmm()}`);
        return;
      }
      if (typeof reply.text !== "string") throw new Error(reply.message || "Not saved.");
      const kept = trimEnd(base);
      if (reply.text.startsWith(kept) && !conflict) {
        // Only an addition at the end (an agent append): keep it below the user's text, save again.
        // From an empty file the file starts with the agent's block: set it off with a blank line.
        const added = kept ? reply.text.slice(kept.length) : `\n\n${reply.text}`;
        const mine = trimEnd(area.value);
        const { selectionStart, selectionEnd } = area;
        area.value = mine ? mine + added : reply.text;
        area.setSelectionRange(selectionStart, selectionEnd);
        form.elements.stamp.value = reply.stamp;
        base = reply.text;
        dirty = true;
        onText();
        toast("The assistant added to this note; its addition is kept at the end.");
        again = true;
        return;
      }
      showConflict(reply);
      toast(reply.message || "Not saved.", { error: true });
    }

    function save(how) {
      clearTimeout(timer);
      if (saving) { again = true; return saving; }
      if (how === "auto" && (!dirty || conflict)) return Promise.resolve();
      saving = send(how)
        .catch((err) => { status("not saved"); toast(err.message || "Not saved.", { error: true }); })
        .finally(() => {
          saving = null;
          if (again) { again = false; if (dirty && !conflict) save(how); }
        });
      return saving;
    }

    area.addEventListener("input", () => {
      dirty = area.value !== base;
      onText();
      clearTimeout(timer);
      if (dirty && !conflict) timer = setTimeout(() => save("auto"), AUTOSAVE_MS);
    });
    area.addEventListener("blur", () => { if (dirty) save("auto"); });
    form.addEventListener("keydown", (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") { e.preventDefault(); save("manual"); }
    });
    form.addEventListener("submit", (e) => { e.preventDefault(); save("manual"); });
    // Leaving with unsaved text: one last save that needs no reply (refused if the file changed).
    window.addEventListener("pagehide", () => {
      if (dirty && !conflict && navigator.sendBeacon) navigator.sendBeacon(form.action, new URLSearchParams(new FormData(form)));
    });
    // Back on the page with nothing unsaved, or told so (learn:refresh, after Add to notes or a
    // Learn Assistant reply): pick up what an agent added meanwhile.
    async function pickUp() {
      if (dirty || saving || conflict) return;
      try {
        const q = new URLSearchParams({ card: form.dataset.card, note: form.dataset.note });
        const reply = await (await fetch(`/learn/notes?${q}`, { credentials: "same-origin" })).json();
        if (!reply.ok || reply.stamp === form.elements.stamp.value || dirty) return;
        area.value = reply.text;
        base = reply.text;
        form.elements.stamp.value = reply.stamp;
        onText();
        status(`updated ${hhmm()}`);
      } catch { /* kept as it is */ }
    }
    document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") pickUp(); });
    form.addEventListener("learn:refresh", pickUp);
    onText();
    return { save };
  }

  // Optional vim keys (vim.js) on an editor form: its [data-vim-toggle] button turns them on or off, remembered
  // per browser for every editor; [data-ed-mode] and [data-ed-pos] show the mode and line,col. `save` runs on :w.
  // Returns paintPos, for the editor to call when its text changes.
  const VIM_KEY = "learn-vim";
  function attachVim(form, area, save) {
    const modeEl = form.querySelector("[data-ed-mode]");
    const posEl = form.querySelector("[data-ed-pos]");
    const vimBtn = form.querySelector("[data-vim-toggle]");
    let vim = null;
    function paintPos() {
      if (!vim) return;
      const { line, col } = HireyVim.position(area.value, area.selectionStart);
      posEl.textContent = `${line},${col}`;
    }
    if (!vimBtn || typeof HireyVim === "undefined") return paintPos;
    function paintMode() {
      if (!vim) return;
      modeEl.textContent = vim.mode === "command" ? `:${vim.cmd}` : vim.mode.toUpperCase();
      modeEl.dataset.mode = vim.mode;
      modeEl.title = vim.mode === "insert" ? "Esc: normal mode" : ":w saves · i inserts";
      paintPos();
    }
    function paintCursor() {
      const p = vim.pos;
      const block = vim.mode !== "insert" && p < area.value.length && area.value[p] !== "\n";
      area.setSelectionRange(p, block ? p + 1 : p);
    }
    function setVim(on) {
      vim = on ? HireyVim.create(area.value, area.selectionStart) : null;
      vimBtn.setAttribute("aria-pressed", String(on));
      vimBtn.textContent = on ? "vim keys on" : "vim keys off";
      modeEl.hidden = !on;
      posEl.hidden = !on;
      form.classList.toggle("is-vim", on);
      if (on) { paintMode(); if (document.activeElement === area) paintCursor(); }
    }
    area.addEventListener("keydown", (e) => {
      if (!vim || e.isComposing || e.metaKey || e.ctrlKey || e.altKey) return;
      const r = HireyVim.step({ ...vim, text: area.value, pos: area.selectionStart }, e.key);
      vim = r.state;
      if (!r.handled) return;
      e.preventDefault();
      if (r.changed) {
        area.value = vim.text;
        area.dispatchEvent(new Event("input"));
      }
      paintCursor();
      paintMode();
      if (r.save) save("manual");
    });
    ["keyup", "click"].forEach((type) => area.addEventListener(type, paintPos));
    area.addEventListener("focus", () => { if (vim) paintCursor(); });
    try { setVim(localStorage.getItem(VIM_KEY) === "1"); } catch { setVim(false); }
    vimBtn.addEventListener("click", () => {
      const on = !vim;
      setVim(on);
      try {
        if (on) localStorage.setItem(VIM_KEY, "1");
        else localStorage.removeItem(VIM_KEY);
      } catch { /* not remembered */ }
      area.focus();
    });
    vimBtn.hidden = false;
    return paintPos;
  }

  const editor = document.querySelector("form[data-learn-notes]");
  if (editor && window.fetch) {
    const area = editor.querySelector("textarea");
    const gutter = editor.querySelector(".ed-gutter");

    // Lines soft-wrap to the panel, so the gutter gives each logical line one number, sized to that
    // line's wrapped height: a hidden mirror with the textarea's width and font measures each line.
    const mirror = document.createElement("div");
    mirror.className = "ed-text ed-mirror";
    mirror.setAttribute("aria-hidden", "true");
    area.after(mirror);
    let painted = "";
    function paintGutter() {
      const lines = area.value.split("\n");
      const width = area.clientWidth;
      const key = `${width}|${area.value}`;
      if (key !== painted && width > 0) {
        painted = key;
        mirror.style.width = `${width}px`; // CSSOM, not an inline style attribute: allowed by the CSP
        mirror.replaceChildren(...lines.map((line) => {
          const row = document.createElement("div");
          row.textContent = line || " ";
          return row;
        }));
        const heights = Array.from(mirror.children, (row) => row.getBoundingClientRect().height);
        gutter.replaceChildren(...heights.map((px, i) => {
          const num = document.createElement("div");
          num.textContent = String(i + 1);
          num.style.height = `${px}px`;
          return num;
        }));
        mirror.replaceChildren();
      }
      gutter.scrollTop = area.scrollTop;
    }
    area.addEventListener("scroll", () => { gutter.scrollTop = area.scrollTop; });
    if (window.ResizeObserver) new ResizeObserver(() => paintGutter()).observe(area);
    else window.addEventListener("resize", paintGutter);
    let paintPos = () => {};
    const { save } = autosaving(editor, { onText: () => { paintGutter(); paintPos(); } });
    paintPos = attachVim(editor, area, save);
  }

  // Your answer: a plain textarea (no vim keys) that grows with the text, from its six rows.
  // Check my answer saves it first (saveAnswer).
  const answer = document.querySelector("form[data-learn-answer]");
  let saveAnswer = () => Promise.resolve();
  if (answer && window.fetch) {
    const area = answer.querySelector("textarea");
    const grow = () => {
      area.style.height = "auto"; // CSSOM, not an inline style attribute: allowed by the CSP
      area.style.height = `${area.scrollHeight + 2}px`;
    };
    const box = autosaving(answer, { onText: grow, savedLabel: "saved" });
    saveAnswer = () => box.save("auto");
  }

  // --- Learn: the Learn Assistant --------------------------------------------------------------------
  // The conversation with the Learn Assistant (/learn-ask). While a message waits for it, poll
  // /learn/thread every few seconds (for at most a few minutes), then swap in the conversation and
  // let the Notes editor pick up anything the agent added. Its forms post with data-ajax="learn".
  let learnPoll = null;
  const learnChat = () => document.querySelector("#learn-chat [data-learn-thread]");
  function scrollLearn() {
    const msgs = learnChat()?.querySelector(".msgs");
    if (msgs) msgs.scrollTop = msgs.scrollHeight;
  }
  function refreshNotes() {
    document.querySelector("form[data-learn-notes]")?.dispatchEvent(new Event("learn:refresh"));
  }
  function swapLearn(html) {
    const thread = learnChat();
    if (thread && html) thread.outerHTML = html;
    scrollLearn();
  }
  function watchLearn() {
    if (learnPoll) clearTimeout(learnPoll.timer);
    learnPoll = null;
    const thread = learnChat();
    if (!thread || !thread.hasAttribute("data-pending") || !window.fetch) return;
    const card = document.getElementById("learn-chat").dataset.card;
    const me = { timer: null, started: Date.now(), before: thread.querySelectorAll(".msg").length };
    learnPoll = me;
    const tick = async () => {
      if (learnPoll !== me) return;
      try {
        const res = await fetch(`/learn/thread?card=${encodeURIComponent(card)}`, {
          headers: { Accept: "application/json" }, credentials: "same-origin",
        });
        const reply = await res.json();
        if (learnPoll !== me) return;
        showResend(reply.failed);
        if (reply.ok && !reply.pending) {
          learnPoll = null;
          swapLearn(reply.html);
          refreshNotes();
          toast("The Learn Assistant replied.");
          return;
        }
      } catch { /* the server restarted or is busy: try again */ }
      if (Date.now() - me.started > POLL_FOR_MS) {
        learnPoll = null;
        const state = learnChat()?.querySelector(".thread-state > span:not(.spinner)"); // Stop stays on the line
        if (state) state.textContent = "Still waiting. The reply shows here when the Learn Assistant answers; reload to check, or Stop.";
        return;
      }
      me.timer = setTimeout(tick, POLL_MS);
    };
    me.timer = setTimeout(tick, POLL_MS);
  }
  scrollLearn();
  watchLearn();

  // --- Learn: the floating "Ask about this card" window ------------------------------------------
  // The pill opens it; – minimises it to its head, ✕ closes it back to the pill. Its state (open, min,
  // closed) and tab (assistant, notes) are kept per browser. ?notes=all opens it on Notes.
  const float = document.getElementById("learn-float");
  const pill = document.querySelector("[data-float-open]");
  const FLOAT_KEY = "learn-float";
  function setFloat(state, tab) {
    if (!float) return;
    if (state) float.dataset.state = state;
    if (tab) float.dataset.tab = tab;
    float.querySelectorAll("[data-float-tab]").forEach((b) =>
      b.setAttribute("aria-selected", String(b.dataset.floatTab === float.dataset.tab)));
    if (pill) pill.hidden = float.dataset.state !== "closed";
    try { localStorage.setItem(FLOAT_KEY, JSON.stringify({ state: float.dataset.state, tab: float.dataset.tab })); }
    catch { /* not remembered */ }
    if (float.dataset.state === "open" && float.dataset.tab === "assistant") scrollLearn();
  }
  if (float) {
    let saved = {};
    try { saved = JSON.parse(localStorage.getItem(FLOAT_KEY) || "{}") || {}; } catch { /* defaults */ }
    const allNotes = new URLSearchParams(location.search).get("notes") === "all";
    const state = allNotes ? "open" : (["open", "min", "closed"].includes(saved.state) ? saved.state : "closed");
    const tab = allNotes ? "notes" : (["assistant", "notes"].includes(saved.tab) ? saved.tab : "assistant");
    float.querySelectorAll("[data-float-min], [data-float-close]").forEach((b) => { b.hidden = false; });
    setFloat(state, tab);
    pill?.addEventListener("click", () => {
      setFloat("open");
      float.querySelector(float.dataset.tab === "notes" ? ".ed-text" : "form.ask textarea")?.focus();
    });
    float.addEventListener("click", (e) => {
      const tabBtn = e.target.closest("[data-float-tab]");
      if (tabBtn) { setFloat("open", tabBtn.dataset.floatTab); return; }
      if (e.target.closest("[data-float-min]")) { setFloat(float.dataset.state === "min" ? "open" : "min"); return; }
      if (e.target.closest("[data-float-close]")) { setFloat("closed"); return; }
      if (float.dataset.state === "min" && e.target.closest(".float-head")) setFloat("open");
    });
  }

  // An attached passage in the chat form: "Attached: “…” ✕" above the box (text only), sent as quote +
  // quote_src (the server marks it in the message).
  const chatForm = () => document.querySelector("#learn-chat form.ask");
  function attachQuote(text, src) {
    const box = chatForm()?.querySelector("[data-ask-quote]");
    if (!box) return;
    box.querySelector("[data-quote-text]").textContent = text;
    box.querySelector("input[name=quote]").value = text;
    box.querySelector("input[name=quote_src]").value = src;
    box.hidden = !text;
  }
  document.addEventListener("click", (e) => {
    if (e.target.closest("[data-quote-clear]")) attachQuote("", "");
  });

  // --- Learn: highlight to ask ---------------------------------------------------------------------
  // Selecting text on the card or in its explainer shows "✦ Ask about this" above it; that opens a box
  // under the text: the quote, a plain textarea (typing or macOS dictation) and Add to notes / Ask ↵.
  // Ask sends quote + words to the Learn Assistant and opens the chat; Ask with no words attaches the
  // quote to the chat instead. Escape or a click outside closes it. The text is only ever text.
  const MAX_QUOTE = 1000;
  const learnView = document.getElementById("learn-view");
  const cardBody = document.querySelector("#learn-card .learn-card-body");
  const frame = document.querySelector("iframe[data-explainer]");
  let hlPop = null;
  let hlBox = null;
  function closeHl() {
    hlPop?.remove(); hlPop = null;
    hlBox?.remove(); hlBox = null;
  }
  const clean = (text) => (text || "").replace(/\s+/g, " ").trim().slice(0, MAX_QUOTE);
  function showPop(text, src, rect) { // rect: the selection, in page coordinates
    closeHl();
    if (!text || !learnView) return;
    hlPop = document.createElement("button");
    hlPop.type = "button";
    hlPop.className = "hl-pop";
    hlPop.textContent = "✦ Ask about this";
    document.body.append(hlPop);
    const left = Math.max(8, Math.min(rect.left + rect.width / 2 - hlPop.offsetWidth / 2,
      document.documentElement.clientWidth - hlPop.offsetWidth - 8));
    hlPop.style.left = `${left}px`;
    hlPop.style.top = `${Math.max(8, rect.top - hlPop.offsetHeight - 8)}px`;
    hlPop.addEventListener("mousedown", (e) => e.preventDefault()); // keep the selection
    hlPop.addEventListener("click", () => showBox(text, src, rect));
  }
  function showBox(text, src, rect) {
    closeHl();
    // A phone-width window: the open chat covers most of the page, so it folds to its head meanwhile.
    if (float?.dataset.state === "open" && innerWidth <= 600) setFloat("min");
    const card = learnView.dataset.card;
    hlBox = document.createElement("div");
    hlBox.className = "hl-box";
    hlBox.setAttribute("role", "dialog");
    hlBox.setAttribute("aria-label", "Ask about the selected text");
    const quote = document.createElement("p");
    quote.className = "hl-quote";
    quote.textContent = `“${text}”`;
    const area = document.createElement("textarea");
    area.rows = 3;
    area.maxLength = 4000;
    area.placeholder = "Ask, or say what to note (dictation works: Fn twice)…";
    area.setAttribute("aria-label", "Your question or note");
    const actions = document.createElement("div");
    actions.className = "hl-actions";
    const noteBtn = document.createElement("button");
    noteBtn.type = "button";
    noteBtn.className = "btn btn-secondary btn-sm";
    noteBtn.textContent = "Add to notes";
    const askBtn = document.createElement("button");
    askBtn.type = "button";
    askBtn.className = "btn btn-primary btn-sm";
    askBtn.textContent = "Ask ↵";
    actions.append(noteBtn, askBtn);
    hlBox.append(quote, area, actions);
    document.body.append(hlBox);
    const width = hlBox.offsetWidth;
    const pageW = document.documentElement.clientWidth;
    hlBox.style.left = `${Math.max(8, Math.min(rect.left, pageW - width - 8))}px`;
    hlBox.style.top = `${rect.bottom + 8}px`;
    hlBox.scrollIntoView({ block: "nearest" });
    area.focus();
    const data = () => new URLSearchParams({ card_id: card, quote: text, quote_src: src, body: area.value.trim() });
    const busy = (on) => { noteBtn.disabled = on; askBtn.disabled = on; };
    async function ask() {
      if (!area.value.trim()) { // nothing typed: attach the quote to the chat and type there
        attachQuote(text, src);
        closeHl();
        setFloat("open", "assistant");
        chatForm()?.querySelector("textarea")?.focus();
        return;
      }
      busy(true);
      try {
        const reply = await post("/learn/message", data());
        swapLearn(reply.html);
        watchLearn();
        closeHl();
        setFloat("open", "assistant");
        toast(reply.message);
      } catch (err) { busy(false); toast(err.message || "Not sent.", { error: true }); }
    }
    async function note() {
      busy(true);
      try {
        const reply = await post("/learn/quote-note", data());
        closeHl();
        refreshNotes();
        toast(reply.message);
      } catch (err) { busy(false); toast(err.message || "Not saved.", { error: true }); }
    }
    askBtn.addEventListener("click", ask);
    noteBtn.addEventListener("click", note);
    area.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); ask(); }
    });
  }
  const pageRect = (r) => ({ left: r.left + scrollX, top: r.top + scrollY, bottom: r.bottom + scrollY, width: r.width });
  if (cardBody && learnView) {
    document.addEventListener("mouseup", (e) => {
      if (e.target.closest?.(".hl-pop, .hl-box")) return;
      setTimeout(() => { // after the click has updated the selection
        const sel = getSelection();
        if (!sel || sel.isCollapsed || !sel.rangeCount) { if (!hlBox) closeHl(); return; }
        const range = sel.getRangeAt(0);
        const node = range.commonAncestorContainer;
        const el = node.nodeType === 1 ? node : node.parentElement;
        if (!el || !cardBody.contains(el) || el.closest("textarea, input, form, .learn-nav")) return;
        showPop(clean(sel.toString()), "card", pageRect(range.getBoundingClientRect()));
      }, 0);
    });
  }
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && (hlPop || hlBox)) closeHl(); });
  document.addEventListener("mousedown", (e) => {
    if ((hlPop || hlBox) && !e.target.closest(".hl-pop, .hl-box")) closeHl();
  });

  // The explainer runs in a sandboxed iframe (no same origin); its standard script (/static/explainer.js)
  // posts {type: "selection", text, rect} and {type: "height", px}. Only messages from that iframe's
  // window are read, and only as plain text and numbers.
  if (frame) {
    window.addEventListener("message", (e) => {
      if (e.source !== frame.contentWindow || !e.data || typeof e.data !== "object") return;
      if (e.data.type === "height") {
        const px = Number(e.data.px);
        if (Number.isFinite(px)) frame.style.height = `${Math.max(120, Math.min(20000, Math.round(px)))}px`;
      } else if (e.data.type === "selection") {
        const text = clean(typeof e.data.text === "string" ? e.data.text : "");
        if (!text) { closeHl(); return; } // a click in the explainer is a click outside the box
        const box = frame.getBoundingClientRect();
        const r = e.data.rect && typeof e.data.rect === "object" ? e.data.rect : {};
        const num = (v, d) => (Number.isFinite(Number(v)) ? Number(v) : d);
        const top = box.top + num(r.top, 0);
        const left = box.left + num(r.left, 0);
        const height = num(r.height, 0);
        showPop(text, "explainer", pageRect({ left, top, bottom: top + height, width: num(r.width, 0) }));
      }
    });
  }

  // Resend under a message still waiting shows once it is due (data-resend-due, epoch ms from the
  // server; at once when the ping failed): both boxes, after every swap too.
  function showResend(ids) { // a failed ping, reported by the poll: Resend at once
    (ids || []).forEach((id) => {
      document.querySelectorAll(`.msg-wait input[name="msg_id"][value="${Number(id)}"]`).forEach((input) => {
        const btn = input.form?.querySelector("[data-resend-due]");
        if (btn) btn.hidden = false;
      });
    });
  }
  setInterval(() => {
    document.querySelectorAll("[data-resend-due][hidden]").forEach((btn) => {
      if (Date.now() >= Number(btn.dataset.resendDue)) btn.hidden = false;
    });
  }, 2000);

  // --- Applications ---------------------------------------------------------------------
  // The server renders every state from the query string (?view=, ?app=ID or ?job=ID side panel,
  // the filters), so this only adds: a panel opens without a reload, Esc closes it, and after any
  // change the list and the panel are fetched again and swapped in (refreshApps), keeping the
  // scroll position. getjo job ids hold ":" and other characters, so selectors escape them.
  const apps = document.querySelector(".apps");
  if (apps) {
    function rowFor(panel) {
      if (!panel) return null;
      return panel.dataset.app ? document.getElementById(`app-${panel.dataset.app}`)
        : document.querySelector(`.app-list tr[data-job="${CSS.escape(panel.dataset.job || "")}"]`);
    }

    function markFocus(panel) {
      document.querySelectorAll(".app-list tr.is-focus").forEach((el) => el.classList.remove("is-focus"));
      rowFor(panel)?.classList.add("is-focus");
    }

    // Fetch `url` and swap in its panel and the Applications Assistant box's "About" line (and with `list`, the
    // pipeline and the conversation too). The message being typed stays. The fetched HTML is the
    // server's own escaped page; scripts in a DOMParser document never run.
    function swap(doc, selector) {
      const next = doc.querySelector(selector);
      const current = document.querySelector(selector);
      if (!next || !current) return null;
      const fresh = document.importNode(next, true);
      current.replaceWith(fresh);
      return fresh;
    }

    async function showPanel(url, push, { list = false } = {}) {
      const current = document.getElementById("panel");
      current?.classList.add("is-loading");
      try {
        const res = await fetch(url, { headers: { Accept: "text/html" }, credentials: "same-origin" });
        if (!res.ok) throw new Error(`Could not open it (HTTP ${res.status}).`);
        const doc = new DOMParser().parseFromString(await res.text(), "text/html");
        if (list) {
          const pipeline = swap(doc, ".card-pipeline");
          if (pipeline) makeSortable(pipeline);
          swap(doc, "#agent [data-thread]");
          swap(doc, ".page-head .subtitle");
          swap(doc, ".sidebar nav"); // the new offers count
          swap(doc, ".page-head-actions"); // My CV marked while its panel is open
        }
        swap(doc, "#agent .agent-about");
        if (push) history.pushState(null, "", url);
        adoptServerSel();
        const back = document.querySelector('#agent input[name="return"]');
        if (back) back.value = push ? location.pathname + location.search : withSel(url);
        const next = doc.getElementById("panel");
        let panel = null;
        if (next) {
          panel = document.importNode(next, true);
          const same = current && current.dataset.app === panel.dataset.app && current.dataset.job === panel.dataset.job;
          const scroll = same ? current.scrollTop : 0;
          // Notes being edited survive a refresh (the agent replied meanwhile): the open editor keeps
          // its text and its stamp, so a save over the agent's addition is still refused, not lost.
          const editing = same && current.querySelector("#card-notes .notes-edit[open]")?.closest("#card-notes");
          if (editing) panel.querySelector("#card-notes")?.replaceWith(editing);
          // So do the plan and CV editors with unsaved text (a stale save is refused, not lost).
          ["#plan-edit", "#tailor-edit", "#master-edit"].forEach((sel) => {
            const open = same && current.querySelector(`${sel}[open]`);
            const area = open?.querySelector("textarea");
            if (area && area.value !== area.defaultValue) panel.querySelector(sel)?.replaceWith(open);
          });
          if (current) current.replaceWith(panel); else apps.append(panel);
          if (list) {
            panel.scrollTop = scroll; // a refresh keeps the reader's place
          } else {
            panel.querySelector(".panel-close")?.focus({ preventScroll: true });
          }
        } else {
          current?.remove();
        }
        markFocus(panel);
        if (panel) setupClamp(panel);
        paintTicks();
        applyChatFilter();
        watchThread();
      } catch (err) {
        current?.classList.remove("is-loading");
        toast(err.message, { error: true });
      }
    }

    refreshApps = () => showPanel(location.href, false, { list: true });
    showPanelAt = (url) => showPanel(withSel(url), true, { list: true });

    // Ticks: every ☐ for a key shows the same state; links and forms carry the ticked jobs along.
    function paintTicks(sel = currentSel()) {
      const on = new Set(sel);
      document.querySelectorAll(".sel-tick[data-sel]").forEach((a) => {
        a.classList.toggle("is-on", on.has(a.dataset.sel));
        a.setAttribute("aria-checked", String(on.has(a.dataset.sel)));
      });
      document.querySelectorAll('input[name="return"]').forEach((i) => { if (i.value) i.value = withSel(i.value, sel); });
      document.querySelectorAll('form[method="get"][action="/"]').forEach((form) => { // New offers filters
        form.querySelectorAll('input[name="sel"]').forEach((i) => i.remove());
        sel.forEach((k) => {
          const input = document.createElement("input");
          input.type = "hidden"; input.name = "sel"; input.value = k;
          form.append(input);
        });
      });
      rememberSel(sel);
    }
    // Plain links to this page (the view switch, stage pills, Show closed…) take the ticks as they are now.
    document.addEventListener("click", (e) => {
      const a = e.target.closest?.("a[href]");
      if (!a || a.hasAttribute("data-sel") || a.hasAttribute("data-sel-clear") || a.target
        || a.getAttribute("href").startsWith("#")) return;
      const u = new URL(a.href, location.href);
      if (u.origin === location.origin && u.pathname === "/") a.setAttribute("href", withSel(u.pathname + u.search));
    }, true);

    // Tick or untick: the URL changes in place (no reload, the list keeps its scroll) and the Agent
    // box's About line is fetched again (the server names the jobs). Only the latest fetch is used.
    let selFetch = 0;
    async function setSel(sel) {
      const url = withSel(location.href, sel);
      history.replaceState(null, "", url);
      paintTicks(sel);
      applyChatFilter();
      const mine = ++selFetch;
      try {
        const res = await fetch(url, { headers: { Accept: "text/html" }, credentials: "same-origin" });
        if (!res.ok) throw new Error();
        const doc = new DOMParser().parseFromString(await res.text(), "text/html");
        if (mine === selFetch) {
          swap(doc, "#agent .agent-about");
          adoptServerSel();
          paintTicks();
          applyChatFilter();
        }
      } catch {
        if (mine === selFetch) toast("Could not update the About line; reload the page.", { error: true });
      }
    }

    function toggleSel(el) {
      const sel = currentSel();
      if (el.hasAttribute("data-sel-clear")) { setSel([]); return; }
      const key = el.dataset.sel;
      if (sel.includes(key)) setSel(sel.filter((k) => k !== key));
      else if (sel.length >= MAX_SEL) toast(`Up to ${MAX_SEL} at a time: untick one first.`, { error: true });
      else setSel([...sel, key]);
    }

    function closePanel() {
      const panel = document.getElementById("panel");
      if (!panel) return;
      const href = withSel(panel.querySelector("[data-close-panel]").getAttribute("href"));
      const row = rowFor(panel);
      showPanel(href, true); // the page without a panel: About goes back to the whole search
      row?.querySelector("a")?.focus({ preventScroll: true });
    }

    document.addEventListener("click", (e) => {
      if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      const tick = e.target.closest("a[data-sel], a[data-sel-clear]");
      if (tick) { e.preventDefault(); toggleSel(tick); return; }
      if (e.target.closest("[data-close-panel]")) { e.preventDefault(); document.body.classList.remove("is-asking"); closePanel(); return; }
      if (e.target.closest("[data-ask-agent]")) { // phones: leave the sheet for the Applications Assistant box, About kept
        e.preventDefault();
        document.body.classList.add("is-asking");
        const box = document.getElementById("agent");
        box?.scrollIntoView({ block: "end" });
        box?.querySelector("textarea")?.focus({ preventScroll: true });
        return;
      }
      if (e.target.closest("[data-back-to-panel]")) {
        document.body.classList.remove("is-asking");
        document.getElementById("panel")?.scrollTo({ top: 0 });
        return;
      }
      const jump = e.target.closest("a[data-jump]");
      if (jump) { // a done shortcut: scroll to its result card (no hash, which would re-render the panel)
        e.preventDefault();
        const card = document.getElementById(jump.dataset.jump);
        if (card) {
          card.scrollIntoView({ block: "start", behavior: "smooth" });
          card.classList.add("is-flash");
          setTimeout(() => card.classList.remove("is-flash"), 1200);
        }
        return;
      }
      // A row's company, a message's tag, My CV and the My CV panel's Open / Compare / Current CV links.
      const link = e.target.closest(".app-list td a:not([target]), a.msg-about, a[data-open-panel], a[data-panel-link]");
      const row = !link && !e.target.closest("form, button") && e.target.closest(".app-list tr[data-href]");
      if (!link && !row) return;
      e.preventDefault();
      document.body.classList.remove("is-asking"); // a chosen job opens as the sheet again on phones
      // The ticked jobs go along; opening a job ticks it only when nothing is ticked yet.
      const raw = link ? link.getAttribute("href") : row.dataset.href;
      const key = keyOf(raw);
      let sel = currentSel();
      if (!sel.length && key && key !== keyOf(location.href)) sel = [key];
      const href = withSel(raw, sel);
      // A message's tag may point at the other view (a job offer from the pipeline): swap the list too.
      const viewOf = (u) => new URL(u, location.href).searchParams.get("view") || "pipeline";
      showPanel(href, true, { list: viewOf(href) !== viewOf(location.href) });
    });
    document.addEventListener("keydown", (e) => {
      const tick = e.key === " " && e.target.closest?.("a.sel-tick");
      if (tick) { e.preventDefault(); toggleSel(tick); return; } // a checkbox toggles on Space too
      if (e.key !== "Escape" || document.querySelector("dialog[open]")) return;
      if (e.target instanceof Element && e.target.closest("input, select, textarea")) return;
      closePanel();
    });
    window.addEventListener("popstate", () => showPanel(location.href, false, { list: true }));

    const focusRow = document.querySelector(".app-list tr.is-focus");
    if (focusRow) focusRow.scrollIntoView({ block: "nearest" });
  }

  // --- Workers sidebar -------------------------------------------------------------------
  // Rows are <details>, so the popover opens without this script. Here: one popover open at a time,
  // anchored next to its row; Esc or a click outside closes it (focus back on the row). The rows are
  // polled from /workers.json every WORKERS_POLL_MS while the tab is visible (not while a popover holds
  // focus), and a long item finished after the page was drawn gets a toast with a link to it.
  const WORKERS_POLL_MS = 15000;
  const workersBox = document.querySelector("[data-workers]");
  const seenRuns = new Set();
  function openPops() { return [...document.querySelectorAll(".worker-pop[open]")]; }
  function placePop(details) {
    const pop = details.querySelector(".popover");
    const row = details.querySelector("summary");
    if (!pop || !row) return;
    const r = row.getBoundingClientRect();
    pop.style.top = `${Math.max(8, Math.min(r.top - 8, window.innerHeight - pop.offsetHeight - 8))}px`;
  }
  function swapWorkers(html, { keepOpen = true } = {}) {
    const list = workersBox?.querySelector(".worker-list");
    if (!list || typeof html !== "string") return;
    const open = keepOpen ? openPops().map((d) => d.closest("[data-worker]")?.dataset.worker) : [];
    list.innerHTML = html; // server-built and escaped
    open.forEach((key) => {
      const d = list.querySelector(`[data-worker="${CSS.escape(key || "")}"] .worker-pop`);
      if (d) { d.open = true; placePop(d); }
    });
  }
  if (workersBox) {
    document.addEventListener("toggle", (e) => {
      const d = e.target;
      if (!(d instanceof HTMLDetailsElement) || !d.classList.contains("worker-pop") || !d.open) return;
      openPops().forEach((o) => { if (o !== d) o.open = false; });
      placePop(d);
    }, true);
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape") return;
      const d = openPops()[0];
      if (!d) return;
      d.open = false;
      d.querySelector("summary")?.focus();
    });
    document.addEventListener("click", (e) => {
      openPops().forEach((d) => { if (!d.contains(e.target)) d.open = false; });
    });
    window.addEventListener("resize", () => openPops().forEach(placePop));
    let since = workersBox.dataset.since || "";
    const tick = async () => {
      if (document.visibilityState === "visible" && window.fetch
          && !openPops().some((d) => d.contains(document.activeElement) && document.activeElement.tagName !== "SUMMARY")) {
        try {
          const query = new URLSearchParams({ since, here: location.pathname + location.search });
          const res = await fetch(`/workers.json?${query}`, { headers: { Accept: "application/json" }, credentials: "same-origin" });
          const reply = await res.json();
          if (reply.ok) {
            swapWorkers(reply.html);
            const sum = workersBox.querySelector(".workers-sum");
            if (sum) sum.textContent = reply.summary;
            (reply.finished || []).forEach((f) => {
              if (seenRuns.has(f.id)) return;
              seenRuns.add(f.id);
              toast(f.text, { action: { label: "Open", run: () => { location.href = f.href; } } });
            });
            since = reply.now || since;
          }
        } catch { /* the server restarted or is busy: try again */ }
      }
      setTimeout(tick, WORKERS_POLL_MS);
    };
    setTimeout(tick, WORKERS_POLL_MS);
  }

  // --- forms without a reload --------------------------------------------------------
  // POST urlencoded (what the server parses) and ask for JSON. Anything that is not a JSON
  // reply (403/404/413 come back as plain text) becomes a generic error.
  async function post(url, data) {
    const res = await fetch(url, {
      method: "POST",
      body: data,
      headers: { Accept: "application/json" },
      credentials: "same-origin",
    });
    let reply = null;
    if ((res.headers.get("content-type") || "").includes("application/json")) reply = await res.json();
    if (!reply) throw new Error(`Not saved (HTTP ${res.status}).`);
    if (!reply.ok) throw new Error(reply.message || "Not saved.");
    return reply;
  }

  // --- Skip pop-up -----------------------------------------------------------------------
  // A Skip link opens the dialog instead of the skip page. The link names where the dialog posts and
  // which field it fills (an offer: /offers/skip and job_id; an application: /applications/skip and
  // app_id). Clicking a reason posts the skip, then the list and the panel are refreshed; a skipped
  // offer gets Undo in the toast.
  const dialog = document.getElementById("skip-dialog");
  if (dialog && dialog.showModal && window.fetch) {
    const form = dialog.querySelector("form");
    document.addEventListener("click", (e) => {
      const link = e.target.closest("a[data-skip]");
      if (!link) return;
      e.preventDefault();
      form.reset();
      form.action = link.dataset.skipAction || "/applications/skip";
      form.elements.app_id.value = "";
      form.elements.job_id.value = "";
      form.elements[link.dataset.skipField || "app_id"].value = link.dataset.skip;
      form.elements.return.value = location.pathname + location.search;
      dialog.querySelector("[data-skip-job]").textContent = link.dataset.skipLabel || "";
      dialog.showModal();
      form.elements.comment.focus();
    });
    dialog.querySelector("[data-close]").addEventListener("click", () => dialog.close());
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!e.submitter || e.submitter.name !== "reason") return; // Enter in the comment: pick a reason
      const data = new URLSearchParams(new FormData(form, e.submitter)); // includes the clicked reason
      const buttons = form.querySelectorAll("button");
      buttons.forEach((b) => { b.disabled = true; });
      try {
        const reply = await post(form.action, data);
        dialog.close();
        await refreshApps();
        const jobId = data.get("job_id");
        const undoSkip = () => post("/offers/unskip", new URLSearchParams({ job_id: jobId }))
          .then((r) => refreshApps().then(() => toast(r.message)))
          .catch((err) => toast(err.message, { error: true }));
        toast(reply.message, jobId ? { action: { label: "Undo", run: undoSkip } } : {});
      } catch (err) {
        toast(err.message || "Not saved.", { error: true });
      } finally {
        buttons.forEach((b) => { b.disabled = false; });
      }
    });
  }

  // --- Notes card ------------------------------------------------------------------------
  // Edit opens the form (a <details>); Cancel closes it with the text as saved. Save posts it and
  // swaps in the card the server returns. A save over notes that changed meanwhile comes back
  // refused with the card again: the current notes above, the user's text still in the editor.
  document.addEventListener("click", (e) => {
    const cancel = e.target.closest("[data-notes-cancel]");
    if (!cancel || !window.fetch) return;
    e.preventDefault();
    const edit = cancel.closest(".notes-edit");
    const area = edit?.querySelector("textarea");
    if (area) area.value = area.defaultValue;
    if (edit) edit.open = false;
  });
  document.addEventListener("toggle", (e) => {
    if (e.target.matches?.(".notes-edit") && e.target.open) e.target.querySelector("textarea")?.focus();
  }, true);
  document.addEventListener("submit", async (e) => {
    const form = e.target.closest("form[data-notes]");
    if (!form || !window.fetch) return;
    e.preventDefault();
    const button = form.querySelector("button");
    if (button) button.disabled = true;
    // Save and write the CV (the plan editor) posts the same text to its own formaction.
    const url = e.submitter?.hasAttribute("formaction") ? e.submitter.formAction : form.action;
    try {
      const res = await fetch(url, {
        method: "POST",
        body: new URLSearchParams(new FormData(form)),
        headers: { Accept: "application/json" },
        credentials: "same-origin",
      });
      let reply = null;
      if ((res.headers.get("content-type") || "").includes("application/json")) reply = await res.json();
      if (!reply) throw new Error(`Not saved (HTTP ${res.status}).`);
      if (reply.ok && !reply.html && refreshApps) { await refreshApps(); toast(reply.message); return; }
      const card = form.closest("[data-swap], #card-notes");
      if (reply.html && card) {
        const tpl = document.createElement("template");
        tpl.innerHTML = reply.html; // built and escaped by the server
        const fresh = tpl.content.firstElementChild;
        card.replaceWith(fresh);
        if (!reply.ok) fresh.querySelector("textarea")?.focus();
      }
      if (!reply.ok) throw new Error(reply.message || "Not saved.");
      toast(reply.message);
    } catch (err) {
      toast(err.message || "Not saved.", { error: true });
    } finally {
      if (button?.isConnected) button.disabled = false;
    }
  });

  // --- CV editors (Tailored CV, My CV) ------------------------------------------------------------
  // Edit opens the form; Save (or Cmd/Ctrl-S, :w with vim keys) posts it. A saved edit refreshes the panel
  // (the PDF, the check and the versions change) and opens the editor again on the saved text; the My CV
  // toast has Undo (Restore of the version it replaced). A save over a file that changed meanwhile comes
  // back refused: the editor again with the current text above, the user's text kept in it.
  document.addEventListener("toggle", (e) => {
    const edit = e.target;
    if (!edit.matches?.(".cv-edit") || !edit.open) return;
    const form = edit.querySelector("form[data-cv-editor]");
    if (!form || form.dataset.vim) return;
    form.dataset.vim = "1";
    attachVim(form, form.querySelector("textarea"), () => form.requestSubmit());
  }, true);
  document.addEventListener("keydown", (e) => {
    const form = e.target.closest?.("form[data-cv-editor]");
    if (form && (e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") { e.preventDefault(); form.requestSubmit(); }
  });
  document.addEventListener("submit", async (e) => {
    const form = e.target.closest("form[data-cv-editor]");
    if (!form || !window.fetch) return;
    e.preventDefault();
    const button = form.querySelector("button");
    if (button) button.disabled = true;
    const edit = form.closest("details");
    try {
      const res = await fetch(form.action, {
        method: "POST",
        body: new URLSearchParams(new FormData(form)),
        headers: { Accept: "application/json" },
        credentials: "same-origin",
      });
      let reply = null;
      if ((res.headers.get("content-type") || "").includes("application/json")) reply = await res.json();
      if (!reply) throw new Error(`Not saved (HTTP ${res.status}).`);
      if (!reply.ok) {
        const box = form.closest("[data-swap]");
        if (reply.html && box) {
          const tpl = document.createElement("template");
          tpl.innerHTML = reply.html; // built and escaped by the server
          const fresh = tpl.content.firstElementChild;
          box.replaceWith(fresh);
          fresh.querySelector("textarea")?.focus();
        }
        throw new Error(reply.message || "Not saved.");
      }
      const id = edit?.id;
      if (edit) edit.open = false; // not kept over the refresh: the fresh one has the new stamp
      await refreshApps();
      const again = id && document.getElementById(id);
      if (again) { again.open = true; again.querySelector("textarea")?.focus(); }
      const undoIt = () => post("/cv/restore", new URLSearchParams({ version: reply.previous }))
        .then((r) => refreshApps().then(() => toast(r.message)))
        .catch((err) => toast(err.message, { error: true }));
      toast(reply.message, reply.previous ? { action: { label: "Undo", run: undoIt } } : {});
    } catch (err) {
      toast(err.message || "Not saved.", { error: true });
    } finally {
      if (button?.isConnected) button.disabled = false;
    }
  });

  // A select marked data-autosubmit (the panel's Employer) saves on change.
  document.addEventListener("change", (e) => {
    const select = e.target.closest?.("select[data-autosubmit]");
    if (select?.form) select.form.requestSubmit();
  });

  document.addEventListener("submit", async (e) => {
    const form = e.target.closest("form[data-ajax]");
    if (!form || !window.fetch) return;
    e.preventDefault();
    const button = form.querySelector("button");
    if (button) button.disabled = true;
    const mode = form.dataset.ajax;
    try {
      if (form.hasAttribute("data-save-answer")) await saveAnswer(); // Check my answer: the latest text
      const data = new URLSearchParams(new FormData(form, e.submitter)); // the button pressed counts (Stop: Back to queue)
      if (mode === "thread") { // the panel open now (Stop clears its queued work; replies keep its waiting line)
        const panel = document.getElementById("panel");
        data.delete("panel_app"); data.delete("panel_job");
        if (panel?.dataset.app) data.set("panel_app", panel.dataset.app);
        else if (panel?.dataset.job) data.set("panel_job", panel.dataset.job);
      }
      const reply = await post(form.action, data);
      if (mode === "interested") {
        // An offer's Interested: its application panel opens in the pipeline (Interested).
        const back = location.pathname + location.search;
        const params = new URLSearchParams({ job_id: form.elements.job_id.value });
        await showPanelAt(reply.redirect);
        const undoIt = () => post("/offers/uninterested", params)
          .then((r) => showPanelAt(back).then(() => toast(r.message)))
          .catch((err) => toast(err.message, { error: true }));
        // Undo only for an application this click created (never one that existed before).
        toast(reply.message, reply.created ? { action: { label: "Undo", run: undoIt } } : {});
      } else if (mode === "refresh") {
        await refreshApps();
        toast(reply.message);
      } else if (mode === "applied") {
        // Mark applied: done at once; Undo moves it back to Interested.
        const params = new URLSearchParams({ app_id: form.elements.app_id.value });
        await refreshApps();
        toast(reply.message, { action: { label: "Undo", run: () => post("/applications/unapply", params)
          .then((r) => refreshApps().then(() => toast(r.message)))
          .catch((err) => toast(err.message, { error: true })) } });
      } else if (mode === "tick") {
        // A panel task ticked done: refresh the list's next task and the panel; Undo reopens it.
        const params = new URLSearchParams({ task_id: form.elements.task_id.value });
        await refreshApps();
        toast(reply.message, { action: { label: "Undo", run: () => post("/tasks/reopen", params)
          .then((r) => refreshApps().then(() => toast(r.message)))
          .catch((err) => toast(err.message, { error: true })) } });
      } else if (mode === "restore") {
        // The My CV panel's Restore: back to the current CV; Undo restores the text it replaced (a version too).
        await showPanelAt(form.elements.return.value);
        const undoIt = () => post("/cv/restore", new URLSearchParams({ version: reply.previous }))
          .then((r) => refreshApps().then(() => toast(r.message)))
          .catch((err) => toast(err.message, { error: true }));
        toast(`v${form.elements.version.value} restored as v${reply.version}. The CV it replaced is kept as v${reply.previous}.`,
          reply.previous ? { action: { label: "Undo", run: undoIt } } : {});
      } else if (mode === "draft") {
        // Draft a reply: the request joins the Applications Assistant box, which waits for the draft.
        const thread = document.querySelector(".agent [data-thread]");
        if (thread && reply.html) thread.outerHTML = reply.html;
        form.reset();
        form.closest("details")?.removeAttribute("open");
        applyChatFilter();
        watchThread();
        toast(reply.message);
      } else if (mode === "workers") {
        // Stop in a worker's popover: the rows come back from the server; the popover closes.
        swapWorkers(reply.html, { keepOpen: false });
        toast(reply.message);
      } else if (mode === "source") {
        // A Learn source's tick: swap in the server-built, escaped Learn box (ticks and count), keep focus.
        const box = form.closest("#learn-sources");
        const i = box ? [...box.querySelectorAll("form[data-ajax='source']")].indexOf(form) : -1;
        if (box && reply.html) {
          const tpl = document.createElement("template");
          tpl.innerHTML = reply.html;
          const fresh = tpl.content.firstElementChild;
          box.replaceWith(fresh);
          fresh.querySelectorAll("form[data-ajax='source'] button")[i]?.focus();
        }
      } else if (mode === "learn") {
        // The Learn Assistant: a message, Check my answer, Want a card for this?, or Add to notes.
        swapLearn(reply.html);
        if (form.elements.body) form.elements.body.value = "";
        if (form.elements.quote) attachQuote("", "");
        if (reply.note) refreshNotes();
        watchLearn();
        if (form.hasAttribute("data-open-chat")) setFloat("open", "assistant");
        toast(reply.message);
      } else if (mode === "thread") {
        // The Applications Assistant box: swap in the server-built, escaped conversation and wait for the reply.
        const thread = form.closest(".agent")?.querySelector("[data-thread]");
        if (thread && reply.html) thread.outerHTML = reply.html;
        if (form.elements.body) form.elements.body.value = ""; // Resend, Cancel and Stop have no text
        applyChatFilter();
        watchThread();
        if (form.action.endsWith("/applications/stop")) await refreshApps(); // the panel's spinners clear too
        toast(reply.message);
      }
    } catch (err) {
      const actions = mode === "learn" && form.action.endsWith("/learn/add-note") && form.closest(".msg-actions");
      if (actions) {
        // Add to notes refused (no short summary, already there, pay): say why under the reply's buttons.
        let hint = actions.nextElementSibling;
        if (!hint?.classList.contains("msg-hint")) {
          hint = document.createElement("p");
          hint.className = "msg-hint";
          hint.setAttribute("role", "status");
          actions.after(hint);
        }
        hint.textContent = err.message || "Not saved.";
      } else {
        toast(err.message || "Not saved.", { error: true });
      }
    } finally {
      if (button) button.disabled = false;
    }
  });
})();
