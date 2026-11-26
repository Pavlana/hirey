// Hirey explainers: the standard script every explainer page loads (/static/explainer.js). The page
// runs in a sandboxed iframe on the Learn page (no same origin), so it can only talk to the page
// around it with postMessage: its height (so the frame fits it) and the text the user selects (so
// "Ask about this" can quote it). Nothing else is sent, and nothing is fetched.
"use strict";

(() => {
  const parent = window.parent;
  if (!parent || parent === window) return;
  const MAX = 1000;
  const send = (msg) => parent.postMessage(msg, "*"); // the parent checks the source; only text and numbers

  let last = 0;
  const height = () => {
    const px = Math.ceil(document.documentElement.scrollHeight);
    if (px !== last) { last = px; send({ type: "height", px }); }
  };
  new ResizeObserver(height).observe(document.documentElement);
  window.addEventListener("load", height);
  height();

  document.addEventListener("mouseup", () => {
    setTimeout(() => {
      const sel = getSelection();
      const text = sel && !sel.isCollapsed ? sel.toString().replace(/\s+/g, " ").trim().slice(0, MAX) : "";
      let rect = null;
      if (text && sel.rangeCount) {
        const r = sel.getRangeAt(0).getBoundingClientRect();
        rect = { left: r.left, top: r.top, width: r.width, height: r.height };
      }
      send({ type: "selection", text, rect });
    }, 0);
  });
})();
