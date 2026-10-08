"""Analyzer for any OpenAI-compatible chat completions endpoint (OpenRouter, an LLM gateway/proxy,
a local vLLM/Ollama/LM Studio server...).

Env vars (CLI flags --llm-base-url / --llm-model override the first two):
    LLM_BASE_URL     default: https://openrouter.ai/api/v1
    LLM_MODEL        required (or --llm-model), e.g. a model id listed by `list-models`
    LLM_API_KEY      bearer token; optional for endpoints without auth. Never pass it on the command line.
    LLM_BATCH_SIZE   reviews per request, default 20
    LLM_CONCURRENCY  parallel requests, default 3
    LLM_TIMEOUT      seconds per request, default 300
    LLM_MAX_TOKENS   answer token limit, default 16384
    LLM_JSON_MODE    auto (default) | json_schema | json_object | prompt
    LLM_SUMMARY_BATCH apps per "ne işe yarar" request, default 5 (big prompts can hit gateway timeouts)
    LLM_MAX_REQUESTS hard cap on HTTP requests per run, retries included (default: no cap). For quota'd
                     free tiers (e.g. 1000 requests/day): the run stops early, results so far are kept.

JSON mode: `auto` starts with `response_format: json_schema` and, when the endpoint rejects it with
HTTP 400 (some models answer "does not support JSON schema output mode"), falls back to `json_object`, then
to plain instructions in the prompt. The answer is always validated by the analyzer itself.

Results are stored under the analyzer name `llm-<model slug>`, so switching models never mixes
their analyses or theme labels.
"""

from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
import urllib.error
import urllib.request

from .. import __version__
from .llm import LLMAnalyzer, LLMError, LLMTruncated, describe_schema, parse_json_items, slug

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL: str | None = None  # no default: the model must be chosen explicitly
JSON_MODES = ("json_schema", "json_object", "prompt")
# 52x: Cloudflare in front of a provider (524 = origin took longer than ~100 s on large prompts)
# Some Cloudflare-fronted endpoints block urllib's default "Python-urllib/x.y" agent
# with "error code: 1010", so every request identifies the tool explicitly.
USER_AGENT = f"play-review-miner/{__version__} (+https://github.com/ZoriaSoft/play-review-miner-oss)"
RETRY_STATUS = (408, 409, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 529)


def analyzer_name(model: str) -> str:
    return "llm-" + slug(model)


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        raise LLMError(f"{name} must be an integer") from None


