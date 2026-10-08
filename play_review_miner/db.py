"""SQLite storage. All writes are idempotent so crawls can be re-run incrementally."""

from __future__ import annotations

import html
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS apps (
    app_id        TEXT PRIMARY KEY,
    title         TEXT,
    developer     TEXT,
    genre_id      TEXT,
    score         REAL,
    ratings       INTEGER,
    installs      TEXT,
    real_installs INTEGER,
    free          INTEGER,
    contains_ads  INTEGER,
    url           TEXT,
    updated_at    TEXT,
    lang          TEXT,         -- language the details (title...) were last fetched in
    summary       TEXT,         -- Play Store short description (latest fetch)
    description   TEXT,         -- Play Store full description, plain text (latest fetch)
    genre         TEXT
);

-- Store listing as seen in a specific language/country (titles & descriptions are localized).
CREATE TABLE IF NOT EXISTS app_locale (
    app_id        TEXT NOT NULL,
    lang          TEXT NOT NULL,
    country       TEXT NOT NULL,
    title         TEXT,
    summary       TEXT,
    description   TEXT,
    genre         TEXT,
    genre_id      TEXT,
    developer     TEXT,
    installs      TEXT,
    real_installs INTEGER,
    score         REAL,
    ratings       INTEGER,
    url           TEXT,
    fetched_at    TEXT,
    PRIMARY KEY (app_id, lang, country)
);

-- Generated "what does this app do" texts (e.g. by Gemini), in Turkish.
CREATE TABLE IF NOT EXISTS app_summary (
    app_id     TEXT NOT NULL,
    source     TEXT NOT NULL,   -- e.g. 'gemini'
    text       TEXT,
    model      TEXT,
    created_at TEXT,
    PRIMARY KEY (app_id, source)
);

-- Which app list (e.g. 'top', 'niche') of a category/locale an app belongs to, with its rank.
CREATE TABLE IF NOT EXISTS app_category (
    list     TEXT NOT NULL DEFAULT 'top',
    app_id   TEXT NOT NULL,
    category TEXT NOT NULL,
    country  TEXT NOT NULL,
    lang     TEXT NOT NULL,
    rank     INTEGER,
    source   TEXT,             -- 'category_page' or 'search:<term>'
    seen_at  TEXT,
    PRIMARY KEY (list, app_id, category, country, lang)
);

-- Play review ids are global (the same review shows up in every country that shares the
-- language), so a row is "this review as seen in this locale": the key includes country/lang.
CREATE TABLE IF NOT EXISTS reviews (
    review_id     TEXT NOT NULL,
    app_id        TEXT NOT NULL,
    country       TEXT NOT NULL,
    lang          TEXT NOT NULL,
    score         INTEGER,
    content       TEXT,
    thumbs_up     INTEGER,
    app_version   TEXT,
    at            TEXT,
    reply_content TEXT,
    replied_at    TEXT,
    fetched_at    TEXT,
    PRIMARY KEY (review_id, country, lang)
);
CREATE INDEX IF NOT EXISTS idx_reviews_app ON reviews(app_id, country, lang, score);

-- One row per (review, analyzer). themes is a JSON list of theme ids.
CREATE TABLE IF NOT EXISTS analysis (
    review_id   TEXT NOT NULL,
    analyzer    TEXT NOT NULL,
    category    TEXT,
    themes      TEXT,
    summary     TEXT,
    low_signal  INTEGER DEFAULT 0,
    analyzed_at TEXT,
    PRIMARY KEY (review_id, analyzer)
);

