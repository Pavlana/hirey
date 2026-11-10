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
  const VIEW_KEY = "apps-view";
  if (location.pathname === "/" && !location.search && document.querySelector(".view-switch")) {
    let view = null;
    try { view = localStorage.getItem(VIEW_KEY); } catch { /* storage blocked: the default view */ }
    if (view === "offers") { location.replace("/?view=offers"); return; }
  }
  document.addEventListener("click", (e) => {
    const tab = e.target.closest(".view-switch a[data-view]");
    if (tab) try { localStorage.setItem(VIEW_KEY, tab.dataset.view); } catch { /* not remembered */ }
  });
  document.documentElement.classList.add("has-js");

  const toasts = document.querySelector(".toasts");

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

  // --- Agent box --------------------------------------------------------------------------
  // One conversation under the pipeline list. While a message waits for the agent, or the open
  // panel's application has queued agent work (research, CV fit, tailored CV: data-busy), poll
  // /applications/thread every few seconds (for at most a few minutes). When it is done, refresh
  // the list, the panel and the conversation: the agent may have moved the stage, added tasks or
  // comments. Only one poll runs; opening another panel or closing it restarts it.
  const POLL_MS = 4000;
  const POLL_FOR_MS = 5 * 60 * 1000;
  const CHAT_KEY = "agent-filter";
  let poll = null;
  let refreshApps = () => location.reload(); // the Applications block below swaps the parts instead
  let showPanelAt = (url) => { location.href = url; return Promise.resolve(); }; // likewise, without a reload
  let chatFilter = "all";
  try { chatFilter = localStorage.getItem(CHAT_KEY) === "job" ? "job" : "all"; } catch { /* default */ }

  function stopPoll() {
    if (poll) clearTimeout(poll.timer);
    poll = null;
  }

  // Show only the open panel's messages ("This job": its application's, or a job offer's) or all of
  // them; the switch shows with a panel.
  function applyChatFilter() {
    const box = document.getElementById("agent");
    if (!box) return;
    const panel = document.getElementById("panel");
    const key = panel?.dataset.app ? ["app", panel.dataset.app] : panel?.dataset.job ? ["job", panel.dataset.job] : null;
    const only = chatFilter === "job" && key;
    box.querySelector(".agent-filter").hidden = !key;
    box.querySelectorAll("[data-filter-chat]").forEach((b) => b.classList.toggle("is-active", b.dataset.filterChat === (only ? "job" : "all")));
    box.querySelectorAll("li.msg").forEach((li) => { li.hidden = Boolean(only) && li.dataset[key[0]] !== key[1]; });
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
    const before = thread.querySelectorAll(".msg").length;
    poll = me;
    const tick = async () => {
      if (poll !== me) return;
      try {
        const res = await fetch(`/applications/thread${app ? `?app=${encodeURIComponent(app)}` : ""}`, {
          headers: { Accept: "application/json" }, credentials: "same-origin",
        });
        const reply = await res.json();
        if (poll !== me) return; // the panel changed while waiting
        if (reply.ok && !reply.pending) {
          poll = null;
          const replied = reply.messages !== before;
          await refreshApps();
          toast(replied ? "The agent replied." : "The agent's work is in.");
          return;
        }
      } catch { /* the server restarted or is busy: try again */ }
      if (Date.now() - me.started > POLL_FOR_MS) {
        poll = null;
        const state = document.querySelector("#agent .thread-state");
        if (state) state.textContent = "Still waiting. The reply shows here when the agent answers; reload to check.";
        return;
      }
      me.timer = setTimeout(tick, POLL_MS);
    };
    me.timer = setTimeout(tick, POLL_MS);
  }
  applyChatFilter();
  watchThread();

  // Cmd/Ctrl+Enter sends the message.
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" || !(e.metaKey || e.ctrlKey)) return;
    const form = e.target.closest?.("form.ask");
    if (!form) return;
    e.preventDefault();
    form.requestSubmit ? form.requestSubmit() : form.submit();
  });

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

    // Fetch `url` and swap in its panel and the Agent box's "About" line (and with `list`, the
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
        }
        swap(doc, "#agent .agent-about");
        const back = document.querySelector('#agent input[name="return"]');
        if (back) back.value = new URL(url, location.href).pathname + new URL(url, location.href).search;
        const next = doc.getElementById("panel");
        let panel = null;
        if (next) {
          panel = document.importNode(next, true);
          const same = current && current.dataset.app === panel.dataset.app && current.dataset.job === panel.dataset.job;
          const scroll = same ? current.scrollTop : 0;
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
        if (push) history.pushState(null, "", url);
        applyChatFilter();
        watchThread();
      } catch (err) {
        current?.classList.remove("is-loading");
        toast(err.message, { error: true });
      }
    }

    refreshApps = () => showPanel(location.href, false, { list: true });
    showPanelAt = (url) => showPanel(url, true, { list: true });

    function closePanel() {
      const panel = document.getElementById("panel");
      if (!panel) return;
      const href = panel.querySelector("[data-close-panel]").getAttribute("href");
      const row = rowFor(panel);
      showPanel(href, true); // the page without a panel: About goes back to the whole search
      row?.querySelector("a")?.focus({ preventScroll: true });
    }

    document.addEventListener("click", (e) => {
      if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      if (e.target.closest("[data-close-panel]")) { e.preventDefault(); document.body.classList.remove("is-asking"); closePanel(); return; }
      if (e.target.closest("[data-ask-agent]")) { // phones: leave the sheet for the Agent box, About kept
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
      const link = e.target.closest(".app-list td a:not([target]), a.msg-about");
      const row = !link && !e.target.closest("form, button") && e.target.closest(".app-list tr[data-href]");
      if (!link && !row) return;
      e.preventDefault();
      document.body.classList.remove("is-asking"); // a chosen job opens as the sheet again on phones
      const href = link ? link.getAttribute("href") : row.dataset.href;
      // A message's tag may point at the other view (a job offer from the pipeline): swap the list too.
      const viewOf = (u) => new URL(u, location.href).searchParams.get("view") || "pipeline";
      showPanel(href, true, { list: viewOf(href) !== viewOf(location.href) });
    });
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape" || document.querySelector("dialog[open]")) return;
      if (e.target instanceof Element && e.target.closest("input, select, textarea")) return;
      closePanel();
    });
    window.addEventListener("popstate", () => showPanel(location.href, false, { list: true }));

    const focusRow = document.querySelector(".app-list tr.is-focus");
    if (focusRow) focusRow.scrollIntoView({ block: "nearest" });
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

  document.addEventListener("submit", async (e) => {
    const form = e.target.closest("form[data-ajax]");
    if (!form || !window.fetch) return;
    e.preventDefault();
    const button = form.querySelector("button");
    if (button) button.disabled = true;
    const mode = form.dataset.ajax;
    try {
      const reply = await post(form.action, new URLSearchParams(new FormData(form)));
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
      } else if (mode === "thread") {
        // The Agent box: swap in the server-built, escaped conversation and wait for the reply.
        const thread = form.closest(".agent")?.querySelector("[data-thread]");
        if (thread && reply.html) thread.outerHTML = reply.html;
        form.elements.body.value = "";
        applyChatFilter();
        watchThread();
        toast(reply.message);
      }
    } catch (err) {
      toast(err.message || "Not saved.", { error: true });
    } finally {
      if (button) button.disabled = false;
    }
  });
})();
