import json
import sqlite3

from conftest import make_review

from play_review_miner import db


def test_same_review_is_kept_per_locale(conn):
    # Play review ids are global: en/us and en/gb return the same review
    r = [make_review("r1")]
    assert db.insert_reviews(conn, "a", "us", "en", r) == 1
    assert db.insert_reviews(conn, "a", "gb", "en", r) == 1
    assert db.insert_reviews(conn, "a", "gb", "en", r) == 0
    assert db.count_reviews(conn, "a", "gb", "en") == 1
    assert db.known_review_ids(conn, ["r1", "r2"], "gb", "en") == {"r1"}
    assert db.known_review_ids(conn, ["r1"], "de", "en") == set()


def test_known_reviews_are_refreshed_and_edits_invalidate_analysis(conn):
    db.insert_reviews(conn, "a", "tr", "tr", [make_review("r1", content="eski metin", thumbs=1)])
    db.save_analysis(conn, "gemini", [{"review_id": "r1", "category": "bug", "themes": ["x"]}])
    db.insert_reviews(conn, "a", "tr", "tr", [make_review("r1", content="eski metin", thumbs=9)])
    row = conn.execute("SELECT thumbs_up FROM reviews WHERE review_id='r1'").fetchone()
    assert row[0] == 9
    assert db.analyzed_ids(conn, "gemini") == {"r1"}  # same text: analysis kept
    db.insert_reviews(conn, "a", "tr", "tr", [make_review("r1", content="yeni metin")])
    assert db.analyzed_ids(conn, "gemini") == set()  # edited: analyse again


def test_many_ids_do_not_hit_sqlite_variable_limit(conn):
    items = [make_review(f"r{i}") for i in range(1500)]
    assert db.insert_reviews(conn, "a", "tr", "tr", items) == 1500
    assert len(db.known_review_ids(conn, [f"r{i}" for i in range(1500)], "tr", "tr")) == 1500


def test_migrates_v03_database(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE apps (app_id TEXT PRIMARY KEY, title TEXT, developer TEXT, genre_id TEXT, score REAL,
            ratings INTEGER, installs TEXT, real_installs INTEGER, free INTEGER, contains_ads INTEGER,
            url TEXT, updated_at TEXT);
        CREATE TABLE app_category (app_id TEXT, category TEXT, country TEXT, lang TEXT, rank INTEGER,
            seen_at TEXT, PRIMARY KEY (app_id, category, country, lang));
        CREATE TABLE reviews (review_id TEXT PRIMARY KEY, app_id TEXT NOT NULL, country TEXT NOT NULL,
            lang TEXT NOT NULL, score INTEGER, content TEXT, thumbs_up INTEGER, app_version TEXT, at TEXT,
            reply_content TEXT, replied_at TEXT, fetched_at TEXT);
        INSERT INTO app_category VALUES ('a', 'PRODUCTIVITY', 'tr', 'tr', 1, '2026-01-01');
        INSERT INTO reviews (review_id, app_id, country, lang, score, content) VALUES ('r1', 'a', 'tr', 'tr', 1, 'x');
    """)
    old.commit()
    old.close()
    conn = db.connect(path)
    pk = [r[1] for r in sorted(conn.execute("PRAGMA table_info(reviews)"), key=lambda r: r[5]) if r[5]]
    assert pk == ["review_id", "country", "lang"]
    assert conn.execute("SELECT content FROM reviews").fetchone()[0] == "x"
    assert conn.execute("SELECT list FROM app_category").fetchone()[0] == "top"
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert db.insert_reviews(conn, "a", "cy", "tr", [make_review("r1")]) == 1
    conn.close()
    db.connect(path).close()  # idempotent


def test_publish_list_swaps_staging_atomically(conn):
    db.link_app_category(conn, "old", "C", "tr", "tr", 1, "top")
    db.link_app_category(conn, "new", "C", "tr", "tr", 1, db.staging_name("top"))
    db.publish_list(conn, "top", "C", "tr", "tr")
    rows = conn.execute("SELECT list, app_id FROM app_category").fetchall()
    assert [tuple(r) for r in rows] == [("top", "new")]


def test_clean_text_keeps_breaks():
    assert db.clean_text("Bir<br/>İki<br />Üç<p>Dört</p> &amp; beş") == "Bir\nİki\nÜç\nDört\n& beş"


def test_save_analysis_roundtrip(conn):
    db.save_analysis(conn, "keyword", [{"review_id": "r", "category": "bug", "themes": ["a", "b"], "low_signal": 0}])
    assert json.loads(db.load_analysis(conn, "keyword")["r"]["themes"]) == ["a", "b"]
