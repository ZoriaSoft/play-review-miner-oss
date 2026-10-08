"""Short Turkish "ne işe yarar" (what is it for) text per app.

Priority:
  1. LLM-written summary (stored in `app_summary` by `analyze --analyzer gemini|openai`): the report's
     own analyzer first, otherwise the most recent one from any LLM analyzer
  2. Play Store short description ("summary") from a Turkish listing
  3. Play Store short description in the report's language
  4. First 1-2 sentences of the Play Store full description (Turkish listing preferred)

No translation is done offline: for non-Turkish crawls the Turkish listing is used only if it was
fetched before (e.g. a tr/tr crawl of the same app); otherwise the store's own language is shown.
"""

from __future__ import annotations

import re

from . import db

MAX_CHARS = 220
_SENT_RE = re.compile(r"(?<=[.!?])\s+")


def _shorten(text: str | None, max_chars: int = MAX_CHARS, sentences: int = 2) -> str | None:
    if not text:
        return None
    text = " ".join(text.split())
    parts = _SENT_RE.split(text)
    out = ""
    for p in parts[:sentences]:
        cand = (out + " " + p).strip()
        if len(cand) > max_chars and out:
            break
        out = cand
    if len(out) > max_chars:
        out = out[: max_chars - 1].rsplit(" ", 1)[0].rstrip(",;:-") + "…"
    return out or None


def _first_paragraph(desc: str | None) -> str | None:
    if not desc:
        return None
    for para in desc.split("\n"):
        para = para.strip()
        if len(para) >= 40:  # skip headings / emoji lines
            return para
    return desc


def _informative(summary: str | None, title: str | None) -> bool:
    """Store short descriptions are sometimes just the app name ("Google Keep") - skip those."""
    if not summary:
        return False
    norm = lambda t: re.sub(r"[^\w]+", " ", (t or "").lower()).strip()
    s_, t_ = norm(summary), norm(title)
    return len(s_.split()) >= 4 and not (t_ and s_ in t_)


def what_it_does(conn, app: dict, lang: str,
                 generated: dict[str, tuple[str, str]] | None = None) -> tuple[str | None, str]:
    """Return (text, source) for an app row from db.select_category_apps()."""
    app_id = app["app_id"]
    title = app.get("title")
    if app.get("summary") and not _informative(app["summary"], title):
        app = {**app, "summary": None}
    if generated and generated.get(app_id):
        text, source = generated[app_id]
        return _shorten(text, 300, 2), source
    tr = db.turkish_listing(conn, app_id) if lang != "tr" else None
    if tr and not _informative(tr.get("summary"), title):
        tr = {**tr, "summary": None}
    if lang == "tr" and app.get("summary"):
        return _shorten(app["summary"]), "play_summary_tr"
    if tr and tr.get("summary"):
        return _shorten(tr["summary"]), "play_summary_tr"
    if app.get("summary"):
        return _shorten(app["summary"]), f"play_summary_{lang}"
    if lang == "tr" and app.get("description"):
        return _shorten(_first_paragraph(app["description"])), "play_description_tr"
    if tr and tr.get("description"):
        return _shorten(_first_paragraph(tr["description"])), "play_description_tr"
    if app.get("description"):
        return _shorten(_first_paragraph(app["description"])), f"play_description_{lang}"
    if app.get("any_summary"):
        return _shorten(app["any_summary"]), "play_summary_other"
    return None, "none"


def build_profiles(conn, category: str, country: str, lang: str, top: int | None,
                   list_name: str = "top", analyzer_name: str | None = None) -> dict[str, dict]:
    generated = db.load_latest_app_summaries(conn)
    if analyzer_name:  # the report's own analyzer wins over others
        generated.update({k: (v, analyzer_name) for k, v in db.load_app_summaries(conn, analyzer_name).items()})
    profiles = {}
    for a in db.select_category_apps(conn, category, country, lang, top, list_name):
        text, source = what_it_does(conn, a, lang, generated)
        profiles[a["app_id"]] = {
            "rank": a["rank"], "app_id": a["app_id"], "title": a["title"], "developer": a["developer"],
            "genre": a["genre"], "installs": a["installs"], "real_installs": a["real_installs"],
            "score": round(a["score"], 2) if a["score"] is not None else None, "ratings": a["ratings"],
            "contains_ads": bool(a["contains_ads"]),
            "what_it_does": text, "what_it_does_source": source,
            "store_summary": a["summary"], "source": a.get("source"),
            "url": f"https://play.google.com/store/apps/details?id={a['app_id']}",
        }
    return profiles
