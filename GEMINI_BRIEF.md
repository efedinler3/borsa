# Piyasa Masası — proje özeti (başka bir yapay zekâya devretmek için)

Bu belge, Claude ile birlikte geliştirilen projenin kapsamını, mimarisini, yapılan deneyleri, sonuçları ve açık
konuları anlatır. Tarih: 26 Ekim 2026. Proje klasörü: `C:\Users\sefed\OneDrive\Masaüstü\trade`.

---

## 1. Amaç ve kısıtlar

- **Kullanıcı:** Türkiye'de yaşayan, ABD hisselerinde gün içi / kısa vadeli işlem yapmak isteyen bir yatırımcı.
  Arayüz ve tüm metinler **Türkçe**.
- **Hedef 1 (tamam):** investing.com'a girmeden ABD borsasıyla ilgili bütün haberleri tek yerde, kaynak kaynak
  ("kim ne diyor") görmek; sabah / öğle / akşam brifingleri; fırsat listesi.
- **Hedef 2 (devam ediyor):** Bu verilerle karar veren, kendini geliştiren bir alım-satım botu. Önce **yalnızca
  sanal parayla** (paper trading). Gerçek emir hiçbir yere gönderilmez.
- **Kısıtlar:**
  - Ücretli yapay zekâ API'si yok (Claude API kullanılmıyor). Analiz kural tabanlı ve yerel (numpy, scikit-learn).
  - Tamamen ücretsiz veri kaynakları.
  - Bilgisayarı yormamalı (masaüstü uygulaması, arka planda çalışır).
  - Alpaca hesabı açılamadı (kayıt sırasında captcha sorunu). Finnhub ücretsiz anahtarı ve SEC e-postası Ayarlar'da
    kayıtlı.
  - Yatırım tavsiyesi verilmez; kâr garantisi iddia edilmez; her sonuç dürüstçe, sınırlarıyla raporlanır.

---

## 2. Mimari

