"""End-to-end: crawl (fake Play) -> keyword analyze -> report through the real CLI entry point."""
import json

from conftest import FakePlay, make_review

from play_review_miner import cli


def test_run_end_to_end(tmp_path, fake_play, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    texts = ["Çok fazla reklam var", "Sürekli çöküyor, açılmıyor", "Abonelik istiyor her şey için",
             "berbat", "Bildirimler | hatırlatıcılar çalmıyor"]
    fake_play(FakePlay({
        "a.one": {"title": "Not | Defteri", "reviews": {1: [make_review(f"a{i}", content=t) for i, t in enumerate(texts)]}},
        "b.two": {"reviews": {2: [make_review(f"b{i}", score=2, content=t) for i, t in enumerate(texts)]}},
    }))
    db_path, out = tmp_path / "r.db", tmp_path / "reports"
    rc = cli.main(["run", "--db", str(db_path), "--out", str(out), "--delay", "0", "-n", "5", "-r", "10",
                   "--analyzer", "keyword"])
    assert rc == 0
    md = (out / "PRODUCTIVITY_tr-tr_keyword.md").read_text(encoding="utf-8")
    data = json.loads((out / "PRODUCTIVITY_tr-tr_keyword.json").read_text(encoding="utf-8"))
    assert data["totals"]["reviews"] == 10 and data["totals"]["low_signal"] == 2
    assert {t["theme_id"] for t in data["themes"]} >= {"ads_intrusive", "crash_wont_open", "paywall_subscription"}
    assert "Not \\| Defteri" in md                      # table cells are escaped
    assert (out / "PRODUCTIVITY_tr-tr_crawl.json").exists()


def test_report_without_data_fails_cleanly(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    rc = cli.main(["report", "--db", str(tmp_path / "e.db"), "--out", str(tmp_path / "o"), "--analyzer", "keyword"])
    assert rc == 2
    assert cli.main(["analyze", "--db", str(tmp_path / "e.db"), "--analyzer", "keyword"]) == 2


def test_argument_validation(capsys):
    for argv in (["report", "--since", "2025-13-01"], ["report", "--list", "bad name"], ["crawl", "-n", "0"]):
        try:
            cli.main(argv)
        except SystemExit as exc:
            assert exc.code == 2
        else:
            raise AssertionError(argv)


def test_gemini_results_are_saved_per_chunk(tmp_path, fake_play, monkeypatch):
    """An error after the first chunk must not lose the analyses already paid for."""
    from play_review_miner import db
    from play_review_miner.analyzers import gemini

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-test-flash")
    fake_play(FakePlay({"a.one": {"reviews": {1: [make_review(f"a{i}") for i in range(5)]}}}))
    db_path = tmp_path / "g.db"
    assert cli.main(["crawl", "--db", str(db_path), "--out", str(tmp_path / "o"), "--delay", "0", "-r", "10"]) == 0

    calls = []

    def fake_analyze(self, reviews):
        calls.append(len(reviews))
        if len(calls) == 2:
            raise KeyboardInterrupt
        return [{"review_id": r["review_id"], "category": "bug", "themes": ["crash_wont_open"],
                 "summary": "s", "low_signal": False} for r in reviews]
    monkeypatch.setattr(gemini.GeminiAnalyzer, "save_every", 2)
    monkeypatch.setattr(gemini.GeminiAnalyzer, "analyze", fake_analyze)
    assert cli.main(["analyze", "--db", str(db_path), "--analyzer", "gemini"]) == 130
    conn = db.connect(db_path)
    assert len(db.analyzed_ids(conn, "gemini")) == 2
    conn.close()


def test_gemini_without_key_fails_cleanly(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert cli.main(["analyze", "--db", str(tmp_path / "x.db"), "--analyzer", "gemini"]) == 1


def test_report_warns_about_unanalyzed_reviews(tmp_path, fake_play, monkeypatch, caplog):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    fake_play(FakePlay({"a.one": {"reviews": {1: [make_review("a0")]}}}))
    db = str(tmp_path / "w.db")
    assert cli.main(["crawl", "--db", db, "--out", str(tmp_path), "--delay", "0", "-r", "5"]) == 0
    assert cli.main(["report", "--db", db, "--out", str(tmp_path), "--analyzer", "keyword"]) == 0
    assert "have no keyword analysis yet" in caplog.text
