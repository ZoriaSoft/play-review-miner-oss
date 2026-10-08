"""Collect apps of a Play Store category and their low-star reviews.

Uses the public `google-play-scraper` package (no API key). The Python package has no
"list apps in category" helper, so the category page HTML is parsed for app ids, with a
keyword-search fallback.
"""

from __future__ import annotations

import logging
import random
import re
import time
import urllib.request
from typing import Callable, Iterable

from google_play_scraper import Sort
from google_play_scraper import app as gp_app
from google_play_scraper import reviews as gp_reviews
from google_play_scraper import search as gp_search
from google_play_scraper.exceptions import NotFoundError

from . import db

log = logging.getLogger(__name__)

CATEGORY_URL = "https://play.google.com/store/apps/category/{category}?hl={lang}&gl={country}"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)
APP_ID_RE = re.compile(r"/store/apps/details\?id=([A-Za-z0-9_.]+)")

# Extra candidate sources (Play search). Used when the category page doesn't yield enough apps,
# e.g. after excluding big developers. Keys: category -> lang -> terms ("*" = any language).
SEARCH_TERMS = {
    "PRODUCTIVITY": {
        "tr": ["yapılacaklar listesi", "not defteri", "pdf okuyucu", "ajanda takvim", "hatırlatıcı",
               "belge tarayıcı", "pomodoro odaklanma", "alışkanlık takibi", "uygulama kilidi",
               "mesai takip", "ders programı", "planlayıcı"],
        "*": ["to do list", "notes app", "pdf reader", "planner calendar", "reminder", "document scanner",
              "pomodoro focus", "habit tracker", "app lock", "work hours tracker", "timetable", "productivity"],
    },
    "TOOLS": {"*": ["file manager", "cleaner", "vpn", "flashlight", "qr scanner", "keyboard"]},
    "EDUCATION": {"*": ["learn english", "math", "quiz", "dictionary", "flashcards"]},
    "FINANCE": {"tr": ["bütçe", "harcama takibi", "borsa", "kripto"],
                "*": ["budget", "expense tracker", "stocks", "crypto wallet"]},
    "HEALTH_AND_FITNESS": {"tr": ["egzersiz", "adım sayar", "diyet", "su içme hatırlatıcı"],
                           "*": ["workout", "step counter", "diet", "water reminder"]},
}

# "Big company" filter (--exclude-big). Developer names are matched case-insensitively as whole
# words/phrases ("apple" must not hit "Pineapple Games", "intuit" not "Intuitive Apps"); package
# prefixes let us skip them before even fetching details. Prefixes match a whole id segment:
# "com.google" matches "com.google" and "com.google.android.keep", not "com.googleplex".
BIG_DEVELOPERS = [
    "google", "microsoft", "adobe", "samsung", "openai", "anthropic", "x.ai", "xai", "spacexai",
    "meta platforms", "facebook", "apple", "amazon", "dropbox", "wps software", "kingsoft", "yandex",
    "huawei", "xiaomi", "bytedance", "tiktok", "tencent", "baidu", "alibaba", "perplexity", "zoom",
    "salesforce", "slack", "atlassian", "notion labs", "opera", "mozilla", "duckduckgo", "box, inc",
    "intuit", "oracle", "ibm", "sony", "lg electronics", "motorola", "oneplus", "oppo", "vivo",
    "deepseek", "mistral", "canva", "evernote", "linkedin", "shareit", "bending spoons", "spring (sg)",
    "hp inc", "hewlett", "github", "dell", "lenovo", "cisco", "spotify", "netflix",
]
BIG_APP_PREFIXES = [
    "com.google", "com.microsoft", "com.adobe", "com.samsung", "com.sec", "com.openai",
    "com.anthropic", "ai.x", "com.facebook", "com.meta", "com.instagram", "com.whatsapp",
    "com.amazon", "com.apple", "com.dropbox", "cn.wps", "ru.yandex", "com.yandex", "com.huawei",
    "com.xiaomi", "com.miui", "com.zhiliaoapp", "com.ss.android", "ai.perplexity", "us.zoom",
    "com.slack", "com.atlassian", "notion.id", "com.opera", "org.mozilla", "com.duckduckgo",
    "com.box", "com.canva", "com.evernote", "com.linkedin", "shareit", "com.lenovo.anyshare",
    "com.deepseek", "com.chrome", "com.github", "com.hp", "com.lenovo", "com.larus",
]

