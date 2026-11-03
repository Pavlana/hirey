// getjo dashboard enhancements: sortable tables, filtering, forms without a reload,
// toasts with undo. Every page and form works without this file; it only adds to them.
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
  // ?app=ID (e.g. from Job Offers) renders that card open and highlighted; bring it into view.
  const focusCard = document.querySelector(".app-card.is-focus");
  if (focusCard) focusCard.scrollIntoView({ block: "center", inline: "center" });

  // Move to…: the interview date only matters for interview stages, so show it only then.
  document.querySelectorAll("form.move[data-interview-stages]").forEach((form) => {
    const stages = form.dataset.interviewStages.split(" ");
    const field = form.querySelector(".interview-date");
    const sync = () => { field.hidden = !stages.includes(form.elements.status.value); };
    form.elements.status.addEventListener("change", sync);
    sync();
  });

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
