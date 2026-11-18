"""Learn: tracks and cards (prep/learn.py), done or to do, and their CLI. Fictional cards only."""

import io
import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from prep import db, learn
from tests.helpers import IsolatedTestCase

TODAY = "2026-01-20"


def card(**fields) -> dict:
    return {"track": "toy", "chapter": "Basics", "front": "What is a widget?", "back": "- A made-up thing", **fields}


class LearnDbTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.conn = db.connect(Path(self._tmp.name) / "prep.db")
        self.addCleanup(self.conn.close)
        self.track = learn.add_track(self.conn, "Toy track", "fictional", slug="toy")

    def test_add_track(self) -> None:
        other = learn.add_track(self.conn, "Widgets & Gadgets!")
        t = learn.get_track(self.conn, other)
        self.assertEqual((t["slug"], t["position"], t["cover"]), ("widgets-gadgets", 2, learn.TRACK_COVERS[1]))
        self.assertEqual(learn.get_track(self.conn, "toy")["id"], self.track)
        with self.assertRaises(ValueError):
            learn.add_track(self.conn, "Toy", slug="toy")
        with self.assertRaises(ValueError):
            learn.add_track(self.conn, "Bad cover", cover="neon")
        with self.assertRaises(LookupError):
            learn.get_track(self.conn, "nope")

    def test_add_card(self) -> None:
        gap = db.add_gap(self.conn, "Widget trade-offs", TODAY)
        card_id = learn.add_card(self.conn, card(
            tags=["round 1", "asked often"], follow_ups=["And gadgets?"], source_gap_id=gap,
            sources=[{"kind": "watch", "title": "Widget talk", "url": "https://example.com/w", "minutes": 12,
                      "why": "clear demo"}]))
        c = learn.get_card(self.conn, card_id)
        self.assertEqual((c["track_slug"], c["chapter_title"], c["status"], c["done_at"]), ("toy", "Basics", "todo", None))
        self.assertEqual(c["tags"], ["round 1", "asked often"])
        self.assertEqual(c["sources"][0]["minutes"], 12)
        self.assertEqual(c["source_gap_id"], gap)
        second = learn.add_card(self.conn, card(chapter="Basics"))
        self.assertEqual(learn.get_card(self.conn, second)["chapter_id"], c["chapter_id"])  # chapter reused

    def test_card_validation(self) -> None:
        bad = (card(colour="red"), card(front=""), card(back=" "), card(track=None), card(track="nope"),
               card(tags="round 1"), card(follow_ups=[""]), card(sources=[{"kind": "listen", "title": "x"}]),
               card(sources=[{"kind": "read"}]), card(sources=[{"kind": "read", "title": "x", "minutes": "ten"}]),
               card(sources=[{"kind": "read", "title": "x", "pages": 3}]), card(source_gap_id=999))
        for c in bad:
            with self.subTest(c=c), self.assertRaises((ValueError, LookupError)):
                learn.add_card(self.conn, c)
        self.assertEqual(learn.card_count(self.conn), 0)

    def test_add_cards_is_all_or_none(self) -> None:
        with self.assertRaises(ValueError):
            learn.add_cards(self.conn, [card(chapter="New chapter"), card(front="")])
        self.assertEqual(learn.card_count(self.conn), 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM chapters").fetchone()[0], 0)
        self.assertEqual(len(learn.add_cards(self.conn, [card(), card(chapter="Later")])), 2)

    def test_done_and_undo(self) -> None:
        card_id = learn.add_card(self.conn, card())
        c = learn.set_status(self.conn, card_id, "done")
        self.assertEqual(c["status"], "done")
        self.assertIsNotNone(c["done_at"])
        c = learn.set_status(self.conn, card_id, "todo")
        self.assertEqual((c["status"], c["done_at"]), ("todo", None))
        with self.assertRaises(ValueError):
            learn.set_status(self.conn, card_id, "skipped")
        with self.assertRaises(LookupError):
            learn.set_status(self.conn, 999, "done")

    def test_tick_and_untick_sources(self) -> None:
        card_id = learn.add_card(self.conn, card(sources=[
            {"kind": "watch", "title": "Widget talk", "url": "https://example.com/w"},
            {"kind": "read", "title": "  Widget notes  "}]))
        c = learn.get_card(self.conn, card_id)
        self.assertEqual(learn.source_progress(self.conn, c), ([False, False], 0))
        learn.set_source_done(self.conn, card_id, "read:Widget notes", True)
        learn.set_source_done(self.conn, card_id, "read:Widget notes", True)  # twice is fine
        self.assertEqual(learn.source_progress(self.conn, c), ([False, True], 1))
        self.assertEqual(learn.get_card(self.conn, card_id)["status"], "todo")
        self.assertNotIn("done", learn.get_card(self.conn, card_id)["sources"][1])  # sources stay as given
        learn.set_source_done(self.conn, card_id, "read:Widget notes", False)
        learn.set_source_done(self.conn, card_id, "read:Widget notes", False)
        self.assertEqual(learn.source_progress(self.conn, c), ([False, False], 0))
        with self.assertRaises(ValueError):
            learn.set_source_done(self.conn, card_id, "https://example.com/other", True)
        with self.assertRaises(LookupError):
            learn.set_source_done(self.conn, 999, "read:Widget notes", True)

    def test_delete_card_removes_its_ticks(self) -> None:
        card_id = learn.add_card(self.conn, card(sources=[{"kind": "read", "title": "Widget notes"}]))
        learn.set_source_done(self.conn, card_id, "read:Widget notes", True)
        learn.delete_card(self.conn, card_id)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM card_source_done").fetchone()[0], 0)

    def test_tracks_done_and_progress(self) -> None:
        learn.add_track(self.conn, "Empty track")
        ids = learn.add_cards(self.conn, [card(), card(chapter="Two"), card(), card()])
        learn.set_status(self.conn, ids[0], "done")
        toy, empty = learn.list_tracks(self.conn)
        self.assertEqual((toy["chapters"], toy["cards"], toy["done"], toy["todo"], toy["progress"]), (2, 4, 1, 3, 25))
        self.assertEqual((empty["cards"], empty["done"], empty["progress"]), (0, 0, 0))
        self.assertEqual([c["id"] for c in learn.list_cards(self.conn, status="todo")], [ids[2], ids[3], ids[1]])  # chapter order
        self.assertEqual([c["id"] for c in learn.list_cards(self.conn, track="toy", status="done")], [ids[0]])
        with self.assertRaises(ValueError):
            learn.list_cards(self.conn, status="due")


IMPORT = """Preamble before the first track: ignored.

# Toy track
slug: toy
subtitle: fictional cards

## Basics

### What is a widget?
tags: confirmed, unknown

- A made-up thing
- Sold by Initech

```python
# not a heading: inside a code block
### nor this
```

follow-ups:
- Who sells it?
sources:
- watch | Widget video | https://example.com/v | 7 min | warm-up
- notes | Two-minute script

### Why widgets?
- Because

# Second track
cover: rose

### No chapter here
- Just a card
"""


class LearnImportTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.conn = db.connect(Path(self._tmp.name) / "prep.db")
        self.addCleanup(self.conn.close)

    def counts(self, text: str = IMPORT, dry_run: bool = False) -> dict:
        return {c["slug"]: c for c in learn.import_markdown(self.conn, text, dry_run)}

    def test_parse(self) -> None:
        toy, second = learn.parse_markdown(IMPORT)
        self.assertEqual((toy["slug"], toy["subtitle"], second["cover"]), ("toy", "fictional cards", "rose"))
        widget = toy["cards"][0]
        self.assertEqual((widget["front"], widget["chapter"]), ("What is a widget?", "Basics"))
        self.assertIn("# not a heading", widget["back"])
        self.assertIn("### nor this", widget["back"])
        self.assertEqual(widget["tags"], ["confirmed", "unknown"])
        self.assertEqual(widget["follow_ups"], ["Who sells it?"])
        self.assertEqual(widget["sources"], [
            {"kind": "watch", "title": "Widget video", "url": "https://example.com/v", "minutes": 7, "why": "warm-up"},
            {"kind": "notes", "title": "Two-minute script"}])
        self.assertEqual(len(toy["cards"]), 2)
        self.assertIsNone(second["cards"][0]["chapter"])

    def test_parse_errors(self) -> None:
        bad = {
            "no track": "### card\n- x",
            "no back": "# T\n### card\ntags: a",
            "loose line in a list": "# T\n### card\n- x\nfollow-ups:\nnot an item",
            "open fence": "# T\n### card\n```\nx",
            "minutes": "# T\n### card\n- x\nsources:\n- read | t | u | ten",
            "stray text under a track": "# T\nhello\n### card\n- x",
        }
        for name, text in bad.items():
            with self.subTest(name), self.assertRaises(ValueError):
                learn.parse_markdown(text)

    def test_import_is_idempotent_and_keeps_done(self) -> None:
        dry = self.counts(dry_run=True)
        self.assertEqual(dry["toy"]["added"], 2)
        self.assertEqual(learn.card_count(self.conn), 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0], 0)

        first = self.counts()
        self.assertEqual((first["toy"]["added"], first["second-track"]["added"]), (2, 1))
        self.assertTrue(first["toy"]["created"])
        widget = learn.list_cards(self.conn, "toy")[0]
        learn.set_status(self.conn, widget["id"], "done")

        again = self.counts()
        self.assertEqual((again["toy"]["added"], again["toy"]["updated"], again["toy"]["unchanged"]), (0, 0, 2))
        self.assertEqual(learn.card_count(self.conn), 3)

        edited = IMPORT.replace("- Sold by Initech", "- Sold by Globex").replace("## Basics", "## Core")
        changed = self.counts(edited)
        self.assertEqual((changed["toy"]["updated"], changed["toy"]["unchanged"]), (2, 0))  # back, chapter
        after = learn.get_card(self.conn, widget["id"])
        self.assertEqual((after["status"], after["position"], after["chapter_title"]), ("done", 1, "Core"))
        self.assertIn("Globex", after["back"])

        fewer = self.counts(edited.split("### Why widgets?")[0])
        self.assertEqual((fewer["toy"]["unchanged"], fewer["toy"]["not_in_file"]), (1, 1))
        self.assertEqual(learn.card_count(self.conn), 3)  # never deleted

    def test_source_ticks_survive_reimport(self) -> None:
        self.counts()
        widget = learn.list_cards(self.conn, "toy")[0]
        video, script = widget["sources"]
        self.assertEqual((learn.source_key(video), learn.source_key(script)),
                         ("https://example.com/v", "notes:Two-minute script"))
        learn.set_source_done(self.conn, widget["id"], learn.source_key(video), True)
        learn.set_source_done(self.conn, widget["id"], learn.source_key(script), True)
        # reordered, the video's title and minutes edited, and a new source added: both ticks kept
        edited = IMPORT.replace(
            "- watch | Widget video | https://example.com/v | 7 min | warm-up\n- notes | Two-minute script",
            "- read | New article\n- notes | Two-minute script\n- watch | Widget video, longer | https://example.com/v | 9 min")
        self.assertEqual(self.counts(edited)["toy"]["updated"], 1)
        after = learn.get_card(self.conn, widget["id"])
        self.assertEqual(learn.source_progress(self.conn, after), ([False, True, True], 2))
        self.assertEqual(after["status"], "todo")  # ticks never mark the card done
        # a ticked source dropped from the file is no longer counted
        fewer = self.counts(edited.replace("- notes | Two-minute script\n", ""))
        self.assertEqual(fewer["toy"]["updated"], 1)
        self.assertEqual(learn.source_progress(self.conn, learn.get_card(self.conn, widget["id"])), ([False, True], 1))

    def test_import_all_or_none(self) -> None:
        bad = IMPORT + "\n# Third\ncover: neon\n### x\n- y\n"
        with self.assertRaises(ValueError):
            self.counts(bad)
        self.assertEqual(learn.card_count(self.conn), 0)
        with self.assertRaises(ValueError):  # the same front twice in one track
            self.counts(IMPORT + "\n# Toy again\nslug: toy\n### Why widgets?\n- a\n### Why widgets?\n- b\n")
        self.assertEqual(learn.card_count(self.conn), 0)

    def test_example_file_imports(self) -> None:
        text = (Path(__file__).resolve().parent.parent / "learn.example.md").read_text(encoding="utf-8")
        counts = self.counts(text)
        self.assertTrue(all(c["added"] > 0 for c in counts.values()))


class GapCardsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.conn = db.connect(Path(self._tmp.name) / "prep.db")
        self.addCleanup(self.conn.close)
        self.app = db.add_application(self.conn, "Initech", "Engineer")
        found = db.add_gap_drills(self.conn, ["Consistent hashing", "Big-O of heaps"], application_id=self.app)
        self.gaps = found["gaps"]
        db.add_gap_drills(self.conn, ["Unrelated gap"])

    def test_drafts_and_one_card_per_gap(self) -> None:
        drafts = learn.gap_drafts(self.conn, application_id=self.app)
        self.assertEqual([d["source_gap_id"] for d in drafts], self.gaps[::-1])  # newest first
        self.assertEqual((drafts[0]["track"], drafts[0]["chapter"], drafts[0]["back"]),
                         ("gaps-to-drill", "Initech", ""))
        self.assertEqual(len(learn.gap_drafts(self.conn)), 3)
        self.assertEqual(len(learn.gap_drafts(self.conn, limit=1)), 1)
        self.assertEqual(learn.gap_drafts(self.conn, gap_ids=[self.gaps[0]])[0]["front"], "Consistent hashing")

        with self.assertRaises(ValueError):  # the back must be written first
            learn.add_cards(self.conn, drafts)
        self.assertFalse(learn._track_exists(self.conn, "gaps-to-drill"))
        learn.add_cards(self.conn, [{**d, "back": "- key points"} for d in drafts])
        self.assertEqual(learn.get_track(self.conn, "gaps-to-drill")["title"], "Gaps to drill")  # made on save
        self.assertEqual(learn.gap_drafts(self.conn, application_id=self.app), [])
        with self.assertRaises(ValueError):  # no second card for the same gap
            learn.add_card(self.conn, {**drafts[0], "front": "Another", "back": "- x"})

        db.close_gap(self.conn, learn.gap_drafts(self.conn)[0]["source_gap_id"])
        self.assertEqual(learn.gap_drafts(self.conn), [])  # closed gaps are not drafted

    def test_gap_line_in_import(self) -> None:
        text = f"# Gaps to drill\nslug: gaps-to-drill\n### Hashing\ngap: {self.gaps[0]}\n- ring\n"
        learn.import_markdown(self.conn, text)
        self.assertEqual(learn.list_cards(self.conn)[0]["source_gap_id"], self.gaps[0])
        learn.import_markdown(self.conn, text)  # the same card again is fine
        with self.assertRaises(ValueError):
            learn.import_markdown(self.conn, text.replace("### Hashing", "### Other"))


