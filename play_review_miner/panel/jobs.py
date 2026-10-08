"""Job validation, command building and the background runner of the panel.

Every job is a subprocess `python -m play_review_miner <cmd> ...` built from a whitelisted, typed
parameter set (no shell). The API key of the chosen endpoint is passed only through the child's
environment (LLM_API_KEY). Daily request limits are enforced before start: the job's
LLM_MAX_REQUESTS is the smaller of the user's cap and what is left today (minus caps reserved by
queued/running jobs on the same endpoint).
"""

from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import date
from pathlib import Path

from ..crawler import CATEGORIES
from .store import Store, now_iso

log = logging.getLogger(__name__)

KINDS = {"run": "Tara + analiz + rapor", "crawl": "Sadece tara", "analyze": "Analiz + rapor (taramadan)",
         "report": "Sadece rapor"}
_LANG = re.compile(r"^[a-z]{2,3}$")
_COUNTRY = re.compile(r"^[a-z]{2}$")
_LIST = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_MODEL = re.compile(r"^[\w./:@+-]{1,200}$")
_TERM = re.compile(r"^[^\x00-\x1f]{1,60}$")


class JobError(ValueError):
    """Invalid job parameters or a limit that prevents starting (shown to the user)."""


def _int(p: dict, key: str, lo: int, hi: int, default: int | None) -> int | None:
    v = p.get(key, default)
    if v in (None, ""):
        return default
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise JobError(f"{key}: sayı olmalı") from None
    if not lo <= n <= hi:
        raise JobError(f"{key}: {lo}-{hi} arasında olmalı")
    return n


def _float(p: dict, key: str, lo: float, hi: float, default: float) -> float:
    try:
        n = float(p.get(key, default))
    except (TypeError, ValueError):
        raise JobError(f"{key}: sayı olmalı") from None
    if not lo <= n <= hi:
        raise JobError(f"{key}: {lo}-{hi} arasında olmalı")
    return n


def _strings(p: dict, key: str, rx: re.Pattern, limit: int = 20) -> list[str]:
    raw = p.get(key) or []
    if isinstance(raw, str):
        raw = [x for x in (s.strip() for s in raw.split(",")) if x]
    if not isinstance(raw, list) or len(raw) > limit:
        raise JobError(f"{key}: en fazla {limit} değer")
    out = []
    for x in raw:
        x = str(x).strip()
        if not rx.match(x):
            raise JobError(f"{key}: geçersiz değer {x[:30]!r}")
        out.append(x)
    return out


def validate(p: dict) -> dict:
    """Return a clean parameter dict or raise JobError."""
    kind = p.get("kind", "run")
    if kind not in KINDS:
        raise JobError("kind: run, crawl, analyze veya report")
    category = str(p.get("category", "PRODUCTIVITY")).upper()
    if category not in CATEGORIES:
        raise JobError("category: bilinmeyen kategori")
    lang, country = str(p.get("lang", "tr")).lower(), str(p.get("country", "tr")).lower()
    if not _LANG.match(lang) or not _COUNTRY.match(country):
        raise JobError("lang/country: ör. tr / tr")
    list_name = str(p.get("list_name") or "top")
    if not _LIST.match(list_name):
        raise JobError("list_name: harf, rakam, - ve _")
    since = (p.get("since") or "").strip() or None
    if since:
        try:
            since = date.fromisoformat(since).isoformat()
        except ValueError:
            raise JobError("since: YYYY-AA-GG") from None
    analyzer = p.get("analyzer", "keyword")
    if analyzer not in ("keyword", "llm"):
        raise JobError("analyzer: keyword veya llm")
    clean = {
        "kind": kind, "category": category, "lang": lang, "country": country, "list_name": list_name,
        "since": since, "analyzer": analyzer,
        "top": _int(p, "top", 1, 500, 20),
        "max_stars": _int(p, "max_stars", 1, 5, 2),
        "reviews_per_app": _int(p, "reviews_per_app", 1, 5000, 200),
        "delay": _float(p, "delay", 0.2, 10, 1.0),
        "min_reviews": _int(p, "min_reviews", 0, 1000, 1),
        "candidates": _int(p, "candidates", 1, 2000, None),
        "exclude_big": bool(p.get("exclude_big")), "strict_genre": bool(p.get("strict_genre")),
        "refresh_apps": bool(p.get("refresh_apps")), "reanalyze": bool(p.get("reanalyze")),
        "search_terms": _strings(p, "search_terms", _TERM),
        "exclude_devs": _strings(p, "exclude_devs", _TERM),
        "themes": _int(p, "themes", 1, 50, 15),
        "quotes": _int(p, "quotes", 0, 10, 3),
    }
    if analyzer == "llm" and kind != "crawl":
        clean["endpoint_id"] = _int(p, "endpoint_id", 1, 10**9, None)
        model = str(p.get("model") or "").strip()
        if not clean["endpoint_id"] or not _MODEL.match(model):
            raise JobError("LLM için uç nokta ve geçerli bir model seçin")
        clean["model"] = model
        clean["batch_size"] = _int(p, "batch_size", 1, 100, 20)
        clean["concurrency"] = _int(p, "concurrency", 1, 8, 3)
        clean["request_cap"] = _int(p, "request_cap", 1, 100_000, None)
    return clean


