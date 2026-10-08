from __future__ import annotations

import logging
import os

from .base import CATEGORIES, CATEGORY_LABELS, Analyzer, AnalyzerError

log = logging.getLogger(__name__)

KINDS = ("auto", "keyword", "gemini", "openai")


def resolve_kind(name: str = "auto") -> str:
    """auto -> gemini if GEMINI_API_KEY is set, openai if LLM_API_KEY is set, otherwise keyword."""
    if name != "auto":
        return name
    if os.environ.get("GEMINI_API_KEY"):
        return "gemini"
    if os.environ.get("LLM_API_KEY"):
        return "openai"
    return "keyword"


def storage_name(name: str = "auto", llm_model: str | None = None) -> str:
    """Name under which an analyzer's results are stored (and reports are named), without building it."""
    kind = resolve_kind(name)
    if kind == "openai":
        from .openai_compat import DEFAULT_MODEL, analyzer_name
        model = llm_model or os.environ.get("LLM_MODEL") or DEFAULT_MODEL
        if not model:
            from .base import AnalyzerError
            raise AnalyzerError("No model chosen: pass --llm-model or set LLM_MODEL")
        return analyzer_name(model)
    return kind


def get_analyzer(name: str = "auto", llm_base_url: str | None = None, llm_model: str | None = None) -> Analyzer:
    kind = resolve_kind(name)
    if kind == "gemini":
        from .gemini import GeminiAnalyzer
        return GeminiAnalyzer()
    if kind == "openai":
        from .openai_compat import OpenAICompatAnalyzer
        return OpenAICompatAnalyzer(base_url=llm_base_url, model=llm_model)
    if kind == "keyword":
        from .keyword import KeywordAnalyzer
        return KeywordAnalyzer()
    raise ValueError(f"Unknown analyzer: {name}")


__all__ = ["Analyzer", "AnalyzerError", "CATEGORIES", "CATEGORY_LABELS", "KINDS", "get_analyzer",
           "resolve_kind", "storage_name"]
