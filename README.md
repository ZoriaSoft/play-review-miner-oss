# Play Review Miner

**Find product opportunities in your competitors' 1–2★ Google Play reviews.**

Play Review Miner crawls the top apps of a Google Play category, collects their low-star reviews,
groups them into complaint themes (bugs, missing features, UX, pricing/ads, performance) and writes an
**opportunity report** that answers: *what do users hate most, and in which apps?*

[Türkçe README](README.tr.md)

- **No API key needed for the basic flow** — public Play Store data via
  [`google-play-scraper`](https://github.com/JoMingyu/google-play-scraper), offline keyword analysis.
- **Three analyzers**
  - `keyword` (default, offline): Turkish + English rules, plus TF-IDF clustering of what the rules miss.
  - `openai`: any **OpenAI-compatible** endpoint — OpenRouter, your own LLM gateway, a local
    vLLM / Ollama / LM Studio server. You pick base URL, key and model.
  - `gemini`: Google Gemini REST API.
- **Incremental SQLite storage** — re-runs fetch only new reviews; a later run with a larger sample digs
  deeper; the same language can be crawled for several countries.
- **Niche mode** — skip big companies (`--exclude-big`) and fill the list from Play search to find indie apps.
- **Web panel** — start crawls/analyses, follow progress, read reports, manage endpoints and models,
  per-endpoint daily request quotas.
- Output: `reports/<CATEGORY>_<lang>-<country>[_<list>]_<analyzer>.md` and `.json`.

> Reports and app summaries are written in **Turkish** (the tool was built for the Turkish market).
> Reviews in any language can be analysed; the keyword rules cover Turkish and English.

See [`reports/examples/`](reports/examples/) for a sample report built from **synthetic** apps and reviews.

## Install

```bash
git clone https://github.com/ZoriaSoft/play-review-miner-oss.git play-review-miner
cd play-review-miner
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[cluster]"          # cluster = scikit-learn for TF-IDF clusters (optional)
```

Python 3.9+. No other services required.

## Quick start

```bash
# Turkey / Turkish, PRODUCTIVITY, top 20 apps, 200 low-star reviews per app, offline analysis
play-review-miner run --category PRODUCTIVITY --lang tr --country tr --top 20 --reviews-per-app 200

# US / English
play-review-miner run -c PRODUCTIVITY -l en -g us -n 20
```

The report path is printed at the end. Step by step:

```bash
play-review-miner crawl   -c TOOLS -l tr -g tr -n 30 -r 300   # collect only
play-review-miner analyze -c TOOLS -l tr -g tr -n 30          # classify
play-review-miner report  -c TOOLS -l tr -g tr -n 30 --themes 20 --since 2025-01-01
play-review-miner details -c TOOLS -l tr -g tr -n 30          # refresh store listings only
play-review-miner list-categories
```

(`python -m play_review_miner …` works too.)

### Niche mode (no big companies)

```bash
play-review-miner run -c PRODUCTIVITY -l tr -g tr --list niche \
    --exclude-big --strict-genre --min-reviews 10 --candidates 160 --top 25 -r 200
```

- `--exclude-big` skips Google, Microsoft, Adobe, Samsung, OpenAI, Meta, Amazon, ByteDance, … (`crawler.py` →
  `BIG_DEVELOPERS`, `BIG_APP_PREFIXES`). Developer names match **whole words** ("apple" matches "Apple Inc.",
  not "Pineapple Games"); package prefixes match whole segments. Add more with `--exclude-dev`.
- The category page has ~40–50 apps, so candidates are topped up from Play search (`SEARCH_TERMS`, `--search-term`).
- `--list niche` keeps a separate ranking and report; `--min-reviews` skips apps without enough low-star reviews.

### Main options

| Option | Default | Meaning |
|---|---|---|
| `-c / --category` | `PRODUCTIVITY` | Play category id (`list-categories`) |
| `-l / --lang`, `-g / --country` | `tr`, `tr` | Review language and store country |
| `-n / --top` | `20` | Apps to analyse |
| `-r / --reviews-per-app` | `200` | Reviews per app (split evenly over the star values) |
| `--max-stars` | `2` | Use reviews with this many stars or fewer |
| `--list` | `top` | Ranking name (separate reports per list) |
| `--exclude-big`, `--exclude-dev`, `--strict-genre`, `--min-reviews`, `--candidates`, `--search-term` | | Filters (see above) |
| `--delay` | `1.0` | Seconds between requests |
| `--analyzer` | `auto` | `auto` / `keyword` / `openai` / `gemini` (auto: `GEMINI_API_KEY` → gemini, `LLM_API_KEY` → openai, else keyword) |
| `--llm-base-url`, `--llm-model` | env | Endpoint and model of the `openai` analyzer |
| `--since` | – | Only reviews after this date in analysis/report |
| `--db`, `--out` | `data/reviews.db`, `reports` | Database and output folder |

Exit codes: `0` ok, `1` analyzer error, `2` no data / nothing passed the filters, `130` interrupted.

## LLM analysis

### Any OpenAI-compatible endpoint

```bash
export LLM_API_KEY=...                                   # never put keys on the command line
export LLM_BASE_URL=https://openrouter.ai/api/v1          # default; any OpenAI-compatible /v1 works
play-review-miner list-models --filter deepseek/          # what the endpoint offers
play-review-miner run -c PRODUCTIVITY -l tr -g tr --analyzer openai --llm-model <model-id>
```

| Variable | Default | Meaning |
|---|---|---|
| `LLM_BASE_URL` | `https://openrouter.ai/api/v1` | Base URL (`--llm-base-url` overrides) |
| `LLM_MODEL` | – (required) | Model id (`--llm-model` overrides) |
| `LLM_API_KEY` | – | Bearer token (environment only) |
| `LLM_BATCH_SIZE` / `LLM_CONCURRENCY` | `20` / `3` | Reviews per request / parallel requests |
| `LLM_MAX_REQUESTS` | unlimited | Hard cap on HTTP requests per run (retries included) — for free tiers with daily quotas |
| `LLM_TIMEOUT` / `LLM_MAX_TOKENS` | `300` / `16384` | Per-request timeout (s) / answer token limit |
| `LLM_JSON_MODE` | `auto` | `auto` / `json_schema` / `json_object` / `prompt` |
| `LLM_SUMMARY_BATCH` | `5` | Apps per "what does it do" request |

### Gemini

```bash
export GEMINI_API_KEY=... GEMINI_MODEL=<a generateContent model available to your key>
play-review-miner run -c PRODUCTIVITY -l tr -g tr --analyzer gemini
```

### How LLM analysis works

- Reviews are classified in batches (category + 1–3 theme ids + a one-line Turkish summary). Review text is
  marked as untrusted data in the prompt.
- **Stable themes across runs:** the keyword themes and every theme stored by earlier runs are offered as
  preferred ids; a consolidation step maps *new* ids onto existing ones and writes a Turkish label and an
  "opportunity" sentence. Existing ids never change meaning, so old analyses stay comparable.
- **JSON mode:** `json_schema` first; if the endpoint rejects it (HTTP 400), `json_object`, then plain
  instructions. Answers are always validated (code fences, `<think>` blocks and `{"items": [...]}` wrappers are tolerated).
- **Robustness:** skipped reviews are asked again and, if still missing, *not* stored (the next run retries
  them); truncated answers split the batch; 429/5xx/Cloudflare 52x are retried (`Retry-After` respected);
  results are saved every 400 reviews, so an interruption keeps what was paid for.
- Results are stored per model (`llm-<model-slug>`), so switching models never mixes analyses.

## Web panel

```bash
play-review-miner panel-password          # once: login password (min. 12 characters)
play-review-miner panel --port 8765       # http://127.0.0.1:8765
```

- **Run:** category, locale, sample size, niche filters, analyzer, endpoint + model (pick from the list or type
  one and save it), batch size, parallelism, request cap.
- **Jobs:** queue, live progress (apps crawled, reviews analysed), log, cancel (partial results are kept).
- **Reports:** browse, read in the panel, download `.md` / `.json`.
- **Endpoints:** base URL, key (only shown masked), daily request limit, default model; fetch the model list
  from the endpoint or **add models by hand**. Jobs are refused when today's quota is used up.
- **Security:** binds to `127.0.0.1`. To reach it remotely, put it behind an authenticating reverse proxy or
  tunnel (e.g. Cloudflare Tunnel + Access) and pass `--public-host your.host`. It also has its own login:
  PBKDF2 password, HttpOnly + SameSite=Strict session cookie, `X-Panel` header + Origin check on every write,
  login back-off, strict CSP, jobs built from whitelisted arguments without a shell. Panel state
  (`data/panel.db`, including endpoint keys) is created with mode 600 and is git-ignored.
- The panel UI is in Turkish.

## Data model (SQLite)

| Table | Content |
|---|---|
| `apps`, `app_locale` | Store listing (latest, and per language/country) |
| `app_category` | Rankings per list / category / locale |
| `reviews` | Reviews, keyed by `(review_id, country, lang)` |
| `analysis`, `theme_meta` | Per-analyzer labels and theme metadata |
| `app_summary` | Generated "what does this app do" texts |

The schema version is kept in `PRAGMA user_version`; older databases are migrated on open.

## Responsible use — please read

- This tool reads **public** Play Store pages, as the `google-play-scraper` library does. Automated access may
  conflict with Google Play's Terms of Service; you are responsible for how and how much you use it. Keep
  `--delay` reasonable.
- Reviews are user-generated content. The tool stores **no user names**, but review text may still contain
  personal information. **Do not republish raw reviews** or reports with quotes; use them for your own product research.
- The keyword analyzer is a quick first look (it can't understand irony and may misclassify). Read the example
  quotes, or use an LLM analyzer, before making decisions.
- Play returns a limited, not strictly chronological sample of reviews; results are not a census.

## Development

```bash
pip install -e ".[dev]"
sh scripts/check.sh            # ruff + pytest
```

Tests never touch the network: the Play Store (`tests/conftest.py` → `FakePlay`) and LLM endpoints are faked.
When changing keyword rules, add both expected matches **and known false positives** to `tests/test_keyword.py`.
Schema changes go into `db._migrate()` with a migration test.

## License

[MIT](LICENSE) © 2026 ZoriaSoft
