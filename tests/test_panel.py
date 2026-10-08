"""Panel: store, job validation, and the HTTP API served for real on a random local port."""
from __future__ import annotations

import http.client
import json
import threading
import time
from http.server import ThreadingHTTPServer

import pytest
from conftest import FakePlay, make_review

from play_review_miner import cli
from play_review_miner.panel import md
from play_review_miner.panel.jobs import JobError, build_argv, progress, validate
from play_review_miner.panel.server import Panel, make_handler
from play_review_miner.panel.store import Store, mask

PASSWORD = "correct horse battery"
SECRET = "sk-very-secret-key-1234"


# ---- unit ------------------------------------------------------------------------------------
def test_validate_and_argv():
    p = validate({"kind": "run", "category": "tools", "lang": "EN", "country": "us", "top": "5", "analyzer": "llm",
                  "endpoint_id": "1", "model": "vendor/small-model:free", "search_terms": "vpn, qr",
                  "exclude_big": True, "since": "2026-10-01"})
    argv = build_argv(p, "data/r.db", "reports", "https://x/v1")
    assert argv[3:5] == ["run", "--db"] and "TOOLS" in argv and "en" in argv
    assert argv[argv.index("--llm-model") + 1] == "vendor/small-model:free"
    assert argv[argv.index("--llm-base-url") + 1] == "https://x/v1"
    assert argv.count("--search-term") == 2 and "--exclude-big" in argv and "--since" in argv
    assert build_argv(validate({"kind": "analyze"}), "d", "r")[3:5] == ["run", "--skip-crawl"]


@pytest.mark.parametrize("bad", [
    {"kind": "rm"}, {"category": "NOPE"}, {"lang": "tr; rm -rf"}, {"top": 0}, {"top": "x"},
    {"list_name": "a b"}, {"since": "2026-13-01"}, {"analyzer": "llm", "endpoint_id": 1, "model": "has space"},
    {"analyzer": "llm"}, {"search_terms": ["x\ny"]},
])
def test_validate_rejects(bad):
    with pytest.raises(JobError):
        validate(bad)


def test_progress_parsing():
    log = "\n".join(["INFO 120 candidate apps", "INFO [ 3/20] com.x", "INFO Analyzer=llm-x, reviews=246",
                     "INFO llm-x m: batches 4-6 / 13 (reviews 61-120 / 246)", "INFO llm-x: 17 HTTP requests this run"])
    assert progress(log) == {"stage": "analiz", "crawl": [3, 20], "analyze": [120, 246], "requests": 17}


def test_markdown_escapes_untrusted_text():
    out = md.render('> “<script>alert(1)</script>” [x](javascript:alert(1)) [ok](https://a.b/c?d=1&e=2)\n\n| a | b |\n|---|---:|\n| <b>x</b> \\| y | 2 |')
    assert "<script>" not in out and "&lt;script&gt;" in out
    assert 'href="javascript' not in out and 'href="https://a.b/c?d=1&amp;e=2"' in out
    assert "<td>&lt;b&gt;x&lt;/b&gt; | y</td>" in out and "<td class=num>2</td>" in out


def test_store_masks_and_keeps_keys(tmp_path):
    s = Store(tmp_path / "p.db")
    eid = s.save_endpoint({"name": "or", "base_url": "https://x/v1", "api_key": SECRET, "daily_limit": 1000})
    assert SECRET not in json.dumps(s.list_endpoints()) and s.list_endpoints()[0]["api_key_masked"] == mask(SECRET)
    s.save_endpoint({"name": "or", "base_url": "https://y/v1", "api_key": ""}, eid)   # empty key keeps it
    assert s.get_endpoint(eid, with_key=True)["api_key"] == SECRET
    s.save_endpoint({"name": "or", "base_url": "https://y/v1", "clear_key": True}, eid)
    assert s.get_endpoint(eid, with_key=True)["api_key"] is None
    s.add_model(eid, "a/manual")
    s.replace_fetched_models(eid, ["b/fetched", "a/manual"])
    assert {(m["model_id"], m["source"]) for m in s.list_models(eid)} == {("a/manual", "manual"), ("b/fetched", "fetched")}
    assert oct((tmp_path / "p.db").stat().st_mode)[-3:] == "600"
    s.set_password(PASSWORD)
    assert s.check_password(PASSWORD) and not s.check_password("wrong")