_BIG_DEV_RE = [(d, re.compile(r"(?<![0-9a-z])" + re.escape(d) + r"(?![0-9a-z])")) for d in BIG_DEVELOPERS]


def is_big(app_id: str, developer: str | None = None, extra_devs: list[str] | None = None) -> str | None:
    """Return the matching rule if the app belongs to a big company, else None.

    `extra_devs` (from --exclude-dev) are plain substrings, as documented for that option.
    """
    aid = app_id.lower()
    for p in BIG_APP_PREFIXES:
        if aid == p or aid.startswith(p + "."):
            return f"package {p}"
    dev = (developer or "").casefold()
    if not dev:
        return None
    for d, rx in _BIG_DEV_RE:
        if rx.search(dev):
            return f"developer '{d}'"
    for d in extra_devs or []:
        if d.casefold() in dev:
            return f"developer '{d.casefold()}'"
    return None


def dev_excluded(developer: str | None, exclude_devs: list[str]) -> bool:
    dev = (developer or "").casefold()
    return bool(dev) and any(d.casefold() in dev for d in exclude_devs)

CATEGORIES = [
    "ART_AND_DESIGN", "AUTO_AND_VEHICLES", "BEAUTY", "BOOKS_AND_REFERENCE", "BUSINESS", "COMICS",
    "COMMUNICATION", "DATING", "EDUCATION", "ENTERTAINMENT", "EVENTS", "FINANCE", "FOOD_AND_DRINK",
    "HEALTH_AND_FITNESS", "HOUSE_AND_HOME", "LIBRARIES_AND_DEMO", "LIFESTYLE", "MAPS_AND_NAVIGATION",
    "MEDICAL", "MUSIC_AND_AUDIO", "NEWS_AND_MAGAZINES", "PARENTING", "PERSONALIZATION", "PHOTOGRAPHY",
    "PRODUCTIVITY", "SHOPPING", "SOCIAL", "SPORTS", "TOOLS", "TRAVEL_AND_LOCAL", "VIDEO_PLAYERS",
    "WEATHER", "GAME", "GAME_ACTION", "GAME_ADVENTURE", "GAME_ARCADE", "GAME_BOARD", "GAME_CARD",
    "GAME_CASINO", "GAME_CASUAL", "GAME_EDUCATIONAL", "GAME_MUSIC", "GAME_PUZZLE", "GAME_RACING",
    "GAME_ROLE_PLAYING", "GAME_SIMULATION", "GAME_SPORTS", "GAME_STRATEGY", "GAME_TRIVIA", "GAME_WORD",
]


class Throttle:
    """Polite delay between requests (+ jitter)."""

    def __init__(self, delay: float):
        self.delay = delay
        self._last = 0.0

    def wait(self) -> None:
        gap = self.delay * random.uniform(0.7, 1.3)
        sleep_for = self._last + gap - time.monotonic()
        if sleep_for > 0:
            time.sleep(sleep_for)
        self._last = time.monotonic()


def with_retry(fn: Callable, *args, retries: int = 4, base: float = 2.0, **kwargs):
    for attempt in range(retries + 1):
        try:
            return fn(*args, **kwargs)
        except NotFoundError:
            raise  # app removed / not available in this country: retrying cannot help
        except Exception as exc:  # scraper raises assorted errors on 429/5xx/parse issues
            if attempt == retries:
                raise
            wait = base * (2 ** attempt) + random.random()
            log.warning("%s failed (%s); retry %d/%d in %.1fs", getattr(fn, "__name__", fn),
                        exc, attempt + 1, retries, wait)
            time.sleep(wait)


