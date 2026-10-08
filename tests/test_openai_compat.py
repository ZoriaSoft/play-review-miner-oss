"""OpenAI-compatible analyzer against a scripted fake endpoint (no network)."""
from __future__ import annotations

import io
import json
import re
import urllib.error

import pytest
from conftest import FakePlay, make_review

from play_review_miner import cli
from play_review_miner.analyzers import llm, openai_compat, storage_name
from play_review_miner.analyzers.llm import LLMError, parse_json_items
from play_review_miner.analyzers.openai_compat import OpenAICompatAnalyzer

SECRET = "sk-test-very-secret-123"


class FakeEndpoint:
    """Answers chat completions by classifying every '[i]' review line; records each request body."""

    def __init__(self, reject_modes=(), script=None, models=("acme/fast-model", "acme/big-model")):
        self.reject_modes = set(reject_modes)   # response_format types answered with HTTP 400
        self.script = list(script or [])        # queued special answers: ("http", code, headers, body) | ("text", s)
        self.models = models
        self.bodies: list[dict] = []

    def _mode(self, body):
        return (body.get("response_format") or {}).get("type", "prompt")

    def urlopen(self, req, timeout=None):
        self.agents = getattr(self, "agents", []) + [req.get_header("User-agent")]
        if req.full_url.endswith("/models"):
            return _Resp({"data": [{"id": m} for m in self.models]})
        body = json.loads(req.data)
        self.bodies.append(body)
        mode = self._mode(body)
        if mode in self.reject_modes:
            raise _http(400, {"error": {"message": f"Model X does not support {mode.replace('_', ' ')} output mode"}})
        if self.script:
            kind, *rest = self.script.pop(0)
            if kind == "http":
                code, headers, payload = rest
                raise _http(code, payload, headers)
            if kind == "text":
                return _Resp({"choices": [{"finish_reason": "stop", "message": {"content": rest[0]}}]})
            if kind == "length":
                return _Resp({"choices": [{"finish_reason": "length", "message": {"content": "{\"items\": ["}}]})
        prompt = body["messages"][0]["content"]
        block = prompt.rsplit("<reviews>", 1)[1] if "<reviews>" in prompt else ""
        idx = [int(m) for m in re.findall(r"^\[(\d+)\]", block, re.M)]
        if idx:
            items = [{"i": i, "category": "bug", "themes": ["crash_wont_open"], "summary": "Uygulama çöküyor.",
                      "low_signal": False} for i in idx]
        elif "app_id:" in prompt:
            items = [{"app_id": a, "summary_tr": f"{a} not tutar."} for a in re.findall(r"app_id: (\S+)", prompt)]
        else:
            items = []
        text = json.dumps({"items": items}, ensure_ascii=False)
        if mode == "prompt":
            text = "```json\n" + text + "\n```"
        return _Resp({"choices": [{"finish_reason": "stop", "message": {"content": text}}],
                      "usage": {"prompt_tokens": 10, "completion_tokens": 5}})


class _Resp:
    def __init__(self, payload):
        self._data = json.dumps(payload).encode()

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http(code, payload, headers=None):
    from email.message import Message
    msg = Message()
    for k, v in (headers or {}).items():
        msg[k] = v
    return urllib.error.HTTPError("http://x/v1/chat/completions", code, "err", msg,
                                  io.BytesIO(json.dumps(payload).encode()))


