// The Learn editor's vim keys (prep/static/vim.js), pure key handling. Run by tests/test_vim.py
// under node when node is installed; exits non-zero on the first failure.
"use strict";
const assert = require("assert");
const path = require("path");
const Vim = require(path.join(__dirname, "..", "prep", "static", "vim.js"));

function keys(text, pos, seq) {
  let s = Vim.create(text, pos);
  let saved = false;
  for (const k of seq) {
    const r = Vim.step(s, k);
    s = r.state;
    saved = saved || r.save;
  }
  return { ...s, saved };
}

const tests = {
  "h l stay on the line"() {
    assert.strictEqual(keys("ab\ncd", 0, ["h"]).pos, 0);
    assert.strictEqual(keys("ab\ncd", 0, ["l", "l", "l"]).pos, 1);
  },
  "j k keep the column, clamped"() {
    assert.strictEqual(keys("abcd\nxy\nlong", 3, ["j"]).pos, 6); // "y"
    assert.strictEqual(keys("abcd\nxy\nlong", 3, ["j", "j"]).pos, 9); // col 1 of "long"
    assert.strictEqual(keys("abcd\nxy", 6, ["k"]).pos, 1);
    assert.strictEqual(keys("one", 1, ["k", "j"]).pos, 1); // nowhere to go
  },
  "w b move by words"() {
    const t = "foo bar, baz";
    assert.strictEqual(keys(t, 0, ["w"]).pos, 4);
    assert.strictEqual(keys(t, 0, ["w", "w"]).pos, 7); // the comma
    assert.strictEqual(keys(t, 0, ["w", "w", "w"]).pos, 9);
    assert.strictEqual(keys(t, 9, ["b"]).pos, 7);
    assert.strictEqual(keys(t, 9, ["b", "b"]).pos, 4);
    assert.strictEqual(keys("a\n  b", 0, ["w"]).pos, 4); // across a line break
  },
  "0 and $"() {
    assert.strictEqual(keys("x\nhello", 4, ["0"]).pos, 2);
    assert.strictEqual(keys("x\nhello", 2, ["$"]).pos, 6);
  },
  "i a o O enter insert mode"() {
    assert.deepStrictEqual([keys("ab", 1, ["i"]).mode, keys("ab", 1, ["i"]).pos], ["insert", 1]);
    assert.strictEqual(keys("ab", 1, ["a"]).pos, 2);
    const o = keys("ab\ncd", 0, ["o"]);
    assert.deepStrictEqual([o.text, o.pos, o.mode], ["ab\n\ncd", 3, "insert"]);
    const O = keys("ab\ncd", 4, ["O"]);
    assert.deepStrictEqual([O.text, O.pos], ["ab\n\ncd", 3]);
  },
  "insert mode leaves keys to the textarea; Esc steps back one"() {
    let s = Vim.step(Vim.create("abc", 0), "a").state; // insert at 1
    const r = Vim.step(s, "z");
    assert.strictEqual(r.handled, false);
    s = { ...s, text: "azbc", pos: 2 }; // the textarea typed it
    s = Vim.step(s, "Escape").state;
    assert.deepStrictEqual([s.mode, s.pos], ["normal", 1]);
  },
  "x deletes a character, not the line break"() {
    const r = keys("ab\ncd", 1, ["x"]);
    assert.deepStrictEqual([r.text, r.pos], ["a\ncd", 0]);
    assert.strictEqual(keys("\ncd", 0, ["x"]).text, "\ncd");
  },
  "dd deletes the line; p puts it below"() {
    const d = keys("one\ntwo\nthree", 5, ["d", "d"]);
    assert.deepStrictEqual([d.text, d.pos], ["one\nthree", 4]);
    const last = keys("one\ntwo", 5, ["d", "d"]);
    assert.deepStrictEqual([last.text, last.pos], ["one", 0]);
    const p = keys("one\ntwo\nthree", 0, ["d", "d", "p"]);
    assert.strictEqual(p.text, "two\none\nthree");
    assert.strictEqual(keys("only", 2, ["d", "d"]).text, "");
  },
  "yy p copies the line; x p moves a character"() {
    assert.strictEqual(keys("a\nb", 0, ["y", "y", "p"]).text, "a\na\nb");
    assert.strictEqual(keys("ab", 0, ["x", "p"]).text, "ba");
  },
  "d then another key cancels"() {
    const r = keys("abc", 0, ["d", "l"]);
    assert.deepStrictEqual([r.text, r.pos, r.pending], ["abc", 0, ""]);
  },
  "u undoes, one change at a time"() {
    assert.strictEqual(keys("abc", 0, ["x", "x", "u"]).text, "bc");
    assert.strictEqual(keys("abc", 0, ["x", "x", "u", "u"]).text, "abc");
    assert.strictEqual(keys("abc", 0, ["u"]).text, "abc");
    assert.strictEqual(keys("ab", 0, ["o", "Escape", "u"]).text, "ab");
  },
  ":w saves; Esc cancels a command"() {
    assert.strictEqual(keys("a", 0, [":", "w", "Enter"]).saved, true);
    const c = keys("a", 0, [":", "w", "Escape"]);
    assert.deepStrictEqual([c.saved, c.mode], [false, "normal"]);
    assert.strictEqual(keys("a", 0, [":", "q", "Enter"]).saved, false);
    assert.strictEqual(keys("a", 0, [":", "Backspace"]).mode, "normal");
  },
  "normal mode swallows other letters but not named keys"() {
    const s = Vim.create("abc", 0);
    assert.strictEqual(Vim.step(s, "z").handled, true);
    assert.strictEqual(Vim.step(s, "z").state.text, "abc");
    assert.strictEqual(Vim.step(s, "ArrowDown").handled, false);
    for (const k of ["Backspace", "Delete", "Enter"]) assert.strictEqual(Vim.step(s, k).handled, true);
  },
  "the state passed in is never changed"() {
    const s = Vim.create("abc", 0);
    Vim.step(s, "x");
    assert.deepStrictEqual([s.text, s.undo.length], ["abc", 0]);
  },
  "position is line,col from 1"() {
    assert.deepStrictEqual(Vim.position("ab\ncd", 4), { line: 2, col: 2 });
    assert.deepStrictEqual(Vim.position("", 0), { line: 1, col: 1 });
  },
};

let failed = 0;
for (const [name, fn] of Object.entries(tests)) {
  try { fn(); } catch (err) { failed++; console.error(`FAIL ${name}\n  ${err.message}`); }
}
console.log(`${Object.keys(tests).length - failed}/${Object.keys(tests).length} vim tests passed`);
process.exit(failed ? 1 : 0);
