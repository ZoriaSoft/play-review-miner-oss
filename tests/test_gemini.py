"""Gemini analyzer logic with a scripted fake of the HTTP call (no API key, no network)."""
import re

import pytest

from play_review_miner.analyzers import gemini
from play_review_miner.analyzers.gemini import GeminiAnalyzer, GeminiTruncated


@pytest.fixture
def analyzer(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_BATCH_SIZE", "10")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-test-flash")
    return GeminiAnalyzer()


def reviews(n):
    return [{"review_id": f"r{i}", "app_title": "App", "score": 1, "content": f"review {i}"} for i in range(n)]


def indices(prompt):
    block = prompt.rsplit("<reviews>", 1)[1]
    return [int(m) for m in re.findall(r"^\[(\d+)\]", block, re.M)]


def label(i, themes=("crash_wont_open",)):
    return {"i": i, "category": "bug", "themes": list(themes), "summary": "çöküyor", "low_signal": False}


def test_skipped_reviews_are_retried_then_left_unstored(analyzer, monkeypatch):
    calls = []

    def fake(prompt, schema, retries=5):
        if "<reviews>" not in prompt:
            return []
        idx = indices(prompt)
        calls.append(len(idx))
        if len(calls) == 1:
            return [label(i) for i in idx if i != 3 and i != 5]   # skips two
        return [label(0)]                                          # retry answers only the first of them
    monkeypatch.setattr(analyzer, "_generate", fake)
    out = analyzer.analyze(reviews(8))
    assert calls == [8, 2]
    assert {l["review_id"] for l in out} == {f"r{i}" for i in range(8)} - {"r5"}


def test_truncated_answer_splits_batch(analyzer, monkeypatch):
    def fake(prompt, schema, retries=5):
        if "<reviews>" not in prompt:
            return []
        idx = indices(prompt)
        if len(idx) > 2:
            raise GeminiTruncated("too long")
        return [label(i) for i in idx]
    monkeypatch.setattr(analyzer, "_generate", fake)
    assert len(analyzer.analyze(reviews(7))) == 7


def test_new_themes_map_into_stored_ones_and_keep_their_labels(analyzer, monkeypatch):
    analyzer.use_existing_themes({"calendar_sync": {"theme_id": "calendar_sync", "label": "Takvim senkronu",
                                                    "category": "bug", "opportunity": "x"}})

    def fake(prompt, schema, retries=5):
        if "<reviews>" in prompt:
            assert "calendar_sync" in prompt  # stored ids are offered to the classifier
            return [label(0, ["Google Calendar Sync"]), label(1, ["cal_sync_broken"]), label(2, ["brand_new"]),
                    label(3, ["crash_wont_open"])]
        assert "Existing themes" in prompt and "calendar_sync" in prompt
        return [
            {"theme_id": "google_calendar_sync", "canonical_id": "calendar_sync", "label": "yeni etiket",
             "category": "bug", "opportunity": "y"},
            {"theme_id": "cal_sync_broken", "canonical_id": "google_calendar_sync", "label": "-",
             "category": "bug", "opportunity": "-"},
            {"theme_id": "brand_new", "canonical_id": "invented_elsewhere", "label": "Yeni tema",
             "category": "ux", "opportunity": "z"},
        ]
    monkeypatch.setattr(analyzer, "_generate", fake)
    out = analyzer.analyze(reviews(4))
    assert [l["themes"] for l in out] == [["calendar_sync"], ["calendar_sync"], ["brand_new"], ["crash_wont_open"]]
    meta = {m["theme_id"]: m for m in analyzer.theme_meta()}
    assert "calendar_sync" not in meta            # stored label is not overwritten
    assert meta["brand_new"]["label"] == "Yeni tema"
    assert meta["crash_wont_open"]["label"]       # first use of a seed id stores its seed meta


def test_consolidation_failure_keeps_ids(analyzer, monkeypatch):
    def fake(prompt, schema, retries=5):
        if "<reviews>" in prompt:
            return [label(0, ["odd_theme"])]
        raise gemini.GeminiError("boom")
    monkeypatch.setattr(analyzer, "_generate", fake)
    out = analyzer.analyze(reviews(1))
    assert out[0]["themes"] == ["odd_theme"]
    assert {m["theme_id"] for m in analyzer.theme_meta()} == {"odd_theme"}


def test_first_batch_error_raises(analyzer, monkeypatch):
    def fake(prompt, schema, retries=5):
        raise gemini.GeminiError("HTTP 403")
    monkeypatch.setattr(analyzer, "_generate", fake)
    with pytest.raises(gemini.GeminiError):
        analyzer.analyze(reviews(3))


def test_app_summaries_survive_a_failing_batch(analyzer, monkeypatch):
    def fake(prompt, schema, retries=5):
        ids = re.findall(r"app_id: (\S+)", prompt)
        if "bad" in ids:
            raise gemini.GeminiError("boom")
        return [{"app_id": i, "summary_tr": f"{i} özet"} for i in ids]
    monkeypatch.setattr(analyzer, "_generate", fake)
    apps = [{"app_id": "ok1"}, {"app_id": "bad"}, {"app_id": "ok2"}]
    out = analyzer.summarize_apps(apps, batch_size=1)
    assert [x["app_id"] for x in out] == ["ok1", "ok2"]


def test_normalize_theme_id():
    assert gemini.normalize_theme_id(" Google Calendar-Sync! ") == "google_calendar_sync"


def test_gemini_model_is_required(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    with pytest.raises(gemini.GeminiError, match="GEMINI_MODEL"):
        GeminiAnalyzer()