def _http_get(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def list_category_apps(category: str, country: str, lang: str, limit: int, throttle: Throttle,
                       extra_terms: list[str] | None = None, use_search: bool = False) -> list[tuple[str, str]]:
    """Return [(app_id, source)]: category page order first, then Play search results.

    Search is used when the page yields fewer than `limit` apps, or always when `use_search`.
    """
    ids: dict[str, str] = {}
    url = CATEGORY_URL.format(category=category, lang=lang, country=country.upper())
    try:
        throttle.wait()
        html = with_retry(_http_get, url)
        for m in APP_ID_RE.finditer(html):
            ids.setdefault(m.group(1), "category_page")
        log.info("Category page %s (%s/%s): %d app ids", category, lang, country, len(ids))
    except Exception as exc:
        log.warning("Category page failed: %s", exc)

    if use_search or len(ids) < limit:
        by_lang = SEARCH_TERMS.get(category, {})
        terms = list(extra_terms or []) + by_lang.get(lang, by_lang.get("*", [category.lower().replace("_", " ")]))
        for term in terms:
            if len(ids) >= limit:
                break
            try:
                throttle.wait()
                hits = with_retry(gp_search, term, lang=lang, country=country, n_hits=30)
                added = 0
                for hit in hits:
                    if hit.get("appId") and hit["appId"] not in ids:
                        ids[hit["appId"]] = f"search:{term}"
                        added += 1
                log.info("Search '%s': %d hits, %d new candidates", term, len(hits), added)
            except Exception as exc:
                log.warning("Search '%s' failed: %s", term, exc)
    return list(ids.items())


def fetch_reviews_for_app(conn, app_id: str, country: str, lang: str, stars: Iterable[int],
                          per_app: int, throttle: Throttle, page_size: int = 200) -> tuple[int, int]:
    """Fetch the newest `per_app` reviews (split evenly over `stars`) of one app in one locale.

    Incremental: the stored reviews of a star are the newest window from earlier crawls. A page made
    only of known reviews means we have caught up with that window, so paging stops there - but only
    once the window is as deep as requested; otherwise we keep paging through known reviews to reach
    older ones (e.g. a later run with a larger --reviews-per-app).

    Returns (fetched, new).
    """
    stars = list(stars)
    per_star = max(1, per_app // len(stars))
    fetched = new = 0
    for star in stars:
        token = None
        got = 0
        while got < per_star:
            n = min(page_size, per_star - got)
            throttle.wait()
            if token is None:
                result, token = with_retry(gp_reviews, app_id, lang=lang, country=country,
                                           sort=Sort.NEWEST, count=n, filter_score_with=star)
            else:
                result, token = with_retry(gp_reviews, app_id, continuation_token=token, count=n)
            if not result:
                break
            known = db.known_review_ids(conn, [r["reviewId"] for r in result], country, lang)
            added = db.insert_reviews(conn, app_id, country, lang, result)
            conn.commit()
            fetched += len(result)
            new += added
            got += len(result)
            if len(known) == len(result) and db.count_reviews(conn, app_id, country, lang, score=star) >= per_star:
                log.debug("%s %d★: caught up with stored reviews, stopping (incremental)", app_id, star)
                break
            if token is None or getattr(token, "token", None) is None:
                break
    return fetched, new


def fetch_details(conn, app_id: str, lang: str, country: str, throttle: Throttle) -> None:
    """Fetch the store listing (title, summary, description, installs, rating...) and persist it."""
    throttle.wait()
    info = with_retry(gp_app, app_id, lang=lang, country=country)
    db.upsert_app(conn, info, lang, country)
    conn.commit()


def refresh_details(conn, category: str, country: str, lang: str, top: int | None,
                    delay: float = 1.0, force: bool = False, list_name: str = "top") -> dict:
    """Re-fetch store listings for apps already linked to a category (no reviews)."""
    throttle = Throttle(delay)
    done, skipped, errors = 0, 0, []
    for a in db.select_category_apps(conn, category, country, lang, top, list_name):
        if not force and db.app_fresh(conn, a["app_id"], lang, country):
            skipped += 1
            continue
        try:
            fetch_details(conn, a["app_id"], lang, country, throttle)
            done += 1
        except Exception as exc:
            log.warning("App details failed for %s: %s", a["app_id"], exc)
            errors.append(f"{a['app_id']}: {exc}")
    log.info("Details: %d fetched, %d already fresh, %d errors", done, skipped, len(errors))
    return {"fetched": done, "fresh": skipped, "errors": errors}


def crawl(conn, category: str, country: str, lang: str, top: int, per_app: int,
          stars: list[int], delay: float = 1.0, exclude_devs: list[str] | None = None,
          strict_genre: bool = False, refresh_apps: bool = False, exclude_big: bool = False,
          list_name: str = "top", search_terms: list[str] | None = None,
          candidates_limit: int | None = None, min_reviews: int = 1) -> dict:
    """Rank `top` apps of a category into `list_name` and collect their low-star reviews.

    The ranking is built under a staging name and swapped in at the end, so an interrupted crawl
    (network error, Ctrl+C) keeps the previous list instead of leaving a half-built one.
    """
    throttle = Throttle(delay)
    exclude = [d for d in (exclude_devs or []) if d.strip()]
    filtering = bool(exclude or strict_genre or exclude_big or min_reviews > 1)
    limit = candidates_limit or (max(top * 4, 100) if filtering else top)
    candidates = list_category_apps(category, country, lang, limit, throttle,
                                    extra_terms=search_terms, use_search=filtering or bool(search_terms))
    log.info("%d candidate apps", len(candidates))
    stats = {"category": category, "country": country, "lang": lang, "list": list_name,
             "candidates": len(candidates), "apps": [], "excluded": [], "errors": []}
    staging = db.staging_name(list_name)
    db.reset_list(conn, staging, category, country, lang)  # leftovers of an interrupted crawl
    conn.commit()
    rank = 0
    for app_id, source in candidates:
        if rank >= top:
            break
        if exclude_big and (why := is_big(app_id)):
            stats["excluded"].append({"app_id": app_id, "reason": why})
            log.info("Skip %s (big company: %s)", app_id, why)
            continue
        try:
            if refresh_apps or not db.app_fresh(conn, app_id, lang, country):
                fetch_details(conn, app_id, lang, country, throttle)
            row = conn.execute("SELECT * FROM apps WHERE app_id=?", (app_id,)).fetchone()
        except NotFoundError:
            stats["excluded"].append({"app_id": app_id, "reason": f"not available in {country}"})
            log.info("Skip %s (not available in %s)", app_id, country)
            continue
        except Exception as exc:
            log.warning("App details failed for %s: %s", app_id, exc)
            stats["errors"].append(f"{app_id}: details: {exc}")
            continue
        dev = row["developer"] or ""
        why = None
        if exclude_big:
            why = is_big(app_id, dev, exclude)
        elif dev_excluded(dev, exclude):
            why = "developer excluded"
        if why:
            stats["excluded"].append({"app_id": app_id, "developer": dev, "reason": why})
            log.info("Skip %s (%s; %s)", app_id, dev, why)
            continue
        if strict_genre and (row["genre_id"] or "") != category:
            stats["excluded"].append({"app_id": app_id, "developer": dev,
                                      "reason": f"genre {row['genre_id']}"})
            log.info("Skip %s (genre %s != %s)", app_id, row["genre_id"], category)
            continue
        db.link_app_category(conn, app_id, category, country, lang, rank + 1, staging, source)
        conn.commit()
        try:
            fetched, new = fetch_reviews_for_app(conn, app_id, country, lang, stars, per_app, throttle)
        except Exception as exc:
            log.warning("Reviews failed for %s: %s", app_id, exc)
            stats["errors"].append(f"{app_id}: reviews: {exc}")
            fetched = new = 0
        have = db.count_reviews(conn, app_id, country, lang, max_score=max(stars))
        if have < min_reviews:
            # e.g. foreign government apps on the category page with no reviews in this language
            db.unlink_app_category(conn, app_id, category, country, lang, staging)
            conn.commit()
            stats["excluded"].append({"app_id": app_id, "developer": dev,
                                      "reason": f"only {have} low-star reviews in {lang}/{country}"})
            log.info("Skip %s (%s): only %d low-star reviews (< %d)", app_id, dev, have, min_reviews)
            continue
        rank += 1
        log.info("[%2d/%d] %-45s %-30s %-22s fetched=%d new=%d", rank, top, app_id,
                 (row["title"] or "")[:30], dev[:22], fetched, new)
        stats["apps"].append({"rank": rank, "app_id": app_id, "title": row["title"], "developer": dev,
                              "source": source, "fetched": fetched, "new": new})
    db.publish_list(conn, list_name, category, country, lang)
    if rank < top:
        log.warning("Only %d apps matched the filters (wanted %d); add --search-term or raise --candidates",
                    rank, top)
    return stats
