"""Offline analyzer: Turkish + English keyword rules, plus TF-IDF clustering of the rest.

Works without any API key. Quality is decent for common complaint types (ads, paywalls,
crashes, sync, login, ...) but it cannot understand nuance or sarcasm; use the Gemini
analyzer for that.

Text is folded to ASCII (ş→s, ı→i, ğ→g, ...), typographic apostrophes are normalised (won’t →
won't) and everything is lowercased before matching, so patterns below are written in folded form
and also match reviews typed without Turkish characters.

Patterns are deliberately specific: a bare word that merely mentions a topic ("downloaded",
"pdf", "fiyat", "notlarım", Turkish "ad" = name) is not a complaint. `tests/test_keyword.py` keeps
both the expected hits and known false positives.
"""

from __future__ import annotations

import logging
import re
from collections import Counter

from .base import Analyzer

log = logging.getLogger(__name__)

_FOLD = str.maketrans({
    "ı": "i", "İ": "i", "I": "i", "ş": "s", "Ş": "s", "ğ": "g", "Ğ": "g", "ü": "u", "Ü": "u",
    "ö": "o", "Ö": "o", "ç": "c", "Ç": "c", "â": "a", "î": "i", "û": "u",
    "\u2019": "'", "\u2018": "'", "\u02bc": "'", "\u00b4": "'", "`": "'",
})


def fold(text: str) -> str:
    return (text or "").translate(_FOLD).lower()


