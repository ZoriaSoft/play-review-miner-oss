"""Aggregate analysis results into an 'opportunities' report (Markdown + JSON)."""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from . import __version__, db
from .analyzers.base import CATEGORIES, CATEGORY_LABELS


def _clip(text: str, n: int = 260) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _pick_quotes(items: list[dict], k: int) -> list[dict]:
    """Prefer informative (30-400 chars), upvoted quotes from different apps."""
    cutoff = (datetime.now() - timedelta(days=548)).isoformat()  # ~18 months

    def quality(r):
        L = len(r["content"] or "")
        length_ok = 1 if 30 <= L <= 400 else 0
        recent = 1 if (r.get("at") or "") >= cutoff else 0
        return (length_ok, recent, r.get("thumbs_up") or 0, min(L, 200))

    ranked = sorted(items, key=quality, reverse=True)
    out, seen_apps = [], set()
    for r in ranked:  # first pass: one per app
        if r["app_id"] not in seen_apps:
            out.append(r)
            seen_apps.add(r["app_id"])
        if len(out) >= k:
            break
    for r in ranked:  # fill up
        if len(out) >= k:
            break
        if r not in out:
            out.append(r)
    return out


def _fmt_int(n, lang: str) -> str:
    if n is None:
        return "—"
    s = f"{int(n):,}"
    return s.replace(",", ".") if lang == "tr" else s


def _fmt_rating(score, ratings, lang: str) -> str:
    if score is None:
        return "—"
    sc = f"{score:.1f}".replace(".", ",") if lang == "tr" else f"{score:.1f}"
    unit = "oy" if lang == "tr" else "ratings"
    return f"{sc} ★ ({_fmt_int(ratings, lang)} {unit})"


def _cell(text: str | None) -> str:
    return (text or "—").replace("|", "\\|").replace("\n", " ")


def _inline(text: str | None) -> str:
    return " ".join((text or "—").split())


def aggregate(reviews: list[dict], labels: dict, meta: dict, n_quotes: int,
              profiles: dict[str, dict] | None = None) -> dict:
    profiles = profiles or {}
    by_id = {r["review_id"]: r for r in reviews}
    apps: dict[str, dict] = {}
    for p in profiles.values():  # include listed apps even if they have no low-star reviews
        apps[p["app_id"]] = {"app_id": p["app_id"], "title": p["title"], "developer": p["developer"],
                             "rank": p["rank"], "reviews": 0, "informative": 0, "themes": Counter()}
    for r in reviews:
        a = apps.setdefault(r["app_id"], {"app_id": r["app_id"], "title": r["app_title"],
                                          "developer": r["developer"], "rank": r["rank"],
                                          "reviews": 0, "informative": 0, "themes": Counter()})
        a["reviews"] += 1

    theme_reviews: dict[str, list[dict]] = defaultdict(list)
    cat_counts: Counter = Counter()
    low_signal = unlabeled = no_theme = 0
    for rid, r in by_id.items():
        lab = labels.get(rid)
        if lab is None:
            unlabeled += 1
            continue
        if lab["low_signal"]:
            low_signal += 1
            continue
        apps[r["app_id"]]["informative"] += 1
        cat_counts[lab["category"] or "other"] += 1
        themes = json.loads(lab["themes"] or "[]")
        if not themes:
            no_theme += 1
        for t in themes:
            theme_reviews[t].append(r)
            apps[r["app_id"]]["themes"][t] += 1

    informative = sum(cat_counts.values())
    n_apps = len(apps)
    themes_out = []
    for t, items in theme_reviews.items():
        app_counter = Counter(r["app_id"] for r in items)
        m = meta.get(t, {})
        count = len(items)
        # Ranking score: volume, boosted when the pain is spread across many apps
        # (a category-wide problem is a better opportunity than one app's bug).
        spread = len(app_counter) / max(n_apps, 1)
        themes_out.append({
            "theme_id": t,
            "label": m.get("label") or t,
            "category": m.get("category") or "other",
            "opportunity": m.get("opportunity"),
            "reviews": count,
            "share_of_informative": round(count / informative, 4) if informative else 0,
            "apps_hit": len(app_counter),
            "score": round(count * math.sqrt(spread), 2),
            "thumbs_up": sum(r.get("thumbs_up") or 0 for r in items),
            "stars": dict(Counter(r["score"] for r in items)),
            "apps": [{"app_id": a, "title": apps[a]["title"], "reviews": c,
                      "what_it_does": (profiles.get(a) or {}).get("what_it_does")}
                     for a, c in app_counter.most_common()],
            "quotes": [{"app": q["app_title"], "app_id": q["app_id"], "stars": q["score"],
                        "date": (q["at"] or "")[:10], "thumbs_up": q.get("thumbs_up") or 0,
                        "text": _clip(q["content"])}
                       for q in _pick_quotes(items, n_quotes)],
        })
    themes_out.sort(key=lambda x: (-x["score"], -x["reviews"]))

    apps_out = []
    for a in sorted(apps.values(), key=lambda a: a["rank"] or 999):
        p = profiles.get(a["app_id"], {})
        apps_out.append({
            "rank": a["rank"], "app_id": a["app_id"], "title": a["title"], "developer": a["developer"],
            "what_it_does": p.get("what_it_does"), "what_it_does_source": p.get("what_it_does_source"),
            "genre": p.get("genre"), "installs": p.get("installs"), "real_installs": p.get("real_installs"),
            "score": p.get("score"), "ratings": p.get("ratings"), "contains_ads": p.get("contains_ads"),
            "url": p.get("url") or f"https://play.google.com/store/apps/details?id={a['app_id']}",
            "list_source": p.get("source"),
            "low_star_reviews": a["reviews"], "informative": a["informative"],
            "top_themes": [{"theme_id": t, "label": (meta.get(t) or {}).get("label") or t, "reviews": c}
                           for t, c in a["themes"].most_common(3)],
        })

    dates = sorted(r["at"] for r in reviews if r.get("at"))
    return {
        "totals": {"apps": n_apps, "reviews": len(reviews), "informative": informative,
                   "low_signal": low_signal, "unlabeled": unlabeled, "no_theme": no_theme,
                   "date_from": dates[0][:10] if dates else None, "date_to": dates[-1][:10] if dates else None},
        "categories": {c: cat_counts.get(c, 0) for c in CATEGORIES},
        "themes": themes_out,
        "apps": apps_out,
    }


