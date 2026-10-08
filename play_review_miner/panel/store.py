"""Panel state: endpoints (OpenAI-compatible base URL + key + daily request limit), their models
(fetched from /models or added by hand), jobs, and the panel password hash.

Lives in its own SQLite file (default data/panel.db, mode 0600), separate from the review database.
API keys are stored here because the panel must pass them to runs; they are never returned by the
API in full (see `mask`).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS endpoints (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE,
    base_url     TEXT NOT NULL,
    api_key      TEXT,
    daily_limit  INTEGER,            -- requests per UTC day, NULL = unlimited
    default_model TEXT,
    note         TEXT,
    created_at   TEXT
);
CREATE TABLE IF NOT EXISTS endpoint_models (
    endpoint_id  INTEGER NOT NULL REFERENCES endpoints(id) ON DELETE CASCADE,
    model_id     TEXT NOT NULL,
    source       TEXT NOT NULL,      -- 'manual' | 'fetched'
    note         TEXT,
    added_at     TEXT,
    PRIMARY KEY (endpoint_id, model_id)
);
CREATE TABLE IF NOT EXISTS jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,
    params       TEXT NOT NULL,      -- JSON, validated
    endpoint_id  INTEGER,
    model        TEXT,
    status       TEXT NOT NULL,      -- queued | running | done | failed | cancelled
    request_cap  INTEGER,
    requests     INTEGER,
    rc           INTEGER,
    pid          INTEGER,
    log_path     TEXT,
    reports      TEXT,               -- JSON list of report file names
    error        TEXT,
    created_at   TEXT,
    started_at   TEXT,
    finished_at  TEXT
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def today_utc() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def mask(key: str | None) -> str | None:
    if not key:
        return None
    return ("•" * 6) + key[-4:] if len(key) > 8 else "•" * 6


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.path.exists()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._lock = threading.RLock()
        if new:
            os.chmod(self.path, 0o600)
        for suffix in ("-wal", "-shm"):
            p = Path(str(self.path) + suffix)
            if p.exists():
                os.chmod(p, 0o600)

    def close(self) -> None:
        self._conn.close()

    def _q(self, sql: str, params=()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params)]

    def _x(self, sql: str, params=()) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(sql, params)
            return cur.lastrowid

    # ---- password ------------------------------------------------------------------------
    def set_password(self, password: str) -> None:
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 300_000)
        self._x("INSERT INTO settings(key, value) VALUES ('password', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (salt.hex() + ":" + digest.hex(),))

    def has_password(self) -> bool:
        return bool(self._q("SELECT 1 FROM settings WHERE key='password'"))

    def check_password(self, password: str) -> bool:
        row = self._q("SELECT value FROM settings WHERE key='password'")
        if not row:
            return False
        salt_hex, digest_hex = row[0]["value"].split(":")
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 300_000)
        return hmac.compare_digest(digest.hex(), digest_hex)

    # ---- endpoints -----------------------------------------------------------------------
    def _public_endpoint(self, e: dict) -> dict:
        out = {k: v for k, v in e.items() if k != "api_key"}
        out["api_key_masked"] = mask(e.get("api_key"))
        out["has_key"] = bool(e.get("api_key"))
        out["used_today"] = self.requests_today(e["id"])
        out["reserved"] = self.reserved_requests(e["id"])
        return out

    def list_endpoints(self) -> list[dict]:
        return [self._public_endpoint(e) for e in self._q("SELECT * FROM endpoints ORDER BY name")]

    def get_endpoint(self, endpoint_id: int, with_key: bool = False) -> dict | None:
        rows = self._q("SELECT * FROM endpoints WHERE id=?", (endpoint_id,))
        if not rows:
            return None
        return rows[0] if with_key else self._public_endpoint(rows[0])

    def save_endpoint(self, data: dict, endpoint_id: int | None = None) -> int:
        """Create or update. An empty/missing api_key on update keeps the stored key;
        `clear_key: true` removes it."""
        fields = {"name": data["name"], "base_url": data["base_url"], "daily_limit": data.get("daily_limit"),
                  "default_model": data.get("default_model") or None, "note": data.get("note") or None}
        if endpoint_id is None:
            fields["api_key"] = data.get("api_key") or None
            fields["created_at"] = now_iso()
            cols = ", ".join(fields)
            return self._x(f"INSERT INTO endpoints ({cols}) VALUES ({', '.join('?' * len(fields))})",
                           tuple(fields.values()))
        if data.get("clear_key"):
            fields["api_key"] = None
        elif data.get("api_key"):
            fields["api_key"] = data["api_key"]
        sets = ", ".join(f"{k}=?" for k in fields)
        self._x(f"UPDATE endpoints SET {sets} WHERE id=?", (*fields.values(), endpoint_id))
        return endpoint_id

    def delete_endpoint(self, endpoint_id: int) -> None:
        self._x("DELETE FROM endpoints WHERE id=?", (endpoint_id,))

    # ---- models --------------------------------------------------------------------------
    def list_models(self, endpoint_id: int) -> list[dict]:
        return self._q("SELECT model_id, source, note, added_at FROM endpoint_models WHERE endpoint_id=? "
                       "ORDER BY source='fetched', model_id", (endpoint_id,))

    def add_model(self, endpoint_id: int, model_id: str, note: str | None = None) -> None:
        self._x("INSERT INTO endpoint_models (endpoint_id, model_id, source, note, added_at) VALUES (?, ?, 'manual', ?, ?) "
                "ON CONFLICT(endpoint_id, model_id) DO UPDATE SET source='manual', note=excluded.note",
                (endpoint_id, model_id, note, now_iso()))

    def delete_model(self, endpoint_id: int, model_id: str) -> None:
        self._x("DELETE FROM endpoint_models WHERE endpoint_id=? AND model_id=?", (endpoint_id, model_id))

    def replace_fetched_models(self, endpoint_id: int, model_ids: list[str]) -> None:
        """Refresh the fetched list; manual models are kept (and stay manual)."""
        ts = now_iso()
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM endpoint_models WHERE endpoint_id=? AND source='fetched'", (endpoint_id,))
            self._conn.executemany(
                "INSERT OR IGNORE INTO endpoint_models (endpoint_id, model_id, source, added_at) VALUES (?, ?, 'fetched', ?)",
                [(endpoint_id, m, ts) for m in dict.fromkeys(model_ids)])

    # ---- jobs ----------------------------------------------------------------------------
    def create_job(self, kind: str, params: dict, endpoint_id: int | None, model: str | None,
                   request_cap: int | None) -> int:
        return self._x("INSERT INTO jobs (kind, params, endpoint_id, model, status, request_cap, created_at) "
                       "VALUES (?, ?, ?, ?, 'queued', ?, ?)",
                       (kind, json.dumps(params, ensure_ascii=False), endpoint_id, model, request_cap, now_iso()))

    def update_job(self, job_id: int, **fields) -> None:
        if "reports" in fields and not isinstance(fields["reports"], str):
            fields["reports"] = json.dumps(fields["reports"], ensure_ascii=False)
        sets = ", ".join(f"{k}=?" for k in fields)
        self._x(f"UPDATE jobs SET {sets} WHERE id=?", (*fields.values(), job_id))

    def get_job(self, job_id: int) -> dict | None:
        rows = self._q("SELECT * FROM jobs WHERE id=?", (job_id,))
        return _job(rows[0]) if rows else None

    def list_jobs(self, limit: int = 50) -> list[dict]:
        return [_job(r) for r in self._q("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,))]

    def jobs_with_status(self, *statuses: str) -> list[dict]:
        q = ",".join("?" * len(statuses))
        return [_job(r) for r in self._q(f"SELECT * FROM jobs WHERE status IN ({q}) ORDER BY id", statuses)]

    def requests_today(self, endpoint_id: int) -> int:
        """Requests recorded by finished jobs of this endpoint that started today (UTC)."""
        row = self._q("SELECT COALESCE(SUM(requests), 0) n FROM jobs WHERE endpoint_id=? AND substr(started_at,1,10)=?",
                      (endpoint_id, today_utc()))
        return int(row[0]["n"])

    def reserved_requests(self, endpoint_id: int) -> int:
        """Caps held by queued/running jobs (their final count is not known yet)."""
        row = self._q("SELECT COALESCE(SUM(request_cap), 0) n FROM jobs WHERE endpoint_id=? "
                      "AND status IN ('queued', 'running')", (endpoint_id,))
        return int(row[0]["n"])


def _job(r: dict) -> dict:
    r = dict(r)
    r["params"] = json.loads(r["params"] or "{}")
    r["reports"] = json.loads(r["reports"]) if r.get("reports") else []
    return r
