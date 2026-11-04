// getjo dashboard enhancements: sortable tables, filtering, forms without a reload,
// toasts with undo. Every page and form works without this file; it only adds to them.
// No external requests: fetch goes to this page's own origin only (relative URLs).
"use strict";

(() => {
  const toasts = document.querySelector(".toasts");

  // Applications: Board | List is remembered per browser; a URL without ?view= gets the
  // remembered one. This runs first, so a flash message (?msg=) survives the redirect.
  const VIEW_KEY = "applications-view";
  const appsPage = document.querySelector(".apps");
  if (appsPage) {
    let stored = null;
    try { stored = localStorage.getItem(VIEW_KEY); } catch { /* storage blocked: default view */ }
    const here = new URL(location.href);
    if (!here.searchParams.has("view") && stored === "list" && appsPage.dataset.view !== "list") {
      here.searchParams.set("view", "list");
      location.replace(here);
      return;
    }
  }

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

  document.querySelectorAll("th[data-sort]").forEach((th) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "sort";
    btn.textContent = th.textContent;
    btn.setAttribute("aria-label", `Sort by ${th.textContent}`);
    th.textContent = "";
    th.append(btn);
    btn.addEventListener("click", () => sortBy(th.closest("table"), th));
  });

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

  // --- Applications board ---------------------------------------------------------------
  // The server renders every state from the query string (?app=ID side panel, ?view=, ?stage=),
  // so this only adds: the panel opens without a reload, Esc closes it, the Board | List choice
  // is remembered, and cards can be dragged between columns.

  // Move to…: the interview date only matters for interview stages, so show it only then.
  function syncMoveForms(root) {
    root.querySelectorAll("form.move[data-interview-stages]").forEach((form) => {
      const stages = form.dataset.interviewStages.split(" ");
      const field = form.querySelector(".interview-date");
      const sync = () => { field.hidden = !stages.includes(form.elements.status.value); };
      form.elements.status.addEventListener("change", sync);
      sync();
    });
  }
  syncMoveForms(document);

  const apps = document.querySelector(".apps");
  if (apps) {
    document.querySelectorAll(".view-btn[data-view]").forEach((link) => {
      link.addEventListener("click", () => {
        try { localStorage.setItem(VIEW_KEY, link.dataset.view); } catch { /* not remembered */ }
      });
    });

    const focusCard = document.querySelector(".app-card.is-focus");
    if (focusCard) focusCard.scrollIntoView({ block: "nearest", inline: "nearest" });

    // Side panel: fetch the page for ?app=ID and swap in only its panel, so the board keeps
    // its scroll position. The fetched HTML is the server's own escaped page; scripts in a
    // DOMParser document never run.
    function markFocus(appId) {
      document.querySelectorAll(".app-card.is-focus, .app-list tr.is-focus").forEach((el) => el.classList.remove("is-focus"));
      if (!appId) return;
      document.getElementById(`app-${appId}`)?.classList.add("is-focus");
      document.querySelector(`.app-list tr[data-app="${CSS.escape(appId)}"]`)?.classList.add("is-focus");
    }

    async function showPanel(url, push) {
      const current = document.getElementById("panel");
      current?.classList.add("is-loading");
      try {
        const res = await fetch(url, { headers: { Accept: "text/html" }, credentials: "same-origin" });
        if (!res.ok) throw new Error(`Could not open it (HTTP ${res.status}).`);
        const doc = new DOMParser().parseFromString(await res.text(), "text/html");
        const next = doc.getElementById("panel");
        if (next) {
          const panel = document.importNode(next, true);
          if (current) current.replaceWith(panel); else apps.append(panel);
          syncMoveForms(panel);
          panel.querySelector(".panel-close")?.focus({ preventScroll: true });
        } else {
          current?.remove();
        }
        markFocus(next ? next.dataset.app : null);
        if (push) history.pushState(null, "", url);
      } catch (err) {
        current?.classList.remove("is-loading");
        toast(err.message, { error: true });
      }
    }

    function closePanel() {
      const panel = document.getElementById("panel");
      if (!panel) return;
      const href = panel.querySelector("[data-close-panel]").getAttribute("href");
      const appId = panel.dataset.app;
      panel.remove();
      markFocus(null);
      history.pushState(null, "", href);
      document.getElementById(`app-${appId}`)?.focus({ preventScroll: true });
    }

    document.addEventListener("click", (e) => {
      if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      if (e.target.closest("[data-close-panel]")) { e.preventDefault(); closePanel(); return; }
      const link = e.target.closest("a.app-card, .app-list a");
      const row = !link && e.target.closest(".app-list tr[data-href]");
      if (!link && !row) return;
      if (row && e.target.closest("button, input, select, form")) return;
      e.preventDefault();
      showPanel(link ? link.getAttribute("href") : row.dataset.href, true);
    });
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape" || document.querySelector("dialog[open]")) return;
      if (e.target instanceof Element && e.target.closest("input, select, textarea")) return;
      closePanel();
    });
    window.addEventListener("popstate", () => showPanel(location.href, false));

    // Drag and drop between columns (native HTML5 events). Interested, Applied and Offer move at
    // once; Interviewing asks for the step and date, Closed for the outcome. Every move posts to
    // /applications/move, then the page reloads from the server with a "Moved" toast.
    const dialog = document.getElementById("move-dialog");
    const STATUS_OF = { interested: "to_apply", applied: "applied", offer: "offer" };
    const STEPS = ["recruiter_call", "technical", "system_design", "final"];
    let dragged = null;

    function reloadMoved() {
      const url = new URL(location.href);
      url.searchParams.set("msg", "moved");
      location.assign(url);
    }

    async function move(data) {
      try {
        await post("/applications/move", data);
        reloadMoved();
      } catch (err) {
        toast(err.message || "Not moved.", { error: true });
      }
    }

    function askMove(card, column) {
      const form = dialog.querySelector("form");
      form.reset();
      form.elements.app_id.value = card.dataset.app;
      form.elements.return.value = location.pathname + location.search;
      const interviewing = column === "interviewing";
      dialog.querySelectorAll("[data-group]").forEach((el) => {
        el.hidden = el.dataset.group !== column;
        el.querySelectorAll("select, input").forEach((input) => { input.disabled = el.hidden; });
      });
      dialog.querySelector("[data-move-title]").textContent = interviewing ? "Move to Interviewing" : "Close this application";
      dialog.querySelector("[data-move-app]").textContent = card.querySelector(".app-head")?.textContent || "";
      if (interviewing) {
        const at = STEPS.indexOf(card.dataset.status);
        form.querySelector('select[name="status"]').value = STEPS[Math.min(at + 1, STEPS.length - 1)];
      }
      dialog.showModal();
      (interviewing ? form.querySelector('select[name="status"]') : form.elements.comment).focus();
    }

    if (dialog && dialog.showModal && window.fetch) {
      const form = dialog.querySelector("form");
      dialog.querySelector("[data-close]").addEventListener("click", () => dialog.close());
      form.addEventListener("submit", async (e) => {
        e.preventDefault();
        const data = new URLSearchParams(new FormData(form, e.submitter)); // an outcome button adds its status
        if (!data.get("status")) return;
        const buttons = form.querySelectorAll("button");
        buttons.forEach((b) => { b.disabled = true; });
        try {
          await post(form.action, data);
          dialog.close();
          reloadMoved();
        } catch (err) {
          toast(err.message || "Not moved.", { error: true });
        } finally {
          buttons.forEach((b) => { b.disabled = false; });
        }
      });

      document.addEventListener("dragstart", (e) => {
        const card = e.target.closest?.("a.app-card");
        if (!card) return;
        dragged = card;
        e.dataTransfer.effectAllowed = "move";
        e.dataTransfer.setData("text/plain", card.dataset.app);
        card.classList.add("is-dragging");
      });
      document.addEventListener("dragend", () => {
        dragged?.classList.remove("is-dragging");
        dragged = null;
        document.querySelectorAll(".col.is-drop").forEach((c) => c.classList.remove("is-drop"));
      });
      document.querySelectorAll("[data-drop]").forEach((col) => {
        col.addEventListener("dragover", (e) => {
          if (!dragged || dragged.dataset.column === col.dataset.drop) return;
          e.preventDefault(); // allows the drop
          e.dataTransfer.dropEffect = "move";
          col.classList.add("is-drop");
        });
        col.addEventListener("dragleave", (e) => {
          if (!col.contains(e.relatedTarget)) col.classList.remove("is-drop");
        });
        col.addEventListener("drop", (e) => {
          e.preventDefault();
          col.classList.remove("is-drop");
          const card = dragged;
          const column = col.dataset.drop;
          if (!card || card.dataset.column === column) return; // same column: nothing to do
          if (STATUS_OF[column]) {
            move(new URLSearchParams({ app_id: card.dataset.app, status: STATUS_OF[column] }));
          } else {
            askMove(card, column);
          }
        });
      });
    }
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

  // --- Skipped pop-up ------------------------------------------------------------------
  // A row's Skipped link opens the dialog instead of the skip page. Clicking a reason posts the
  // skip; on the New and All views the row leaves with an Undo toast, elsewhere the page reloads.
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
      form.elements.job_id.value = link.dataset.skip;
      form.elements.return.value = location.pathname + location.search;
      dialog.querySelector("[data-skip-job]").textContent =
        row ? `${row.cells[1].textContent.trim()} — ${row.cells[2].textContent.trim()}` : "";
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
      } else if (mode === "done" && row) {
        const place = removeRow(row, tableId);
        const params = { task_id: form.elements.task_id.value };
        toast(reply.message, { action: { label: "Undo", run: () => undo(row, place, "/tasks/reopen", params, tableId) } });
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
      } else if (mode === "add-row") {
        const table = document.getElementById(form.dataset.target);
        if (!table || !reply.html) { location.reload(); return; } // was an empty state
        // reply.html is a row the server built and escaped.
        table.tBodies[0].insertAdjacentHTML("afterbegin", reply.html);
        table.tBodies[0].rows[0].classList.add("is-new");
        refreshCount(form.dataset.target);
        form.reset();
        form.elements.title?.focus();
        toast(reply.message);
      }
    } catch (err) {
      toast(err.message || "Not saved.", { error: true });
    } finally {
      if (button) button.disabled = false;
    }
  });
})();
