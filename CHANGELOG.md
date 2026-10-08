# Changelog

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
