"""Provider error bodies must never leak credentials into logs/panel output.

Only synthetic keys are used here; no real key and no network.
"""
from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from email.message import Message

import pytest

from play_review_miner.analyzers.gemini import GeminiAnalyzer
from play_review_miner.analyzers.llm import LLMError
from play_review_miner.analyzers.openai_compat import OpenAICompatAnalyzer
from play_review_miner.redact import redact

SYNTHETIC = "sk-synthetic-test-key-0001"


@pytest.mark.parametrize("text", [
    f"Bearer {SYNTHETIC}",
    f"key={SYNTHETIC}",
    f"https://example.test/v1?key={SYNTHETIC}&x=1",
    f'{{"api_key": "{SYNTHETIC}"}}',
    f"api_key={SYNTHETIC}",
    f"x-goog-api-key: {SYNTHETIC}",
    f"Authorization: Bearer {SYNTHETIC}",
])
def test_redact_masks_credential_patterns(text):
    out = redact(text)
    assert SYNTHETIC not in out
    assert "***" in out


def test_redact_keeps_ordinary_text():
    assert redact("HTTP 500 from provider: slow down") == "HTTP 500 from provider: slow down"
    assert redact("") == ""
    assert redact(None) is None


def _http_error(code: int, body: str) -> urllib.error.HTTPError:
    headers = Message()
    return urllib.error.HTTPError("https://provider.test/v1", code, "err", headers,
                                  io.BytesIO(body.encode()))


def test_openai_error_body_is_redacted(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", SYNTHETIC)
    body = json.dumps({"error": f"bad credential Bearer {SYNTHETIC} key={SYNTHETIC} "
                                f"\"api_key\": \"{SYNTHETIC}\""})

    def fake_urlopen(req, timeout=None):
        raise _http_error(401, body)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    analyzer = OpenAICompatAnalyzer(model="acme/test")
    with pytest.raises(LLMError) as exc:
        analyzer._generate("prompt", {"type": "object"})
    assert SYNTHETIC not in str(exc.value)
    assert "***" in str(exc.value)


def test_gemini_error_body_is_redacted(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", SYNTHETIC)
    monkeypatch.setenv("GEMINI_MODEL", "test-model")
    body = f"invalid request key={SYNTHETIC} Bearer {SYNTHETIC}"

    def fake_urlopen(req, timeout=None):
        raise _http_error(400, body)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    analyzer = GeminiAnalyzer()
    with pytest.raises(LLMError) as exc:
        analyzer._generate("prompt", {"type": "object"})
    assert SYNTHETIC not in str(exc.value)
    assert "***" in str(exc.value)


def test_panel_job_view_redacts_log_and_error(tmp_path):
    from play_review_miner.panel.server import Panel

    (tmp_path / "data").mkdir()
    (tmp_path / "reports").mkdir()
    panel = Panel(tmp_path, "data/reviews.db", "reports", tmp_path / "data")
    try:
        log_path = tmp_path / "job.log"
        log_path.write_text(f"failed Bearer {SYNTHETIC}\n", encoding="utf-8")
        job = {"id": 1, "pid": 123, "log_path": str(log_path),
               "error": f"provider said key={SYNTHETIC}", "params": {}, "reports": []}
        view = panel.job_view(job, with_log=True)
        assert SYNTHETIC not in view["error"]
        assert SYNTHETIC not in view["log"]
        assert "***" in view["error"] and "***" in view["log"]
    finally:
        panel.runner.shutdown()
        panel.store.close()
