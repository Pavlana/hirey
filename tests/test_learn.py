"""Learn: tracks and cards (prep/learn.py), done or to do, and their CLI. Fictional cards only."""

import io
import json
import tempfile
import unittest
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
        code, _, _ = self.run_cli("cards", "list", "--todo", "--done")
        self.assertEqual(code, 2)

    def test_today_shows_done_and_todo(self) -> None:
        _, out, _ = self.run_cli("today")
        self.assertNotIn("Learn:", out)  # no cards at all: no line
        self.run_cli("tracks", "add", "Toy track")
        self.run_cli("tracks", "add", "Other track")
        self.add([card(track="toy-track"), card(track="toy-track"), card(track="other-track")])
        self.run_cli("cards", "done", "1")
        _, out, _ = self.run_cli("today")
        self.assertIn("Learn: 1 done, 2 to do (Toy track 1/2, Other track 0/1)", out)


if __name__ == "__main__":
    unittest.main()
