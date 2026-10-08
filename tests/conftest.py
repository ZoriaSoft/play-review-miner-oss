"""Shared fixtures: a temporary database and an in-memory fake of the Play Store.

No test touches the network: `fake_play` replaces the google-play-scraper calls used by
`play_review_miner.crawler` with deterministic data.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from play_review_miner import crawler, db  # noqa: E402


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    yield c
    c.close()


def make_review(rid: str, score: int = 1, content: str = "Uygulama sürekli çöküyor, açılmıyor", age_days: int = 0,
                thumbs: int = 0) -> dict:
    return {"reviewId": rid, "score": score, "content": content, "thumbsUpCount": thumbs,
            "at": datetime(2026, 10, 1) - timedelta(days=age_days), "replyContent": None, "repliedAt": None,
            "appVersion": "1.0"}


class _Token:
    def __init__(self, app_id, star, offset, count, exhausted):
        self.app_id, self.star, self.offset, self.count = app_id, star, offset, count
        self.token = None if exhausted else f"t{offset}"


class FakePlay:
    """apps: {app_id: {"developer":..., "genre_id":..., "reviews": {star: [newest first]}}}"""

    def __init__(self, apps: dict, category_ids: list[str] | None = None, search: dict | None = None):
        self.apps = apps
        self.category_ids = category_ids if category_ids is not None else list(apps)
        self.search_hits = search or {}
        self.review_calls = 0
        self.fail_reviews_for: set[str] = set()

    def http_get(self, url, timeout=30):
        return "".join(f'<a href="/store/apps/details?id={a}">' for a in self.category_ids)

    def app(self, app_id, lang="en", country="us"):
        from google_play_scraper.exceptions import NotFoundError
        a = self.apps.get(app_id)
        if a is None or a.get("missing"):
            raise NotFoundError("App not found(404).")
        return {"appId": app_id, "title": a.get("title", app_id.split(".")[-1].title()),
                "developer": a.get("developer", "Indie Dev"), "genreId": a.get("genre_id", "PRODUCTIVITY"),
                "genre": "Verimlilik", "summary": a.get("summary", "Notlarınızı ve görevlerinizi kolayca düzenleyin."),
                "description": "Uzun açıklama.<br>İkinci satır.", "score": 4.1, "ratings": 1234,
                "installs": "100.000+", "realInstalls": 123456, "free": True, "containsAds": True,
                "url": f"https://play.google.com/store/apps/details?id={app_id}"}

    def search(self, term, lang="en", country="us", n_hits=30):
        return [{"appId": a} for a in self.search_hits.get(term, [])]

    def reviews(self, app_id, lang="en", country="us", sort=None, count=100, filter_score_with=None,
                continuation_token=None):
        self.review_calls += 1
        if app_id in self.fail_reviews_for:
            raise RuntimeError("simulated network failure")
        if continuation_token is not None:
            app_id, star, offset = continuation_token.app_id, continuation_token.star, continuation_token.offset
            count = continuation_token.count  # the real scraper also reuses the first page size
        else:
            star, offset = filter_score_with, 0
        items = self.apps[app_id]["reviews"].get(star, [])
        page = items[offset:offset + count]
        nxt = offset + len(page)
        return page, _Token(app_id, star, nxt, count, exhausted=nxt >= len(items))


@pytest.fixture
def fake_play(monkeypatch):
    def install(play: FakePlay) -> FakePlay:
        monkeypatch.setattr(crawler, "_http_get", play.http_get)
        monkeypatch.setattr(crawler, "gp_app", play.app)
        monkeypatch.setattr(crawler, "gp_search", play.search)
        monkeypatch.setattr(crawler, "gp_reviews", play.reviews)
        monkeypatch.setattr(crawler.time, "sleep", lambda s: None)
        return play
    return install
