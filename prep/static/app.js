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
    history.replaceState(null, "", location.pathname);
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

  // --- forms without a reload --------------------------------------------------------
  function refreshCount(tableId) {
    const table = document.getElementById(tableId);
    const chip = document.querySelector(`[data-count-for="${CSS.escape(tableId)}"]`);
    if (table && chip) {
      chip.textContent = Array.from(table.tBodies[0].rows).filter((r) => !r.classList.contains("no-results")).length;
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

  function undoDone(row, place, taskId, tableId) {
    post("/tasks/reopen", new URLSearchParams({ task_id: taskId }))
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
        const taskId = form.elements.task_id.value;
        toast(reply.message, { action: { label: "Undo", run: () => undoDone(row, place, taskId, tableId) } });
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