# (theme_id, category, label, opportunity, [regex patterns on folded text])
# Patterns: Turkish stems are left open-ended to catch suffixes (reklam -> reklamlar, reklamdan).
THEMES: list[tuple[str, str, str, str, list[str]]] = [
    ("ads_intrusive", "pricing_ads", "Aşırı / rahatsız edici reklam",
     "Reklamsız ya da çok az ve rahatsız etmeyen reklamla çalışan bir alternatif; tek seferlik ücretle reklam kaldırma.",
     [r"\breklam", r"\bads\b", r"\b(an|the|one|every|another|this|video|full ?screen) ad\b", r"\bad (after|every|pops?|break)",
      r"\badvert", r"\bcommercials?\b", r"\bpop-?ups?\b"]),
    ("paywall_subscription", "pricing_ads", "Zorunlu abonelik / ödeme duvarı",
     "Temel özellikleri ücretsiz bırakan, adil ve şeffaf fiyatlı (tek seferlik veya ucuz) bir model.",
     [r"\babone", r"\bpremium", r"\bucretli", r"\bucret(i|ini|siz degil|lendir)", r"\bparali", r"\bpara (ist|iste|odet|odemek|vermek)", r"\bpro (surum|versiyon|ozellik)",
      r"\bsubscri", r"\bpaywall", r"\bpay(ing)? (for|to)\b", r"\bodeme (yap|istiyor|yapmadan|duvari)",
      r"\bdeneme sure", r"\bfree trial", r"\bparagoz", r"\bpara(sini|yi)? isti", r"\bucretsiz olsa", r"\bucretsiz (degil|sanip|diye)", r"\bnot free\b"]),
    ("billing_refund", "pricing_ads", "Haksız ücret kesintisi / iade sorunu",
     "Şeffaf faturalama, kolay iptal ve iade süreci; deneme bitmeden net hatırlatma.",
     [r"\bpara(m|mi|yi)? (kesil|cekil|cekti|aldi|gitti)", r"\bucret (kesil|alindi|aldi)", r"\biade",
      r"\brefund", r"\bcharged\b", r"\bcharging me\b", r"\bdolandir", r"\bscam", r"\bkartim", r"\biptal (edemi|edilmi|edemiyorum)",
      r"\bcancel\w* (my |the )?(subscri|plan|membership|trial)", r"\b(can'?t|cannot|unable to) cancel"]),
    ("expensive", "pricing_ads", "Fiyat çok pahalı",
     "Yerel (Türkiye) fiyatlandırma, öğrenci/aile planı veya daha ucuz bir alternatif.",
     [r"\bpahali", r"\bexpensive", r"\bovercharg", r"\boverpriced", r"\bfahis", r"\bcok para",
      r"\bfiyat\w* (cok |asiri |fazla |biraz )?(yuksek|ucuk|abarti|fahis|artti|zamlan)", r"\bzam (geldi|yapil|yaptiniz)",
      r"\bprices? (is |are )?(too |so |way |very )?(high|steep|much)", r"\b(over|high|too high)[ -]?priced",
      r"\bfiyat\w*\s+(\w+\s+)?(cok|asiri|fazla)\b(?!\s+(uygun|ucuz|iyi|makul|guzel))", r"\breasonable price", r"\bfiyatlandirma", r"\bnot worth (the|its|it'?s) (price|money|cost)"]),
    ("usage_limits", "pricing_ads", "Kullanım limiti / kota (mesaj, dosya, cihaz)",
     "Ücretsiz katmanda daha cömert limitler ya da limitlerin baştan net gösterilmesi.",
     [r"\blimit", r"\bkota", r"\bquota", r"\bsinir(i|a|lan|li)", r"\bhakki(m|n|miz)?\b", r"\bhak(kim|kin|kimiz)? bit", r"\bmesaj hakk",
      r"\bcihaz (sinir|limit)", r"\b\d+ (mesaj|soru|gorsel|resim|fotograf)\w* (hakk|sonra|limit|sinir|ile sinir)", r"\b\d+ saat (sonra|bekle)", r"\bwait \d+ (hours?|minutes?)"]),
    ("crash_wont_open", "bug", "Çöküyor / açılmıyor / çalışmıyor",
     "Stabil, hafif ve hızlı açılan bir uygulama; çökme oranını düşük tutmak tek başına fark yaratır.",
     [r"\bcok(u|uy|tu|me)", r"\bacilmi", r"\bacilmaz", r"\bacmiyor", r"\bkendi kendine kapan", r"\bkapaniyor",
      r"\bcrash", r"\bwon'?t open", r"\bdoesn'?t open", r"\bnot open", r"\bkeeps (closing|stopping)",
      r"\bdurduruldu", r"\bhata (veriyor|aliyorum|verdi|cikiyor)", r"\bhatasi (veriyor|cikiyor|aliyorum|verdi)", r"\berror", r"\b(beyaz|siyah|bos) ekran\w*\s+(geliyor|geldi|cikiyor|cikti|kaliyor|kaldi|veriyor|oluyor)", r"\b(beyaz|siyah|bos) ekranda (kal|takil|donu)",
      r"\b(stuck on|only|just|blank|shows?|showing|get|gets|getting) (a |an |the )?(black|white|blank) screen", r"\b(black|white|blank) screen (of death|appears|only|and (nothing|crash|freez))", r"\bcalismiyor", r"\bnot working", r"\bdoesn'?t work"]),
    ("update_broke", "bug", "Güncellemeden sonra bozuldu",
     "Geriye dönük uyumluluğa ve sürüm kalitesine önem veren, eski kullanıcıları kızdırmayan bir ürün.",
     [r"\bguncelleme(den|yle|ile| sonra|ler)", r"\bguncellen(di|dikten)", r"\bson (surum|guncelleme)",
      r"\bafter (the )?(latest |last |recent )?update", r"\bsince (the )?(latest |last )?update", r"\bnew update",
      r"\bnew version", r"\byeni (surum|guncelleme|versiyon)", r"\bguncellenemiyor", r"\bguncelleme (gelmiyor|yapamiyorum|olmuyor)"]),
    ("data_loss", "bug", "Veri / not kaybı",
     "Güvenilir yerel+bulut yedekleme, sürüm geçmişi ve 'hiçbir şey kaybolmaz' vaadi.",
     [r"\bkayboldu", r"\bkaybol", r"\bkayip", r"\bsiliniyor", r"\bkendi kendine sil", r"\bsilindi", r"\bsilinmis", r"\buctu",
      r"\b(notlarim|verilerim|kayitlarim|belgelerim|fotograflarim)\w* (git|sil|kaybol|yok ol|uc|gelmiyor|gorunmuyor|ulasam|erisem)",
      r"\bdosyalarim (git|sil|yok)", r"\blost (all|my)", r"\bdeleted (all|my)", r"\bdisappeared"]),
    ("sync_backup", "bug", "Senkronizasyon / yedekleme sorunu",
     "Cihazlar arası sorunsuz senkronizasyon ve kolay yedek alma/geri yükleme.",
     [r"\bsenkron", r"\besitle", r"\byedek", r"\bsync", r"\bbackup", r"\bbulut", r"\bcloud", r"\byuklenmiyor",
      r"\byukleme (yapam|olmuyor)",
      r"\bupload"]),
    ("login_account", "bug", "Giriş / hesap / doğrulama sorunu",
     "Hesap açmadan kullanılabilen veya sorunsuz, hızlı giriş sunan bir alternatif.",
     [r"\bgiris (yap|yapa|yapamiyorum|olmuyor|yapilmiyor)", r"\boturum", r"\bhesab\w* (giri|ulas|dogrula|kilit|kapat|eris|baglan)",
      r"\bhesap (ac|olustur|acil|kilit)", r"\bsifre", r"\blog ?in", r"\bsign ?in", r"\baccount (locked|suspended|banned|disabled)",
      r"\bpassword", r"\bdogrulama", r"\bverif", r"\bkod gel", r"\bbanned?\b", r"\bhesabim\w* (kapatildi|engellendi|askiya)"]),
    ("notifications_reminders", "bug", "Bildirim / hatırlatıcı çalışmıyor",
     "Zamanında ve güvenilir çalışan hatırlatıcılar (Android pil optimizasyonuna dayanıklı).",
     [r"\bbildirim", r"\bhatirlat", r"\balarm", r"\bnotification", r"\bremind"]),
    ("files_pdf_print", "bug", "Dosya açma / kaydetme / PDF / yazdırma / yükleme sorunu",
     "Dosyaları sorunsuz açan, kaydeden ve dışa aktaran sade bir belge aracı.",
     [r"\bdosya\w*\b.{0,40}\b(ac(il)?(ma|mi|ama|ami)|kaydedil?eme|kaydedil?emi|yuklen?eme|yuklen?emi|indiril?eme|indiril?emi|gorunmu|gorunt\w*le(n)?mi|paylasila|duzenlene|donusturule|bozul|bozuk)",
      r"\b(ac(il)?(ma|mi|ama|ami)|kaydedemi|yukleyemi|indiremi)\w*\b.{0,25}\bdosya",
      r"\bpdf\w*\b.{0,40}\b(ac(il)?(ma|mi|ama|ami)|gorunmu|kaydedile?mi|yuklenmi|bozul|bozuk|indirile?mi|duzenlene?mi|duzenlenemi|kullanilmi|donusturule?mi)",
      # Turkish negative verb (-mıyor/-madı/-maz, incl. -ama-/-ile-) near a file/pdf/document object
      r"\b(pdf|dosya|belge)\w*\b.{0,40}\b(donustur|ulas|eris|ac|kaydet|kaydedil|yazdir|indir|yukle|gonder|paylas|duzenle|goruntule)\w{0,3}?m[aeiu](yor|d|z)",
      r"\b(donustur|ulas|eris|ac|kaydet|kaydedil|yazdir|indir|yukle|gonder|paylas|duzenle|goruntule)\w{0,3}?m[aeiu](yor|d|z)\w*\b.{0,25}\b(pdf|dosya|belge)",
      r"\b(unable to|can'?t|cannot|not able to|ability to) (get to|access|reach|upload|download|open) (\w+ ){0,3}(pdf|files?|documents?|docs?)\b",
      r"\bkaydet(mi|emi)", r"\bkaydedemi", r"\bkaydedilmi", r"\byazdir(ami|ilami|amiyor|ilmiyor)", r"\bdisa aktar",
      r"\bexport", r"\b(can'?t|cannot|unable to|won'?t|doesn'?t|does not) (save|print|upload|download)",
      r"\b(not (allowing|letting) me to|won'?t (let|allow) me to) (open|save|print|export|upload|download)",
      r"\b(can'?t|cannot|unable to|won'?t|doesn'?t|does not|fails? to) (open|load|view|read|display) (the |my |a |any |some |these |large |all )?(pdf|files?|documents?|docs?|attachments?|sheets?|spreadsheets?|word files?|excel files?|presentations?)",
      r"\b(pdf|files?|documents?|docs?|attachments?|sheets?|spreadsheets?|word files?|excel files?|presentations?) (won'?t|doesn'?t|does not|do not|don'?t|can'?t|cannot|fail\w* to|are not|is not|not) (open|load|save|show|display|print)",
      r"\bcorrupt(ed|s)? (the |my )?(pdf|files?|documents?|docs?|attachments?|sheets?|spreadsheets?|word files?|excel files?|presentations?)", r"\b(pdf|files?|documents?|docs?|attachments?|sheets?|spreadsheets?|word files?|excel files?|presentations?) (are |is |got |gets |come out |comes out )?corrupt",
      r"\bpdf\w* (creation|conversion|export)\w*.{0,40}(fail|unstable|corrupt|blank)",
      r"\bprint(ing)? (doesn'?t|does not|won'?t|isn'?t|is not|fails|not working)",
      r"\bdownload(s|ing)? (fail|doesn'?t|won'?t|is not|isn'?t|not working|stuck)",
      r"\bindir(emiyorum|ilmiyor)", r"\byukleyemiyorum"]),
    ("ai_quality", "other", "Yapay zekâ yanlış yanıt / komutu anlamıyor",
     "Belirli bir işe odaklı, doğruluğu yüksek ve kaynak gösteren niş bir AI asistanı.",
     [r"\byanlis (cevap|bilgi|yanit|hesap)", r"\bsacma (cevap|yanit)", r"\bhatali (cevap|bilgi|yanit)", r"\bwrong answer",
      r"\bhallucinat", r"\buyduruyor", r"\b(beni|soruyu|soylediklerimi|komut\w*|ne dedigimi|dedigimi) anlamiyor",
      r"\bkomut\w* (uymuyor|anlamiyor|dinlemiyor)", r"\bdedigimi yapmiyor", r"\bsansur", r"\bcensor",
      r"\bgorsel (olustur|uret|yap)\w* (olmuyor|yapmiyor|berbat|kotu|sacma)", r"\bresim (olustur|ciz|uret)\w* (olmuyor|yapmiyor|berbat|kotu|sacma)", r"\b(doesn'?t|does not) (follow|understand)",
      r"\b(ai|gpt|yapay zeka)\b.{0,40}(aptal|kotu|berbat|yanlis|sacma)"]),
    ("slow_laggy", "performance", "Yavaş / donuyor / kasıyor",
     "Düşük donanımlı telefonlarda bile hızlı çalışan hafif bir sürüm (lite).",
     [r"\byavas", r"\bkas(iyor|ma)", r"\bdon(uyor|du|ma)", r"\btakil", r"\bgecikm", r"\bslow", r"\blag(gy|ging|s)?\b",
      r"\bfreez", r"\bstuck", r"\byukleniyor", r"\bloading", r"\bbekliyor\b", r"\bbekletiyor"]),
    ("battery_storage", "performance", "Pil / depolama / RAM tüketimi",
     "Pil ve depolama dostu, küçük boyutlu bir uygulama.",
     [r"\bsarj", r"\bpil\b", r"\bbatarya", r"\bbattery", r"\bdepolama", r"\bstorage", r"\byer kapl",
      r"\bhafiza", r"\bram\b", r"\bmemory", r"\bisiniyor", r"\bisinma", r"\boverheat"]),
    ("hard_to_use_ui", "ux", "Karmaşık / kullanışsız arayüz",
     "Sade, öğrenmesi kolay, gereksiz özelliklerden arındırılmış bir arayüz.",
     [r"\bkarmasik", r"\bkullanissiz", r"\bkullanimi (zor|karisik|cok zor)", r"\bzor kullan", r"\banlasilmiyor",
      r"\barayuz[a-z]*\b(?!\s+(\w+\s+)?(guzel|iyi|harika|hos|sade|temiz|basarili|mukemmel|kullanisli|guzeldi|iyiydi))",
      r"\b(karmasik|karisik|kotu|berbat|kullanissiz|cirkin) (bir )?(arayuz|tasarim)",
      r"\btasarim\w* (cok |asiri )?(karmasik|karisik|kotu|berbat|cirkin)",
      r"\bconfusing", r"\bcomplicated", r"\bhard to use", r"\bnot user friendly", r"\bclunky",
      r"\b(bad|terrible|awful|horrible|cluttered|confusing|outdated|ugly) (ui|interface|design|layout)\b",
      r"\b(ui|interface|design|layout) (is |are )?(bad|terrible|awful|horrible|cluttered|confusing|outdated|ugly|hard)",
      r"\bunintuitive", r"\b(ruined|changed|messed up) the (ui|interface|design|layout)\b", r"\bno easy (ui|interface)",
      r"\bkullanisli degil", r"\bbulamiyorum", r"\bcan'?t find"]),
    ("removed_feature", "ux", "Kaldırılan / değiştirilen özellik (eski hali daha iyiydi)",
     "Kullanıcıların sevdiği ama rakibin kaldırdığı özellikleri geri sunan bir alternatif.",
     [r"\bkaldir(mis|ildi|diniz|dilar)", r"\beski (hali|halini|surum|versiyon|tasarim)", r"\beskisi gibi",
      r"\bgeri getir", r"\bremoved", r"\bbring back", r"\bold version",
      r"(?<!got )(?<!get )(?<!gets )(?<!getting )\bused to (be|have|work|let|show|allow|do|sync|open)\b", r"\beskiden"]),
    ("language_translation", "ux", "Türkçe / dil / çeviri eksikliği",
     "Tam Türkçe destekli, yerelleştirmesi özenli bir alternatif.",
     [r"\bturkce[a-z]*\b(?!\s+(yaz|konus|okuy|bil)[a-z]*)", r"\bceviri", r"\bcevir(mi|i)", r"\bdil (destek|desteg|secen|yok)",
      r"\btranslat", r"(?<!foul )(?<!bad )(?<!offensive )(?<!programming )(?<!mathematical )(?<!body )\blanguage"]),
    ("widget_missing", "missing_feature", "Widget / ana ekran bileşeni",
     "İyi tasarlanmış, işlevsel ana ekran widget'ları.",
     [r"\bwidget", r"\bbilesen", r"\bana ekran"]),
    ("offline_mode", "missing_feature", "İnternetsiz (offline) kullanım",
     "İnternet olmadan da tam çalışan, verileri cihazda tutan bir uygulama.",
     [r"\binternetsiz", r"\bcevrimdisi", r"\binternet (olmadan|yokken|olmayinca)", r"\boffline",
      r"\bwithout internet"]),
    ("feature_request", "missing_feature", "Özellik isteği / eksik özellik",
     "Kullanıcıların açıkça istediği eksik özellikleri birinci sürümden sunmak.",
     [r"\bkeske", r"\beklen(se|meli|mesi|irse|sin)", r"\beklerseniz", r"\bekleyin", r"\bozellig?i (yok|olsa|olmali|eklen)",
      r"\bsecenegi? (yok|olsa|olmali)", r"\bolsa (iyi|guzel|keske)", r"\bolmasi lazim", r"\bolmali\b", r"\bgelmeli", r"\bgelsin\b", r"\bgetirin\b",
      r"\bplease add", r"\bwould be (nice|great)", r"\bi wish", r"\bmissing", r"\bshould (have|be able)",
      r"\bneeds? (a|an|to)\b", r"\byok mu\b", r"\bneden yok", r"\bnasil (yapilir|yapacagim|yapabilirim)"]),
    ("support_unresponsive", "other", "Destek ekibine ulaşılamıyor",
     "Hızlı ve insan tarafından yanıt veren destek; yorumlara cevap vermek bile fark yaratır.",
     [r"\bdestek (ekib|yok|vermiyor|alamiyorum)", r"\bmuhatap", r"\bcevap (vermiyor|yok|alamadim)",
      r"\bulasam", r"\biletisim", r"\bcustomer (service|support)", r"\bno (response|reply)", r"\bsupport team"]),
    ("privacy_permissions", "other", "Gizlilik / gereksiz izinler",
     "Az izin isteyen, veriyi cihazda tutan, gizliliği ön plana çıkaran bir alternatif.",
     [r"\bgizlilik", r"\bizin (istiyor|vermeden|veriyorum)", r"\bizinler", r"\bkisisel veri", r"\bprivacy",
      r"\bpermission", r"\btracking", r"\bkvkk", r"\bverilerimi (sat|topla|paylas|cal|izinsiz)"]),
]