class OpenAICompatAnalyzer(LLMAnalyzer):
    def __init__(self, base_url: str | None = None, model: str | None = None) -> None:
        super().__init__()
        self.base_url = (base_url or os.environ.get("LLM_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.model = model or os.environ.get("LLM_MODEL") or DEFAULT_MODEL
        if not self.model:
            raise LLMError("No model chosen: pass --llm-model or set LLM_MODEL (see 'list-models')")
        self.api_key = os.environ.get("LLM_API_KEY") or ""
        self.name = analyzer_name(self.model)
        self.batch_size = _env_int("LLM_BATCH_SIZE", 20)
        self.concurrency = _env_int("LLM_CONCURRENCY", 3)
        self.timeout = _env_int("LLM_TIMEOUT", 300)
        self.max_tokens = _env_int("LLM_MAX_TOKENS", 16384)
        self.summary_batch = _env_int("LLM_SUMMARY_BATCH", 5)
        self.summary_chars = 800
        mode = (os.environ.get("LLM_JSON_MODE") or "auto").strip().lower()
        if mode not in ("auto",) + JSON_MODES:
            raise LLMError(f"LLM_JSON_MODE must be auto, {', '.join(JSON_MODES)}")
        self._auto = mode == "auto"
        self.json_mode = "json_schema" if self._auto else mode
        self._mode_lock = threading.Lock()
        cap = os.environ.get("LLM_MAX_REQUESTS")
        try:
            self.max_requests = int(cap) if cap else None
        except ValueError:
            raise LLMError("LLM_MAX_REQUESTS must be an integer") from None
        self.requests = 0  # HTTP requests sent by this analyzer (all attempts)
        self._count_lock = threading.Lock()

    # ---- request building ----------------------------------------------------------------
    def _body(self, prompt: str, item_schema: dict, mode: str) -> dict:
        wrapped = {"type": "object", "properties": {"items": {"type": "array", "items": item_schema}},
                   "required": ["items"]}
        if mode == "json_schema":
            # some providers map json_schema onto json_object, which requires the word "JSON" in the
            # messages (DeepSeek: "'messages' must contain the word 'json'"); say it explicitly
            prompt += '\n\nAnswer in JSON: {"items": [ ... ]}.'
        else:
            prompt += ('\n\nAnswer with ONLY a JSON object of the form {"items": [ ... ]}, no prose and no code '
                       "fences. Each item: " + describe_schema(item_schema))
        body = {"model": self.model, "temperature": 0.1, "max_tokens": self.max_tokens,
                "messages": [{"role": "user", "content": prompt}]}
        if mode == "json_schema":
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": "items", "schema": wrapped}}
        elif mode == "json_object":
            body["response_format"] = {"type": "json_object"}
        return body

    def _downgrade(self, failed_mode: str, detail: str) -> bool:
        """Switch to the next JSON mode after a 400 that rejects the current one. Thread-safe."""
        if not self._auto:
            return False
        low = detail.lower()
        if not any(w in low for w in ("schema", "response_format", "json", "structured", "format")):
            return False
        with self._mode_lock:
            if self.json_mode != failed_mode:  # another thread already downgraded
                return True
            idx = JSON_MODES.index(failed_mode)
            if idx + 1 >= len(JSON_MODES):
                return False
            self.json_mode = JSON_MODES[idx + 1]
            log.warning("%s rejected %s (%s); using %s from now on", self.model, failed_mode,
                        detail[:120], self.json_mode)
            return True

    def summarize_apps(self, apps: list[dict], batch_size: int | None = None) -> list[dict]:
        return super().summarize_apps(apps, batch_size or self.summary_batch)

    # ---- HTTP ----------------------------------------------------------------------------
    def _post(self, body: dict) -> dict:
        with self._count_lock:
            if self.max_requests is not None and self.requests >= self.max_requests:
                raise LLMBudgetExceeded(f"request budget reached (LLM_MAX_REQUESTS={self.max_requests})")
            self.requests += 1
        headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(f"{self.base_url}/chat/completions", data=json.dumps(body).encode(),
                                     method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read())

    def _generate(self, prompt: str, item_schema: dict, retries: int = 6) -> list:
        attempt = parse_failures = 0
        while True:
            mode = self.json_mode
            try:
                payload = self._post(self._body(prompt, item_schema, mode))
                choice = (payload.get("choices") or [None])[0]
                if not isinstance(choice, dict):
                    raise ValueError(f"no choices in answer: {str(payload)[:200]}")
                if choice.get("finish_reason") == "length":
                    raise LLMTruncated("answer truncated at the output token limit")
                content = (choice.get("message") or {}).get("content") or ""
                return parse_json_items(content)
            except LLMTruncated:
                raise
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode(errors="replace")[:500]
                if exc.code == 400 and self._downgrade(mode, detail):
                    continue  # same request in the next JSON mode; not counted as a retry
                if exc.code in (401, 403):
                    raise LLMError(f"HTTP {exc.code} from {self.base_url}: check LLM_API_KEY ({detail[:200]})") from exc
                if exc.code == 404:
                    raise LLMError(f"HTTP 404 for model {self.model!r} at {self.base_url}: not registered? "
                                   f"Try 'list-models'. ({detail[:200]})") from exc
                if exc.code in RETRY_STATUS and attempt < retries:
                    wait = _retry_after(exc) or min(120.0, 5 * (2 ** attempt)) + random.random()
                    log.warning("%s HTTP %s, retry %d/%d in %.0fs", self.model, exc.code, attempt + 1, retries, wait)
                    attempt += 1
                    time.sleep(wait)
                    continue
                raise LLMError(f"HTTP {exc.code} from {self.base_url}: {detail}") from exc
            except ValueError as exc:  # unparseable / non-list answer: the model may do better next time
                parse_failures += 1
                if parse_failures <= 2:
                    log.warning("%s gave an unusable answer (%s); asking again", self.model, exc)
                    continue
                raise LLMError(f"{self.model} keeps returning unusable JSON: {exc}") from exc
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                if attempt < retries:
                    attempt += 1
                    log.warning("%s request failed (%s), retry %d/%d", self.model, exc, attempt, retries)
                    time.sleep(min(60.0, 3 * attempt))
                    continue
                raise LLMError(f"cannot reach {self.base_url}: {exc}") from exc


class LLMBudgetExceeded(LLMError):
    """LLM_MAX_REQUESTS reached; never retried."""


def _retry_after(exc: urllib.error.HTTPError) -> float | None:
    value = exc.headers.get("Retry-After") if exc.headers else None
    try:
        return min(300.0, max(0.0, float(value))) if value is not None else None
    except ValueError:
        return None  # HTTP-date form: fall back to exponential backoff


def list_models(base_url: str | None = None, timeout: int = 30, api_key: str | None = None) -> list[str]:
    """Model ids from GET {base_url}/models (OpenAI format). Key: `api_key`, else LLM_API_KEY."""
    base = (base_url or os.environ.get("LLM_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    headers = {"User-Agent": USER_AGENT}
    key = api_key if api_key is not None else os.environ.get("LLM_API_KEY")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        with urllib.request.urlopen(urllib.request.Request(f"{base}/models", headers=headers), timeout=timeout) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as exc:
        raise LLMError(f"HTTP {exc.code} from {base}/models: {exc.read().decode(errors='replace')[:200]}") from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise LLMError(f"cannot list models at {base}: {exc}") from exc
    return [m["id"] for m in data.get("data", []) if isinstance(m, dict) and m.get("id")]