def render_markdown(data: dict, args, analyzer_name: str, n_themes: int) -> str:
    t = data["totals"]
    L: list[str] = []
    stars = "1" if args.max_stars == 1 else f"1–{args.max_stars}"
    list_name = getattr(args, "list_name", "top")
    title_suffix = "" if list_name == "top" else f" · liste: {list_name}"
    L.append(f"# Fırsat Raporu: {args.category.upper()} ({args.lang}/{args.country.upper()}){title_suffix}")
    L.append("")
    L.append(f"_Oluşturma: {data['generated_at']} · play-review-miner v{__version__} · analiz: **{analyzer_name}**_")
    L.append("")
    L.append(f"Kategorideki ilk **{t['apps']}** uygulamanın **{stars} yıldızlı** toplam **{t['reviews']}** "
             f"yorumu incelendi ({t['date_from']} → {t['date_to']}"
             + (f", filtre: {args.since} sonrası" if getattr(args, "since", None) else "") + "). "
             f"Bilgi içeren yorum: **{t['informative']}**, düşük sinyalli (ör. sadece \"berbat\"): {t['low_signal']}, "
             f"hiçbir temaya girmeyen: {t['no_theme']}.")
    L.append("")
    crawl = data.get("crawl")
    if crawl and crawl.get("excluded"):
        L.append(f"Bu liste filtrelenmiştir: {crawl.get('candidates')} aday uygulamadan "
                 f"{len(crawl['excluded'])} tanesi elendi (büyük şirket / geliştirici / tür / asgari yorum filtresi). "
                 "Sıra numarası, Play kategori sayfası + Play aramasından gelen adayların filtre sonrası sırasıdır; "
                 "resmî liste sıralaması değildir.")
        L.append("")
    L.append("Sıralama skoru = yorum sayısı × √(etkilenen uygulama oranı). Yani birçok uygulamada tekrar eden "
             "şikâyetler, tek bir uygulamaya özgü olanlardan daha yukarıda.")
    L.append("")

    lang = args.lang.lower()
    L.append("## Uygulamalar")
    L.append("")
    L.append("Kategoride incelenen uygulamalar ve ne işe yaradıkları. “Ne işe yarar” metni "
             + (f"dil modeli ({', '.join(sorted(gen))}) tarafından mağaza açıklamasından yazıldı." if (gen := {
                 a.get("what_it_does_source") for a in data["apps"]
                 if a.get("what_it_does_source") and not a["what_it_does_source"].startswith("play_")
                 and a["what_it_does_source"] != "none"})
                else "Play Store'daki kısa açıklamadan (yoksa uzun açıklamanın ilk cümlelerinden) alındı; "
                     "geliştiricinin kendi tanıtım metnidir."))
    L.append("")
    L.append("| # | Uygulama | Ne işe yarar | Geliştirici | İndirme | Puan | Düşük puanlı yorum |")
    L.append("|---:|---|---|---|---:|---|---:|")
    for a in data["apps"]:
        L.append(f"| {a['rank']} | [{_cell(a['title'])}]({a['url']}) | {_cell(a.get('what_it_does'))} | "
                 f"{_cell(a['developer'])} | {_cell(a.get('installs'))} | "
                 f"{_fmt_rating(a.get('score'), a.get('ratings'), lang)} | {a['low_star_reviews']} |")
    L.append("")

    L.append("## En büyük fırsatlar")
    L.append("")
    L.append("| # | Tema | Tür | Yorum | Bilgili yorumların %'si | Etkilenen uygulama | 👍 |")
    L.append("|---|---|---|---:|---:|---:|---:|")
    for i, th in enumerate(data["themes"][:n_themes], 1):
        L.append(f"| {i} | {_cell(th['label'])} | {_cell(CATEGORY_LABELS.get(th['category'], th['category']))} | "
                 f"{th['reviews']} | {th['share_of_informative']*100:.1f}% | {th['apps_hit']}/{t['apps']} | {th['thumbs_up']} |")
    L.append("")
    L.append("_Bir yorum birden fazla temaya girebildiği için yüzdelerin toplamı %100'ü aşabilir._")
    L.append("")

    for i, th in enumerate(data["themes"][:n_themes], 1):
        L.append(f"### {i}. {_inline(th['label'])}")
        L.append("")
        if th.get("opportunity"):
            L.append(f"**Fırsat:** {th['opportunity']}")
            L.append("")
        L.append(f"- **{th['reviews']} yorum**, {th['apps_hit']} uygulamada. En çok etkilenenler:")
        for a in th["apps"][:3]:
            desc = f" — _{_clip(a['what_it_does'], 110)}_" if a.get("what_it_does") else ""
            L.append(f"  - **{a['title']}** ({a['reviews']}){desc}")
        if len(th["apps"]) > 3:
            rest = ", ".join(f"{a['title']} ({a['reviews']})" for a in th["apps"][3:8])
            more = f" +{len(th['apps']) - 8} daha" if len(th["apps"]) > 8 else ""
            L.append(f"  - Diğer: {rest}{more}")
        L.append("")
        for q in th["quotes"]:
            L.append(f"> “{q['text']}”  ")
            L.append(f"> — _{q['app']}, {q['stars']}★, {q['date']}"
                     + (f", 👍{q['thumbs_up']}" if q["thumbs_up"] else "") + "_")
            L.append("")

    L.append("## Şikâyet türlerine göre dağılım")
    L.append("")
    L.append("| Tür | Yorum |")
    L.append("|---|---:|")
    for c, n in sorted(data["categories"].items(), key=lambda x: -x[1]):
        L.append(f"| {CATEGORY_LABELS.get(c, c)} | {n} |")
    L.append("")

    L.append("## Uygulama bazında şikâyetler")
    L.append("")
    L.append("| Sıra | Uygulama | Düşük puanlı yorum | Bilgi içeren | En sık temalar |")
    L.append("|---:|---|---:|---:|---|")
    for a in data["apps"]:
        tops = _cell("; ".join(f"{x['label']} ({x['reviews']})" for x in a["top_themes"]) or None)
        L.append(f"| {a['rank']} | [{_cell(a['title'])}]({a['url']}) | {a['low_star_reviews']} | "
                 f"{a['informative']} | {tops} |")
    L.append("")

    extra = data.get("extra") or {}
    if extra.get("clusters"):
        L.append("## Kural dışı kalan yorumlarda kendiliğinden çıkan kümeler (TF-IDF)")
        L.append("")
        L.append(f"Hiçbir anahtar kelime temasına girmeyen {extra.get('unmatched_count')} bilgili yorum "
                 "TF-IDF + KMeans ile kümelendi. Etiketler otomatik terimlerdir, elle yorumlanmalıdır.")
        L.append("")
        for i, c in enumerate(extra["clusters"], 1):
            apps = ", ".join(f"{a} ({n})" for a, n in c["apps"])
            L.append(f"**Küme {i}** ({c['size']} yorum) — terimler: `{', '.join(c['top_terms'])}` — {apps}")
            L.append("")
            for e in c["examples"][:2]:
                L.append(f"> “{_clip(e['content'], 200)}” — _{e['app']}, {e['score']}★_")
                L.append("")

    L.append("## Yöntem ve sınırlamalar")
    L.append("")
    if analyzer_name == "keyword":
        L.append("- Analiz **anahtar kelime kuralları** (Türkçe + İngilizce) ile yapıldı; bir yorum birden fazla temaya "
                 "girebilir. Bağlamı, ironiyi ve yazım hatalarını tam anlayamaz. Daha isabetli sonuç için "
                 "bir dil modeliyle (`--analyzer openai` + `LLM_API_KEY`, ya da `--analyzer gemini`) tekrar çalıştırın.")
    else:
        model = (data.get("params") or {}).get("model")
        L.append(f"- Analiz **{analyzer_name}**" + (f" (`{model}`)" if model else "")
                 + " ile yapıldı; temalar model tarafından üretilip birleştirildi.")
    L.append("- Play Store sadece sınırlı sayıda yorumu ve en yeni yorumları döndürür; bu bir örneklemdir, tüm yorumlar değil.")
    L.append("- Kategori listesi Play Store'un kategori sayfasındaki sıradır (kişiselleştirme/bölgeye göre değişebilir).")
    L.append("- Büyük şirketlerin uygulamaları listede baskınsa `--exclude-dev \"Google LLC\"` gibi filtrelerle "
             "niş uygulamalara odaklanabilirsiniz.")
    L.append("")
    return "\n".join(L)