# ---- HTTP ------------------------------------------------------------------------------------
class Client:
    def __init__(self, port):
        self.port, self.cookie = port, None

    def req(self, method, path, body=None, headers=None, panel_header=True):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Content-Type": "application/json"}
        if panel_header:
            h["X-Panel"] = "1"
        if self.cookie:
            h["Cookie"] = self.cookie
        h.update(headers or {})
        c.request(method, path, json.dumps(body) if body is not None else None, h)
        r = c.getresponse()
        raw = r.read()
        if sc := r.getheader("Set-Cookie"):
            self.cookie = sc.split(";")[0]
        try:
            data = json.loads(raw)
        except ValueError:
            data = raw
        return r.status, data, dict(r.getheaders())

    def login(self, pw=PASSWORD):
        return self.req("POST", "/api/login", {"password": pw})


@pytest.fixture
def panel(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "reports").mkdir()
    (tmp_path / "data").mkdir()
    Store(tmp_path / "data" / "panel.db").set_password(PASSWORD)
    p = Panel(tmp_path, "data/reviews.db", "reports", tmp_path / "data", "http://127.0.0.1:4000/dashboard",
              public_hosts=["miner.example.com"])
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(p))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield p, httpd.server_address[1], tmp_path
    p.runner.shutdown()
    httpd.shutdown()
    p.store.close()


def test_static_and_security_headers(panel):
    _, port, _ = panel
    status, body, headers = Client(port).req("GET", "/")
    assert status == 200 and b"Review Miner" in body
    assert "script-src 'self'" in headers["Content-Security-Policy"] and headers["X-Frame-Options"] == "DENY"
    assert Client(port).req("GET", "/static/../server.py")[0] == 404


def test_auth_required_and_login_backoff(panel):
    _, port, _ = panel
    c = Client(port)
    assert c.req("GET", "/api/endpoints")[0] == 401
    assert c.req("GET", "/api/session")[1] == {"authenticated": False, "password_set": True}
    for _ in range(3):
        assert c.login("nope")[0] == 401
    assert c.login("nope")[0] == 429            # backing off now, even for the right password soon
    c2 = Client(port)
    status, _, headers = c2.req("POST", "/api/login", {"password": PASSWORD}, {"CF-Connecting-IP": "203.0.113.9"})
    assert status == 200 and "HttpOnly" in headers["Set-Cookie"] and "SameSite=Strict" in headers["Set-Cookie"]
    assert c2.req("GET", "/api/endpoints")[0] == 200


def test_csrf_protections(panel):
    _, port, _ = panel
    c = Client(port)
    assert c.login()[0] == 200
    assert c.req("POST", "/api/endpoints", {"name": "x", "base_url": "https://x/v1"}, panel_header=False)[0] == 403
    assert c.req("POST", "/api/endpoints", {"name": "x", "base_url": "https://x/v1"}, {"Origin": "https://evil.test"})[0] == 403
    assert c.req("POST", "/api/endpoints", {"name": "x", "base_url": "https://x/v1"}, {"Origin": "https://miner.example.com"})[0] == 201


def test_endpoint_and_model_management_never_leaks_key(panel):
    _, port, _ = panel
    c = Client(port)
    c.login()
    status, e, _ = c.req("POST", "/api/endpoints", {"name": "gateway", "base_url": "https://gateway.example.com/v1/",
                                                    "api_key": SECRET, "daily_limit": 1000})
    assert status == 201 and e["base_url"] == "https://gateway.example.com/v1" and e["has_key"]
    assert c.req("POST", "/api/endpoints", {"name": "gateway", "base_url": "https://x/v1"})[0] == 409
    assert c.req("POST", "/api/endpoints", {"name": "bad", "base_url": "ftp://x"})[0] == 400
    for path in ("/api/endpoints", f"/api/endpoints/{e['id']}/models", "/api/jobs"):
        assert SECRET not in json.dumps(c.req("GET", path)[1])
    st, models, _ = c.req("POST", f"/api/endpoints/{e['id']}/models", {"model_id": "vendor/small-model:free", "note": "hızlı"})
    assert st == 201 and models[0]["model_id"] == "vendor/small-model:free" and models[0]["source"] == "manual"
    assert c.req("POST", f"/api/endpoints/{e['id']}/models", {"model_id": "bad id"})[0] == 400
    st, models, _ = c.req("DELETE", f"/api/endpoints/{e['id']}/models/vendor%2Fsmall-model%3Afree")
    assert st == 200 and models == []


