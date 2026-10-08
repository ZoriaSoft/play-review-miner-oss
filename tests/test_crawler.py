import pytest
from conftest import FakePlay, make_review

from play_review_miner import crawler, db


def _reviews(prefix: str, n: int, score: int = 1) -> list[dict]:
    return [make_review(f"{prefix}{i}", score=score, age_days=i) for i in range(n)]


def _crawl(conn, top=5, per_app=10, stars=(1,), **kw):
    return crawler.crawl(conn, "PRODUCTIVITY", "tr", "tr", top, per_app, list(stars), delay=0, **kw)


def _list(conn, name="top"):
    return [r["app_id"] for r in db.select_category_apps(conn, "PRODUCTIVITY", "tr", "tr", None, name)]


def test_basic_crawl_ranks_and_stores(conn, fake_play):
    fake_play(FakePlay({"a.one": {"reviews": {1: _reviews("a", 5)}},
                        "b.two": {"reviews": {1: _reviews("b", 3)}}}))
    stats = _crawl(conn)
    assert [a["app_id"] for a in stats["apps"]] == ["a.one", "b.two"]
    assert _list(conn) == ["a.one", "b.two"]
    assert db.count_reviews(conn, "a.one", "tr", "tr") == 5


def test_larger_sample_later_reaches_older_reviews(conn, fake_play):
    fake_play(FakePlay({"a.one": {"reviews": {1: _reviews("a", 30)}}}))
    _crawl(conn, per_app=10)
    assert db.count_reviews(conn, "a.one", "tr", "tr") == 10
    _crawl(conn, per_app=25)  # first page is fully known, but the stored window is too shallow
    assert db.count_reviews(conn, "a.one", "tr", "tr") == 25


def test_incremental_crawl_stops_when_caught_up(conn, fake_play):
    play = fake_play(FakePlay({"a.one": {"reviews": {1: _reviews("a", 30)}}}))
    _crawl(conn, per_app=10)
    calls = play.review_calls
    _crawl(conn, per_app=10)
    assert play.review_calls - calls == 1  # one fully-known page, then stop


def test_second_country_same_language_gets_its_own_reviews(conn, fake_play):
    fake_play(FakePlay({"a.one": {"reviews": {1: _reviews("a", 5)}}}))
    crawler.crawl(conn, "PRODUCTIVITY", "us", "en", 5, 10, [1], delay=0)
    stats = crawler.crawl(conn, "PRODUCTIVITY", "gb", "en", 5, 10, [1], delay=0)
    assert [a["app_id"] for a in stats["apps"]] == ["a.one"]
    assert db.count_reviews(conn, "a.one", "gb", "en") == 5


def test_min_reviews_and_missing_apps_are_excluded(conn, fake_play):
    fake_play(FakePlay({"a.one": {"reviews": {1: _reviews("a", 5)}},
                        "gov.app": {"reviews": {}},
                        "gone.app": {"missing": True, "reviews": {}}},
                       category_ids=["gone.app", "gov.app", "a.one"]))
    stats = _crawl(conn, min_reviews=2)
    assert [a["app_id"] for a in stats["apps"]] == ["a.one"]
    assert stats["apps"][0]["rank"] == 1
    reasons = {e["app_id"]: e["reason"] for e in stats["excluded"]}
    assert "not available" in reasons["gone.app"]
    assert "only 0" in reasons["gov.app"]
    assert stats["errors"] == []


def test_interrupted_crawl_keeps_previous_list(conn, fake_play, monkeypatch):
    play = fake_play(FakePlay({"a.one": {"reviews": {1: _reviews("a", 3)}},
                               "b.two": {"reviews": {1: _reviews("b", 3)}}}))
    _crawl(conn)
    assert _list(conn) == ["a.one", "b.two"]

    def boom(*a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr(crawler, "fetch_reviews_for_app", boom)
    play.category_ids = ["b.two"]
    with pytest.raises(KeyboardInterrupt):
        _crawl(conn)
    assert _list(conn) == ["a.one", "b.two"]
    monkeypatch.undo()
    fake_play(play)
    _crawl(conn)
    assert _list(conn) == ["b.two"]
    assert _list(conn, db.staging_name("top")) == []


def test_exclude_big_uses_word_boundaries(conn, fake_play):
    fake_play(FakePlay({"com.google.android.keep": {"reviews": {1: _reviews("g", 2)}},
                        "x.notes": {"developer": "Microsoft Corporation", "reviews": {1: _reviews("m", 2)}},
                        "x.pine": {"developer": "Pineapple Games", "reviews": {1: _reviews("p", 2)}}}))
    stats = _crawl(conn, exclude_big=True)
    assert [a["app_id"] for a in stats["apps"]] == ["x.pine"]


@pytest.mark.parametrize("dev", ["Intuitive Apps", "Opportunity Labs", "Canvas Studio", "Pineapple Games",
                                 "Cooperative Ops", "Zoomify", "Modell Software", "Vivoo Health"])
def test_is_big_false_positives(dev):
    assert crawler.is_big("x.y", dev) is None


@pytest.mark.parametrize("app_id,dev", [("x.y", "Google LLC"), ("x.y", "Apple Inc."), ("x.y", "Zoom Video Communications"),
                                        ("x.y", "Intuit Inc"), ("x.y", "Opera"), ("com.whatsapp", None), ("com.larus.wolf", None),
                                        ("com.whatsapp.w4b", None), ("com.Slack", None), ("notion.id", None)])
def test_is_big_hits(app_id, dev):
    assert crawler.is_big(app_id, dev)


def test_package_prefix_matches_whole_segment():
    assert crawler.is_big("com.googleplex.notes") is None
    assert crawler.is_big("com.evernotes.clone") is None


def test_not_found_is_not_retried(monkeypatch):
    from google_play_scraper.exceptions import NotFoundError
    calls = []

    def fn():
        calls.append(1)
        raise NotFoundError("404")
    monkeypatch.setattr(crawler.time, "sleep", lambda s: None)
    with pytest.raises(NotFoundError):
        crawler.with_retry(fn)
    assert len(calls) == 1


def test_review_failure_is_reported_not_fatal(conn, fake_play):
    play = fake_play(FakePlay({"a.one": {"reviews": {1: _reviews("a", 3)}},
                               "b.two": {"reviews": {1: _reviews("b", 3)}}}))
    play.fail_reviews_for = {"a.one"}
    stats = _crawl(conn)
    assert [a["app_id"] for a in stats["apps"]] == ["b.two"]
    assert any("a.one: reviews" in e for e in stats["errors"])
