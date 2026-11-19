"""Learn: tracks, chapters and cards in prep.db. A card is to do or done; there is no schedule.

Card content (answers, stories) is private: it lives in prep.db only, never in committed files.
"""

import json
import re
import sqlite3

from prep import db

TRACK_COVERS = ("matcha", "teal", "amber", "rose", "violet", "slate")
SOURCE_KINDS = ("watch", "read", "notes")
CARD_FIELDS = ("track", "chapter", "front", "prompt", "back", "tags", "follow_ups", "sources", "source_gap_id")
SOURCE_FIELDS = ("kind", "title", "url", "minutes", "why")
CARD_STATUSES = ("todo", "done")
# (slug, title, subtitle) of the track that cards drafted from gaps go to; added with its first card.
GAPS_TRACK = ("gaps-to-drill", "Gaps to drill", "Weak spots from mocks, interviews and CV fits")


def slugify(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


# --- tracks and chapters ----------------------------------------------------------

def add_track(conn: sqlite3.Connection, title: str, subtitle: str | None = None, cover: str | None = None,
              slug: str | None = None, commit: bool = True) -> int:
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
    if commit:
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
    """Add one card from a dict with CARD_FIELDS (track: id or slug; chapter: a title, added when new;
    prompt: optional Markdown shown under the question). It starts as to do, at the end of its track."""
    unknown = set(card) - set(CARD_FIELDS)
    if unknown:
        raise ValueError(f"unknown card field(s): {', '.join(sorted(unknown))}")
    front, back = str(card.get("front") or "").strip(), str(card.get("back") or "").strip()
    if not front or not back:
        raise ValueError("a card needs a front (the question) and a back (key points)")
    if card.get("track") is None:
        raise ValueError("a card needs a track (id or slug)")
    prompt = _prompt(card.get("prompt"))
    gap_id = _check_gap(conn, card.get("source_gap_id"))
    if gap_id is not None and str(card["track"]) == GAPS_TRACK[0] and not _track_exists(conn, GAPS_TRACK[0]):
        add_track(conn, GAPS_TRACK[1], GAPS_TRACK[2], slug=GAPS_TRACK[0], commit=False)
    track = get_track(conn, card["track"])
    tags, follow_ups, sources = _strings("tags", card.get("tags")), _strings("follow_ups", card.get("follow_ups")), \
        _sources(card.get("sources"))
    chapter = card.get("chapter")
    chapter_id = _chapter_id(conn, track["id"], chapter) if chapter and str(chapter).strip() else None
    position = conn.execute("SELECT COUNT(*) FROM cards WHERE track_id = ?", (track["id"],)).fetchone()[0] + 1
    cursor = conn.execute(
        """
        INSERT INTO cards (track_id, chapter_id, position, front, prompt, back, tags, follow_ups, sources,
                           source_gap_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (track["id"], chapter_id, position, front, prompt, back, json.dumps(tags), json.dumps(follow_ups),
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


def _prompt(value: object) -> str | None:
    """The card's prompt: stripped Markdown, or None when there is none."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("prompt must be a string")
    return value.strip() or None


def _track_exists(conn: sqlite3.Connection, slug: str) -> bool:
    return conn.execute("SELECT 1 FROM tracks WHERE slug = ?", (slug,)).fetchone() is not None


def _check_gap(conn: sqlite3.Connection, gap_id: object, card_id: int | None = None) -> int | None:
    """The gap id when it names a gap that no other card is drafted from yet (one card per gap)."""
    if gap_id is None:
        return None
    if not isinstance(gap_id, int) or isinstance(gap_id, bool):
        raise ValueError("source_gap_id must be a whole number")
    if not conn.execute("SELECT 1 FROM gaps WHERE id = ?", (gap_id,)).fetchone():
        raise LookupError(f"no gap with id {gap_id}")
    other = conn.execute("SELECT id FROM cards WHERE source_gap_id = ? AND id IS NOT ?", (gap_id, card_id)).fetchone()
    if other:
        raise ValueError(f"gap #{gap_id} already has card #{other[0]}")
    return gap_id


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


def fronts(conn: sqlite3.Connection, card_ids: set[int]) -> dict[int, str]:
    """The question (front) of each card that still exists, by id; removed cards are left out."""
    if not card_ids:
        return {}
    marks = ",".join("?" * len(card_ids))
    rows = conn.execute(f"SELECT id, front FROM cards WHERE id IN ({marks})", sorted(card_ids))
    return {row[0]: row[1] for row in rows}


def delete_card(conn: sqlite3.Connection, card_id: int) -> dict:
    """Remove a card for good (its chapter and track stay); returns the card as it was."""
    card = get_card(conn, card_id)
    conn.execute("DELETE FROM card_source_done WHERE card_id = ?", (card_id,))
    conn.execute("DELETE FROM cards WHERE id = ?", (card_id,))
    conn.commit()
    return card


# --- sources ticked done ---------------------------------------------------------

def source_key(source: dict) -> str:
    """What a tick is stored under: the source's URL, else "kind:title"."""
    url = str(source.get("url") or "").strip()
    return url or f"{source['kind']}:{str(source['title']).strip()}"


def done_sources(conn: sqlite3.Connection, card_id: int) -> set[str]:
    """The keys of this card's sources ticked done (stale keys of removed sources included)."""
    return {r[0] for r in conn.execute("SELECT source_key FROM card_source_done WHERE card_id = ?", (card_id,))}


def source_progress(conn: sqlite3.Connection, card: dict) -> tuple[list[bool], int]:
    """(done flag per current source, in order; how many are done)."""
    keys = done_sources(conn, card["id"])
    flags = [source_key(s) in keys for s in card["sources"]]
    return flags, sum(flags)


def set_source_done(conn: sqlite3.Connection, card_id: int, key: str, done: bool) -> dict:
    """Tick (done) or untick one of the card's current sources; the card's own status is not touched.
    Returns the card."""
    card = get_card(conn, card_id)
    if key not in {source_key(s) for s in card["sources"]}:
        raise ValueError(f"card {card_id} has no such source")
    if done:
        conn.execute("INSERT OR IGNORE INTO card_source_done (card_id, source_key, done_at) VALUES (?, ?, ?)",
                     (card_id, key, db.now_iso()))
    else:
        conn.execute("DELETE FROM card_source_done WHERE card_id = ? AND source_key = ?", (card_id, key))
    conn.commit()
    return card


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


# --- Markdown import (docs/learn-format.md) ---------------------------------------

_HEADING = re.compile(r"^(#{1,3}) +(.+?)\s*$")
_KEY = re.compile(r"^(slug|subtitle|cover|tags|gap|follow-ups|sources):\s*(.*?)\s*$")
_TRACK_KEYS, _CARD_KEYS = ("slug", "subtitle", "cover"), ("tags", "gap", "follow-ups", "sources")


def _source_line(text: str, where: str) -> dict:
    """`kind | title | url | minutes | why`; trailing fields may be left out, empty ones are skipped."""
    parts = [p.strip() for p in text.split("|")]
    if len(parts) > 5:
        raise ValueError(f"{where}: a source has at most 5 fields (kind | title | url | minutes | why)")
    parts += [""] * (5 - len(parts))
    kind, title, url, minutes, why = parts
    source: dict = {"kind": kind, "title": title}
    if url:
        source["url"] = url
    if minutes:
        m = re.fullmatch(r"(\d+)(?:\s*min)?", minutes)
        if not m:
            raise ValueError(f"{where}: source minutes must be a whole number, got {minutes!r}")
        source["minutes"] = int(m.group(1))
    if why:
        source["why"] = why
    return source


def parse_markdown(text: str) -> list[dict]:
    """Tracks from the import format: `# Track` (then `slug:`, `subtitle:`, `cover:` lines), `## Chapter`,
    `### Card front`, its back as Markdown, and `tags:`, `gap:`, `follow-ups:` and `sources:` lines.
    Text above a card's first `---` line (outside a ``` fence) is its prompt, shown under the question;
    the rest is the back. Anything before the first `# ` heading is ignored; lines inside ``` fences are
    card text.
    Returns [{"title", "slug", "subtitle", "cover", "cards": [card dicts with CARD_FIELDS but track]}]."""
    tracks: list[dict] = []
    track = card = None
    chapter: str | None = None
    mode, fence = "back", False

    def finish() -> None:
        if card is not None:
            card["back"] = "\n".join(card.pop("_back")).strip()
            prompt = card.pop("_prompt")
            card["prompt"] = "\n".join(prompt).strip() or None if prompt is not None else None
            if not card["back"]:
                raise ValueError(f"card {card['front']!r} has no back (key points)")

    for n, raw in enumerate(text.splitlines(), 1):
        where, line = f"line {n}", raw.rstrip()
        if fence or line.lstrip().startswith("```"):
            if line.lstrip().startswith("```"):
                fence = not fence
            if card is not None:
                if mode != "back":
                    raise ValueError(f"{where}: a code block belongs in the back, before follow-ups and sources")
                card["_back"].append(line)
            continue
        heading = _HEADING.match(line)
        if heading:
            level, title = len(heading.group(1)), heading.group(2)
            finish()
            card, mode = None, "back"
            if level == 1:
                track = {"title": title, "slug": None, "subtitle": None, "cover": None, "cards": []}
                tracks.append(track)
                chapter = None
            elif track is None:
                raise ValueError(f"{where}: a chapter or card before the first `# Track` heading")
            elif level == 2:
                chapter = title
            else:
                card = {"front": title, "chapter": chapter, "tags": [], "follow_ups": [], "sources": [],
                        "source_gap_id": None, "_back": [], "_prompt": None}
                track["cards"].append(card)
            continue
        if track is None:
            continue  # preamble
        key = _KEY.match(line)
        if card is None:
            if not line.strip():
                continue
            if not key or key.group(1) not in _TRACK_KEYS:
                raise ValueError(f"{where}: under a track or chapter heading only slug:, subtitle: and cover: "
                                 "lines may come before the first `### card`")
            track[key.group(1)] = key.group(2) or None
            continue
        if key and key.group(1) in _CARD_KEYS:
            name, value = key.groups()
            if name == "tags":
                card["tags"] = [t.strip() for t in value.split(",") if t.strip()]
            elif name == "gap":
                if not value.lstrip("#").isdigit():
                    raise ValueError(f"{where}: gap: takes a gap id")
                card["source_gap_id"] = int(value.lstrip("#"))
            else:
                if value:
                    raise ValueError(f"{where}: put {name} on their own `- ` lines under `{name}:`")
                mode = name
            continue
        if mode == "back":
            if line.strip() == "---" and card["_prompt"] is None:
                card["_prompt"], card["_back"] = card["_back"], []  # all above the first --- is the prompt
            else:
                card["_back"].append(line)
        elif line.strip():
            if not line.startswith("- "):
                raise ValueError(f"{where}: under {mode}: every line is a `- ` item")
            item = line[2:].strip()
            if mode == "follow-ups":
                card["follow_ups"].append(item)
            else:
                card["sources"].append(_source_line(item, where))
    if fence:
        raise ValueError("a ``` code block is not closed")
    finish()
    if not tracks:
        raise ValueError("no `# Track` heading found")
    return tracks


def _import_track(conn: sqlite3.Connection, t: dict) -> dict:
    slug = slugify(t["slug"] or t["title"])
    db._check("cover", t["cover"], TRACK_COVERS)
    if _track_exists(conn, slug):
        track = get_track(conn, slug)
        conn.execute("UPDATE tracks SET title = ?, subtitle = COALESCE(?, subtitle), cover = COALESCE(?, cover) "
                     "WHERE id = ?", (t["title"], t["subtitle"], t["cover"], track["id"]))
        created = False
    else:
        track = get_track(conn, add_track(conn, t["title"], t["subtitle"], t["cover"], slug, commit=False))
        created = True
    counts = {"slug": slug, "title": t["title"], "created": created, "added": 0, "updated": 0, "unchanged": 0}
    seen: set[str] = set()
    for c in t["cards"]:
        if c["front"] in seen:
            raise ValueError(f"track {slug!r} has the card {c['front']!r} twice")
        seen.add(c["front"])
        row = conn.execute("SELECT * FROM cards WHERE track_id = ? AND front = ?", (track["id"], c["front"])).fetchone()
        if row is None:
            add_card(conn, {**c, "track": track["id"]}, commit=False)
            counts["added"] += 1
            continue
        _check_gap(conn, c["source_gap_id"], row["id"])
        tags, follow_ups, sources = _strings("tags", c["tags"]), _strings("follow_ups", c["follow_ups"]), \
            _sources(c["sources"])
        chapter_id = _chapter_id(conn, track["id"], c["chapter"]) if c["chapter"] else None
        new = (chapter_id, _prompt(c.get("prompt")), c["back"], json.dumps(tags), json.dumps(follow_ups),
               json.dumps(sources), c["source_gap_id"])
        old = (row["chapter_id"], row["prompt"], row["back"], row["tags"], row["follow_ups"], row["sources"],
               row["source_gap_id"])
        if new == old:
            counts["unchanged"] += 1
        else:
            conn.execute("UPDATE cards SET chapter_id = ?, prompt = ?, back = ?, tags = ?, follow_ups = ?, sources = ?, "
                         "source_gap_id = ? WHERE id = ?", (*new, row["id"]))
            counts["updated"] += 1
    counts["not_in_file"] = conn.execute("SELECT COUNT(*) FROM cards WHERE track_id = ?",
                                         (track["id"],)).fetchone()[0] - len(seen)
    return counts


def import_markdown(conn: sqlite3.Connection, text: str, dry_run: bool = False) -> list[dict]:
    """Add or update the tracks and cards in an import file, all or none. A card is matched by its track
    and front: a match gets the file's prompt, back, chapter, tags, follow-ups, sources and gap, and keeps its id,
    position and done status. Cards missing from the file are left alone (counted as not_in_file).
    With dry_run nothing is saved. Returns per-track counts: added, updated, unchanged, not_in_file."""
    tracks = parse_markdown(text)
    try:
        counts = [_import_track(conn, t) for t in tracks]
    except Exception:
        conn.rollback()
        raise
    if dry_run:
        conn.rollback()
    else:
        conn.commit()
    return counts


# --- gaps to drill ----------------------------------------------------------------

def gap_drafts(conn: sqlite3.Connection, gap_ids: list[int] | None = None, application_id: int | None = None,
               session_id: int | None = None, limit: int | None = None) -> list[dict]:
    """Card skeletons for open gaps that have no card yet, newest gap first: track GAPS_TRACK, chapter the
    application's company (else "General"), front the gap, an empty back for the agent to write, tag "gap"
    and source_gap_id. Nothing is saved: the agent fills the back, shows the user, then `cards add`."""
    where, params = ["g.status = 'open'", "NOT EXISTS (SELECT 1 FROM cards c WHERE c.source_gap_id = g.id)"], []
    if gap_ids:
        where.append(f"g.id IN ({', '.join('?' * len(gap_ids))})")
        params += gap_ids
    if application_id is not None:
        where.append("g.application_id = ?")
        params.append(application_id)
    if session_id is not None:
        where.append("g.session_id = ?")
        params.append(session_id)
    sql = ("SELECT g.id, g.gap, a.company FROM gaps g LEFT JOIN applications a ON a.id = g.application_id WHERE "
           + " AND ".join(where) + " ORDER BY g.id DESC" + (f" LIMIT {int(limit)}" if limit else ""))
    return [{"track": GAPS_TRACK[0], "chapter": r["company"] or "General", "front": r["gap"], "back": "",
             "tags": ["gap"], "source_gap_id": r["id"]} for r in conn.execute(sql, params)]
