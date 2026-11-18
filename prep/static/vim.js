// A small vim layer for the Learn Notes editor: pure key handling over {text, pos}, no DOM.
// app.js feeds it the textarea's keys when vim keys are on, and writes the result back.
// Normal mode: h j k l, w b, 0 $, i a o O, x dd yy p, u, ":" (":w" saves), Esc.
// Insert mode is the textarea itself; only Esc is taken (back to normal, one character left).
// Tested under node by tests/vim_test.js (run from tests/test_vim.py).
"use strict";

const HireyVim = (() => {
  const UNDO_LIMIT = 100;
  const EDITING_KEYS = ["Backspace", "Delete", "Enter"];

  const lineStart = (t, p) => (p <= 0 ? 0 : t.lastIndexOf("\n", p - 1) + 1);  // p - 1 = -1 would search from 0
  const lineEnd = (t, p) => { const i = t.indexOf("\n", p); return i < 0 ? t.length : i; };
  // In normal mode the cursor sits on a character, never past the end of a line.
  const onChar = (t, p) => {
    const s = lineStart(t, p), e = lineEnd(t, p);
    return Math.max(s, Math.min(p, e > s ? e - 1 : s));
  };
  const isWord = (c) => /[\p{L}\p{N}_]/u.test(c);
  const kind = (c) => (c === undefined ? 0 : /\s/.test(c) ? 0 : isWord(c) ? 1 : 2);

  function create(text = "", pos = 0) {
    return { text, pos: onChar(text, pos), mode: "normal", pending: "", cmd: "", reg: { text: "", line: false }, undo: [] };
  }

  function position(text, pos) {
    const before = text.slice(0, pos);
    const line = before.split("\n").length;
    return { line, col: pos - lineStart(text, pos) + 1 };
  }

  function snapshot(s) {
    s.undo = s.undo.concat([{ text: s.text, pos: s.pos }]).slice(-UNDO_LIMIT);
  }

  function nextWord(t, p) {
    const k = kind(t[p]);
    let i = p;
    if (k) while (i < t.length && kind(t[i]) === k) i++;
    while (i < t.length && kind(t[i]) === 0) i++;
    return Math.min(i, Math.max(t.length - 1, 0));
  }

  function prevWord(t, p) {
    let i = p - 1;
    while (i > 0 && kind(t[i]) === 0) i--;
    const k = kind(t[i]);
    while (i > 0 && kind(t[i - 1]) === k && k) i--;
    return Math.max(i, 0);
  }

  function vertical(t, p, down) {
    const s = lineStart(t, p), col = p - s;
    let target;
    if (down) {
      const e = lineEnd(t, p);
      if (e >= t.length) return p;
      target = e + 1;
    } else {
      if (s === 0) return p;
      target = lineStart(t, s - 1);
    }
    return onChar(t, Math.min(target + col, lineEnd(t, target)));
  }

  // Returns { state, handled, save, changed }. handled false: let the textarea have the key.
  function step(prev, key) {
    const s = { ...prev };
    const out = (handled = true, extra = {}) => ({ state: s, handled, save: false, changed: s.text !== prev.text, ...extra });
    const t = s.text;

    if (s.mode === "insert") {
      if (key !== "Escape") return out(false);
      s.mode = "normal";
      s.pos = onChar(t, s.pos > lineStart(t, s.pos) ? s.pos - 1 : s.pos);
      return out();
    }

    if (s.mode === "command") {
      if (key === "Escape") { s.mode = "normal"; s.cmd = ""; return out(); }
      if (key === "Enter") {
        const cmd = s.cmd.trim();
        s.mode = "normal"; s.cmd = "";
        return out(true, { save: cmd === "w" || cmd === "w!" || cmd === "wq" || cmd === "x" });
      }
      if (key === "Backspace") {
        if (s.cmd) s.cmd = s.cmd.slice(0, -1);
        else s.mode = "normal";
        return out();
      }
      if (key.length === 1) s.cmd += key;
      return out();
    }

    // normal mode
    if (s.pending) {
      const op = s.pending;
      s.pending = "";
      if (key === op && op === "d") {
        const ls = lineStart(t, s.pos), le = lineEnd(t, s.pos);
        snapshot(s);
        s.reg = { text: t.slice(ls, le) + "\n", line: true };
        let from = ls, to = le;
        if (le < t.length) to = le + 1;
        else if (ls > 0) from = ls - 1;
        s.text = t.slice(0, from) + t.slice(to);
        const at = Math.min(from === ls ? ls : lineStart(s.text, from), s.text.length);
        s.pos = onChar(s.text, at);
        return out();
      }
      if (key === op && op === "y") {
        s.reg = { text: t.slice(lineStart(t, s.pos), lineEnd(t, s.pos)) + "\n", line: true };
        return out();
      }
      return out(); // anything else cancels the operator
    }

    const enterInsert = (pos) => { snapshot(s); s.pos = pos; s.mode = "insert"; };
    switch (key) {
      case "Escape": return out();
      case "h": s.pos = Math.max(lineStart(t, s.pos), s.pos - 1); return out();
      case "l": s.pos = onChar(t, s.pos + 1 <= lineEnd(t, s.pos) ? s.pos + 1 : s.pos); return out();
      case "j": s.pos = vertical(t, s.pos, true); return out();
      case "k": s.pos = vertical(t, s.pos, false); return out();
      case "w": s.pos = nextWord(t, s.pos); return out();
      case "b": s.pos = prevWord(t, s.pos); return out();
      case "0": s.pos = lineStart(t, s.pos); return out();
      case "$": s.pos = onChar(t, lineEnd(t, s.pos)); return out();
      case "i": enterInsert(s.pos); return out();
      case "a": enterInsert(Math.min(s.pos + 1, lineEnd(t, s.pos))); return out();
      case "o": {
        const e = lineEnd(t, s.pos);
        snapshot(s);
        s.text = t.slice(0, e) + "\n" + t.slice(e);
        s.pos = e + 1; s.mode = "insert";
        return out();
      }
      case "O": {
        const ls = lineStart(t, s.pos);
        snapshot(s);
        s.text = t.slice(0, ls) + "\n" + t.slice(ls);
        s.pos = ls; s.mode = "insert";
        return out();
      }
      case "x": {
        if (s.pos >= lineEnd(t, s.pos)) return out();
        snapshot(s);
        s.reg = { text: t[s.pos], line: false };
        s.text = t.slice(0, s.pos) + t.slice(s.pos + 1);
        s.pos = onChar(s.text, s.pos);
        return out();
      }
      case "d": case "y": s.pending = key; return out();
      case "p": {
        if (!s.reg.text) return out();
        snapshot(s);
        if (s.reg.line) {
          const e = lineEnd(t, s.pos);
          s.text = t.slice(0, e) + "\n" + s.reg.text.replace(/\n$/, "") + t.slice(e);
          s.pos = e + 1;
        } else {
          const at = s.pos < lineEnd(t, s.pos) ? s.pos + 1 : s.pos;
          s.text = t.slice(0, at) + s.reg.text + t.slice(at);
          s.pos = at + s.reg.text.length - 1;
        }
        return out();
      }
      case "u": {
        if (!s.undo.length) return out();
        const last = s.undo[s.undo.length - 1];
        s.undo = s.undo.slice(0, -1);
        s.text = last.text;
        s.pos = onChar(s.text, last.pos);
        return out();
      }
      case ":": s.mode = "command"; s.cmd = ""; return out();
      default:
        // Other printable keys, and the named keys that would edit the one-character selection that
        // shows the cursor, do nothing in normal mode; arrows, Tab and the rest stay with the textarea.
        return out(key.length === 1 || EDITING_KEYS.includes(key));
    }
  }

  return { create, step, position };
})();

if (typeof module !== "undefined") module.exports = HireyVim;
