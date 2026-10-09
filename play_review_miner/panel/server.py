"""Review Miner web panel: start crawls/analyses, follow jobs, read reports, manage endpoints/models.

    python -m play_review_miner panel-password          # set the login password (once)
    python -m play_review_miner panel --port 8765        # serve on 127.0.0.1:8765

Standard library only. Meant to sit behind Cloudflare Tunnel + Access, but it has its own login as
well (defense in depth): password (PBKDF2 hash in data/panel.db), HttpOnly + SameSite=Strict
session cookie, a required X-Panel header + Origin check on every state-changing request, login
back-off, strict CSP. API keys never leave the server unmasked.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .. import __version__
from ..analyzers.base import AnalyzerError
from ..crawler import CATEGORIES
from . import md
from .jobs import KINDS, JobError, Runner, progress, read_tail
from .store import Store, mask

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
SESSION_TTL = 7 * 24 * 3600
COOKIE = "prm_session"
_REPORT_NAME = re.compile(r"^[A-Za-z0-9_.-]+\.(md|json)$")
_URL = re.compile(r"^https?://[^\s/$.?#][^\s]*$")
_CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
          ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".json": "application/json; charset=utf-8",
          ".md": "text/markdown; charset=utf-8"}


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status, self.message = status, message


class Panel:
    def __init__(self, project_dir: Path, db_path: str, out_dir: str, data_dir: Path,
                 proxy_dashboard: str | None = None, max_parallel: int = 2,
                 public_hosts: list[str] | None = None) -> None:
        self.project_dir = project_dir
        self.public_hosts = set(public_hosts or [])
        self.out_dir = out_dir
        self.store = Store(data_dir / "panel.db")
        self.runner = Runner(self.store, project_dir, db_path, out_dir, data_dir / "jobs", max_parallel)
        self.proxy_dashboard = proxy_dashboard
        self.sessions: dict[str, float] = {}
        self.failures: dict[str, tuple[int, float, float]] = {}  # (count, backoff-until, last-attempt)
        self.lock = threading.Lock()
        self._last_sweep = 0.0

    # ---- auth ----------------------------------------------------------------------------
    def sweep(self, force: bool = False) -> None:
        """Drop expired sessions and stale login-failure entries so the maps stay bounded.

        Called on every login attempt (force) and, throttled to once a minute,
        on each request — otherwise both dicts grow forever on a long-running panel.
        """
        now = time.time()
        with self.lock:
            if not force and now - self._last_sweep < 60:
                return
            self._last_sweep = now
            self.sessions = {t: e for t, e in self.sessions.items() if e > now}
            self.failures = {ip: v for ip, v in self.failures.items() if v[1] > now or v[2] > now - 3600}

    def login(self, ip: str, password: str) -> str:
        self.sweep(force=True)
        with self.lock:
            n, until, _ = self.failures.get(ip, (0, 0.0, 0.0))
            if until > time.time():
                raise ApiError(429, f"Çok fazla hatalı deneme; {int(until - time.time()) + 1} sn bekleyin")
        if not self.store.has_password():
            raise ApiError(503, "Panel parolası ayarlanmamış: python -m play_review_miner panel-password")
        if not self.store.check_password(password or ""):
            with self.lock:
                n += 1
                # (count, backoff-until, last-attempt) — last-attempt is what
                # makes "stale" definable without resetting the counter
                self.failures[ip] = (n, time.time() + (min(300, 2 ** n) if n >= 3 else 0), time.time())
            raise ApiError(401, "Parola yanlış")
        with self.lock:
            self.failures.pop(ip, None)
            token = secrets.token_urlsafe(32)
            self.sessions[token] = time.time() + SESSION_TTL
        return token

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        with self.lock:
            exp = self.sessions.get(token)
            if not exp or exp < time.time():
                self.sessions.pop(token, None)
                return False
        return True

    def logout(self, token: str | None) -> None:
        with self.lock:
            self.sessions.pop(token or "", None)

    # ---- API -----------------------------------------------------------------------------
    def meta(self) -> dict:
        from ..analyzers.openai_compat import DEFAULT_MODEL
        return {"version": __version__, "categories": CATEGORIES, "kinds": KINDS,
                "default_model": DEFAULT_MODEL or "", "proxy_dashboard": self.proxy_dashboard}

    def endpoint_payload(self, body: dict, partial: bool = False) -> dict:
        name = str(body.get("name") or "").strip()
        base_url = str(body.get("base_url") or "").strip().rstrip("/")
        if not name or len(name) > 60:
            raise ApiError(400, "Ad gerekli (en fazla 60 karakter)")
        if not _URL.match(base_url):
            raise ApiError(400, "Base URL http(s):// ile başlamalı, ör. https://openrouter.ai/api/v1")
        limit = body.get("daily_limit")
        if limit in ("", None):
            limit = None
        else:
            try:
                limit = int(limit)
            except (TypeError, ValueError):
                raise ApiError(400, "Günlük istek sınırı sayı olmalı") from None
            if limit < 1:
                raise ApiError(400, "Günlük istek sınırı en az 1")
        key = body.get("api_key")
        if key is not None and (not isinstance(key, str) or len(key) > 500 or "\n" in key):
            raise ApiError(400, "Anahtar geçersiz")
        return {"name": name, "base_url": base_url, "daily_limit": limit, "api_key": (key or "").strip() or None,
                "default_model": str(body.get("default_model") or "").strip()[:200] or None,
                "note": str(body.get("note") or "").strip()[:300] or None, "clear_key": bool(body.get("clear_key"))}

    def fetch_models(self, endpoint_id: int) -> dict:
        from ..analyzers.openai_compat import list_models
        e = self.store.get_endpoint(endpoint_id, with_key=True)
        if not e:
            raise ApiError(404, "Uç nokta yok")
        try:
            ids = list_models(e["base_url"], timeout=30, api_key=e.get("api_key") or "")
        except AnalyzerError as exc:
            msg = str(exc)
            if e.get("api_key"):
                msg = msg.replace(e["api_key"], mask(e["api_key"]) or "")
            raise ApiError(502, f"Model listesi alınamadı: {msg}") from None
        self.store.replace_fetched_models(endpoint_id, ids)
        return {"fetched": len(ids)}

    def job_view(self, job: dict, with_log: bool = False) -> dict:
        out = dict(job)
        out.pop("pid", None)
        out["log_path"] = None
        text = read_tail(job.get("log_path"), 200_000 if with_log else 20_000)
        out["progress"] = progress(text)
        if with_log:
            out["log"] = text[-60_000:]
        return out

    def reports(self) -> list[dict]:
        out_dir = self.project_dir / self.out_dir
        items = []
        for mdp in sorted(out_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
            meta: dict = {"name": mdp.name, "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(mdp.stat().st_mtime))}
            js = mdp.with_suffix(".json")
            if js.exists():
                try:
                    d = json.loads(js.read_text(encoding="utf-8"))
                    meta.update(json_name=js.name, params=d.get("params"), totals=d.get("totals"),
                                top_themes=[{"label": t.get("label"), "reviews": t.get("reviews")} for t in d.get("themes", [])[:3]])
                except (OSError, ValueError):
                    pass
            items.append(meta)
        return items

    def report_file(self, name: str) -> Path:
        if not _REPORT_NAME.match(name):
            raise ApiError(400, "Geçersiz rapor adı")
        p = (self.project_dir / self.out_dir / name).resolve()
        if p.parent != (self.project_dir / self.out_dir).resolve() or not p.exists():
            raise ApiError(404, "Rapor yok")
        return p


def make_handler(panel: Panel):
    class Handler(BaseHTTPRequestHandler):
        server_version = "review-miner-panel"
        sys_version = ""

        # -- plumbing
        def log_message(self, fmt, *args):  # keep request logs short; never log bodies
            log.info("%s %s", self.client_ip(), fmt % args)

        def client_ip(self) -> str:
            peer = self.client_address[0]
            # CF-Connecting-IP is honored only in tunnel mode (--public-host is
            # set, so a cloudflared-style tunnel fronts the panel) AND the peer
            # is loopback; otherwise any caller could spoof the login back-off.
            if peer in ("127.0.0.1", "::1") and panel.public_hosts:
                return self.headers.get("CF-Connecting-IP") or peer
            return peer

        def https(self) -> bool:
            return (self.headers.get("X-Forwarded-Proto") == "https"
                    or '"scheme":"https"' in (self.headers.get("CF-Visitor") or "").replace(" ", ""))

        def token(self) -> str | None:
            c = SimpleCookie(self.headers.get("Cookie") or "")
            return c[COOKIE].value if COOKIE in c else None

        def send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Security-Policy", _CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def json(self, status: int, data, extra: dict | None = None) -> None:
            self.send(status, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8", extra)

        def body(self) -> dict:
            raw_len = self.headers.get("Content-Length")
            try:
                n = int(raw_len) if raw_len is not None else 0
            except ValueError:
                raise ApiError(400, "Geçersiz Content-Length") from None
            if n < 0:
                raise ApiError(400, "Geçersiz Content-Length")
            if n > 256_000:
                raise ApiError(413, "İstek çok büyük")
            raw = self.rfile.read(n) if n else b"{}"
            try:
                data = json.loads(raw or b"{}")
            except ValueError:
                raise ApiError(400, "JSON bekleniyor") from None
            if not isinstance(data, dict):
                raise ApiError(400, "JSON nesnesi bekleniyor")
            return data

        def check_write(self) -> None:
            if self.headers.get("X-Panel") != "1":
                raise ApiError(403, "Eksik X-Panel başlığı")
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).netloc not in ({self.headers.get("Host")} | panel.public_hosts):
                raise ApiError(403, "Origin uyuşmuyor")

        # -- dispatch
        def do_GET(self):
            self.route("GET")

        def do_HEAD(self):
            self.route("GET")

        def do_POST(self):
            self.route("POST")

        def do_PUT(self):
            self.route("PUT")

        def do_DELETE(self):
            self.route("DELETE")

        def route(self, method: str) -> None:
            url = urlparse(self.path)
            path = url.path
            panel.sweep()  # bounded session/failure maps; no-op more than once a minute
            try:
                if method == "GET" and (path == "/" or path.startswith("/static/")):
                    return self.static(path)
                if path == "/healthz":
                    return self.json(200, {"ok": True})
                if method != "GET":
                    self.check_write()
                if path == "/api/login" and method == "POST":
                    token = panel.login(self.client_ip(), str(self.body().get("password") or ""))
                    cookie = f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_TTL}"
                    if self.https():
                        cookie += "; Secure"
                    return self.json(200, {"ok": True}, {"Set-Cookie": cookie})
                if path == "/api/session":
                    return self.json(200, {"authenticated": panel.valid(self.token()),
                                           "password_set": panel.store.has_password()})
                if not panel.valid(self.token()):
                    raise ApiError(401, "Oturum gerekli")
                return self.api(method, path, parse_qs(url.query))
            except ApiError as exc:
                self.json(exc.status, {"error": exc.message})
            except JobError as exc:
                self.json(400, {"error": str(exc)})
            except Exception:
                log.exception("panel error on %s %s", method, path)
                self.json(500, {"error": "Sunucu hatası (ayrıntı panel günlüğünde)"})

        def static(self, path: str) -> None:
            name = "index.html" if path == "/" else path[len("/static/"):]
            if not re.match(r"^[A-Za-z0-9_.-]+$", name):
                raise ApiError(404, "yok")
            p = STATIC / name
            if not p.is_file():
                raise ApiError(404, "yok")
            self.send(200, p.read_bytes(), _TYPES.get(p.suffix, "application/octet-stream"))

        def api(self, method: str, path: str, query: dict) -> None:
            s, r = panel.store, panel.runner
            parts = [x for x in path.split("/") if x][1:]  # drop "api"
            if parts == ["logout"] and method == "POST":
                panel.logout(self.token())
                return self.json(200, {"ok": True}, {"Set-Cookie": f"{COOKIE}=; Path=/; Max-Age=0"})
            if parts == ["meta"]:
                return self.json(200, panel.meta())
            # endpoints
            if parts == ["endpoints"]:
                if method == "GET":
                    return self.json(200, s.list_endpoints())
                if method == "POST":
                    data = panel.endpoint_payload(self.body())
                    try:
                        eid = s.save_endpoint(data)
                    except Exception as exc:
                        if "UNIQUE" in str(exc):
                            raise ApiError(409, "Bu adla bir uç nokta zaten var") from None
                        raise
                    return self.json(201, s.get_endpoint(eid))
            if len(parts) >= 2 and parts[0] == "endpoints" and parts[1].isdigit():
                eid = int(parts[1])
                if not s.get_endpoint(eid):
                    raise ApiError(404, "Uç nokta yok")
                if len(parts) == 2 and method == "PUT":
                    s.save_endpoint(panel.endpoint_payload(self.body()), eid)
                    return self.json(200, s.get_endpoint(eid))
                if len(parts) == 2 and method == "DELETE":
                    if any(j["endpoint_id"] == eid for j in s.jobs_with_status("queued", "running")):
                        raise ApiError(409, "Bu uç noktada çalışan iş var")
                    s.delete_endpoint(eid)
                    return self.json(200, {"ok": True})
                if parts[2:] == ["models"] and method == "GET":
                    return self.json(200, s.list_models(eid))
                if parts[2:] == ["models"] and method == "POST":
                    b = self.body()
                    mid = str(b.get("model_id") or "").strip()
                    if not re.match(r"^[\w./:@+-]{1,200}$", mid):
                        raise ApiError(400, "Model id geçersiz (boşluksuz, ör. vendor/model-name:free)")
                    s.add_model(eid, mid, str(b.get("note") or "").strip()[:200] or None)
                    return self.json(201, s.list_models(eid))
                if parts[2:] == ["models", "fetch"] and method == "POST":
                    return self.json(200, panel.fetch_models(eid))
                if len(parts) == 4 and parts[2] == "models" and method == "DELETE":
                    from urllib.parse import unquote
                    s.delete_model(eid, unquote(parts[3]))
                    return self.json(200, s.list_models(eid))
            # jobs
            if parts == ["jobs"]:
                if method == "GET":
                    return self.json(200, [panel.job_view(j) for j in s.list_jobs(int((query.get("limit") or [50])[0]))])
                if method == "POST":
                    jid = r.submit(self.body())
                    return self.json(201, panel.job_view(s.get_job(jid)))
            if len(parts) >= 2 and parts[0] == "jobs" and parts[1].isdigit():
                job = s.get_job(int(parts[1]))
                if not job:
                    raise ApiError(404, "İş yok")
                if len(parts) == 2 and method == "GET":
                    return self.json(200, panel.job_view(job, with_log=True))
                if parts[2:] == ["cancel"] and method == "POST":
                    if not r.cancel(job["id"]):
                        raise ApiError(409, "İş zaten bitmiş")
                    return self.json(200, {"ok": True})
            # reports
            if parts == ["reports"]:
                return self.json(200, panel.reports())
            if len(parts) == 2 and parts[0] == "reports":
                p = panel.report_file(parts[1])
                if "raw" in query:
                    return self.send(200, p.read_bytes(), _TYPES.get(p.suffix, "text/plain"),
                                     {"Content-Disposition": f'attachment; filename="{p.name}"'})
                if p.suffix != ".md":
                    raise ApiError(400, "Görüntüleme yalnız .md için")
                return self.json(200, {"name": p.name, "html": md.render(p.read_text(encoding="utf-8"))})
            raise ApiError(404, "Bilinmeyen API yolu")

    return Handler


def serve(project_dir: Path, db_path: str, out_dir: str, data_dir: Path, host: str = "127.0.0.1",
          port: int = 8765, proxy_dashboard: str | None = None, max_parallel: int = 2,
          public_hosts: list[str] | None = None) -> None:
    panel = Panel(project_dir, db_path, out_dir, data_dir, proxy_dashboard, max_parallel, public_hosts)
    httpd = ThreadingHTTPServer((host, port), make_handler(panel))
    httpd.daemon_threads = True
    log.info("Review Miner panel on http://%s:%d (data: %s)", host, port, data_dir)
    if not panel.store.has_password():
        log.warning("No panel password yet: run 'python -m play_review_miner panel-password'")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        panel.runner.shutdown()
        httpd.server_close()
