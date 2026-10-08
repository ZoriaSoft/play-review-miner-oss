"""Analyzer interface.

An analyzer receives review dicts (review_id, app_id, app_title, score, content, ...) and
returns one label dict per review:

    {"review_id": str, "category": one of CATEGORIES, "themes": [theme_id, ...],
     "summary": str | None, "low_signal": bool}

and theme metadata (label / category / opportunity text) via `theme_meta()`.
Adding a new backend (OpenAI, local LLM, ...) = implement this class and register it in
`analyzers/__init__.py`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class AnalyzerError(RuntimeError):
    """An analyzer backend failed (bad API key, quota, unusable answers...)."""


CATEGORIES = ["bug", "missing_feature", "ux", "pricing_ads", "performance", "other"]

CATEGORY_LABELS = {
    "bug": "Hata / Bug",
    "missing_feature": "Eksik özellik",
    "ux": "Kullanılabilirlik / UX",
    "pricing_ads": "Fiyat / Reklam",
    "performance": "Performans",
    "other": "Diğer",
}


class Analyzer(ABC):
    name: str = "base"

    @abstractmethod
    def analyze(self, reviews: list[dict]) -> list[dict]:
        ...

    def use_existing_themes(self, meta: dict[str, dict]) -> None:  # noqa: B027 (optional hook)
        """Theme metadata stored by earlier runs; LLM analyzers reuse these ids (no-op by default)."""

    def theme_meta(self) -> list[dict]:
        """[{theme_id, label, category, opportunity}]"""
        return []

    def extra_sections(self, reviews: list[dict], labels: dict[str, dict]) -> dict:
        """Optional extra data for the report (e.g. emergent clusters)."""
        return {}
