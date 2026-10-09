"""Panel: store, job validation, and the HTTP API served for real on a random local port."""
from __future__ import annotations

import http.client
import json
import signal
import socket
import sqlite3
import subprocess
import threading
import time
from http.server import ThreadingHTTPServer
from unittest import mock

import pytest
from conftest import FakePlay, make_review

from play_review_miner import cli
from play_review_miner.panel import jobs as jobs_mod
from play_review_miner.panel import md
from play_review_miner.panel.jobs import JobError, Runner, _proc_start, build_argv, progress, validate
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


# ---- hardening: pid identity, header trust, body limits, map sweeps -------------------------------
def test_store_migration_adds_process_start(tmp_path):
    """A pre-0.6.2 jobs table (no process_start) is migrated; user_version bumps."""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE endpoints (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
            base_url TEXT NOT NULL, api_key TEXT, daily_limit INTEGER, default_model TEXT,
            note TEXT, created_at TEXT);
        CREATE TABLE endpoint_models (endpoint_id INTEGER NOT NULL, model_id TEXT NOT NULL,
            source TEXT NOT NULL, note TEXT, added_at TEXT,
            PRIMARY KEY (endpoint_id, model_id));
        CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
            params TEXT NOT NULL, endpoint_id INTEGER, model TEXT, status TEXT NOT NULL,
            request_cap INTEGER, requests INTEGER, rc INTEGER, pid INTEGER, log_path TEXT,
            reports TEXT, error TEXT, created_at TEXT, started_at TEXT, finished_at TEXT);
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
    """)
    conn.execute("INSERT INTO jobs (kind, params, status) VALUES ('run', '{}', 'running')")
    conn.commit()
    conn.close()

    s = Store(db)
    try:
        cols = {r["name"] for r in s._conn.execute("PRAGMA table_info(jobs)")}
        assert "process_start" in cols
        job = s.list_jobs()[0]
        assert job["kind"] == "run" and job["process_start"] is None  # row survived intact
    finally:
        s.close()
    check = sqlite3.connect(db)
    assert check.execute("PRAGMA user_version").fetchone()[0] == 1
    check.close()


def test_proc_start_identifies_a_process_lifetime():
    proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        start = _proc_start(proc.pid)
        assert start and start == _proc_start(proc.pid)
        assert Runner._is_our_process(proc.pid, None, start) is True
        assert Runner._is_our_process(proc.pid, None, "bogus") is False
        assert Runner._is_our_process(proc.pid, None, None) is False
    finally:
        proc.kill()
        proc.wait()
    # dead pid: stat unreadable -> cannot verify -> not ours
    assert Runner._is_our_process(proc.pid, None, start) is False


def test_cancel_never_signals_an_unverifiable_pid(panel, monkeypatch):
    """PID reuse hazard: a 'running' job whose pid fails identity must be
    marked failed — never signalled."""
    p, _, _ = panel
    timer = mock.Mock()
    monkeypatch.setattr(jobs_mod.threading, "Timer", lambda *a, **k: timer)

    jid = p.store.create_job("run", {"kind": "run"}, None, None, None)
    # stored pid is live but has a different start identity -> foreign process
    foreign = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        p.store.update_job(jid, status="running", pid=foreign.pid, process_start="bogus")
        assert p.runner.cancel(jid) is True
        job = p.store.get_job(jid)
        assert job["status"] == "failed"
        assert "no longer running" in job["error"]
        assert timer.start.call_count == 0          # no kill timer was armed
        assert foreign.poll() is None               # the stranger was never signalled
    finally:
        foreign.kill()
        foreign.wait()


def test_cancel_signals_verified_process(panel, monkeypatch):
    """A job whose pid + start identity still match gets the SIGINT."""
    p, _, _ = panel
    timer = mock.Mock()
    monkeypatch.setattr(jobs_mod.threading, "Timer", lambda *a, **k: timer)
    proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
    jid = p.store.create_job("run", {"kind": "run"}, None, None, None)
    p.store.update_job(jid, status="running", pid=proc.pid, process_start=_proc_start(proc.pid))
    try:
        assert p.runner.cancel(jid) is True
        rc = proc.wait(timeout=10)
        assert rc == -signal.SIGINT
        timer.start.assert_called_once()            # SIGTERM fallback armed
    finally:
        if proc.poll() is None:
            proc.kill()


def test_recover_keeps_a_completed_job_done(tmp_path):
    """Process gone but its log shows a produced report -> stays 'done',
    not relabelled failed."""
    data = tmp_path / "data"
    data.mkdir()
    reports = tmp_path / "reports"
    reports.mkdir()
    s = Store(data / "panel.db")
    jid = s.create_job("report", {"kind": "report"}, None, None, None)
    (data / "jobs").mkdir()
    (data / "jobs" / f"{jid}.log").write_text("Report: reports/X_tr-tr_keyword.md\n", encoding="utf-8")
    (reports / "X_tr-tr_keyword.md").write_text("# ok\n", encoding="utf-8")
    s.update_job(jid, status="running", pid=99999999, process_start=None,
                 log_path=str(data / "jobs" / f"{jid}.log"))
    s.close()

    r = Runner(Store(data / "panel.db"), tmp_path, "x.db", "reports", data / "jobs")
    try:
        job = r.store.get_job(jid)
        assert job["status"] == "done" and job["reports"] == ["X_tr-tr_keyword.md"]
    finally:
        r.shutdown()
        r.store.close()


def test_recover_fails_unverifiable_running_jobs(tmp_path):
    """A job left 'running' whose recorded pid cannot be verified is closed
    as failed — and the foreign process holding that pid is untouched."""
    data = tmp_path / "data"
    data.mkdir()
    (tmp_path / "reports").mkdir()
    s = Store(data / "panel.db")
    foreign = subprocess.Popen(["sleep", "60"], start_new_session=True)
    jid = s.create_job("run", {"kind": "run"}, None, None, None)
    # pre-0.6.2 style row: pid recorded, no identity
    s.update_job(jid, status="running", pid=foreign.pid, process_start=None)
    s.close()

    r = Runner(Store(data / "panel.db"), tmp_path, "x.db", "reports", data / "jobs")
    try:
        job = r.store.get_job(jid)
        assert job["status"] == "failed"
        assert "no longer running" in job["error"]
        assert foreign.poll() is None
    finally:
        r.shutdown()
        r.store.close()
        foreign.kill()
        foreign.wait()


def test_cf_connecting_ip_requires_tunnel_mode(tmp_path, monkeypatch):
    """Without --public-host the panel trusts the peer, not the header —
    so spoofing CF-Connecting-IP cannot rotate the login back-off."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "reports").mkdir()
    (tmp_path / "data").mkdir()
    Store(tmp_path / "data" / "panel.db").set_password(PASSWORD)
    p = Panel(tmp_path, "data/reviews.db", "reports", tmp_path / "data", None)  # no public_hosts
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(p))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        c = Client(httpd.server_address[1])
        for _ in range(3):
            assert c.req("POST", "/api/login", {"password": "x"})[0] == 401
        st = c.req("POST", "/api/login", {"password": "x"},
                   {"CF-Connecting-IP": "198.51.100.7"})[0]
        assert st == 429   # header ignored: still the loopback peer's backoff
    finally:
        p.runner.shutdown()
        httpd.shutdown()
        p.store.close()


def _raw_status(port: int, request: bytes) -> int:
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        s.sendall(request)
        return int(s.makefile("rb").readline().split()[1])
    finally:
        s.close()


def test_content_length_validation(panel):
    _, port, _ = panel
    base = b"POST /api/login HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Panel: 1\r\n"
    assert _raw_status(port, base + b"Content-Length: abc\r\n\r\n") == 400
    assert _raw_status(port, base + b"Content-Length: -1\r\n\r\n") == 400
    assert _raw_status(port, base + b"Content-Length: 999999\r\n\r\n") == 413


def test_sessions_and_failures_are_swept(panel):
    p, port, _ = panel
    c = Client(port)
    p.sessions["dead"] = time.time() - 10
    p.failures["1.2.3.4"] = (9, 0.0, time.time() - 3700)  # stale: last attempt >1h ago
    p.sweep(force=True)
    assert "dead" not in p.sessions and "1.2.3.4" not in p.failures

    # throttled: a plain sweep inside 60s is a no-op
    p.sessions["stale"] = time.time() - 1
    p.sweep()
    assert "stale" in p.sessions

    # every login attempt forces a sweep
    p.sessions["stale2"] = time.time() - 1
    c.login("wrong")
    assert "stale2" not in p.sessions
