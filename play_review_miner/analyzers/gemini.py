"""Gemini analyzer (used automatically when GEMINI_API_KEY is set).

Env vars:
    GEMINI_API_KEY      required
    GEMINI_MODEL        required: any generateContent model your key can use (a cheap "flash" model is enough)
    GEMINI_BATCH_SIZE   reviews per request, default 40
    GEMINI_CONCURRENCY  parallel requests, default 1
    GEMINI_API_BASE     default: https://generativelanguage.googleapis.com/v1beta

The analysis flow (batching, retries, theme consolidation) lives in `llm.py`.
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
import urllib.error
import urllib.request

from .llm import LLMAnalyzer, LLMError, LLMTruncated, normalize_theme_id
from .openai_compat import USER_AGENT

log = logging.getLogger(__name__)

# Backwards-compatible names
GeminiError = LLMError
GeminiTruncated = LLMTruncated
__all__ = ["GeminiAnalyzer", "GeminiError", "GeminiTruncated", "normalize_theme_id", "to_gemini_schema"]


def to_gemini_schema(schema: dict) -> dict:
    """Plain JSON Schema -> Gemini responseSchema (OpenAPI subset: upper-case types, no additionalProperties)."""
    out: dict = {}
    for k, v in schema.items():
        if k == "type":
            out[k] = str(v).upper()
        elif k == "properties":
            out[k] = {name: to_gemini_schema(sub) for name, sub in v.items()}
        elif k == "items":
            out[k] = to_gemini_schema(v)
        elif k in ("required", "enum", "description"):
            out[k] = v
    return out


class GeminiAnalyzer(LLMAnalyzer):
    name = "gemini"

    def __init__(self) -> None:
        super().__init__()
        self.api_key = os.environ.get("GEMINI_API_KEY")
        if not self.api_key:
            raise LLMError("GEMINI_API_KEY is not set")
        self.model = os.environ.get("GEMINI_MODEL") or ""
        if not self.model:
            raise LLMError("GEMINI_MODEL is not set (any generateContent model available to your key)")
        self.base = os.environ.get("GEMINI_API_BASE", "https://generativelanguage.googleapis.com/v1beta")
        self.batch_size = max(1, int(os.environ.get("GEMINI_BATCH_SIZE", "40")))
        self.concurrency = max(1, int(os.environ.get("GEMINI_CONCURRENCY", "1")))

    def _generate(self, prompt: str, item_schema: dict, retries: int = 5) -> list:
        url = f"{self.base}/models/{self.model}:generateContent"
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json",
                                 "responseSchema": to_gemini_schema({"type": "array", "items": item_schema})},
        }
        data = json.dumps(body).encode()
        for attempt in range(retries + 1):
            req = urllib.request.Request(url, data=data, method="POST", headers={
                "Content-Type": "application/json", "x-goog-api-key": self.api_key, "User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(req, timeout=180) as resp:
                    payload = json.loads(resp.read())
                cand = payload["candidates"][0]
                if cand.get("finishReason") == "MAX_TOKENS":
                    raise LLMTruncated("answer truncated at the output token limit")
                text = "".join(p.get("text", "") for p in cand["content"]["parts"])
                result = json.loads(text)
                if not isinstance(result, list):
                    raise ValueError("answer is not a JSON list")
                return result
            except LLMTruncated:
                raise
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode(errors="replace")[:500]
                if exc.code in (429, 500, 502, 503, 504) and attempt < retries:
                    wait = 5 * (2 ** attempt) + random.random()
                    log.warning("Gemini HTTP %s, retry in %.0fs", exc.code, wait)
                    time.sleep(wait)
                    continue
                raise LLMError(f"Gemini HTTP {exc.code}: {detail}") from exc
            except (KeyError, IndexError, ValueError, urllib.error.URLError, TimeoutError) as exc:
                if attempt < retries:
                    time.sleep(3 * (attempt + 1))
                    continue
                raise LLMError(f"Gemini bad response: {exc}") from exc
        raise LLMError("unreachable")