-- Human readable metadata for theme ids produced by an analyzer.
CREATE TABLE IF NOT EXISTS theme_meta (
    analyzer    TEXT NOT NULL,
    theme_id    TEXT NOT NULL,
    label       TEXT,
    category    TEXT,
    opportunity TEXT,
    PRIMARY KEY (analyzer, theme_id)
);
"""


SCHEMA_VERSION = 4  # PRAGMA user_version; migrations below are detection-based and idempotent


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path: str | Path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(apps)")}
    for col in ("lang", "summary", "description", "genre"):  # older databases
        if col not in cols:
            conn.execute(f"ALTER TABLE apps ADD COLUMN {col} TEXT")
    ac_cols = {r[1] for r in conn.execute("PRAGMA table_info(app_category)")}
    if "list" not in ac_cols:  # v0.2 -> v0.3: lists become part of the key
        conn.executescript("""
            ALTER TABLE app_category RENAME TO app_category_old;
            CREATE TABLE app_category (
                list TEXT NOT NULL DEFAULT 'top', app_id TEXT NOT NULL, category TEXT NOT NULL,
                country TEXT NOT NULL, lang TEXT NOT NULL, rank INTEGER, source TEXT, seen_at TEXT,
                PRIMARY KEY (list, app_id, category, country, lang));
            INSERT INTO app_category (list, app_id, category, country, lang, rank, source, seen_at)
                SELECT 'top', app_id, category, country, lang, rank, 'category_page', seen_at
                FROM app_category_old;
            DROP TABLE app_category_old;
        """)
    pk = [r[1] for r in sorted(conn.execute("PRAGMA table_info(reviews)"), key=lambda r: r[5]) if r[5]]
    if pk == ["review_id"]:  # v0.3 -> v0.4: a review is stored once per locale it was seen in
        conn.executescript("""
            ALTER TABLE reviews RENAME TO reviews_old;
            DROP INDEX IF EXISTS idx_reviews_app;
            CREATE TABLE reviews (
                review_id TEXT NOT NULL, app_id TEXT NOT NULL, country TEXT NOT NULL, lang TEXT NOT NULL,
                score INTEGER, content TEXT, thumbs_up INTEGER, app_version TEXT, at TEXT,
                reply_content TEXT, replied_at TEXT, fetched_at TEXT,
                PRIMARY KEY (review_id, country, lang));
            INSERT INTO reviews SELECT review_id, app_id, country, lang, score, content, thumbs_up, app_version,
                                       at, reply_content, replied_at, fetched_at FROM reviews_old;
            DROP TABLE reviews_old;
            CREATE INDEX IF NOT EXISTS idx_reviews_app ON reviews(app_id, country, lang, score);
        """)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()


def upsert_app(conn: sqlite3.Connection, info: dict, lang: str | None = None,
               country: str | None = None) -> None:
    """Save app details to `apps` (latest) and, when lang/country are given, `app_locale`."""
    row = {
        "app_id": info["appId"],
        "title": info.get("title"),
        "summary": clean_text(info.get("summary")),
        "description": clean_text(info.get("description")),
        "genre": info.get("genre"),
        "genre_id": info.get("genreId"),
        "developer": info.get("developer"),
        "installs": info.get("installs"),
        "real_installs": info.get("realInstalls"),
        "score": info.get("score"),
        "ratings": info.get("ratings"),
        "url": info.get("url"),
        "lang": lang,
        "country": country,
        "now": now_iso(),
    }
    if lang and country:
        conn.execute(
            """
            INSERT INTO app_locale (app_id, lang, country, title, summary, description, genre, genre_id,
                                    developer, installs, real_installs, score, ratings, url, fetched_at)
            VALUES (:app_id, :lang, :country, :title, :summary, :description, :genre, :genre_id,
                    :developer, :installs, :real_installs, :score, :ratings, :url, :now)
            ON CONFLICT(app_id, lang, country) DO UPDATE SET
                title=excluded.title, summary=excluded.summary, description=excluded.description,
                genre=excluded.genre, genre_id=excluded.genre_id, developer=excluded.developer,
                installs=excluded.installs, real_installs=excluded.real_installs, score=excluded.score,
                ratings=excluded.ratings, url=excluded.url, fetched_at=excluded.fetched_at
            """,
            row,
        )
    conn.execute(
        """
        INSERT INTO apps (app_id, title, developer, genre_id, score, ratings, installs,
                          real_installs, free, contains_ads, url, updated_at, lang, summary,
                          description, genre)
        VALUES (:app_id, :title, :developer, :genre_id, :score, :ratings, :installs,
                :real_installs, :free, :contains_ads, :url, :updated_at, :lang, :summary,
                :description, :genre)
        ON CONFLICT(app_id) DO UPDATE SET
            title=excluded.title, developer=excluded.developer, genre_id=excluded.genre_id,
            score=excluded.score, ratings=excluded.ratings, installs=excluded.installs,
            real_installs=excluded.real_installs, free=excluded.free,
            contains_ads=excluded.contains_ads, url=excluded.url, updated_at=excluded.updated_at,
            lang=excluded.lang, summary=excluded.summary, description=excluded.description,
            genre=excluded.genre
        """,
        {
            "app_id": info["appId"],
            "title": info.get("title"),
            "developer": info.get("developer"),
            "genre_id": info.get("genreId"),
            "score": info.get("score"),
            "ratings": info.get("ratings"),
            "installs": info.get("installs"),
            "real_installs": info.get("realInstalls"),
            "free": int(bool(info.get("free"))),
            "contains_ads": int(bool(info.get("containsAds"))),
            "url": info.get("url"),
            "updated_at": now_iso(),
            "lang": lang,
            "summary": row["summary"],
            "description": row["description"],
            "genre": row["genre"],
        },
    )


def app_fresh(conn: sqlite3.Connection, app_id: str, lang: str, country: str,
              max_age_hours: float = 24) -> bool:
    """True if we have a recent, complete store listing for this app in this locale."""
    row = conn.execute(
        "SELECT fetched_at, title, summary, description FROM app_locale WHERE app_id=? AND lang=? AND country=?",
        (app_id, lang, country)).fetchone()
    if not row or not row["fetched_at"] or not row["title"] or not (row["summary"] or row["description"]):
        return False
    age = datetime.now(timezone.utc) - datetime.fromisoformat(row["fetched_at"])
    return age.total_seconds() < max_age_hours * 3600


STAGING_SUFFIX = ".__staging"


def staging_name(list_name: str) -> str:
    """A crawl builds its ranking under this name and swaps it in only when it finishes."""
    return list_name + STAGING_SUFFIX


def reset_list(conn, list_name: str, category: str, country: str, lang: str) -> None:
    """Forget a ranking (reviews are kept)."""
    conn.execute("DELETE FROM app_category WHERE list=? AND category=? AND country=? AND lang=?",
                 (list_name, category, country, lang))


def publish_list(conn, list_name: str, category: str, country: str, lang: str) -> None:
    """Atomically replace `list_name` with its staging ranking (an interrupted crawl leaves the old one)."""
    with conn:
        reset_list(conn, list_name, category, country, lang)
        conn.execute("UPDATE app_category SET list=? WHERE list=? AND category=? AND country=? AND lang=?",
                     (list_name, staging_name(list_name), category, country, lang))


def unlink_app_category(conn, app_id, category, country, lang, list_name: str) -> None:
    conn.execute("DELETE FROM app_category WHERE list=? AND app_id=? AND category=? AND country=? AND lang=?",
                 (list_name, app_id, category, country, lang))


def link_app_category(conn, app_id, category, country, lang, rank, list_name: str = "top",
                      source: str | None = None) -> None:
    conn.execute(
        """
        INSERT INTO app_category (list, app_id, category, country, lang, rank, source, seen_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(list, app_id, category, country, lang) DO UPDATE SET
            rank=excluded.rank, source=excluded.source, seen_at=excluded.seen_at
        """,
        (list_name, app_id, category, country, lang, rank, source, now_iso()),
    )


def _iso(value) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def insert_reviews(conn, app_id: str, country: str, lang: str, items: list[dict]) -> int:
    """Upsert reviews of one locale. Returns the number of reviews that were NEW in this locale.

    Known reviews get their mutable fields refreshed (likes, developer reply, edited text). When the
    text changed, stored analyses of that review are dropped so the next `analyze` redoes them.
    """
    items = [r for r in items if r.get("reviewId")]
    if not items:
        return 0
    old = {r["review_id"]: r["content"] for r in _chunked_select(
        conn, "SELECT review_id, content FROM reviews WHERE country=? AND lang=? AND review_id IN ({})",
        [country, lang], [r["reviewId"] for r in items])}
    fetched = now_iso()
    rows = [
        (
            r["reviewId"], app_id, country, lang, r.get("score"), r.get("content") or "",
            r.get("thumbsUpCount") or 0, r.get("appVersion") or r.get("reviewCreatedVersion"),
            _iso(r.get("at")), r.get("replyContent"), _iso(r.get("repliedAt")), fetched,
        )
        for r in items
    ]
    conn.executemany(
        """
        INSERT INTO reviews (review_id, app_id, country, lang, score, content,
            thumbs_up, app_version, at, reply_content, replied_at, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(review_id, country, lang) DO UPDATE SET
            score=excluded.score, content=excluded.content, thumbs_up=excluded.thumbs_up,
            app_version=excluded.app_version, at=excluded.at, reply_content=excluded.reply_content,
            replied_at=excluded.replied_at, fetched_at=excluded.fetched_at
        """,
        rows,
    )
    edited = [r["reviewId"] for r in items
              if r["reviewId"] in old and (r.get("content") or "") != (old[r["reviewId"]] or "")]
    if edited:
        _chunked_exec(conn, "DELETE FROM analysis WHERE review_id IN ({})", [], edited)
    return len({r["reviewId"] for r in items} - set(old))


def known_review_ids(conn, ids: list[str], country: str, lang: str) -> set[str]:
    """Which of `ids` we already have for this locale."""
    if not ids:
        return set()
    return {r[0] for r in _chunked_select(
        conn, "SELECT review_id FROM reviews WHERE country=? AND lang=? AND review_id IN ({})",
        [country, lang], ids)}


def count_reviews(conn, app_id: str, country: str, lang: str, score: int | None = None,
                  max_score: int | None = None) -> int:
    sql, params = "SELECT COUNT(*) FROM reviews WHERE app_id=? AND country=? AND lang=?", [app_id, country, lang]
    if score is not None:
        sql += " AND score=?"
        params.append(score)
    if max_score is not None:
        sql += " AND score<=?"
        params.append(max_score)
    return conn.execute(sql, params).fetchone()[0]


_IN_CHUNK = 500  # stay well below SQLITE_MAX_VARIABLE_NUMBER on old builds (999)


def _chunked_select(conn, sql: str, params: list, ids: list[str]) -> list[sqlite3.Row]:
    out: list = []
    for i in range(0, len(ids), _IN_CHUNK):
        part = ids[i:i + _IN_CHUNK]
        out.extend(conn.execute(sql.format(",".join("?" * len(part))), params + part).fetchall())
    return out


def _chunked_exec(conn, sql: str, params: list, ids: list[str]) -> None:
    for i in range(0, len(ids), _IN_CHUNK):
        part = ids[i:i + _IN_CHUNK]
        conn.execute(sql.format(",".join("?" * len(part))), params + part)


def select_reviews(conn, category: str, country: str, lang: str, max_score: int,
                   app_limit: int | None = None, list_name: str = "top") -> list[sqlite3.Row]:
    """Reviews belonging to apps listed in the given category/locale."""
    sql = """
        SELECT r.*, COALESCE(al.title, a.title, r.app_id) AS app_title, a.developer AS developer,
               ac.rank AS rank
        FROM reviews r
        JOIN app_category ac ON ac.app_id = r.app_id AND ac.list = ?
             AND ac.category = ? AND ac.country = ? AND ac.lang = ?
        JOIN apps a ON a.app_id = r.app_id
        LEFT JOIN app_locale al ON al.app_id = r.app_id AND al.lang = ? AND al.country = ?
        WHERE r.country = ? AND r.lang = ? AND r.score <= ?
    """
    params: list = [list_name, category, country, lang, lang, country, country, lang, max_score]
    if app_limit:
        sql += " AND ac.rank <= ?"
        params.append(app_limit)
    sql += " ORDER BY ac.rank, r.at DESC"
    return conn.execute(sql, params).fetchall()


def save_analysis(conn, analyzer: str, rows: list[dict]) -> None:
    ts = now_iso()
    conn.executemany(
        """
        INSERT INTO analysis (review_id, analyzer, category, themes, summary, low_signal, analyzed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(review_id, analyzer) DO UPDATE SET
            category=excluded.category, themes=excluded.themes, summary=excluded.summary,
            low_signal=excluded.low_signal, analyzed_at=excluded.analyzed_at
        """,
        [
            (r["review_id"], analyzer, r.get("category"), json.dumps(r.get("themes") or [], ensure_ascii=False),
             r.get("summary"), int(bool(r.get("low_signal"))), ts)
            for r in rows
        ],
    )


def analyzed_ids(conn, analyzer: str) -> set[str]:
    return {r[0] for r in conn.execute("SELECT review_id FROM analysis WHERE analyzer=?", (analyzer,))}


def save_theme_meta(conn, analyzer: str, metas: list[dict]) -> None:
    conn.executemany(
        """
        INSERT INTO theme_meta (analyzer, theme_id, label, category, opportunity)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(analyzer, theme_id) DO UPDATE SET
            label=excluded.label, category=excluded.category, opportunity=excluded.opportunity
        """,
        [(analyzer, m["theme_id"], m.get("label"), m.get("category"), m.get("opportunity")) for m in metas],
    )


def load_analysis(conn, analyzer: str) -> dict[str, sqlite3.Row]:
    return {r["review_id"]: r for r in conn.execute("SELECT * FROM analysis WHERE analyzer=?", (analyzer,))}


def load_theme_meta(conn, analyzer: str) -> dict[str, dict]:
    return {
        r["theme_id"]: dict(r)
        for r in conn.execute("SELECT * FROM theme_meta WHERE analyzer=?", (analyzer,))
    }


_TAG_RE = re.compile(r"<[^>]+>")
_BREAK_RE = re.compile(r"<\s*(br|/p|/div|/li|/h\d)\s*/?\s*>|<\s*(p|div|li|h\d)(\s[^>]*)?>", re.I)


def clean_text(text: str | None) -> str | None:
    """Strip HTML tags/entities and normalise whitespace (keeps paragraph breaks)."""
    if not text:
        return text
    text = html.unescape(_TAG_RE.sub(" ", _BREAK_RE.sub("\n", text)))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [" ".join(l.split()) for l in text.split("\n")]
    out = "\n".join(l for l in lines if l)
    return out.strip() or None


def select_category_apps(conn, category: str, country: str, lang: str, top: int | None = None,
                         list_name: str = "top") -> list[dict]:
    """Apps listed in a category/locale (ordered by rank) with their localized store listing."""
    sql = """
        SELECT ac.rank, a.app_id,
               COALESCE(al.title, a.title, a.app_id) AS title,
               COALESCE(al.developer, a.developer) AS developer,
               COALESCE(al.genre, a.genre) AS genre,
               COALESCE(al.installs, a.installs) AS installs,
               COALESCE(al.real_installs, a.real_installs) AS real_installs,
               COALESCE(al.score, a.score) AS score,
               COALESCE(al.ratings, a.ratings) AS ratings,
               al.summary AS summary, al.description AS description,
               a.summary AS any_summary, a.description AS any_description,
               a.contains_ads, a.free, ac.source AS source
        FROM app_category ac
        JOIN apps a ON a.app_id = ac.app_id
        LEFT JOIN app_locale al ON al.app_id = ac.app_id AND al.lang = ? AND al.country = ?
        WHERE ac.list = ? AND ac.category = ? AND ac.country = ? AND ac.lang = ?
    """
    params: list = [lang, country, list_name, category, country, lang]
    if top:
        sql += " AND ac.rank <= ?"
        params.append(top)
    sql += " ORDER BY ac.rank"
    return [dict(r) for r in conn.execute(sql, params)]


def turkish_listing(conn, app_id: str) -> dict | None:
    """Any Turkish store listing we have for the app (prefer country tr)."""
    row = conn.execute(
        "SELECT summary, description, country FROM app_locale WHERE app_id=? AND lang='tr' "
        "ORDER BY (country='tr') DESC, fetched_at DESC LIMIT 1", (app_id,)).fetchone()
    return dict(row) if row else None


def save_app_summaries(conn, source: str, model: str | None, items: list[dict]) -> None:
    ts = now_iso()
    conn.executemany(
        """
        INSERT INTO app_summary (app_id, source, text, model, created_at) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(app_id, source) DO UPDATE SET text=excluded.text, model=excluded.model,
            created_at=excluded.created_at
        """,
        [(i["app_id"], source, i["text"], model, ts) for i in items if i.get("text")],
    )


def load_latest_app_summaries(conn) -> dict[str, tuple[str, str]]:
    """app_id -> (text, source) of the most recent generated summary from any analyzer."""
    out: dict[str, tuple[str, str]] = {}
    for r in conn.execute("SELECT app_id, text, source FROM app_summary WHERE text IS NOT NULL "
                          "ORDER BY created_at DESC"):
        out.setdefault(r["app_id"], (r["text"], r["source"]))
    return out


def load_app_summaries(conn, source: str) -> dict[str, str]:
    return {r["app_id"]: r["text"] for r in
            conn.execute("SELECT app_id, text FROM app_summary WHERE source=?", (source,))}