@pytest.fixture
def endpoint(monkeypatch):
    def install(**kw) -> FakeEndpoint:
        ep = FakeEndpoint(**kw)
        monkeypatch.setattr(openai_compat.urllib.request, "urlopen", ep.urlopen)
        return ep
    monkeypatch.setenv("LLM_API_KEY", SECRET)
    monkeypatch.setenv("LLM_MODEL", "acme/fast-model")
    for var in ("LLM_BASE_URL", "LLM_JSON_MODE", "LLM_BATCH_SIZE", "LLM_CONCURRENCY", "GEMINI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    sleeps: list[float] = []
    monkeypatch.setattr(openai_compat.time, "sleep", sleeps.append)
    install.sleeps = sleeps
    return install


def reviews(n):
    return [{"review_id": f"r{i}", "app_title": "App", "score": 1, "content": f"review {i}"} for i in range(n)]


def test_schema_rejected_falls_back_to_json_object_and_sticks(endpoint):
    ep = endpoint(reject_modes={"json_schema"})
    a = OpenAICompatAnalyzer()
    assert len(a.analyze(reviews(45))) == 45  # 3 batches of 20
    modes = [ep._mode(b) for b in ep.bodies]
    assert modes[0] == "json_schema" and set(modes[1:]) == {"json_object"}
    assert modes.count("json_schema") == 1   # downgraded once, not per request
    assert '{"items": [ ... ]}' in ep.bodies[1]["messages"][0]["content"]


def test_falls_back_to_prompt_mode_and_parses_code_fences(endpoint):
    ep = endpoint(reject_modes={"json_schema", "json_object"})
    out = OpenAICompatAnalyzer().analyze(reviews(3))
    assert len(out) == 3
    assert "response_format" not in ep.bodies[-1]


def test_fixed_json_mode_does_not_downgrade(endpoint, monkeypatch):
    endpoint(reject_modes={"json_schema"})
    monkeypatch.setenv("LLM_JSON_MODE", "json_schema")
    with pytest.raises(LLMError, match="HTTP 400"):
        OpenAICompatAnalyzer().analyze(reviews(2))


def test_request_shape(endpoint, monkeypatch):
    ep = endpoint()
    monkeypatch.setenv("LLM_MAX_TOKENS", "4096")
    OpenAICompatAnalyzer(model="acme/big-model").analyze(reviews(1))
    body = ep.bodies[0]
    assert body["model"] == "acme/big-model" and body["max_tokens"] == 4096 and body["temperature"] == 0.1
    schema = body["response_format"]["json_schema"]["schema"]
    assert schema["properties"]["items"]["items"]["properties"]["category"]["enum"] == llm.CATEGORIES
    assert ep.agents[0].startswith("play-review-miner/")  # Cloudflare 1010 blocks Python-urllib


def test_rate_limit_respects_retry_after(endpoint):
    ep = endpoint(script=[("http", 429, {"Retry-After": "7"}, {"error": "slow down"})])
    assert len(OpenAICompatAnalyzer().analyze(reviews(2))) == 2
    assert endpoint.sleeps == [7.0]
    assert len(ep.bodies) == 2


def test_auth_error_is_clear_and_does_not_leak_key(endpoint):
    endpoint(script=[("http", 401, {}, {"error": "Unauthorized"})])
    with pytest.raises(LLMError) as exc:
        OpenAICompatAnalyzer().analyze(reviews(1))
    assert "LLM_API_KEY" in str(exc.value) and SECRET not in str(exc.value)


def test_unknown_model_points_to_list_models(endpoint):
    endpoint(script=[("http", 404, {}, {"error": "model not found"})])
    with pytest.raises(LLMError, match="list-models"):
        OpenAICompatAnalyzer(model="acme/nope").analyze(reviews(1))


def test_unusable_json_is_retried_then_fails(endpoint):
    endpoint(script=[("text", "sorry, I cannot"), ("text", "{}")])
    assert len(OpenAICompatAnalyzer().analyze(reviews(2))) == 2
    endpoint(script=[("text", "nope")] * 3)
    with pytest.raises(LLMError, match="unusable JSON"):
        OpenAICompatAnalyzer().analyze(reviews(2))


def test_truncated_answer_splits_batch(endpoint):
    ep = endpoint(script=[("length",)])
    assert len(OpenAICompatAnalyzer().analyze(reviews(4))) == 4
    assert [len(re.findall(r"^\[\d+\]", b["messages"][0]["content"].rsplit("<reviews>", 1)[1], re.M))
            for b in ep.bodies] == [4, 2, 2]


def test_parallel_batches_keep_order(endpoint, monkeypatch):
    endpoint()
    monkeypatch.setenv("LLM_BATCH_SIZE", "3")
    monkeypatch.setenv("LLM_CONCURRENCY", "4")
    out = OpenAICompatAnalyzer().analyze(reviews(20))
    assert [l["review_id"] for l in out] == [f"r{i}" for i in range(20)]


def test_failed_wave_keeps_earlier_results(endpoint, monkeypatch):
    # first wave (2 batches) succeeds, then the endpoint keeps failing: stop early, keep wave 1
    monkeypatch.setenv("LLM_BATCH_SIZE", "2")
    monkeypatch.setenv("LLM_CONCURRENCY", "2")
    ep = endpoint()
    real = ep.urlopen
    calls = []

    def flaky(req, timeout=None):
        calls.append(1)
        if len(calls) > 2:
            raise _http(403, {"error": "quota"})
        return real(req, timeout)
    monkeypatch.setattr(openai_compat.urllib.request, "urlopen", flaky)
    out = OpenAICompatAnalyzer().analyze(reviews(8))
    assert [l["review_id"] for l in out] == ["r0", "r1", "r2", "r3"]


def test_model_is_required(monkeypatch):
    monkeypatch.delenv("LLM_MODEL", raising=False)
    with pytest.raises(LLMError, match="No model chosen"):
        OpenAICompatAnalyzer()
    from play_review_miner.analyzers.base import AnalyzerError
    with pytest.raises(AnalyzerError):
        storage_name("openai")


def test_names_and_storage():
    assert openai_compat.analyzer_name("acme/fast-model") == "llm-acme-fast-model"
    assert storage_name("openai", "acme/big-model") == "llm-acme-big-model"
    assert storage_name("keyword") == "keyword"


@pytest.mark.parametrize("text,n", [
    ('[{"i": 0}]', 1), ('{"items": [{"i": 0}, {"i": 1}]}', 2), ('```json\n{"items": []}\n```', 0),
    ('<think>hmm</think>{"labels": [{"i": 0}]}', 1), ('Sure! {"items": [{"i": 3}]} done', 1), ('{"i": 5}', 1),
])
def test_parse_json_items(text, n):
    assert len(parse_json_items(text)) == n


@pytest.mark.parametrize("text", ["", "no json", '{"a": [1], "b": [2]}', '"just a string"'])
def test_parse_json_items_rejects(text):
    with pytest.raises(ValueError):
        parse_json_items(text)


def test_list_models_command(endpoint, capsys):
    endpoint()
    assert cli.main(["list-models", "--filter", "fast"]) == 0
    assert capsys.readouterr().out.strip() == "acme/fast-model"


def test_run_end_to_end_with_openai(tmp_path, fake_play, endpoint):
    ep = endpoint(reject_modes={"json_schema"})
    fake_play(FakePlay({"a.one": {"reviews": {1: [make_review(f"a{i}") for i in range(5)]}}}))
    out = tmp_path / "reports"
    rc = cli.main(["run", "--db", str(tmp_path / "r.db"), "--out", str(out), "--delay", "0", "-r", "10",
                   "--analyzer", "openai", "--llm-model", "acme/fast-model",
                   "--llm-base-url", "http://proxy.test/v1"])
    assert rc == 0
    stem = "PRODUCTIVITY_tr-tr_llm-acme-fast-model"
    md = (out / f"{stem}.md").read_text(encoding="utf-8")
    data = json.loads((out / f"{stem}.json").read_text(encoding="utf-8"))
    assert data["params"]["model"] == "acme/fast-model"
    assert data["themes"][0]["theme_id"] == "crash_wont_open" and data["totals"]["unlabeled"] == 0
    assert data["apps"][0]["what_it_does"] == "a.one not tutar."     # LLM-written app summary
    assert "dil modeli (llm-acme-fast-model)" in md
    assert all(b["model"] == "acme/fast-model" for b in ep.bodies)
    # a second run only sends new reviews: nothing to classify, report still works
    n = len(ep.bodies)
    assert cli.main(["run", "--skip-crawl", "--db", str(tmp_path / "r.db"), "--out", str(out),
                     "--analyzer", "openai", "--llm-base-url", "http://proxy.test/v1"]) == 0
    assert len(ep.bodies) == n
    # report-only resolves the same storage name without building the analyzer
    assert cli.main(["report", "--db", str(tmp_path / "r.db"), "--out", str(out), "--analyzer", "openai"]) == 0


def test_cloudflare_timeout_is_retried(endpoint):
    ep = endpoint(script=[("http", 524, {}, {"error": "Gateway Timeout", "message": "origin timed out (524)"})])
    assert len(OpenAICompatAnalyzer().analyze(reviews(2))) == 2
    assert len(ep.bodies) == 2


def test_app_summaries_use_small_batches(endpoint):
    ep = endpoint()
    apps = [{"app_id": f"app{i}", "title": "T", "description": "x" * 5000} for i in range(12)]
    out = OpenAICompatAnalyzer().summarize_apps(apps)
    assert len(out) == 12 and len(ep.bodies) == 3            # 5 + 5 + 2
    assert "x" * 801 not in ep.bodies[0]["messages"][0]["content"]


def test_request_budget_stops_run_and_keeps_results(endpoint, monkeypatch):
    ep = endpoint()
    monkeypatch.setenv("LLM_MAX_REQUESTS", "2")
    monkeypatch.setenv("LLM_BATCH_SIZE", "2")
    monkeypatch.setenv("LLM_CONCURRENCY", "1")
    a = OpenAICompatAnalyzer()
    out = a.analyze(reviews(10))
    assert [l["review_id"] for l in out] == ["r0", "r1", "r2", "r3"]
    assert len(ep.bodies) == 2 and a.requests == 2
    with pytest.raises(LLMError, match="budget"):
        a._generate("p", llm.CLASSIFY_ITEM)


def test_retries_count_against_budget(endpoint, monkeypatch):
    endpoint(script=[("http", 429, {"Retry-After": "1"}, {})] * 5)
    monkeypatch.setenv("LLM_MAX_REQUESTS", "3")
    with pytest.raises(LLMError, match="budget"):
        OpenAICompatAnalyzer().analyze(reviews(1))


def test_left_out_app_summary_is_asked_again(endpoint):
    ep = endpoint(script=[("text", json.dumps({"items": [{"app_id": "a1", "summary_tr": "bir"}]}))])
    out = OpenAICompatAnalyzer().summarize_apps([{"app_id": "a1"}, {"app_id": "a2"}])
    assert sorted(x["app_id"] for x in out) == ["a1", "a2"] and len(ep.bodies) == 2
    assert "a1" not in re.findall(r"app_id: (\S+)", ep.bodies[1]["messages"][0]["content"])


def test_schema_mode_prompt_mentions_json(endpoint):
    ep = endpoint()
    OpenAICompatAnalyzer().analyze(reviews(1))
    assert "JSON" in ep.bodies[0]["messages"][0]["content"] and ep._mode(ep.bodies[0]) == "json_schema"