LOW_SIGNAL_MIN_CHARS = 20
LOW_SIGNAL_MIN_WORDS = 4


class KeywordAnalyzer(Analyzer):
    name = "keyword"

    def __init__(self) -> None:
        self._compiled = [(tid, cat, [re.compile(p) for p in pats]) for tid, cat, _, _, pats in THEMES]

    def classify_text(self, text: str) -> tuple[list[str], str]:
        t = fold(text)
        hits = [(tid, cat) for tid, cat, pats in self._compiled if any(p.search(t) for p in pats)]
        if not hits:
            return [], "other"
        # primary category = most frequent among hits, ties broken by THEMES order
        cats = Counter(c for _, c in hits)
        best = max(cats.values())
        primary = next(c for _, c in hits if cats[c] == best)
        return [tid for tid, _ in hits], primary

    def analyze(self, reviews: list[dict]) -> list[dict]:
        out = []
        for r in reviews:
            content = (r.get("content") or "").strip()
            themes, cat = self.classify_text(content)
            low = not themes and (len(content) < LOW_SIGNAL_MIN_CHARS or len(content.split()) < LOW_SIGNAL_MIN_WORDS)
            out.append({"review_id": r["review_id"], "category": cat, "themes": themes,
                        "summary": None, "low_signal": low})
        return out

    def theme_meta(self) -> list[dict]:
        return [{"theme_id": tid, "category": cat, "label": label, "opportunity": opp}
                for tid, cat, label, opp, _ in THEMES]

    # ---- emergent clusters for reviews no rule matched ---------------------------------
    def extra_sections(self, reviews: list[dict], labels: dict[str, dict]) -> dict:
        rest = [r for r in reviews
                if r["review_id"] in labels and not labels[r["review_id"]]["themes"]
                and not labels[r["review_id"]]["low_signal"]]
        return {"unmatched_count": len(rest), "clusters": tfidf_clusters(rest)}


