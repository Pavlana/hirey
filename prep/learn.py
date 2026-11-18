"""Learn: tracks, chapters and cards in prep.db. A card is to do or done; there is no schedule.

Card content (answers, stories) is private: it lives in prep.db only, never in committed files.
"""

import json
import re
import sqlite3

from prep import db

TRACK_COVERS = ("matcha", "teal", "amber", "rose", "violet", "slate")
SOURCE_KINDS = ("watch", "read", "notes")
CARD_FIELDS = ("track", "chapter", "front", "back", "tags", "follow_ups", "sources", "source_gap_id")
SOURCE_FIELDS = ("kind", "title", "url", "minutes", "why")
CARD_STATUSES = ("todo", "done")


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


def list_tracks(conn: sqlite3.Connection) -> list[dict]:
    """Every track in order with its chapters, cards, done (cards marked done), todo and progress
    (the percentage done)."""
    rows = conn.execute(
        """
        SELECT t.*,
               (SELECT COUNT(*) FROM chapters h WHERE h.track_id = t.id) AS chapters,
               COUNT(c.id) AS cards,
               COALESCE(SUM(c.status = 'done'), 0) AS done
        FROM tracks t LEFT JOIN cards c ON c.track_id = t.id
        GROUP BY t.id ORDER BY t.position, t.id
        """)
    tracks = [dict(r) for r in rows]
    for t in tracks:
        t["todo"] = t["cards"] - t["done"]
        t["progress"] = round(100 * t["done"] / t["cards"]) if t["cards"] else 0
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


def add_card(conn: sqlite3.Connection, card: dict, commit: bool = True) -> int:
    """Add one card from a dict with CARD_FIELDS (track: id or slug; chapter: a title, added when new).
    It starts as to do, at the end of its track."""
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
    cursor = conn.execute(
        """
        INSERT INTO cards (track_id, chapter_id, position, front, back, tags, follow_ups, sources, source_gap_id,
                           created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (track["id"], chapter_id, position, front, back, json.dumps(tags), json.dumps(follow_ups),
         json.dumps(sources), gap_id, db.now_iso()))
    if commit:
        conn.commit()
    return cursor.lastrowid


def add_cards(conn: sqlite3.Connection, cards: list[dict]) -> list[int]:
    """Add several cards in one transaction: a bad card adds none."""
    try:
        ids = [add_card(conn, card, commit=False) for card in cards]
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
    SELECT c.*, t.slug AS track_slug, t.title AS track_title, h.title AS chapter_title
    FROM cards c JOIN tracks t ON t.id = c.track_id LEFT JOIN chapters h ON h.id = c.chapter_id
"""


def get_card(conn: sqlite3.Connection, card_id: int) -> dict:
    row = conn.execute(_CARD_SELECT + " WHERE c.id = ?", (card_id,)).fetchone()
    if row is None:
        raise LookupError(f"no card with id {card_id}")
    return _card(row)


def list_cards(conn: sqlite3.Connection, track: str | int | None = None, status: str | None = None) -> list[dict]:
    """Cards (one track's, or with one status) in track, chapter and card order."""
    db._check("status", status, CARD_STATUSES)
    where, params = [], []
    if track is not None:
        where.append("c.track_id = ?")
        params.append(get_track(conn, track)["id"])
    if status is not None:
        where.append("c.status = ?")
        params.append(status)
    sql = _CARD_SELECT + (" WHERE " + " AND ".join(where) if where else "") + \
        " ORDER BY t.position, t.id, COALESCE(h.position, 0), c.position, c.id"
    return [_card(r) for r in conn.execute(sql, params)]


def set_status(conn: sqlite3.Connection, card_id: int, status: str) -> dict:
    """Mark a card done (stamping done_at) or back to do; returns the card as it is now."""
    db._check("status", status, CARD_STATUSES)
    get_card(conn, card_id)
    conn.execute("UPDATE cards SET status = ?, done_at = ? WHERE id = ?",
                 (status, db.now_iso() if status == "done" else None, card_id))
    conn.commit()
    return get_card(conn, card_id)


def card_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]


def counts(conn: sqlite3.Connection) -> dict:
    """All cards: done and todo (the Learn page's header and the nav badge)."""
    total, done = conn.execute("SELECT COUNT(*), COALESCE(SUM(status = 'done'), 0) FROM cards").fetchone()
    return {"done": done, "todo": total - done}


def next_todo(conn: sqlite3.Connection, card_id: int) -> int | None:
    """The track's next card to do after this one, wrapping to the start; None when no other card in
    the track is to do."""
    card = get_card(conn, card_id)
    ids = [c["id"] for c in list_cards(conn, card["track_id"], "todo") if c["id"] != card_id]
    order = [c["id"] for c in list_cards(conn, card["track_id"])]
    after = [i for i in ids if order.index(i) > order.index(card_id)]
    return (after or ids or [None])[0]


def first_card(conn: sqlite3.Connection, track: str | int) -> int | None:
    """Where a track opens: its first card to do, else its first card; None for an empty track."""
    cards = list_cards(conn, track)
    todo = [c for c in cards if c["status"] == "todo"]
    return (todo or cards or [{"id": None}])[0]["id"]