def _model_for(analyzer_name: str, args) -> str | None:
    if analyzer_name.startswith("llm-"):
        import os

        from .analyzers.openai_compat import DEFAULT_MODEL
        return getattr(args, "llm_model", None) or os.environ.get("LLM_MODEL") or DEFAULT_MODEL
    if analyzer_name == "gemini":
        import os
        return os.environ.get("GEMINI_MODEL") or None
    return None


def build_report(conn, analyzer_name: str, reviews: list[dict], args) -> tuple[Path, Path]:
    from .app_profiles import build_profiles
    labels = db.load_analysis(conn, analyzer_name)
    meta = db.load_theme_meta(conn, analyzer_name)
    list_name = getattr(args, "list_name", "top")
    profiles = build_profiles(conn, args.category.upper(), args.country.lower(), args.lang.lower(), args.top,
                              list_name, analyzer_name)
    data = aggregate(reviews, labels, meta, args.quotes, profiles)
    data["generated_at"] = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    data["params"] = {"category": args.category.upper(), "lang": args.lang, "country": args.country,
                      "top": args.top, "max_stars": args.max_stars, "since": getattr(args, "since", None),
                      "list": list_name,
                      "analyzer": analyzer_name, "model": _model_for(analyzer_name, args)}
    if analyzer_name == "keyword":
        from .analyzers.keyword import KeywordAnalyzer
        lab = {rid: {"themes": json.loads(r["themes"] or "[]"), "low_signal": bool(r["low_signal"])}
               for rid, r in labels.items()}
        data["extra"] = KeywordAnalyzer().extra_sections(reviews, lab)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    from .cli import _stem
    stem = f"{_stem(args)}_{analyzer_name}"
    crawl_file = out / f"{_stem(args)}_crawl.json"
    if crawl_file.exists():  # filters used by the last crawl of this list
        crawl = json.loads(crawl_file.read_text(encoding="utf-8"))
        data["crawl"] = {"candidates": crawl.get("candidates"), "excluded": crawl.get("excluded", []),
                         "errors": crawl.get("errors", [])}
    md_path, json_path = out / f"{stem}.md", out / f"{stem}.json"
    md_path.write_text(render_markdown(data, args, analyzer_name, args.themes), encoding="utf-8")
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return md_path, json_path