| Parça | Ayrıntı |
|---|---|
| Dil / çalışma | Python 3.14 (Windows'ta `py`), Flask JSON API `127.0.0.1:8765`, pywebview penceresi (Edge WebView2) |
| Arayüz | Saf JavaScript tek sayfa uygulaması (`web/index.html`, `web/app.js`, `web/app.css`), CDN yok |
| Veritabanı | SQLite (WAL). Canlı veri: `%LOCALAPPDATA%\PiyasaMasasi\marketdesk.db`. Test laboratuvarı: aynı klasörde `lab.db` (~600 MB) |
| Başlatma | Masaüstündeki "Piyasa Masası" kısayolu → `pythonw -m marketdesk.main` (konsolsuz). `run.bat` de var |
| Zamanlayıcı | `marketdesk/scheduler.py`: toplayıcılar, kümeleme, fırsat skoru, brifingler, sanal işlem motoru (15 sn), sinyal karnesi (30 dk), test laboratuvarı görevleri |

**Ana modüller (`marketdesk/`):**
- `collectors/` — ~78 kaynak: 52 RSS (Reuters, CNBC, MarketWatch, Yahoo, Benzinga, basın bültenleri, Fed, SEC …),
  Google News sorguları, SEC EDGAR (8-K, Form 4, S-1/424B, 13D), Nasdaq bilanço / halka arz takvimi, ForexFactory
  makro takvim, Yahoo fiyat / hareketliler, CNN Korku-Açgözlülük, Reddit / ApeWisdom, Finnhub, işlem durdurmaları.
- `analysis/` — kural tabanlı sınıflandırma: ticker çıkarma, ~35 kategori (bilanço, FDA, birleşme, analist notu …),
  duygu (sentiment), önem puanı, aynı haberin kümelenmesi (yayıncıya göre "kim ne diyor"), fırsat skoru.
- `briefs.py` / `export.py` — Türkçe kural tabanlı brifingler; Claude Code ile dosya üzerinden köprü (`CLAUDE_BRIEF.md`).
- `scorecard.py` — Sinyal Karnesi.
- `paper.py` — Sanal İşlem motoru.
- `lab/` — Test Laboratuvarı: `download.py`, `bots.py`, `hakem.py`, `sweep.py`, `evolve.py`, `evo_live.py`,
  `intraday.py`, `runner.py`, `store.py`.

**Arayüz sayfaları:** Haberler · Kim Ne Diyor · Fırsatlar · Sinyal Karnesi · Piyasa Panosu · Takvim · Brifingler ·
Hisse · Kaynaklar & Ayarlar · Sanal İşlem · Test Laboratuvarı.

---

## 3. Fırsat skoru (canlı, 0–100)

Son ~24 saatin haberleri ve piyasa verisiyle her hisse için hesaplanır. Bir "al" sinyali değil, **haber ve ilgi
yoğunluğu** ölçüsüdür.
- Haber yoğunluğu (≤ ~60 puan): önem × zaman azalması (6 saatte yarıya) × hissenin haberdeki rolü (konu / başlıkta / özette).
- Farklı yayıncı sayısı (≤ 14), güçlü olay (bilanço, FDA, birleşme, beklenti değişikliği…, ≤ 10).
- Fiyat hareketi (+4 … +15), göreli hacim (+4 / +7), bugün/yarın bilanço (+7 / +4), EPS sürprizi, Reddit / StockTwits, izleme listesi (+4).
- Yön: %75 haber tonu + olay türü, %25 fiyat hareketi → Yükseliş / Düşüş / Karışık.

**Sinyal Karnesi:** Her fırsat sinyali kaydedilir; giriş gününün kapanışından sonra Yahoo 5 dakikalık verisiyle
30 dk / 60 dk / kapanış getirisi, MFE/MAE, SPY'ye göre fark ve botun çıkış kuralı ölçülür. Kayıt 24 Ekim'de başladı,
ilk sonuçlar 26 Ekim kapanışından sonra gelecek.

---

## 4. Sanal İşlem motoru (`paper.py`)

- Başlangıç 100.000 $. Emirler Yahoo fiyatına kayma (varsayılan 5 baz puan) eklenerek dolar; piyasa / limit / stop
  emirleri; uzatılmış seansta yalnızca limit; tam hisse; emir defteri yok.
- Risk limitleri: tek hisse ≤ %20 özsermaye, ≤ 5 pozisyon, kaldıraç yok, günlük %3 zararda yeni pozisyon açılmaz.
- Botlar:
  1. **Fırsat botu** (açık): fırsat skoru ≥ 60, günlük hareket %2–15, göreli hacim ≥ 1,5x, fiyat ≥ 5 $; işlem başına
     %0,5 risk, %2 zarar durdur, %4 kâr al; kapanıştan 5 dk önce kapatır.
  2. **Hakem bot** (kapalı): 4 basit botun oylarından karar verir (aşağıda).
  3. **Evrim botu** (hazır, kapalı): turnuvanın final botlarını açılıştan sonra ~2.600 hissede tarar, en iyi 5
     sinyale girer, pozisyonu kuralın süresi kadar (≤ 5 gün) tutar; kendine ait pozisyon sınırı (10 pozisyon, her biri %10).
- Çok günlü pozisyonlar `exit_at` ile o günün kapanışında kapanır; gün sonu kapatması bu pozisyonlara uygulanmaz.

---

## 5. Test Laboratuvarı — veri

| Veri | Kaynak | Kapsam |
|---|---|---|
| Hisse evreni | Nasdaq screener | Piyasa değeri ≥ 300 M$ ve günlük işlem hacmi ≥ 5 M$ olan **2.655** ABD hissesi (+ SPY, QQQ, IWM, ^VIX, 11 sektör fonu) |
| Günlük fiyat | Yahoo (bölünmeye göre düzeltilmiş) | 2016-10 → 2026-10, 5,77 milyon gün kaydı |
| Bilanço | Nasdaq takvimi (gün gün) | 10 yıl, 87.239 açıklama (EPS gerçekleşen / beklenen / sürpriz, açılış öncesi / sonrası) |
| SEC | EDGAR submissions | 10 yıl, 1,27 milyon bildirim (8-K madde kodları, S-1/S-3/424B4, 13D, Form 4) |
| Haber başlığı | Finnhub (ücretsiz plan) | Yalnızca son ~12 ay, 106 büyük hisse, ~390 bin başlık (aynı kural tabanlı sınıflandırıcıyla etiketlendi) |

Bilinen sınır: **bugün borsada olmayan şirketler veride yok** (ücretsiz delist verisi yok) → "düşeni al" türü
stratejiler olduğundan iyi görünür.

---

## 6. Deneyler ve sonuçlar (kronolojik)

### 6.1 Dört basit bot + hakem (5 yıl, 462 hisse)
- Botlar (karar anı = günün açılışı, yalnızca o ana kadar bilinen veri):
  momentum / kırılım, ortalamaya dönüş (RSI2), açılış boşluğu, olay/haber (bilanço sürprizi, gece 8-K, gece haberi).
- İşlem: açılışta gir; %2 zarar durdur / %4 kâr al / kapanış; aynı gün iki seviyeye de değerse zarar sayılır; kayma dahil.
- Hakem: (a) ağırlıklı oy (botun eğitim getirisine göre), (b) iki lojistik regresyon (yükseliş / düşüş işlemi kazanır mı).
- Test: son 2 ay hiç görülmemiş + son 12 ay 2'şer aylık kayan pencere (yalnızca öncesiyle eğitilmiş).
- **Sonuç:** 4 bot da her yıl masraf sonrası zararda. Ağırlıklı oy hiç işlem açmadı (kârlı bot yok). Öğrenen hakem
  test döneminde ~0 (−%0,01/işlem), kayan pencerede −%0,06/işlem. SPY'yi elde tutmak daha iyiydi.

### 6.2 Toplu deneme (24 çıkış kuralı × tutma süresi)
- 8 çıkış kuralı (%1–5 sabit, 0,5–1,5×ATR, zarar durdursuz) × 1/3/5 gün. 5/24 ayar "tutarlı".
- 36 aylık ek kontrol: iyi görünen ayarların kazancı büyük ölçüde **piyasanın yükselişinden** geliyordu.
- Ders: her sonuç, aynı çıkış kuralıyla "her hisseyi körlemesine almak" ile karşılaştırılmalı.

### 6.3 Evrim turnuvası (genetik algoritma) — ana deney
- **Genom:** 1–3 giriş koşulu (31 gösterge: açılış boşluğu, RSI2/14, getiriler, ortalamalara / 52 hafta zirve-dibe
  uzaklık, art arda düşüş, SPY'ye / sektöre göre güç, piyasa genişliği, VIX, bilançoya kalan gün, EPS sürprizi, 8-K
  puanı, haber tonu, işlem hacmi, haftanın günü, ay başı/sonu) + yön + çıkış kuralı (sabit % / ×ATR / yok; oran;
  1–5 gün).
- **Havuz:** 80 bot; elit 6 aynen klonlanır, turnuva seçimi, çaprazlama, mutasyon, %5 rastgele yeni bot. 4 basit bot
  ve "düşeni al" kuralı başlangıç havuzunda.
- **Uygunluk:** gerçek net getirinin **günlük kümelenmiş** t-istatistiği (aynı günün işlemleri tek gözlem) × pozitif
  yıl oranı − karmaşıklık cezası. Hayatta kalmak için ayrıca piyasa sürüklenmesini (aynı yön ve çıkışla rastgele giriş)
  geçmek, en az 40 işlem / 15 farklı gün ve yılların ≥ %60'ında artı olmak gerekir.
- **Aşamalı turnuva (kullanıcının fikri):** hisseler 5 sabit gruba (A–E) bölünür. Aşama 1: A'da üret → B'de sına
  (hiç görülmemiş) → kazananlar klonlanıp A+B'de geliştirilir → C'de sınanır → … → E. Her aşamada iç doğrulama yılı
  15 nesil gelişmezse durur (ezber önlemi). **Kilitli kasa:** son 6 ay, tüm hisseler, sadece en sonda bir kez açılır.
- **Gerçekçilik:** kayma likiditeye göre taraf başına %0,03–0,20; test sınırlarında "purge" (çok günlük işlemler
  sınırı aşmasın); fiyat yolları baz puan hassasiyetinde.
- **Bulunan ve düzeltilen hatalar:**
  1. Uygunluk yalnızca "rastgeleye göre fark" iken evrim, düşük oynaklıklı hisselerde **zarar eden ama rastgeleden
     az zarar eden** botları seçti → uygunluk gerçek net kâra çevrildi.
  2. Fiyat yolları float16 ile ~%0,1 adımlara yuvarlanıyordu → int16 baz puan.
  3. Çöküş günlerinde yüzlerce hisseye birden giren kurallar binlerce "bağımsız" işlem gibi görünüyordu → günlük kümeleme.
  4. Çok günlü tutmalarda test sınırında etiket sızıntısı → purge.
- **Sonuç (10 yıl, 2.579 hisse, 3 bağımsız turnuva, ~18 bin bot):** üç turnuvanın üçü de aynı kural ailesini buldu:
  **"Korku günü (VIX > ~20) + aşırı satış (art arda düşüş, aşağı açılış, düşük RSI, zirveden uzak) → al, 1,5–2×ATR
  zarar durdur / kâr al, en fazla 5 gün tut."** Görülmemiş hisse sınavlarında ~680–740 farklı günde işlem başına net
  +%0,5–0,8, 2018–2026 yıllarının neredeyse hepsinde artı; kazancın ~yarısı zamanlama, ~yarısı hisse seçimi.

### 6.4 Portföy simülasyonu (günde ≤ 5 yeni işlem, her biri %10, ≤ 10 pozisyon, günlük değerleme)

| Dönem | Evrim botları | Basit "düşeni al" | SPY al-tut |
|---|---|---|---|
| Görülmemiş hisseler (grup E), 2017-08 → 2026-04 | yıllık **%22,0**, en büyük düşüş −%34,7, Sharpe 0,90, zamanın %35'i yatırımda | yıllık %11,5, düşüş −%54,9 | yıllık %13,0, düşüş −%34,1, Sharpe 0,74 |
| Kilitli kasa (son 6 ay, tüm hisseler) | **−%1,9** (69 işlem; VIX nadiren 20'yi aştı) | +%12,5 | +%7,3 |

Diğer iki turnuvada görülmemiş hisse portföyü: yıllık %22,7 (düşüş −%36,8) ve %25,9 (düşüş −%47,1).

**Önemli yorum:** "Hangi günlerde girilir" (VIX > 20) bilgisi, görülmemiş hisselerle **aynı yıllardan** öğrenildi;
zaman açısından tek bağımsız kanıt kilitli kasa ve o dönem sakin geçtiği için onay yok.

### 6.5 Gün içi denetim (son 60 gün, 5 dakikalık veri, 120 hisse, 7.200 gün)
Günlük veride "aynı gün iki seviyeye de değdiyse zarar say" varsayımının etkisi: 1,5–2×ATR seviyelerinde belirsiz
gün oranı ~%0,01 (etki yok); 0,5×ATR'de %2,5 (gerçekte ~%47'sinde önce kâr al; işlem başına +0,03 puan).

### 6.6 Haber dönemi kontrolü
Haberler yalnızca son ~12 ayda olduğu için evrim haberden öğrenemedi. Bu dönemde final botların işlemleri gece haberi
olan günlerde (olumlu ya da olumsuz) işlem başına +%1–2, haber olmayan günlerde +%0,2–1,2 (örnekler küçük: 50–200).
Uygulama haber toplamaya devam ettikçe bu analiz güçlenecek.

---

## 7. Şu anki durum (26 Ekim 2026)
- Uygulama çalışıyor; sanal hesap 100.000 $, açık pozisyon yok. Fırsat botu açık, hakem ve evrim botu kapalı.
- 3 turnuvanın birleşik 40 final botu `lab.db` → `kv.evo_survivors` içinde (mutlak eşiklerle); canlı bot bunları kullanır.
- 23 Ekim kapanışında VIX 19,45 (evrim botları çoğunlukla 20 üstünü bekler).
- Lab verisi her işlem günü kapanıştan sonra otomatik güncellenir.

## 8. Önerilen sonraki adımlar
1. Evrim botunu Sanal İşlem'de açıp **ileriye dönük** (yanılgısız) test etmek; korku günlerinde gerçek davranışı izlemek.
2. Sinyal Karnesi'nin birikmesini beklemek (fırsat skoru gerçekten işe yarıyor mu).
3. Haber geçmişi uzadıkça (veya Alpaca haber API'si ile 2015'e kadar) evrime haber göstergelerini dahil etmek.
4. Hakem testini ve toplu denemeyi yeni (10 yıl, 2.600 hisse) veriyle yeniden çalıştırmak; evrim botlarını hakeme üye yapmak.
5. Delist olmuş şirket verisi bulunursa hayatta kalan şirket yanılgısını azaltmak.
6. Gerçek parayla işleme ancak uzun süreli sanal başarıdan sonra ve aracı kurum API'si (ör. Interactive Brokers) ile
   geçilebilir; şu an böyle bir bağlantı **yok**.

## 9. Ana API uç noktaları (yerel)
- Haber masası: `/api/status`, `/api/news`, `/api/clusters`, `/api/opportunities`, `/api/market`, `/api/calendar`,
  `/api/briefs`, `/api/signals`, `/api/scorecard`.
- Sanal işlem: `GET /api/paper`, `POST /api/paper/order|cancel|close|position|settings|reset`, `GET /api/paper/quote`.
- Laboratuvar: `GET /api/lab`, `POST /api/lab/download|stop|test|sweep|evolve` (`{"runs": 3}`), `POST /api/lab/audit`,
  `POST /api/lab/today` (hakem), `POST /api/lab/evo_today` (evrim sinyalleri, emir açmaz).

## 10. Bu projede çalışacak yapay zekâ için kurallar
- Gerçek para ile işlem yapma, aracı kuruma emir gönderme, hesap açma, şifre / API anahtarı girme yok.
- Yatırım tavsiyesi verme; sonuçları sınırlarıyla (hayatta kalan şirket yanılgısı, tek dönem, örnek sayısı) anlat.
- Geçmiş veride "mükemmel" sonuca kadar arama yapma; her iddiayı hiç görülmemiş veriyle (zaman ve hisse) sına,
  sınav verisini seçim için kullanma.
- Arayüz metinleri Türkçe; kod yorumları İngilizce; veri klasörü OneDrive dışında (`%LOCALAPPDATA%\PiyasaMasasi`).