def test_fetch_models_uses_endpoint_key(panel, monkeypatch):
    p, port, _ = panel
    seen = {}

    def fake_list(base, timeout=30, api_key=None):
        seen.update(base=base, key=api_key)
        return ["a/one", "b/two"]
    monkeypatch.setattr("play_review_miner.analyzers.openai_compat.list_models", fake_list)
    c = Client(port)
    c.login()
    e = c.req("POST", "/api/endpoints", {"name": "p", "base_url": "http://127.0.0.1:4000/v1", "api_key": SECRET})[1]
    assert c.req("POST", f"/api/endpoints/{e['id']}/models/fetch")[1] == {"fetched": 2}
    assert seen == {"base": "http://127.0.0.1:4000/v1", "key": SECRET}
    assert [m["model_id"] for m in c.req("GET", f"/api/endpoints/{e['id']}/models")[1]] == ["a/one", "b/two"]


def test_daily_quota_is_enforced(panel):
    p, port, _ = panel
    c = Client(port)
    c.login()
    e = c.req("POST", "/api/endpoints", {"name": "or", "base_url": "https://x/v1", "daily_limit": 10})[1]
    jid = p.store.create_job("analyze", validate({"kind": "analyze"}), e["id"], "m/x", 10)
    p.store.update_job(jid, status="done", requests=10, started_at=time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()))
    st, err, _ = c.req("POST", "/api/jobs", {"kind": "analyze", "analyzer": "llm", "endpoint_id": e["id"], "model": "m/x"})
    assert st == 400 and "kota" in err["error"]
    # report-only jobs send no requests, so they are allowed
    assert c.req("POST", "/api/jobs", {"kind": "report", "analyzer": "llm", "endpoint_id": e["id"], "model": "m/x"})[0] == 201


def test_report_listing_view_and_traversal(panel):
    _, port, root = panel
    (root / "reports" / "A_tr-tr_keyword.md").write_text("# Rapor\n\n> “<img src=x onerror=alert(1)>”\n", encoding="utf-8")
    (root / "reports" / "A_tr-tr_keyword.json").write_text(json.dumps({"params": {"category": "A"}, "totals": {"reviews": 3},
                                                                       "themes": [{"label": "T", "reviews": 2}]}), encoding="utf-8")
    (root / "secret.md").write_text("nope")
    c = Client(port)
    c.login()
    items = c.req("GET", "/api/reports")[1]
    assert items[0]["name"] == "A_tr-tr_keyword.md" and items[0]["top_themes"] == [{"label": "T", "reviews": 2}]
    view = c.req("GET", "/api/reports/A_tr-tr_keyword.md")[1]
    assert "<img" not in view["html"] and "&lt;img" in view["html"]
    assert c.req("GET", "/api/reports/..%2Fsecret.md")[0] in (400, 404)
    assert c.req("GET", "/api/reports/A_tr-tr_keyword.json?raw=1")[0] == 200


def test_job_runs_for_real_and_produces_report(panel, fake_play):
    p, port, root = panel
    fake_play(FakePlay({"a.one": {"reviews": {1: [make_review(f"a{i}", content=t) for i, t in enumerate(
        ["Çok fazla reklam var", "Sürekli çöküyor", "Abonelik istiyor her şey için"])]}}}))
    assert cli.main(["crawl", "--db", "data/reviews.db", "--out", "reports", "--delay", "0", "-r", "5"]) == 0
    c = Client(port)
    c.login()
    st, job, _ = c.req("POST", "/api/jobs", {"kind": "analyze", "analyzer": "keyword", "top": 5})
    assert st == 201
    for _ in range(150):
        j = c.req("GET", f"/api/jobs/{job['id']}")[1]
        if j["status"] not in ("queued", "running"):
            break
        time.sleep(0.2)
    assert j["status"] == "done", j.get("log")
    assert "PRODUCTIVITY_tr-tr_keyword.md" in j["reports"] and "Report:" in j["log"]
    assert (root / "reports" / "PRODUCTIVITY_tr-tr_keyword.md").exists()


def test_failed_job_reports_error_and_cancel(panel):
    p, port, _ = panel
    c = Client(port)
    c.login()
    job = c.req("POST", "/api/jobs", {"kind": "report", "analyzer": "keyword"})[1]   # empty database
    for _ in range(150):
        j = c.req("GET", f"/api/jobs/{job['id']}")[1]
        if j["status"] not in ("queued", "running"):
            break
        time.sleep(0.2)
    assert j["status"] == "failed" and j["rc"] == 2 and "crawl" in (j["error"] or "")
    assert c.req("POST", f"/api/jobs/{job['id']}/cancel")[0] == 409
