"""Learn: tracks, chapters and spaced-repetition cards in prep.db (scheduling rules in prep/srs.py).

Card content (answers, stories) is private: it lives in prep.db only, never in committed files.
"""

import json
import re
import sqlite3

from prep import db, srs

TRACK_COVERS = ("matcha", "teal", "amber", "rose", "violet", "slate")
SOURCE_KINDS = ("watch", "read", "notes")
CARD_FIELDS = ("track", "chapter", "front", "back", "tags", "follow_ups", "sources", "source_gap_id")
SOURCE_FIELDS = ("kind", "title", "url", "minutes", "why")


def slugify(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


# --- tracks and chapters ----------------------------------------------------------

def add_track(conn: sqlite3.Connection, title: str, subtitle: str | None = None, cover: str | None = None,
              slug: str | None = None) -> int:
    """A new track at the end of the list; `cover` defaults to the next colour in TRACK_COVERS."""
    title = title.strip()
    if not title:
        raise ValueError("a track needs a title")
    slug = slugify(slug or title)
    if not slug:
        raise ValueError(f"cannot make a slug from {title!r}; give --slug")
    if conn.execute("SELECT 1 FROM tracks WHERE slug = ?", (slug,)).fetchone():
        raise ValueError(f"a track with slug {slug!r} already exists")
    count = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    cover = cover or TRACK_COVERS[count % len(TRACK_COVERS)]
    db._check("cover", cover, TRACK_COVERS)
    cursor = conn.execute(
        "INSERT INTO tracks (slug, title, subtitle, cover, position, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (slug, title, subtitle, cover, count + 1, db.now_iso()))
    conn.commit()
    return cursor.lastrowid


def get_track(conn: sqlite3.Connection, ref: str | int) -> dict:
    """A track by id or slug; LookupError when there is none."""
    ref = str(ref)
    row = conn.execute("SELECT * FROM tracks WHERE " + ("id = ?" if ref.isdigit() else "slug = ?"),
                       (int(ref) if ref.isdigit() else ref,)).fetchone()
    if row is None:
        raise LookupError(f"no track {ref!r} (give its id or slug; see `tracks list`)")
    return dict(row)


def _chapter_id(conn: sqlite3.Connection, track_id: int, title: str) -> int:
    """The track's chapter with this title, added at the end when it is new."""
    title = title.strip()
    row = conn.execute("SELECT id FROM chapters WHERE track_id = ? AND title = ?", (track_id, title)).fetchone()
    if row:
        return row[0]
    position = conn.execute("SELECT COUNT(*) FROM chapters WHERE track_id = ?", (track_id,)).fetchone()[0] + 1
    return conn.execute("INSERT INTO chapters (track_id, title, position) VALUES (?, ?, ?)",
                        (track_id, title, position)).lastrowid


def list_tracks(conn: sqlite3.Connection, today: str | None = None, new_per_day: int | None = None) -> list[dict]:
    """Every track in order with chapters, cards, due (as `due_cards` counts them) and progress: the
    percentage of its cards answered right since their last lapse (reps > 0)."""
    rows = conn.execute(
        """
        SELECT t.*,
               (SELECT COUNT(*) FROM chapters h WHERE h.track_id = t.id) AS chapters,
               COUNT(c.id) AS cards,
               COALESCE(SUM(c.reps > 0), 0) AS learned
        FROM tracks t LEFT JOIN cards c ON c.track_id = t.id
        GROUP BY t.id ORDER BY t.position, t.id
        """)
    tracks = [dict(r) for r in rows]
    due: dict[int, int] = {}
    for c in due_cards(conn, today, new_per_day):
        due[c["track_id"]] = due.get(c["track_id"], 0) + 1
    for t in tracks:
        t["due"] = due.get(t["id"], 0)
        t["progress"] = round(100 * t["learned"] / t["cards"]) if t["cards"] else 0
    return tracks


# --- cards ------------------------------------------------------------------------

def _strings(name: str, value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ValueError(f"{name} must be a list of non-empty strings")
    return [v.strip() for v in value]


def _sources(value: object) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(s, dict) for s in value):
        raise ValueError("sources must be a list of objects")
    out = []
    for s in value:
        unknown = set(s) - set(SOURCE_FIELDS)
        if unknown:
            raise ValueError(f"unknown source field(s): {', '.join(sorted(unknown))}")
        db._check("source kind", s.get("kind"), SOURCE_KINDS)
        if not s.get("kind") or not str(s.get("title") or "").strip():
            raise ValueError("each source needs a kind and a title")
        minutes = s.get("minutes")
        if minutes is not None and (not isinstance(minutes, int) or isinstance(minutes, bool) or minutes < 0):
            raise ValueError("source minutes must be a whole number")
        out.append({k: s[k] for k in SOURCE_FIELDS if s.get(k) is not None})
    return out


def add_card(conn: sqlite3.Connection, card: dict, today: str | None = None, commit: bool = True) -> int:
    """Add one card from a dict with CARD_FIELDS (track: id or slug; chapter: a title, added when new).
    It is due `today`, at the end of its track."""
    unknown = set(card) - set(CARD_FIELDS)
    if unknown:
        raise ValueError(f"unknown card field(s): {', '.join(sorted(unknown))}")
    front, back = str(card.get("front") or "").strip(), str(card.get("back") or "").strip()
    if not front or not back:
        raise ValueError("a card needs a front (the question) and a back (key points)")
    if card.get("track") is None:
        raise ValueError("a card needs a track (id or slug)")
    track = get_track(conn, card["track"])
    gap_id = card.get("source_gap_id")
    if gap_id is not None and not conn.execute("SELECT 1 FROM gaps WHERE id = ?", (gap_id,)).fetchone():
        raise LookupError(f"no gap with id {gap_id}")
    tags, follow_ups, sources = _strings("tags", card.get("tags")), _strings("follow_ups", card.get("follow_ups")), \
        _sources(card.get("sources"))
    chapter = card.get("chapter")
    chapter_id = _chapter_id(conn, track["id"], chapter) if chapter and str(chapter).strip() else None
    position = conn.execute("SELECT COUNT(*) FROM cards WHERE track_id = ?", (track["id"],)).fetchone()[0] + 1
    state = srs.CardState()
    cursor = conn.execute(
        """
        INSERT INTO cards (track_id, chapter_id, position, front, back, tags, follow_ups, sources, source_gap_id,
                           ease, interval_days, due_date, reps, lapses, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (track["id"], chapter_id, position, front, back, json.dumps(tags), json.dumps(follow_ups),
         json.dumps(sources), gap_id, state.ease, state.interval_days, today or db.today_iso(), state.reps,
         state.lapses, db.now_iso()))
    if commit:
        conn.commit()
    return cursor.lastrowid


def add_cards(conn: sqlite3.Connection, cards: list[dict], today: str | None = None) -> list[int]:
    """Add several cards in one transaction: a bad card adds none."""
    try:
        ids = [add_card(conn, card, today, commit=False) for card in cards]
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    return ids


def _card(row: sqlite3.Row) -> dict:
    card = dict(row)
    for key in ("tags", "follow_ups", "sources"):
        card[key] = json.loads(card[key])
    return card


_CARD_SELECT = """
    SELECT c.*, t.slug AS track_slug, t.title AS track_title, h.title AS chapter_title,
           (SELECT r.rating FROM card_reviews r WHERE r.card_id = c.id ORDER BY r.id DESC LIMIT 1) AS last_rating
    FROM cards c JOIN tracks t ON t.id = c.track_id LEFT JOIN chapters h ON h.id = c.chapter_id
"""


def get_card(conn: sqlite3.Connection, card_id: int) -> dict:
    row = conn.execute(_CARD_SELECT + " WHERE c.id = ?", (card_id,)).fetchone()
    if row is None:
        raise LookupError(f"no card with id {card_id}")
    return _card(row)


def list_cards(conn: sqlite3.Connection, track: str | int | None = None) -> list[dict]:
    """Every card (or one track's) in track, chapter and card order."""
    where, params = "", ()
    if track is not None:
        where, params = " WHERE c.track_id = ?", (get_track(conn, track)["id"],)
    sql = _CARD_SELECT + where + _ORDER
    return [_card(r) for r in conn.execute(sql, params)]


_ORDER = " ORDER BY t.position, t.id, COALESCE(h.position, 0), c.position, c.id"


def new_introduced(conn: sqlite3.Connection, today: str) -> int:
    """New cards first reviewed on `today`."""
    return conn.execute("SELECT COUNT(*) FROM cards WHERE introduced_on = ?", (today,)).fetchone()[0]


def due_cards(conn: sqlite3.Connection, today: str | None = None, new_per_day: int | None = None,
              track: str | int | None = None) -> list[dict]:
    """Cards to review today: every reviewed card due today or earlier (most overdue first), then
    new cards in track order, at most `new_per_day` a day counting those already started today
    (None: no cap). The cap is across all tracks; `track` then keeps one track's share."""
    today = today or db.today_iso()
    reviews = [_card(r) for r in conn.execute(
        _CARD_SELECT + " WHERE c.introduced_on IS NOT NULL AND c.due_date <= ? ORDER BY c.due_date, c.id", (today,))]
    sql = _CARD_SELECT + " WHERE c.introduced_on IS NULL AND c.due_date <= ?" + _ORDER
    params: tuple = (today,)
    if new_per_day is not None:
        sql += " LIMIT ?"
        params += (max(0, new_per_day - new_introduced(conn, today)),)
    cards = reviews + [_card(r) for r in conn.execute(sql, params)]
    if track is not None:
        track_id = get_track(conn, track)["id"]
        cards = [c for c in cards if c["track_id"] == track_id]
    return cards


def new_waiting(conn: sqlite3.Connection, today: str | None = None, new_per_day: int | None = None) -> int:
    """New cards held back by the daily cap."""
    today = today or db.today_iso()
    total = conn.execute("SELECT COUNT(*) FROM cards WHERE introduced_on IS NULL AND due_date <= ?",
                         (today,)).fetchone()[0]
    shown = sum(1 for c in due_cards(conn, today, new_per_day) if c["introduced_on"] is None)
    return total - shown


def card_state(card: dict) -> srs.CardState:
    return srs.CardState(card["ease"], card["interval_days"], card["reps"], card["lapses"])


def rate_card(conn: sqlite3.Connection, card_id: int, rating: str, today: str | None = None) -> dict:
    """Record one review and schedule the card's next one; returns the card as it is now."""
    today = today or db.today_iso()
    card = get_card(conn, card_id)
    before = card_state(card)
    after = srs.next_state(before, rating)
    conn.execute(
        """
        UPDATE cards SET ease = ?, interval_days = ?, due_date = ?, reps = ?, lapses = ?,
                         introduced_on = COALESCE(introduced_on, ?)
        WHERE id = ?
        """,
        (after.ease, after.interval_days, srs.due_date(today, after.interval_days), after.reps, after.lapses, today,
         card_id))
    conn.execute(
        """
        INSERT INTO card_reviews (card_id, rating, reviewed_at, interval_before, interval_after, ease_after)
        VALUES (?, ?, ?, ?, ?, ?)
        """, (card_id, rating, db.now_iso(), before.interval_days, after.interval_days, after.ease))
    conn.commit()
    return get_card(conn, card_id)


def card_reviews(conn: sqlite3.Connection, card_id: int) -> list[dict]:
    rows = conn.execute("SELECT * FROM card_reviews WHERE card_id = ? ORDER BY id", (card_id,))
    return [dict(r) for r in rows]


def due_by_track(conn: sqlite3.Connection, today: str | None = None,
                 new_per_day: int | None = None) -> list[tuple[str, int]]:
    """(track title, cards due) for each track with any due, in track order."""
    return [(t["title"], t["due"]) for t in list_tracks(conn, today, new_per_day) if t["due"]]


def card_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
