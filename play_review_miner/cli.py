"""Command line interface.

    python -m play_review_miner run --category PRODUCTIVITY --lang tr --country tr --top 20
    python -m play_review_miner crawl ...   # only collect
    python -m play_review_miner analyze ... # only classify (keyword, gemini or any OpenAI-compatible LLM)
    python -m play_review_miner list-models # models of the OpenAI-compatible endpoint (LLM_BASE_URL)
    python -m play_review_miner report ...  # only write Markdown + JSON
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import date
from pathlib import Path

from . import db
from .analyzers.base import AnalyzerError

DEFAULT_DB = "data/reviews.db"
DEFAULT_OUT = "reports"


class NoDataError(RuntimeError):
    """Nothing to analyze/report for the requested category/locale/list."""


def _since(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise argparse.ArgumentTypeError(f"tarih YYYY-MM-DD olmalı: {value!r}") from None


def _list_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", value):
        raise argparse.ArgumentTypeError("liste adı sadece harf, rakam, '-' ve '_' içerebilir (en fazla 40)")
    return value


def _positive(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("1 veya daha büyük olmalı")
    return n


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--db", default=DEFAULT_DB, help=f"SQLite dosyası (varsayılan: {DEFAULT_DB})")
    p.add_argument("--category", "-c", default="PRODUCTIVITY",
                   help="Play kategori id'si (PRODUCTIVITY, TOOLS, EDUCATION, ...). 'list-categories' komutuna bakın.")
    p.add_argument("--lang", "-l", default="tr", help="Dil kodu (tr, en, ...). Varsayılan: tr")
    p.add_argument("--country", "-g", default="tr", help="Ülke kodu (tr, us, ...). Varsayılan: tr")
    p.add_argument("--top", "-n", type=_positive, default=20, help="Kategoride ilk kaç uygulama (varsayılan: 20)")
    p.add_argument("--max-stars", type=int, default=2, choices=[1, 2, 3, 4, 5],
                   help="Bu puan ve altındaki yorumlar (varsayılan: 2 → 1 ve 2 yıldız)")
    p.add_argument("--list", dest="list_name", type=_list_name, default="top", metavar="NAME",
                   help="Uygulama listesinin adı (varsayılan: top). Farklı filtrelerle ayrı listeler/raporlar "
                        "tutmak için, ör. --list niche")
    p.add_argument("--since", type=_since, default=None, metavar="YYYY-MM-DD",
                   help="Analiz/raporda sadece bu tarihten sonraki yorumları kullan (varsayılan: hepsi)")


def _add_crawl(p: argparse.ArgumentParser) -> None:
    p.add_argument("--reviews-per-app", "-r", type=_positive, default=200,
                   help="Uygulama başına çekilecek yorum (yıldızlara eşit bölünür, varsayılan: 200)")
    p.add_argument("--delay", type=float, default=1.0, help="İstekler arası bekleme sn (varsayılan: 1.0)")
    p.add_argument("--exclude-dev", action="append", default=[],
                   help="Geliştirici adında bu metin geçen uygulamaları atla (tekrarlanabilir, büyük/küçük "
                        "harf duyarsız), örn. --exclude-dev Google --exclude-dev 'Microsoft'")
    p.add_argument("--exclude-big", action="store_true",
                   help="Büyük şirketleri (Google, Microsoft, Adobe, Samsung, OpenAI, Meta, Amazon, ...) atla. "
                        "Liste: crawler.BIG_DEVELOPERS / BIG_APP_PREFIXES")
    p.add_argument("--search-term", action="append", default=[],
                   help="Aday uygulama bulmak için ek Play araması (tekrarlanabilir)")
    p.add_argument("--min-reviews", type=int, default=1,
                   help="Bu dil/ülkede en az bu kadar düşük puanlı yorumu olmayan uygulamaları listeye alma (varsayılan: 1)")
    p.add_argument("--candidates", type=_positive, default=None,
                   help="Filtrelemeden önce toplanacak aday uygulama sayısı (varsayılan: filtre varsa max(4×top, 100))")
    p.add_argument("--strict-genre", action="store_true",
                   help="Sadece ana türü tam olarak bu kategori olan uygulamaları al")
    p.add_argument("--refresh-apps", action="store_true", help="Uygulama detaylarını 24 saatten yeni olsa da yenile")


def _add_analyze(p: argparse.ArgumentParser) -> None:
    p.add_argument("--analyzer", choices=["auto", "keyword", "gemini", "openai"], default="auto",
                   help="auto: GEMINI_API_KEY varsa gemini, LLM_API_KEY varsa openai, yoksa keyword. "
                        "openai = herhangi bir OpenAI uyumlu uç nokta (OpenRouter, LLM gateway, yerel sunucu) (varsayılan: auto)")
    _add_llm(p)
    p.add_argument("--reanalyze", action="store_true", help="Önceden analiz edilmiş yorumları da tekrar analiz et")


def _add_llm(p: argparse.ArgumentParser) -> None:
    p.add_argument("--llm-base-url", default=None, metavar="URL",
                   help="OpenAI uyumlu base URL (varsayılan: LLM_BASE_URL ya da https://openrouter.ai/api/v1). "
                        "Anahtar yalnız LLM_API_KEY ortam değişkeninden okunur.")
    p.add_argument("--llm-model", default=None, metavar="ID",
                   help="Model id, zorunlu (veya LLM_MODEL). 'list-models' ile listele.")


def _add_report(p: argparse.ArgumentParser) -> None:
    p.add_argument("--out", "-o", default=DEFAULT_OUT, help=f"Rapor klasörü (varsayılan: {DEFAULT_OUT})")
    p.add_argument("--themes", type=_positive, default=15, help="Raporda kaç tema detaylansın (varsayılan: 15)")
    p.add_argument("--quotes", type=int, default=3, help="Tema başına örnek yorum (varsayılan: 3)")


def _stem(args) -> str:
    """Report file stem: PRODUCTIVITY_tr-tr[_<list>]"""
    base = f"{args.category.upper()}_{args.lang.lower()}-{args.country.lower()}"
    return base if args.list_name == "top" else f"{base}_{args.list_name}"


def cmd_crawl(conn, args) -> dict:
    from .crawler import crawl
    stars = list(range(1, args.max_stars + 1))
    stats = crawl(conn, args.category.upper(), args.country.lower(), args.lang.lower(), args.top,
                  args.reviews_per_app, stars, delay=args.delay, exclude_devs=args.exclude_dev,
                  strict_genre=args.strict_genre, refresh_apps=args.refresh_apps,
                  exclude_big=args.exclude_big, list_name=args.list_name, search_terms=args.search_term,
                  candidates_limit=args.candidates, min_reviews=args.min_reviews)
    if stats["excluded"]:
        logging.info("Excluded %d candidates (big company / developer / genre / min-reviews filters)", len(stats["excluded"]))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{_stem(args)}_crawl.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2),
                                                    encoding="utf-8")
    total_f = sum(a["fetched"] for a in stats["apps"])
    total_n = sum(a["new"] for a in stats["apps"])
    logging.info("Crawl done: %d apps, %d reviews fetched, %d new, %d errors",
                 len(stats["apps"]), total_f, total_n, len(stats["errors"]))
    return stats


def _load_reviews(conn, args) -> list[dict]:
    rows = db.select_reviews(conn, args.category.upper(), args.country.lower(), args.lang.lower(),
                             args.max_stars, app_limit=args.top, list_name=args.list_name)
    out = [dict(r) for r in rows]
    if getattr(args, "since", None):
        out = [r for r in out if (r.get("at") or "") >= args.since]
    return out


def cmd_analyze(conn, args) -> str:
    from .analyzers import get_analyzer
    analyzer = get_analyzer(args.analyzer, llm_base_url=args.llm_base_url, llm_model=args.llm_model)
    try:
        return _analyze(conn, args, analyzer)
    finally:
        if getattr(analyzer, "requests", None) is not None:  # parsed by the panel for daily quotas
            logging.info("%s: %d HTTP requests this run", analyzer.name, analyzer.requests)


def _analyze(conn, args, analyzer) -> str:
    reviews = _load_reviews(conn, args)
    if not reviews:
        raise NoDataError("Analiz edilecek yorum yok. Önce 'crawl' çalıştırın.")
    analyzer.use_existing_themes(db.load_theme_meta(conn, analyzer.name))
    todo = reviews
    if not args.reanalyze and analyzer.name != "keyword":
        done = db.analyzed_ids(conn, analyzer.name)
        todo = [r for r in reviews if r["review_id"] not in done]
    logging.info("Analyzer=%s, reviews=%d, to analyze=%d", analyzer.name, len(reviews), len(todo))
    # Paid/slow analyzers are saved chunk by chunk, so an interruption keeps what was already analyzed.
    chunk = getattr(analyzer, "save_every", None) or len(todo) or 1
    classified = 0
    for start in range(0, len(todo), chunk):
        labels = analyzer.analyze(todo[start:start + chunk])
        with conn:
            db.save_analysis(conn, analyzer.name, labels)
            db.save_theme_meta(conn, analyzer.name, analyzer.theme_meta())
        classified += len(labels)
        if len(todo) > chunk:
            logging.info("Saved %d / %d analyses", classified, len(todo))
    if len(todo) - classified:
        logging.warning("%d reviews were not classified; run 'analyze' again to retry them", len(todo) - classified)
    if hasattr(analyzer, "summarize_apps"):
        _summarize_apps(conn, analyzer, args)
    return analyzer.name


def _summarize_apps(conn, analyzer, args) -> None:
    """Let an LLM analyzer write Turkish 'ne işe yarar' texts for apps that don't have one yet."""
    apps = db.select_category_apps(conn, args.category.upper(), args.country.lower(), args.lang.lower(), args.top,
                                   args.list_name)
    have = set() if args.reanalyze else set(db.load_app_summaries(conn, analyzer.name))
    todo = [{"app_id": a["app_id"], "title": a["title"],
             "summary": a["summary"] or a["any_summary"],
             "description": a["description"] or a["any_description"]}
            for a in apps if a["app_id"] not in have and (a["description"] or a["any_description"])]
    if not todo:
        return
    logging.info("Writing Turkish app summaries with %s for %d apps", analyzer.name, len(todo))
    items = analyzer.summarize_apps(todo)  # failing batches are skipped inside; Play text is the fallback
    db.save_app_summaries(conn, analyzer.name, getattr(analyzer, "model", None), items)
    conn.commit()


