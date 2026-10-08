"""Run: python -m pytest"""
import pytest

from play_review_miner.analyzers.keyword import KeywordAnalyzer, fold

A = KeywordAnalyzer()


def themes(text):
    return A.classify_text(text)[0]


def test_fold():
    assert fold("ŞIKÇA Güncellendi") == "sikca guncellendi"


def test_turkish():
    assert "ads_intrusive" in themes("Çok fazla reklam var, kullanılmıyor")
    assert "paywall_subscription" in themes("Her şey için abonelik istiyor")
    assert "crash_wont_open" in themes("Uygulama açılmıyor sürekli çöküyor")
    assert "notifications_reminders" in themes("hatırlatıcılar zamanında çalmıyor")
    assert "update_broke" in themes("Son güncellemeden sonra her şey bozuldu")
    assert "usage_limits" in themes("mesaj hakkım hemen bitiyor, limit çok düşük")


def test_english():
    assert "ads_intrusive" in themes("Way too many ads")
    assert "crash_wont_open" in themes("It keeps crashing on startup")
    assert "feature_request" in themes("Please add a dark theme option")


def test_low_signal():
    r = A.analyze([{"review_id": "x", "content": "berbat"}])[0]
    assert r["low_signal"] and r["themes"] == []


def test_app_summary_fallback():
    from play_review_miner.app_profiles import _informative, _shorten
    assert not _informative("Google Keep", "Google Keep - Not ve listeler")
    assert _informative("Google'dan ücretsiz çevrimiçi depolama alanı.", "Google Drive")
    assert _shorten("Bir. İki. Üç.") == "Bir. İki."


def test_typographic_apostrophes():
    assert fold("won’t") == "won't"
    assert "crash_wont_open" in themes("It won’t open at all")
    assert "crash_wont_open" in themes("doesn’t work")


@pytest.mark.parametrize("text,theme", [
    ("I can't download my files anymore", "files_pdf_print"),
    ("PDF açılmıyor, sürekli hata", "files_pdf_print"),
    ("Files won't open after export", "files_pdf_print"),
    ("Fiyatı çok yüksek, öğrenciye pahalı", "expensive"),
    ("Notlarım silindi, hepsi gitti", "data_loss"),
    ("Notlarımın hepsi kayboldu", "data_loss"),
    ("Every ad is a 30 second video", "ads_intrusive"),
    ("It used to sync automatically, now it doesn't", "removed_feature"),
    ("Arayüz çok karmaşık", "hard_to_use_ui"),
    ("Türkçe desteği yok", "language_translation"),
    ("Can't cancel my subscription", "billing_refund"),
    ("Uygulama eklediğim dosyaları açmıyor, bekleyip duruyor", "files_pdf_print"),
    ("dosya ekledim ama açılmadı", "files_pdf_print"),
    ("bir türlü açmıyor kaydettiğim dosyayı", "files_pdf_print"),
    ("pdf belgesi düzenlenemiyor", "files_pdf_print"),
    ("Since yesterday it won't open my docs", "files_pdf_print"),
    ("it is not allowing me to print my docs", "files_pdf_print"),
    ("every file I export comes out corrupted", "files_pdf_print"),
    ("It is stuck on a white screen after login", "crash_wont_open"),
    ("açınca beyaz ekran geliyor, hiçbir şey olmuyor", "crash_wont_open"),
    ("taramayı yapıyor ama pdf olarak dönüştürmüyor", "files_pdf_print"),
    ("güncellemeden beri pdf dosyalarına ulaşamıyorum", "files_pdf_print"),
    ("I am unable to access my files anymore", "files_pdf_print"),
    ("Türkçe dil desteği yok, eklenmeli", "language_translation"),
    ("güzel uygulama ama türkce olsa keşke", "language_translation"),
    ("the app won't let me change the language", "language_translation"),
    ("my uploads never finish", "sync_backup"),
    ("an over priced app behind a paywall", "expensive"),
    ("rakipler ücretsizken bu fiyatlar çok", "expensive"),
    ("geçen yılki notlarıma ulaşamıyorum", "data_loss"),
    ("arayüzü çok yorucu", "hard_to_use_ui"),
    ("The new layout is so unintuitive", "hard_to_use_ui"),
])
def test_true_positives(text, theme):
    assert theme in themes(text)


@pytest.mark.parametrize("text,theme", [
    ("I downloaded this app and it is useless", "files_pdf_print"),
    ("The pdf viewer is fine but too many steps", "files_pdf_print"),
    ("Ad soyad alanı boş kalıyor", "ads_intrusive"),
    ("Notlarımı düzenleyemiyorum, rengi değiştirmek zor", "data_loss"),
    ("I got used to the old one", "removed_feature"),
    ("Fiyatı uygun ama senkronizasyon yok", "expensive"),
    ("Arayüz güzel ama sürekli donuyor", "hard_to_use_ui"),
    ("Türkçe yazıyorum ama klavye kasıyor", "language_translation"),
    ("Google Keep Plus gibi değil", "paywall_subscription"),
    ("Phone keeps charging slowly", "billing_refund"),
    ("her pdf açılışında reklam izletiyor", "files_pdf_print"),
    ("worst downloading experience ever", "files_pdf_print"),
    ("dosya paylaşım uygulaması gibi ama reklam çok", "files_pdf_print"),
    ("PDF özelliği için neden ücret ödeyeyim", "files_pdf_print"),
    ("Please add a dark theme option, the white screen hurts at night", "crash_wont_open"),
    ("beyaz ekran gözümü yoruyor, koyu tema olsun", "crash_wont_open"),
    ("Açıklamada dosya yöneticisi yazıyor", "files_pdf_print"),
    ("Fiyatı çok uygun ama reklam var", "expensive"),
    ("so much foul language in the chat", "language_translation"),
])
def test_known_false_positives(text, theme):
    assert theme not in themes(text)


def test_is_big():
    from play_review_miner.crawler import is_big
    assert is_big("com.google.android.keep")
    assert is_big("some.app", "Microsoft Corporation")
    assert is_big("x.y", "Indie Dev", ["indie"])
    assert is_big("com.splendapps.splendo", "Splend Apps") is None
