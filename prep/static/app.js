// getjo dashboard enhancements: sortable tables, filtering, forms without a reload,
// toasts with undo, the Applications panel and its refresh. Every page and form works without
// this file; it only adds to them.
// No external requests: fetch goes to this page's own origin only (relative URLs).
"use strict";

(() => {
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

  // "/" focuses the first filter box, as in many web apps.
  document.addEventListener("keydown", (e) => {
    if (e.key !== "/" || e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.target.closest("input, select, textarea, [contenteditable]")) return;
    const input = Array.from(filters).find((f) => !f.hidden);
    if (input) { e.preventDefault(); input.focus(); }
  });

  // --- Job Offers filters -----------------------------------------------------------
  // The filter bar is a GET form, so every view has a URL. Selects and checkboxes reload
  // with the new query; company and role narrow the loaded rows as you type and keep the
  // URL (and the forms' return fields) in step. Widening a search needs rows that were not
  // loaded, so it reloads after a pause and puts the cursor back in the box.
  const live = document.querySelector("form[data-live-filters]");
  if (live) {
    const FOCUS_KEY = "offers-focus";
    const table = document.getElementById("offers");
    const value = (name) => live.elements[name].value.trim().toLowerCase();
    const loaded = { company: value("company"), role: value("role") };
    let timer = null;

    const submit = () => (live.requestSubmit ? live.requestSubmit() : live.submit());
    live.querySelector("button")?.setAttribute("hidden", "");
    live.addEventListener("change", (e) => { if (e.target.type !== "search") submit(); });

    function narrow(name) {
      const company = value("company");
      const role = value("role");
      clearTimeout(timer);
      if (!company.includes(loaded.company) || !role.includes(loaded.role)) {
        timer = setTimeout(() => {
          try { sessionStorage.setItem(FOCUS_KEY, name); } catch { /* storage blocked: lose focus only */ }
          submit();
        }, 500);
        return;
      }
      if (table) {
        for (const row of table.tBodies[0].rows) {
          if (row.classList.contains("no-results")) continue;
          row.hidden = !(row.dataset.company || "").includes(company) || !(row.dataset.role || "").includes(role);
        }
        refreshCount("offers");
      }
      const url = new URL(location.href);
      url.searchParams.delete("msg");
      for (const key of ["company", "role"]) {
        const v = live.elements[key].value.trim();
        if (v) url.searchParams.set(key, v); else url.searchParams.delete(key);
      }
      history.replaceState(null, "", url);
      document.querySelectorAll('input[name="return"]').forEach((i) => { i.value = url.pathname + url.search; });
    }

    for (const name of ["company", "role"]) {
      live.elements[name].addEventListener("input", () => narrow(name));
    }
    let refocus = null;
    try { refocus = sessionStorage.getItem(FOCUS_KEY); sessionStorage.removeItem(FOCUS_KEY); } catch { /* none */ }
    const box = refocus && live.elements[refocus];
    if (box) { box.focus(); box.setSelectionRange(box.value.length, box.value.length); }
  }

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
  let chatFilter = "all";
  try { chatFilter = localStorage.getItem(CHAT_KEY) === "job" ? "job" : "all"; } catch { /* default */ }

  function stopPoll() {
    if (poll) clearTimeout(poll.timer);
    poll = null;
  }

  // Show only the open job's messages ("This job") or all of them; the switch shows with a panel.
  function applyChatFilter() {
    const box = document.getElementById("agent");
    if (!box) return;
    const app = document.getElementById("panel")?.dataset.app || "";
    const only = chatFilter === "job" && app;
    box.querySelector(".agent-filter").hidden = !app;
    box.querySelectorAll("[data-filter-chat]").forEach((b) => b.classList.toggle("is-active", b.dataset.filterChat === (only ? "job" : "all")));
    box.querySelectorAll("li.msg").forEach((li) => { li.hidden = only && li.dataset.app !== app; });
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
  // The server renders every state from the query string (?app=ID side panel, ?stage=, ?closed=),
  // so this only adds: the panel opens without a reload, Esc closes it, and after any change the
  // list and the panel are fetched again and swapped in (refreshApps), keeping the scroll position.
  const apps = document.querySelector(".apps");
  if (apps) {
    function markFocus(appId) {
      document.querySelectorAll(".app-list tr.is-focus").forEach((el) => el.classList.remove("is-focus"));
      if (appId) document.getElementById(`app-${appId}`)?.classList.add("is-focus");
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
        }
        swap(doc, "#agent .agent-about");
        const back = document.querySelector('#agent input[name="return"]');
        if (back) back.value = new URL(url, location.href).pathname + new URL(url, location.href).search;
        const next = doc.getElementById("panel");
        if (next) {
          const panel = document.importNode(next, true);
          const scroll = current ? current.scrollTop : 0;
          if (current) current.replaceWith(panel); else apps.append(panel);
          if (list) {
            panel.scrollTop = scroll; // a refresh keeps the reader's place
          } else {
            panel.querySelector(".panel-close")?.focus({ preventScroll: true });
          }
        } else {
          current?.remove();
        }
        markFocus(next ? next.dataset.app : null);
        if (push) history.pushState(null, "", url);
        applyChatFilter();
        watchThread();
      } catch (err) {
        current?.classList.remove("is-loading");
        toast(err.message, { error: true });
      }
    }

    refreshApps = () => showPanel(location.href, false, { list: true });

    function closePanel() {
      const panel = document.getElementById("panel");
      if (!panel) return;
      const href = panel.querySelector("[data-close-panel]").getAttribute("href");
      const appId = panel.dataset.app;
      showPanel(href, true); // the page without a panel: About goes back to the whole search
      document.querySelector(`#app-${CSS.escape(appId)} a`)?.focus({ preventScroll: true });
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
      const link = e.target.closest(".app-list a, a.msg-about");
      const row = !link && e.target.closest(".app-list tr[data-href]");
      if (!link && !row) return;
      e.preventDefault();
      document.body.classList.remove("is-asking"); // a chosen job opens as the sheet again on phones
      showPanel(link ? link.getAttribute("href") : row.dataset.href, true);
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
  function refreshCount(tableId) {
    const table = document.getElementById(tableId);
    const chip = document.querySelector(`[data-count-for="${CSS.escape(tableId)}"]`);
    if (table && chip) {
      chip.textContent = Array.from(table.tBodies[0].rows).filter((r) => !r.classList.contains("no-results") && !r.hidden).length;
    }
  }

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

  function removeRow(row, tableId) {
    const place = { parent: row.parentNode, next: row.nextSibling };
    row.classList.add("is-leaving");
    place.timer = setTimeout(() => { row.remove(); refreshCount(tableId); }, 200);
    return place;
  }

  // Undo a removal: post `url` with `params`, then put the row back where it was.
  function undo(row, place, url, params, tableId) {
    post(url, new URLSearchParams(params))
      .then((reply) => {
        clearTimeout(place.timer); // a fast undo can land before the fade-out removes the row
        row.classList.remove("is-leaving");
        row.classList.add("is-new");
        place.parent.insertBefore(row, place.next && place.next.parentNode === place.parent ? place.next : null);
        refreshCount(tableId);
        toast(reply.message);
      })
      .catch((err) => toast(err.message, { error: true }));
  }

  // --- Skip pop-up -----------------------------------------------------------------------
  // A Skip link opens the dialog instead of the skip page. Clicking a reason posts the skip. Job
  // Offers: on the New and All views the row leaves with an Undo toast, elsewhere the page reloads.
  // Applications (data-after="refresh"): the list and the panel are refreshed.
  const dialog = document.getElementById("skip-dialog");
  if (dialog && dialog.showModal && window.fetch) {
    const form = dialog.querySelector("form");
    let opener = null;
    document.addEventListener("click", (e) => {
      const link = e.target.closest("a[data-skip]");
      if (!link) return;
      e.preventDefault();
      opener = link;
      const row = link.closest("tr");
      form.reset();
      form.elements[form.dataset.field].value = link.dataset.skip; // job_id on Job Offers, app_id on Applications
      form.elements.return.value = location.pathname + location.search;
      dialog.querySelector("[data-skip-job]").textContent = link.dataset.skipLabel
        || (row ? `${row.cells[1].textContent.trim()} — ${row.cells[2].textContent.trim()}` : "");
      dialog.showModal();
      form.elements.comment.focus();
    });
    dialog.querySelector("[data-close]").addEventListener("click", () => dialog.close());
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!e.submitter || e.submitter.name !== "reason") return; // Enter in the comment: pick a reason
      const data = new URLSearchParams(new FormData(form, e.submitter)); // includes the clicked reason
      const link = opener;
      const buttons = form.querySelectorAll("button");
      buttons.forEach((b) => { b.disabled = true; });
      try {
        const reply = await post(form.action, data);
        dialog.close();
        if (link?.dataset.after === "refresh") { await refreshApps(); toast(reply.message); return; }
        const row = link && link.closest("tr");
        if (!row || !("remove" in link.dataset)) { location.reload(); return; }
        const tableId = row.closest("table").id;
        const place = removeRow(row, tableId);
        const params = { job_id: data.get("job_id") };
        toast(reply.message, { action: { label: "Undo", run: () => undo(row, place, "/offers/unskip", params, tableId) } });
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
    const row = form.closest("tr");
    const tableId = row ? row.closest("table").id : form.dataset.target;
    try {
      const reply = await post(form.action, new URLSearchParams(new FormData(form)));
      if (mode === "remove-row" && row) {
        removeRow(row, tableId);
        toast(reply.message);
      } else if (mode === "interested" && row) {
        // Job Offers: the row stays, its actions cell becomes "Interested · research queued".
        const cell = form.closest("td");
        const before = Array.from(cell.childNodes);
        cell.innerHTML = reply.html; // a cell the server built and escaped
        row.classList.add("is-new");
        const params = new URLSearchParams({ job_id: form.elements.job_id.value });
        const undoIt = () => post("/offers/uninterested", params)
          .then((r) => { cell.replaceChildren(...before); toast(r.message); })
          .catch((err) => toast(err.message, { error: true }));
        // Undo only for an application this click created (never one that existed before).
        toast(reply.message, reply.created ? { action: { label: "Undo", run: undoIt } } : {});
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