def build_argv(p: dict, db_path: str, out_dir: str, base_url: str | None = None) -> list[str]:
    cmd = {"run": ["run"], "crawl": ["crawl"], "analyze": ["run", "--skip-crawl"], "report": ["report"]}[p["kind"]]
    argv = [sys.executable, "-m", "play_review_miner", *cmd, "--db", db_path, "--out", out_dir,
            "-c", p["category"], "-l", p["lang"], "-g", p["country"], "-n", str(p["top"]),
            "--max-stars", str(p["max_stars"]), "--list", p["list_name"]]
    if p["since"]:
        argv += ["--since", p["since"]]
    if p["kind"] in ("run", "crawl"):
        argv += ["-r", str(p["reviews_per_app"]), "--delay", str(p["delay"]), "--min-reviews", str(p["min_reviews"])]
        if p["candidates"]:
            argv += ["--candidates", str(p["candidates"])]
        for flag in ("exclude_big", "strict_genre", "refresh_apps"):
            if p[flag]:
                argv.append("--" + flag.replace("_", "-"))
        for t in p["search_terms"]:
            argv += ["--search-term", t]
        for d in p["exclude_devs"]:
            argv += ["--exclude-dev", d]
    if p["kind"] != "crawl":
        if p["analyzer"] == "llm":
            argv += ["--analyzer", "openai", "--llm-model", p["model"]]
            if base_url:
                argv += ["--llm-base-url", base_url]
        else:
            argv += ["--analyzer", "keyword"]
        if p["kind"] in ("run", "analyze") and p["reanalyze"]:
            argv.append("--reanalyze")
        argv += ["--themes", str(p["themes"]), "--quotes", str(p["quotes"])]
    return argv


# ---- log parsing -------------------------------------------------------------------------------
_CRAWL_RE = re.compile(r"\[\s*(\d+)/(\d+)\]")
_ANALYZE_RE = re.compile(r"reviews \d+-(\d+) / (\d+)\)")
_REQ_RE = re.compile(r": (\d+) HTTP requests this run")
_REPORT_RE = re.compile(r"(?:Report|JSON):\s+(\S+)")


def read_tail(path: str | None, max_bytes: int = 64_000) -> str:
    if not path or not os.path.exists(path):
        return ""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        return f.read().decode("utf-8", errors="replace")


def progress(text: str) -> dict:
    stage = "başlıyor"
    if "candidate apps" in text or "Category page" in text:
        stage = "taranıyor"
    if "Analyzer=" in text:
        stage = "analiz"
    if "Writing Turkish app summaries" in text:
        stage = "uygulama özetleri"
    if "Report:" in text:
        stage = "rapor"
    out: dict = {"stage": stage}
    if m := _CRAWL_RE.findall(text):
        out["crawl"] = [int(m[-1][0]), int(m[-1][1])]
    if m := _ANALYZE_RE.findall(text):
        out["analyze"] = [int(m[-1][0]), int(m[-1][1])]
    if m := _REQ_RE.findall(text):
        out["requests"] = int(m[-1])
    return out


def _result(text: str, out_dir: Path) -> tuple[int | None, list[str], str | None]:
    reqs = [int(x) for x in _REQ_RE.findall(text)]
    reports = []
    for path in _REPORT_RE.findall(text):
        name = Path(path).name
        if (out_dir / name).exists() and name not in reports:
            reports.append(name)
    errors = [ln.split(" ERROR ", 1)[1] for ln in text.splitlines() if " ERROR " in ln]
    if not errors:
        errors = [ln for ln in text.splitlines() if ln.startswith(("Traceback", "play-review-miner: error"))]
    return (sum(reqs) if reqs else None), reports, (errors[-1][:500] if errors else None)