class LearnCliTest(IsolatedTestCase):
    def add(self, cards: object) -> tuple[int, str, str]:
        with mock.patch("sys.stdin", io.StringIO(json.dumps(cards))):
            return self.run_cli("cards", "add")

    def test_tracks_and_cards(self) -> None:
        code, out, _ = self.run_cli("tracks", "add", "Toy track", "--cover", "rose")
        self.assertEqual(code, 0)
        self.assertIn("toy-track", out)
        code, out, _ = self.add([card(track="toy-track"), card(track="toy-track", chapter="Two", front="Why?")])
        self.assertEqual(code, 0, out)
        self.assertEqual(out.count("Added card"), 2)
        code, _, err = self.add(card(track="toy-track", sources=[{"kind": "podcast", "title": "x"}]))
        self.assertEqual(code, 2)
        self.assertIn("source kind", err)

        _, out, _ = self.run_cli("cards", "show", "1")
        self.assertIn("Q: What is a widget?", out)
        self.assertNotIn("Learn more", out)  # no sources
        self.assertIn("· to do", out)
        self.assertIn("· to do", out)
        _, out, _ = self.run_cli("cards", "done", "1")
        self.assertIn("Done: #1", out)
        _, out, _ = self.run_cli("cards", "list", "--todo")
        self.assertNotIn("#1 ", out)
        self.assertIn("#2 [toy-track / Two] Why? (to do)", out)
        _, out, _ = self.run_cli("cards", "list", "--done")
        self.assertIn("#1 ", out)
        _, out, _ = self.run_cli("tracks", "list")
        self.assertIn("toy-track · Toy track · 2 chapters · done 1 / 2 · 50%", out)
        _, out, _ = self.run_cli("cards", "undo", "1")
        self.assertIn("Back to do: #1", out)
        code, out, _ = self.run_cli("cards", "delete", "1")
        self.assertEqual(code, 0)
        self.assertIn("Deleted card #1 ", out)
        code, _, err = self.run_cli("cards", "delete", "1")
        self.assertNotEqual(code, 0)
        self.assertIn("no card with id 1", err)
        _, out, _ = self.run_cli("tracks", "list")
        self.assertIn("done 0 / 1", out)
        code, _, _ = self.run_cli("cards", "list", "--todo", "--done")
        self.assertEqual(code, 2)

    def test_show_ticks_sources(self) -> None:
        self.run_cli("tracks", "add", "Toy track")
        self.add(card(track="toy-track", front="Who sells widgets?", sources=[
            {"kind": "watch", "title": "Widget talk", "url": "https://example.com/w", "minutes": 5},
            {"kind": "notes", "title": "Script"}]))
        with closing(db.connect(self.prep_db)) as conn:
            learn.set_source_done(conn, 1, "https://example.com/w", True)
        _, out, _ = self.run_cli("cards", "show", "1")
        self.assertIn("Learn more (1 / 2 done):\n[x] watch: Widget talk · 5 min · https://example.com/w\n[ ] notes: Script", out)

    def test_today_shows_done_and_todo(self) -> None:
        _, out, _ = self.run_cli("today")
        self.assertNotIn("Learn:", out)  # no cards at all: no line
        self.run_cli("tracks", "add", "Toy track")
        self.run_cli("tracks", "add", "Other track")
        self.add([card(track="toy-track"), card(track="toy-track"), card(track="other-track")])
        self.run_cli("cards", "done", "1")
        _, out, _ = self.run_cli("today")
        self.assertIn("Learn: 1 done, 2 to do (Toy track 1/2, Other track 0/1)", out)

    def test_import_and_gaps(self) -> None:
        path = self.tmp / "toy.md"
        path.write_text(IMPORT, encoding="utf-8")
        code, out, _ = self.run_cli("cards", "import", str(path), "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("toy · Toy track (new track): 2 added, 0 updated, 0 unchanged", out)
        self.assertIn("Dry run: nothing saved.", out)
        self.run_cli("cards", "import", str(path))
        _, out, _ = self.run_cli("cards", "import", str(path))
        self.assertIn("toy · Toy track: 0 added, 0 updated, 2 unchanged", out)

        _, out, err = self.run_cli("cards", "gaps")
        self.assertEqual(json.loads(out), [])
        self.assertIn("No open gaps", err)
        self.run_cli("gap", "add", "Consistent hashing")
        code, out, _ = self.run_cli("cards", "gaps", "--gap", "1")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)[0]["source_gap_id"], 1)


if __name__ == "__main__":
    unittest.main()