TR_STOP = set("""
acaba ama ancak artik aslinda az bana bazi belki ben beni benim bile bir biraz birkac biz bize bu buna bunda
bundan bunu bunun burada cok cunku da daha de degil diye dolayi en fakat gibi hala hem hep hepsi her hic icin
ile ise kadar ki kim mi mu ne neden nasil o olan olarak oldu olsun olur ona onu onun oyle sadece sen siz su
sey simdi tum uygulama uygulamayi uygulamada uygulamanin uygulamasi uygulamaya ve veya ya yani yine zaten
var yok mi mu mı mü gibi olmus olmuyor yapiyor oluyor neden niye sonra once artik iyi kotu berbat rezalet
lutfen tesekkurler app apps just really very get got even dont don't don can't cant im i'm it's its use using
""".split())


def tfidf_clusters(reviews: list[dict], max_clusters: int = 15, min_size: int = 5) -> list[dict]:
    """Cluster leftover reviews with TF-IDF + KMeans; label each cluster by top terms."""
    if len(reviews) < min_size * 2:
        return []
    try:
        from sklearn.cluster import KMeans
        from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS, TfidfVectorizer
    except ImportError:
        log.warning("scikit-learn not installed; skipping emergent clusters")
        return []
    texts = [fold(r["content"]) for r in reviews]
    stop = list(TR_STOP | set(ENGLISH_STOP_WORDS))
    vec = TfidfVectorizer(stop_words=stop, ngram_range=(1, 2), min_df=3, max_df=0.4,
                          token_pattern=r"(?u)\b[a-z][a-z]{2,}\b", sublinear_tf=True)
    try:
        X = vec.fit_transform(texts)
    except ValueError:
        return []
    if X.shape[1] < 5:
        return []
    k = max(2, min(max_clusters, len(reviews) // 40))
    km = KMeans(n_clusters=k, n_init=10, random_state=42).fit(X)
    terms = vec.get_feature_names_out()
    clusters = []
    for c in range(k):
        idx = [i for i, lab in enumerate(km.labels_) if lab == c]
        if len(idx) < min_size or len(idx) > 0.4 * len(reviews):
            continue  # too small, or a catch-all blob that carries no theme
        centroid = km.cluster_centers_[c]
        top_terms = [terms[i] for i in centroid.argsort()[::-1][:6]]
        # examples closest to centroid
        sims = X[idx] @ centroid
        order = [idx[i] for i in sims.argsort()[::-1]]
        apps = Counter(reviews[i]["app_title"] for i in idx)
        clusters.append({
            "size": len(idx),
            "top_terms": top_terms,
            "apps": apps.most_common(5),
            "examples": [{"app": reviews[i]["app_title"], "score": reviews[i]["score"],
                          "content": reviews[i]["content"]} for i in order[:3]],
        })
    clusters.sort(key=lambda c: -c["size"])
    return clusters