# ---- runner ------------------------------------------------------------------------------------
class Runner:
    def __init__(self, store: Store, project_dir: Path, db_path: str, out_dir: str, jobs_dir: Path,
                 max_parallel: int = 2) -> None:
        self.store, self.project_dir = store, project_dir
        self.db_path, self.out_dir, self.jobs_dir = db_path, out_dir, jobs_dir
        self.max_parallel = max_parallel
        self._procs: dict[int, subprocess.Popen] = {}
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = False
        jobs_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(jobs_dir, 0o700)
        self._recover()
        self._thread = threading.Thread(target=self._loop, name="job-scheduler", daemon=True)
        self._thread.start()

    # -- submission
    def submit(self, raw: dict) -> int:
        p = validate(raw)
        cap = p.get("request_cap")
        if p.get("endpoint_id"):
            endpoint = self.store.get_endpoint(p["endpoint_id"], with_key=True)
            if not endpoint:
                raise JobError("uç nokta bulunamadı")
            if endpoint.get("daily_limit") and p["kind"] != "report":  # a report sends no requests
                left = (endpoint["daily_limit"] - self.store.requests_today(endpoint["id"])
                        - self.store.reserved_requests(endpoint["id"]))
                if left <= 0:
                    raise JobError(f"{endpoint['name']}: bugünkü istek kotası dolu "
                                   f"({endpoint['daily_limit']}/gün, çalışan işlerin ayırdığı dahil)")
                cap = min(cap or left, left)
        job_id = self.store.create_job(p["kind"], p, p.get("endpoint_id"), p.get("model"), cap)
        self._wake.set()
        return job_id

    def cancel(self, job_id: int) -> bool:
        job = self.store.get_job(job_id)
        if not job or job["status"] not in ("queued", "running"):
            return False
        if job["status"] == "queued":
            self.store.update_job(job_id, status="cancelled", finished_at=now_iso())
            return True
        with self._lock:
            proc = self._procs.get(job_id)
        pid = proc.pid if proc else job.get("pid")
        if pid:
            try:
                os.killpg(pid, signal.SIGINT)  # the CLI saves what it has and exits with 130
            except ProcessLookupError:
                pass
            threading.Timer(45, self._force_kill, args=(pid,)).start()
        return True

    @staticmethod
    def _force_kill(pid: int) -> None:
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    def shutdown(self) -> None:
        self._stop = True
        self._wake.set()

    # -- scheduling
    def _loop(self) -> None:
        while not self._stop:
            try:
                self._schedule()
            except Exception:  # never let the scheduler die
                log.exception("scheduler error")
            self._wake.wait(2)
            self._wake.clear()

    @staticmethod
    def _key(p: dict) -> tuple:
        return (p["category"], p["lang"], p["country"], p["list_name"])

    def _schedule(self) -> None:
        running = self.store.jobs_with_status("running")
        busy = {self._key(j["params"]) for j in running}
        free = self.max_parallel - len(running)
        for job in self.store.jobs_with_status("queued"):
            if free <= 0:
                break
            if self._key(job["params"]) in busy:
                continue  # same category/locale/list: wait, both would write the same ranking
            self._start(job)
            busy.add(self._key(job["params"]))
            free -= 1

    def _start(self, job: dict) -> None:
        p = job["params"]
        endpoint = self.store.get_endpoint(job["endpoint_id"], with_key=True) if job["endpoint_id"] else None
        argv = build_argv(p, self.db_path, self.out_dir, endpoint["base_url"] if endpoint else None)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("LLM_", "GEMINI_"))}
        env["PYTHONUNBUFFERED"] = "1"
        # run exactly the package copy the panel itself was loaded from, wherever the cwd is
        pkg_root = str(Path(__file__).resolve().parents[2])
        env["PYTHONPATH"] = pkg_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        if endpoint:
            if endpoint.get("api_key"):
                env["LLM_API_KEY"] = endpoint["api_key"]
            env["LLM_BATCH_SIZE"] = str(p["batch_size"])
            env["LLM_CONCURRENCY"] = str(p["concurrency"])
            if job["request_cap"]:
                env["LLM_MAX_REQUESTS"] = str(job["request_cap"])
        log_path = self.jobs_dir / f"{job['id']}.log"
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as logf:
            logf.write("$ " + " ".join(argv[1:]) + "\n")
            logf.flush()
            proc = subprocess.Popen(argv, cwd=self.project_dir, env=env, stdout=logf, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, start_new_session=True)
        with self._lock:
            self._procs[job["id"]] = proc
        self.store.update_job(job["id"], status="running", started_at=now_iso(), pid=proc.pid, log_path=str(log_path))
        threading.Thread(target=self._wait, args=(job["id"], proc), daemon=True).start()

    def _wait(self, job_id: int, proc: subprocess.Popen) -> None:
        rc = proc.wait()
        with self._lock:
            self._procs.pop(job_id, None)
        self._finish(job_id, rc)

    def _finish(self, job_id: int, rc: int | None) -> None:
        job = self.store.get_job(job_id)
        text = read_tail(job["log_path"], 2_000_000)
        requests, reports, error = _result(text, Path(self.project_dir) / self.out_dir)
        if rc is None:  # re-attached after a panel restart: exit code unknown, infer from the log
            rc = 0 if reports and not error else 1
        status = "done" if rc == 0 else "cancelled" if rc in (130, -2) else "failed"
        self.store.update_job(job_id, status=status, rc=rc, requests=requests, reports=reports,
                              error=None if status == "done" else error, finished_at=now_iso())
        self._wake.set()

    def _recover(self) -> None:
        """Jobs left 'running' by a previous panel process: watch them if still alive, else close them."""
        for job in self.store.jobs_with_status("running"):
            pid = job.get("pid")
            if pid and _alive(pid):
                threading.Thread(target=self._watch_pid, args=(job["id"], pid), daemon=True).start()
            else:
                self._finish(job["id"], None)

    def _watch_pid(self, job_id: int, pid: int) -> None:
        while _alive(pid):
            time.sleep(2)
        self._finish(job_id, None)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:  # a zombie child of ours counts as finished
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split()[2] != "Z"
    except OSError:
        return True
