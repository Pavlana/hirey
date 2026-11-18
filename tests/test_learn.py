"""Learn: SM-2 scheduling (prep/srs.py), cards and tracks (prep/learn.py), and their CLI. Fictional cards only."""

import io
import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from prep import db, learn, srs
from tests.helpers import IsolatedTestCase

TODAY = "2026-01-20"


def run(ratings: list[str], state: srs.CardState | None = None) -> list[srs.CardState]:
    states, state = [], state or srs.CardState()
    for rating in ratings:
        state = srs.next_state(state, rating)
        states.append(state)
    return states


class Sm2Test(unittest.TestCase):
    def test_good_follows_sm2(self) -> None:
        states = run(["good"] * 5)
        self.assertEqual([s.interval_days for s in states], [1, 6, 15, 38, 95])  # 37.5 rounds half up
        self.assertEqual({s.ease for s in states}, {2.5})
        self.assertEqual([s.reps for s in states], [1, 2, 3, 4, 5])

    def test_easy_raises_ease_and_spreads(self) -> None:
        states = run(["easy"] * 3)
        self.assertEqual([s.ease for s in states], [2.6, 2.7, 2.8])
        # new: 4 days; reps 1: max(6 + 1, 6 * 1.3) = 8; reps 2: Good = 8 * 2.7 = 22, Easy = 22 * 1.3 = 29
        self.assertEqual([s.interval_days for s in states], [4, 8, 29])

    def test_hard_lowers_ease(self) -> None:
        states = run(["good", "good", "hard"])
        self.assertEqual([s.interval_days for s in states], [1, 6, 7])  # max(6 + 1, 6 * 1.2)
        self.assertEqual(states[-1].ease, 2.36)

    def test_again_starts_over_and_counts_a_lapse(self) -> None:
        states = run(["good", "good", "good", "again", "good", "good"])
        lapse = states[3]
        self.assertEqual((lapse.interval_days, lapse.reps, lapse.lapses, lapse.ease), (1, 0, 1, 2.18))
        self.assertEqual([s.interval_days for s in states[4:]], [1, 6])
        self.assertEqual(states[-1].lapses, 1)

    def test_ease_never_drops_below_floor(self) -> None:
        states = run(["again"] * 6)
        self.assertEqual(states[-1].ease, srs.MIN_EASE)
        self.assertEqual(states[-1].lapses, 6)

    def test_buttons_never_decrease(self) -> None:
        for state in (srs.CardState(), srs.CardState(2.5, 1, 1, 0), srs.CardState(2.5, 6, 2, 0),
                      srs.CardState(1.3, 6, 2, 3), srs.CardState(1.3, 40, 5, 2), srs.CardState(3.0, 100, 7, 0)):
            days = [d for d, _ in srs.preview(state, TODAY).values()]
            self.assertEqual(days, sorted(days), state)
            self.assertLess(days[2], days[3], state)

    def test_preview(self) -> None:
        self.assertEqual(srs.preview(srs.CardState(2.5, 6, 2, 0), TODAY),
                         {"again": (1, "2026-01-21"), "hard": (7, "2026-01-27"), "good": (15, "2026-02-04"),
                          "easy": (20, "2026-02-09")})

    def test_unknown_rating(self) -> None:
        with self.assertRaises(ValueError):
            srs.next_state(srs.CardState(), "perfect")


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
                      "why": "clear demo"}]), TODAY)
        c = learn.get_card(self.conn, card_id)
        self.assertEqual((c["track_slug"], c["chapter_title"], c["due_date"], c["reps"], c["ease"]),
                         ("toy", "Basics", TODAY, 0, 2.5))
        self.assertEqual(c["tags"], ["round 1", "asked often"])
        self.assertEqual(c["sources"][0]["minutes"], 12)
        self.assertEqual(c["source_gap_id"], gap)
        self.assertIsNone(c["last_rating"])
        second = learn.add_card(self.conn, card(chapter="Basics"), TODAY)
        self.assertEqual(learn.get_card(self.conn, second)["chapter_id"], c["chapter_id"])  # chapter reused

    def test_card_validation(self) -> None:
        bad = (card(colour="red"), card(front=""), card(back=" "), card(track=None), card(track="nope"),
               card(tags="round 1"), card(follow_ups=[""]), card(sources=[{"kind": "listen", "title": "x"}]),
               card(sources=[{"kind": "read"}]), card(sources=[{"kind": "read", "title": "x", "minutes": "ten"}]),
               card(sources=[{"kind": "read", "title": "x", "pages": 3}]), card(source_gap_id=999))
        for c in bad:
            with self.subTest(c=c), self.assertRaises((ValueError, LookupError)):
                learn.add_card(self.conn, c, TODAY)
        self.assertEqual(learn.card_count(self.conn), 0)

    def test_add_cards_is_all_or_none(self) -> None:
        with self.assertRaises(ValueError):
            learn.add_cards(self.conn, [card(chapter="New chapter"), card(front="")], TODAY)
        self.assertEqual(learn.card_count(self.conn), 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM chapters").fetchone()[0], 0)
        self.assertEqual(len(learn.add_cards(self.conn, [card(), card(chapter="Later")], TODAY)), 2)

    def test_rate_card_schedules_and_records(self) -> None:
        card_id = learn.add_card(self.conn, card(), TODAY)
        c = learn.rate_card(self.conn, card_id, "good", TODAY)
        self.assertEqual((c["interval_days"], c["due_date"], c["reps"], c["last_rating"]), (1, "2026-01-21", 1, "good"))
        c = learn.rate_card(self.conn, card_id, "again", "2026-01-21")
        self.assertEqual((c["interval_days"], c["due_date"], c["reps"], c["lapses"]), (1, "2026-01-22", 0, 1))
        reviews = learn.card_reviews(self.conn, card_id)
        self.assertEqual([(r["rating"], r["interval_before"], r["interval_after"]) for r in reviews],
                         [("good", 0, 1), ("again", 1, 1)])
        self.assertEqual(reviews[-1]["ease_after"], 2.18)
        with self.assertRaises(LookupError):
            learn.rate_card(self.conn, 999, "good", TODAY)

    def test_tracks_due_and_progress(self) -> None:
        learn.add_track(self.conn, "Empty track")
        ids = learn.add_cards(self.conn, [card(), card(chapter="Two"), card(), card()], TODAY)
        learn.rate_card(self.conn, ids[0], "good", TODAY)   # due tomorrow, learned
        learn.rate_card(self.conn, ids[1], "again", TODAY)  # due tomorrow, not learned
        toy, empty = learn.list_tracks(self.conn, TODAY)
        self.assertEqual((toy["chapters"], toy["cards"], toy["due"], toy["progress"]), (2, 4, 2, 25))
        self.assertEqual((empty["cards"], empty["due"], empty["progress"]), (0, 0, 0))
        self.assertEqual(learn.due_by_track(self.conn, TODAY), [("Toy track", 2)])
        self.assertEqual(learn.due_by_track(self.conn, "2026-01-21"), [("Toy track", 4)])
        self.assertEqual([c["id"] for c in learn.due_cards(self.conn, TODAY)], ids[2:])
        self.assertEqual(len(learn.list_cards(self.conn, track="toy")), 4)

    def test_new_cards_capped_per_day(self) -> None:
        learn.add_track(self.conn, "Second", slug="second")
        ids = learn.add_cards(self.conn, [card() for _ in range(5)] + [card(track="second") for _ in range(3)], TODAY)
        due = learn.due_cards(self.conn, TODAY, new_per_day=3)
        self.assertEqual([c["id"] for c in due], ids[:3])  # new cards in track order
        self.assertEqual(learn.new_waiting(self.conn, TODAY, 3), 5)
        self.assertEqual(len(learn.due_cards(self.conn, TODAY)), 8)  # None: no cap
        learn.rate_card(self.conn, ids[0], "again", TODAY)  # started today: due tomorrow, uses a slot
        learn.rate_card(self.conn, ids[1], "good", TODAY)
        self.assertEqual([c["id"] for c in learn.due_cards(self.conn, TODAY, 3)], [ids[2]])
        self.assertEqual(learn.due_by_track(self.conn, TODAY, 3), [("Toy track", 1)])
        self.assertEqual(learn.due_cards(self.conn, TODAY, 3, track="second"), [])
        self.assertEqual(learn.due_cards(self.conn, TODAY, 0), [])
        tomorrow = "2026-01-21"  # both reviews due, plus a fresh 3 new
        due = [c["id"] for c in learn.due_cards(self.conn, tomorrow, 3)]
        self.assertEqual(due, [ids[0], ids[1], ids[2], ids[3], ids[4]])
        self.assertEqual(learn.get_card(self.conn, ids[0])["introduced_on"], TODAY)
        self.assertEqual(learn.due_by_track(self.conn, tomorrow, 3), [("Toy track", 5)])


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
        self.assertIn("Next review: Again → 1 day · Hard → 1 day · Good → 1 day · Easy → 4 days", out)
        _, out, _ = self.run_cli("cards", "rate", "1", "easy")
        self.assertIn("next review in 4 days", out)
        _, out, _ = self.run_cli("cards", "list", "--due")
        self.assertNotIn("#1 ", out)
        self.assertIn("#2 [toy-track / Two] Why?", out)
        _, out, _ = self.run_cli("tracks", "list")
        self.assertIn("toy-track · Toy track · 2 chapters, 2 cards · 50% · 1 due today", out)

    def test_today_shows_cards_due(self) -> None:
        _, out, _ = self.run_cli("today")
        self.assertNotIn("Cards due", out)  # no cards at all: no line
        self.run_cli("tracks", "add", "Toy track")
        self.add([card(track="toy-track"), card(track="toy-track")])
        _, out, _ = self.run_cli("today")
        self.assertIn("Cards due today: 2 (Toy track 2)", out)
        self.run_cli("cards", "rate", "1", "good")
        self.run_cli("cards", "rate", "2", "good")
        _, out, _ = self.run_cli("today")
        self.assertIn("Cards due today: 0\n", out)

    def test_cap_in_today_and_due_list(self) -> None:
        self.run_cli("tracks", "add", "Toy track")
        self.add([card(track="toy-track") for _ in range(4)])
        with mock.patch.dict("os.environ", {"PREP_LEARN_NEW_PER_DAY": "2"}):
            _, out, _ = self.run_cli("today")
            self.assertIn("Cards due today: 2 (Toy track 2) · 2 new held for later days (learn_new_per_day 2)", out)
            _, out, _ = self.run_cli("cards", "list", "--due")
            self.assertEqual(out.count("(new)"), 2)
            self.assertIn("2 new held for later days", out)
            _, out, _ = self.run_cli("tracks", "list")
            self.assertIn("2 due today", out)
        _, out, _ = self.run_cli("today")  # default cap 10
        self.assertIn("Cards due today: 4 (Toy track 4)\n", out)


if __name__ == "__main__":
    unittest.main()
