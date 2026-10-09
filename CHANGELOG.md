# Changelog

## 0.6.2 — 2026-10-09 — panel hardening

Fixes from the pre-release security review:

- **Job pids are now verified by start-time identity** (`jobs.process_start`,
  from `/proc/<pid>/stat`). A stored pid that no longer matches — dead or
  reused by an unrelated process — is never signalled: `cancel` closes the job
  as `failed` ("process no longer running (panel restarted)") and `_recover`
  does the same instead of `killpg`-ing a stranger. Panel DB migrates to
  `user_version=1`.
- **`CF-Connecting-IP` is honored only in tunnel mode** — a `--public-host`
  is configured and the peer is loopback. Otherwise the header is ignored, so
  spoofing it cannot rotate the per-IP login back-off.
- **`Content-Length` is validated**: non-numeric → 400, negative or > 256000 →
  400/413 without reading the body.
- **Sessions and login-failure maps are bounded** — expired entries are swept
  on each login attempt and at most once a minute per request.
- Also included from the unreleased 0.6.1 work: white/black-screen false
  matches tightened in the keyword rules, and a warning when a report is built
  without analysis.

## 0.6.0 — 2026-10-08 — first public release


- Crawler: category page + Play search candidates, niche mode (`--exclude-big` with whole-word developer
  matching, `--strict-genre`, `--min-reviews`, named lists), incremental review collection that can deepen
  later, reviews stored per locale, rankings published atomically (an interrupted crawl keeps the previous list).
- Analyzers: offline Turkish + English keyword rules with TF-IDF clustering; LLM analyzers for any
  OpenAI-compatible endpoint and for Gemini, with stable themes across runs, automatic JSON-mode fallback,
  retries (429/5xx/Cloudflare 52x), batch splitting on truncation, per-run request caps and chunked saving.
- Reports in Markdown + JSON with Turkish "what does this app do" summaries.
- Web panel: run form, job queue with live progress/log/cancel, report viewer, endpoint and model management
  (fetched + manual models), per-endpoint daily request quotas.
- 161 tests (no network), ruff, Python 3.9+.