def cmd_report(conn, args, analyzer_name: str | None = None) -> tuple[Path, Path]:
    from .analyzers import storage_name
    from .report import build_report
    name = analyzer_name or storage_name(args.analyzer, args.llm_model)
    reviews = _load_reviews(conn, args)
    if not db.select_category_apps(conn, args.category.upper(), args.country.lower(), args.lang.lower(), args.top,
                                   args.list_name):
        raise NoDataError("Bu kategori/dil/ülke/liste için veri yok. Önce 'crawl' çalıştırın.")
    labeled = db.analyzed_ids(conn, name)
    unlabeled = sum(1 for r in reviews if r["review_id"] not in labeled)
    if unlabeled:
        logging.warning("%d of %d reviews have no %s analysis yet; run 'analyze' first or the report will "
                        "under-count themes", unlabeled, len(reviews), name)
    md_path, json_path = build_report(conn, name, reviews, args)
    logging.info("Report: %s", md_path)
    logging.info("JSON:   %s", json_path)
    return md_path, json_path


def cmd_details(conn, args) -> dict:
    from .crawler import refresh_details
    return refresh_details(conn, args.category.upper(), args.country.lower(), args.lang.lower(),
                           args.top, delay=args.delay, force=args.force, list_name=args.list_name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="play-review-miner",
        description="Google Play'de bir kategorideki uygulamaların düşük puanlı yorumlarını toplayıp "
                    "şikâyet/hata/eksik özellik temalarına ayırır ve fırsat raporu üretir.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="crawl + analyze + report")
    _add_common(p)
    _add_crawl(p)
    _add_analyze(p)
    _add_report(p)
    p.add_argument("--skip-crawl", action="store_true", help="Sadece mevcut veriden analiz+rapor")

    p = sub.add_parser("crawl", help="Uygulama ve yorumları topla")
    _add_common(p)
    _add_crawl(p)
    p.add_argument("--out", "-o", default=DEFAULT_OUT,
                   help=f"Tarama özeti (<...>_crawl.json) klasörü; report ile aynı olmalı (varsayılan: {DEFAULT_OUT})")

    p = sub.add_parser("analyze", help="Yorumları sınıflandır")
    _add_common(p)
    _add_analyze(p)

    p = sub.add_parser("report", help="Markdown + JSON rapor yaz")
    _add_common(p)
    _add_analyze(p)
    _add_report(p)

    p = sub.add_parser("details", help="Sadece uygulama detaylarını (açıklama, indirme, puan) yenile")
    _add_common(p)
    p.add_argument("--delay", type=float, default=1.0)
    p.add_argument("--force", action="store_true", help="Taze olsa bile yeniden çek")

    sub.add_parser("list-categories", help="Kategori id'lerini listele")

    p = sub.add_parser("panel", help="Web paneli: tarama/analiz başlat, işleri izle, raporlar, uç noktalar/modeller")
    p.add_argument("--host", default="127.0.0.1", help="Dinleme adresi (varsayılan 127.0.0.1; dışarıya tünelle açın)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--out", "-o", default=DEFAULT_OUT)
    p.add_argument("--data-dir", default="data", help="panel.db ve iş günlükleri (varsayılan: data)")
    p.add_argument("--max-parallel", type=int, default=2, help="Aynı anda çalışacak iş sayısı")
    p.add_argument("--public-host", action="append", default=[],
                   help="Tünelden gelen istekler için izinli Origin host'u, ör. panel.example.com (tekrarlanabilir)")
    p.add_argument("--proxy-dashboard", default=None, help="Panelde gösterilecek LLM gateway/proxy paneli bağlantısı (isteğe bağlı)")

    p = sub.add_parser("panel-password", help="Panel giriş parolasını ayarla (en az 12 karakter)")
    p.add_argument("--data-dir", default="data")
    p.add_argument("--stdin", action="store_true", help="Parolayı stdin'den oku (betikler için)")

    p = sub.add_parser("list-models", help="OpenAI uyumlu uç noktadaki modelleri listele (LLM_BASE_URL / LLM_API_KEY)")
    p.add_argument("--llm-base-url", default=None, metavar="URL")
    p.add_argument("--filter", "-f", default=None, help="Sadece bu metni içeren model id'leri (ör. deepseek/)")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    # the scraper and urllib can be chatty
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    if args.cmd == "list-categories":
        from .crawler import CATEGORIES
        print("\n".join(CATEGORIES))
        return 0
    if args.cmd == "panel":
        from .panel.server import serve
        serve(Path.cwd(), args.db, args.out, Path(args.data_dir), args.host, args.port, args.proxy_dashboard,
              max(1, args.max_parallel), args.public_host)
        return 0
    if args.cmd == "panel-password":
        return _panel_password(args)
    if args.cmd == "list-models":
        from .analyzers.openai_compat import list_models
        try:
            ids = list_models(args.llm_base_url)
        except AnalyzerError as exc:
            logging.error("%s", exc)
            return 1
        print("\n".join(i for i in ids if not args.filter or args.filter.lower() in i.lower()))
        return 0
    conn = db.connect(args.db)
    try:
        return _dispatch(conn, args)
    except NoDataError as exc:
        logging.error("%s", exc)
        return 2
    except AnalyzerError as exc:
        logging.error("Analiz başarısız: %s (önceki parçaların sonuçları kaydedildi)", exc)
        return 1
    except KeyboardInterrupt:
        logging.error("Interrupted; data collected so far is saved (the previous app list is kept)")
        return 130
    finally:
        conn.close()


def _panel_password(args) -> int:
    import getpass

    from .panel.store import Store
    if args.stdin:
        pw = sys.stdin.readline().rstrip("\n")
    else:
        pw = getpass.getpass("Yeni panel parolası: ")
        if getpass.getpass("Tekrar: ") != pw:
            logging.error("Parolalar eşleşmiyor")
            return 1
    if len(pw) < 12:
        logging.error("Parola en az 12 karakter olmalı")
        return 1
    store = Store(Path(args.data_dir) / "panel.db")
    store.set_password(pw)
    store.close()
    logging.info("Panel parolası ayarlandı (%s)", Path(args.data_dir) / "panel.db")
    return 0


def _dispatch(conn, args) -> int:
    if args.cmd == "crawl":
        stats = cmd_crawl(conn, args)
        print(json.dumps({**{k: v for k, v in stats.items() if k not in ("apps", "excluded")},
                          "apps": len(stats["apps"]), "excluded": len(stats["excluded"])}, ensure_ascii=False))
        return 0 if stats["apps"] else 2
    if args.cmd == "details":
        stats = cmd_details(conn, args)
        print(json.dumps(stats, ensure_ascii=False))
        return 1 if stats["errors"] and not stats["fetched"] and not stats["fresh"] else 0
    if args.cmd == "analyze":
        cmd_analyze(conn, args)
        return 0
    if args.cmd == "report":
        cmd_report(conn, args)
        return 0
    if args.cmd == "run":
        if not args.skip_crawl:
            cmd_crawl(conn, args)
        name = cmd_analyze(conn, args)
        md, _ = cmd_report(conn, args, name)
        print(md)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
