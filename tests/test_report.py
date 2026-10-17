"""--quotes 0 must suppress verbatim reviews in both Markdown and JSON reports."""
from __future__ import annotations

import json
from types import SimpleNamespace

from play_review_miner.analyzers import keyword as keyword_mod
from play_review_miner.analyzers.keyword import KeywordAnalyzer
from play_review_miner.report import _pick_quotes, aggregate, render_markdown

QUOTE_TEXT = "Bu uygulama sürekli çöküyor ve hiç açılmıyor artık"


def _reviews():
    return [{"review_id": "r1", "app_id": "a.one", "app_title": "One", "developer": "Dev",
             "rank": 1, "score": 1, "content": QUOTE_TEXT, "at": "2026-10-01", "thumbs_up": 5}]


def _labels():
    return {"r1": {"themes": '["crash"]', "low_signal": 0, "category": "bug"}}


def _args(quotes):
    return SimpleNamespace(category="PRODUCTIVITY", lang="tr", country="tr", max_stars=2,
                           list_name="top", since=None, quotes=quotes, themes=15)


def test_pick_quotes_zero_returns_empty():
    items = [{"app_id": "a.one", "content": QUOTE_TEXT, "at": "2026-10-01", "thumbs_up": 1}]
    assert _pick_quotes(items, 0) == []
    assert _pick_quotes(items, -1) == []
    assert len(_pick_quotes(items, 3)) == 1


def test_quotes_zero_empties_json_and_markdown():
    data = aggregate(_reviews(), _labels(), {"crash": {"label": "Çökme", "category": "bug"}}, 0)
    data["generated_at"] = "2026-10-17 00:00 UTC"
    # TF-IDF cluster examples are verbatim reviews too: they must obey --quotes 0.
    data["extra"] = {"unmatched_count": 1, "clusters": [
        {"size": 3, "top_terms": ["crash"], "apps": [("One", 3)],
         "examples": [{"app": "One", "score": 1, "content": QUOTE_TEXT}]}]}

    payload = json.loads(json.dumps(data, ensure_ascii=False))  # the JSON report body
    assert all(theme["quotes"] == [] for theme in payload["themes"])

    md = render_markdown(data, _args(0), "keyword", 15)
    assert QUOTE_TEXT not in md
    # Sanity: with quotes enabled the same data does render the quote.
    md_with = render_markdown(data, _args(3), "keyword", 15)
    assert QUOTE_TEXT[:30] in md_with


def test_extra_sections_passes_zero_examples(monkeypatch):
    seen = {}

    def fake_tfidf(reviews, max_examples=3):
        seen["n"] = len(reviews)
        seen["max_examples"] = max_examples
        examples = [{"app": "One", "score": 1, "content": QUOTE_TEXT}] if max_examples > 0 else []
        return [{"size": 1, "top_terms": ["x"], "apps": [("One", 1)], "examples": examples}]

    monkeypatch.setattr(keyword_mod, "tfidf_clusters", fake_tfidf)
    labels = {"r1": {"themes": [], "low_signal": False}}
    out = KeywordAnalyzer().extra_sections(_reviews(), labels, max_examples=0)
    assert seen == {"n": 1, "max_examples": 0}
    assert out["clusters"][0]["examples"] == []
