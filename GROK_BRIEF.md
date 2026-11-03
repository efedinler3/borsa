# Piyasa Masası — Grok için devir brifi

Tarih: 3 Kasım 2026. Proje klasörü: `C:\Users\sefed\OneDrive\Masaüstü\trade`.
Bu belge, Claude ile geliştirilen projenin **amacını, bugüne kadar yapılanları, sonuçları ve açık soruları**
anlatır. Daha eski ve daha ayrıntılı geçmiş için `GEMINI_BRIEF.md` (26 Ekim) ve `SPEC.md` dosyalarına bakın;
bu belge onlardan sonrasını ve bugünkü durumu kapsar.

---

## 1. Amaç

Kullanıcı Türkiye'de yaşayan bireysel bir yatırımcı; yazılımcı değil. Arayüz ve tüm metinler **Türkçe**.

Asıl hedef: **kendi kendine öğrenen, dallara ayrılmış bir bot ağacı.**
- Botlar hiç görmedikleri veride de kazanabilen alım kurallarını geçmiş veriden öğrenmeli.
- Canlıda (sanal parayla) her gün işlem yapmalı.
- Her gece zayıf olanlar elenip iyiler klonlanarak gelişmeye devam etmeli.
- İki pazar var:
  - **ABD hisseleri:** 100 bot.
  - **Kripto:** 40 bot, 7/24 çalışıyor.
- Kullanıcı hepsini uygulamanın **Laboratuvar** sayfasından dal dal izliyor.

## 2. Değişmez kurallar

- **Yalnızca sanal para.** Hiçbir aracı kuruma ya da borsaya emir gönderilmez. Hesap açılmaz; şifre, API anahtarı
  veya token girilmez. Kripto tarafı Binance'in yalnızca **herkese açık** fiyat uçlarını kullanır.
- Yatırım tavsiyesi verilmez. Kâr garantisi iddia edilmez.
- Her sonuç dürüstçe ve sınırlarıyla raporlanır: örnek sayısı, tek dönem, hayatta kalan şirket yanılgısı gibi.
- Geçmişte "mükemmel" sonuç aranmaz. Her iddia hiç görülmemiş veriyle sınanır. Sınav verisi seçim için kullanılmaz.
- Ücretli yapay zekâ API'si yok. Botlar yerel çalışır (numpy). Veri kaynakları ücretsiz.
- Kullanıcının programlarını (ör. Blender) kapatmak kullanıcının kararıdır.
- Veritabanından veri silmeden önce kullanıcıya sorulur.
- Saatler "Türkiye saati / New York saati" diye yazılır.
- Kod yorumları İngilizce, arayüz metinleri Türkçe.

## 3. Mimari (kısa)

| Parça | Ayrıntı |
|---|---|
| Çalışma | Python 3.14 (`py`), Windows 11, ~7 GB RAM (bellek sık sık sınırda) |
| Sunucu | Flask, `127.0.0.1:8765`; `pythonw -m marketdesk.main --no-window` |
| Arayüz | `web/index.html`, `web/app.js`, `web/app.css` (saf JS, CDN yok) |
| Veri klasörü | `%LOCALAPPDATA%\PiyasaMasasi\` (OneDrive dışında): `marketdesk.db` (canlı), `lab.db` (~600 MB, 10 yıl hisse), `crypto.db` (kripto) |
| Zamanlayıcı | `marketdesk/scheduler.py`: hisse filosu dakikalık `tick`, kripto döngüsü her 60 sn (`_run_crypto`) |

**Bot ağacıyla ilgili dosyalar:**
- **Hisse:**
  - `marketdesk/lab/tree.py`: 10 yıllık gün gün yürüyüş ve finaller.
  - `marketdesk/lab/longpath.py`: 1–90 günlük tutmalarda stop, hedef ve süre sonucu.
  - `marketdesk/fleet.py`: 100 botun canlı sanal hesapları.
- **Kripto (`marketdesk/crypto/`):**
  - `cstore.py`: `crypto.db` tabloları.
  - `data.py`: Binance REST ve data.binance.vision arşivi; Korku-Açgözlülük endeksi (alternative.me).
  - `cpool.py`: 29 göstergeli veri havuzu.
  - `ctree.py`: kural ve sinir ağı dallarının yürüyüşü.
  - `cfleet.py`: canlı filo.
  - `jobs.py`: arka plan işleri.
- **Ortak yardımcılar:**
  - `marketdesk/lab/evolve.py`: `STOP_SET` (stop seçenekleri), `RATIO_SET` (hedef oranları).
  - `marketdesk/lab/neuro.py`: istatistik ve mutasyon.
  - `marketdesk/lab/learn.py`: kural maskesi.
- **API uçları:**
  - Hisse: `GET /api/fleet`, `POST /api/lab/tree`.
  - Kripto: `GET /api/crypto/fleet`, `POST /api/crypto/tree`, `POST /api/crypto/download`.

## 4. Ağacın tasarımı (iki pazarda ortak)

- **İki dal:**
  - **kural:** 1–3 koşullu kurallar, genetik evrimle bulunur.
  - **sinir:** küçük sinir ağları (hissede gösterge sayısı → 12 nöron → 1 çıkış; kriptoda 29 → 12 → 1).
    Ağırlıklar evrimle aranır, gradyan kullanılmaz.
- **Beş grup:**

| Grup | Hisse tutma süresi | Kripto tutma süresi | Not |
|---|---|---|---|
| gunluk | 1–2 gün | 1–2 gün | her gün işlem yapmaya zorlanır (kapsama cezası) |
| korku | 1–5 gün | 1–7 gün | hisse: VIX ≥ 20; kripto: dünkü Korku-Açgözlülük ≤ 30 |
| haftalik | 5 gün | 7 gün | |
| aylik | 20 gün | 30 gün | |
| serbest | 1–90 gün | 1–90 gün | süreyi kendi seçer, stop zorunlu |

- **Genom:**
  - Giriş koşulları, ya da sinir ağında ağırlıklar.
  - Sıralama ölçütü: aynı gün çok sinyal varsa hangisi önce alınır.
  - Stop: `STOP_SET` = sabit %1/%2/%3/%5, 0,5/1/1,5/2×ATR; ağaçta "stopsuz" seçeneği yok.
  - Hedef oranı: 1, 1,5, 2 ya da 3 × stop.
  - Tutma süresi.
- **Eğitim (gün gün yürüyüş):** Her gün sırası gelen grupta botlar yalnızca o güne kadar kapanmış işlemleriyle
  puanlanır. En kötü bot ölür, iyilerden çaprazlama/mutasyonla yenisi doğar.
  - Uygunluk: 0,45 × t(getiri) + 0,55 × t(rastgele girişe göre fazla getiri); zarara, az işleme ve düşük kapsamaya
    ceza var.
  - Varlıkların %20'si (grup 4) **sınav** olarak ayrılır; eğitimde görülmez.
- **Final:** Her grupta sınavı geçen en iyi botlar alınır. Geçen az çıkarsa boş yerler "en iyi 3'ün klonu" ile
  doldurulur. Bu klonlar sınavı geçmemiştir.
- **Canlı öğrenme (her gece):** Her botun canlı işlemleri, eğitim sonucuna doğru küçültülerek puanlanır.
  Her grupta yeterince kanıtı olan (≥ 8 işlem, ≥ 10 gün) ve puanı eksi olan en zayıf bot emekli olur. Yerine en iyi
  3'ten birinin klonu gelir.

## 5. Hisse ağacı (100 bot)

- **Hesaplar:** Grup başına 10 bot. 2. ve 5. sıradakiler $100 hesapla (kesirli hisse), diğer 8'i $100.000 hesapla
  işlem yapar.
- **Kurulum:** 2 Kasım 2026, 21:27 Türkiye saatinde, 19 dakikada. Veri 2017-08 → 2026-10, yaklaşık 2.600 hisse.
  Eski botlar (fırsat, hakem, evrim) silinmeden **kapatıldı**.
- **Günlük işleyiş:** Alış 09:35–10:30 New York saatinde (17:35–18:30 Türkiye saati). Çıkışlar her dakika
  kontrol edilir (stop, hedef, süre).
- **Durum:** İlk canlı alış 3 Kasım açılışında bekleniyor; brif yazılırken henüz alış yok.

Sınavı geçen bot sayısı ve şampiyonun geçmiş kaydı:

| Dal / grup | Geçen / 10 | Şampiyon günlük % | t (rastgeleye göre) |
|---|---|---|---|
| kural gunluk | 3 | +0,047 | −0,27 |
| kural korku | 6 | +0,675 | −0,10 |
| kural haftalik | 5 | +0,45 | **+1,61** |
| kural aylik | 3 | +0,35 | −1,86 |
| kural serbest | 5 | +0,615 | +0,78 |
| sinir gunluk | 2 | +0,296 | **+2,66** |
| sinir korku | 3 | −0,008 | −2,43 |
| sinir haftalik | 3 | +0,236 | +0,50 |
| sinir aylik | 3 | +0,292 | −2,38 |
| sinir serbest | **0** | +1,492 | −0,35 |

**Yorum:**
- Mutlak getiriler çoğunlukla artı. Ama piyasanın kendi yükselişini (rastgele giriş) belirgin şekilde geçen
  grup az: yalnızca sinir gunluk (t 2,66) ve kural haftalik (t 1,61).
- 100 botun 63'ü sınavı geçmemiş dolgu klonu.

**Önceki önemli bulgular (10 yıl, hisse):**
- 3 bağımsız evrim turnuvası aynı aileyi buldu: "Korku günü (VIX > ~20) + aşırı satış → al, 1,5–2×ATR stop/hedef,
  ≤ 5 gün". Görülmemiş hisselerde işlem başına +%0,5–0,8.
- Hiçbir çıkış değişikliği (kâr kilidi, iz süren stop, erken zarar kesme) düz çıkışı geçmedi. Avantaj, geri
  çekilmelerde pozisyonu tutmaktan geliyor.
- Giriş filtresi ATR > %5,7, kilitli kasada işlem başına +%3,6'yı +%6,3'e çıkardı. Kasa artık harcandı; bu
  ancak ileriye dönük test edilebilir.
- Opus'a kör sınav: korku günlerinde iyi (+%1,74), sakin günlerde kötü (−%1,09). Avantaj seçicide değil,
  rejimde (korku günleri).
- Bilinen yanılgı: hisse verisinde **borsadan çıkmış şirketler yok** (hayatta kalan şirket yanılgısı).

## 6. Kripto ağacı (40 bot)

- **Kullanıcının seçimleri:**
  - 20 kural + 20 sinir botu; aynı 5 grup, grup başına 4 bot.
  - Hesaplar hissenin tersi: grup başına 1. sıra $100.000, 2–4. sıra **$100**. Kullanıcının sözleriyle "100 dolarla
    maksimum kâr amaç".
- **Evren:** Günün 20 günlük işlem hacmine göre en büyük 150 USDT paritesi.
- **Veri:**
  - Kapsam: 2018-02-02 → 2026-10-31, 597 coin. Data.binance.vision arşivinden **borsadan çıkmış coinler dahil**.
  - Hariç tutulanlar: stabil coinler, kaldıraçlı tokenler, Binance'in tokenize hisseleri.
  - Mum geçmişinde 2 günden uzun boşluk varsa geçmiş bölünür (LUNA → Terra 2.0 gibi). Her parça ayrı bir coin sayılır.
  - Göstergeler: önceki günün mumu → 00:00 UTC açılışında giriş.
  - Haber kullanılmıyor. Kripto haberlerinin ücretsiz geçmişi bulunamadı.
- **Maliyet:** taraf başına %0,1 komisyon + hacme göre %0,02–0,2 fark.
- **Canlı işleyiş:**
  - Hesaplar:
    - $100k hesaplar: coin başına %10, en fazla 10 coin.
    - $100 hesaplar: en fazla 2 coin, her biri paranın yarısı.
  - Günlük tur: 00:05 UTC (03:05 Türkiye saati). Sırası: veri güncelle → süresi dolanları sat → gece öğrenmesi →
    alış. 3 saatten fazla gecikirse o gün alış yapılmaz.
  - Çıkışlar her dakika kontrol edilir. Fiyatı bir günden uzun gelmeyen coin son fiyattan satılır.
- **Eğitim sonucu:** 1.196 sn sürdü. **40 bottan yalnızca 5'i sınavı geçti**, kalan 35 dolgu klonu.

| Dal | gunluk | korku | haftalik | aylik | serbest |
|---|---|---|---|---|---|
| kural (geçen / dolgu) | 1 / 3 | 1 / 3 | 1 / 3 | 0 / 4 | 0 / 4 |
| sinir (geçen / dolgu) | 0 / 4 | 1 / 3 | 0 / 4 | 0 / 4 | 0 / 4 |

  10 grubun 9'unda şampiyonun geçmiş kaydı bile eksi; yalnızca sinir aylik artı (+%0,233/gün).
- **Canlı durum (3 Kasım 14:00 Türkiye saati):**
  - 2 Kasım'da 17 bot 40 alış yaptı. 3 Kasım'da 9 alış daha oldu.
  - $100k hesaplar toplam $996.931 / $1.000.000. $100 hesaplar $2.955 / $3.000.
  - Tek kapanmış işlem: ZEN, stopla −%5,5.
  - Aynı gruptaki klonlar birebir aynı coinleri aldı; çeşitlilik çok düşük.

### 6.1 Stop genişliği deneyi (3 Kasım) — `deneyler/stop_test.py`, sonuç `deneyler/stop_test_result.json`

**Soru:** Kullanıcı stopların neden bu kadar geniş olduğunu sordu. Stoplar canlıda %14–20 civarında; altcoinlerde
2×ATR, medyan günlük ATR %9,5. Stop eğitimde öğrenilen bir gen. 40 botun 22'si en geniş seçenek olan 2×ATR'yi seçmiş.

**Yöntem:**
- Her botun alışları birebir aynı tutuldu; yalnızca stop 9 seçenek arasında değiştirildi (8 + stopsuz).
- Hedef = stop × botun kendi oranı.
- Sonuçlar üç ayrı bölümde ölçüldü: eğitim coinleri, sınav coinleri ve son 365 gün.
- Ayrıca rastgele giriş yapan bir karşılaştırma (günde 5 rastgele coin) çalıştırıldı.

**Sonuç (botlar arası medyan, işlem başına net %):**

| Stop | Eğitim | Sınav | Son 365 gün |
|---|---|---|---|
| %1 | −0,96 | −0,98 | −0,98 |
| %2 | −1,14 | −1,26 | −1,18 |
| %3 | −1,14 | −1,23 | −1,32 |
| %5 | −0,90 | −0,92 | −1,28 |
| 0,5×ATR | −0,43 | −0,64 | −0,91 |
| 1×ATR | −0,35 | −0,75 | −1,05 |
| 1,5×ATR | −0,16 | −0,71 | −1,20 |
| 2×ATR | −0,02 | −0,68 | −1,44 |
| stopsuz | −0,70 | −0,61 | −5,21 |

- **Hiçbir stop genişliği botları kâra geçirmiyor.** Sorun stopta değil, giriş seçiminde.
- Dar sabit stoplar (%1–3) her yerde en kötü sonucu veriyor. Kripto günlük oynaklığında sürekli stopa çarpıyorlar,
  üstüne her seferinde komisyon ödeniyor.
- Son 365 günde 1×ATR, 40 botun 22'sinde botun kendi stopundan daha iyi. 2×ATR yalnızca 2'sinde daha iyi.
  Hafif bir daraltma kaybı azaltabilir ama kâra çevirmez.
- Rastgele girişte stopsuz tutmak en iyisi: 30 gün tutmada işlem başına +%2,9. Bu piyasanın genel yükselişi.
  Stop eklemek rastgele girişleri her durumda kötüleştiriyor.
- **Bu sonuç bağımsız olarak doğrulanmadı.** Kullanıcı stopları değiştirme kararını henüz vermedi; botlarda
  hiçbir şey değiştirilmedi.

## 7. Açık konular ve sorular (Grok'tan görüş istenenler)

1. **Kripto girişleri:** Eğitim kriptoda kalıcı avantajlı bir giriş kuralı bulamadı. Rastgele giriş + stopsuz
   tutma, botlardan iyi. Hangi göstergeler, rejim filtreleri veya tutma yapıları denenmeli? Sınav kurgusu
   (coinlerin %20'si sınav, zaman dilimi yok) yeterli mi?
2. **Dolgu klonları:** Sınavı geçmeyen klonlarla grup doldurmak mı daha iyi, yoksa grubu boş/küçük bırakıp nakitte
   beklemek mi? Aynı gruptaki klonlar aynı coinleri alıyor.
3. **$100 hesaplar:** 2 coinlik defterle işlem yapıyorlar ama eğitim bunu ayrıca sınamadı (eğitim günde 5 seçim,
   10 pozisyon). Kullanıcı bunu "şimdilik kalsın" dedi.
4. **Hisse ağacı:** 100 botun 63'ü dolgu klonu. Rastgeleye göre anlamlı fark yalnızca 2 grupta var. Canlı
   öğrenmenin (gece eleme) bu durumu düzeltmesi ne kadar sürer, eşikler (≥ 8 işlem, ≥ 10 gün) makul mü?
5. **Hayatta kalan şirket yanılgısı** (hisse): ücretsiz delist verisi kaynağı var mı?
6. **Bellek:** Hisse ağacı iki kez bellek yetmediği için çöktü. Kurulum artık her bitmiş dalı kaydediyor
   (`lab.db` kv `tree_partial`).

## 8. Bu projede çalışacak yapay zekâ için

- Gerçek emir, hesap açma, şifre/anahtar girme yok. Yatırım tavsiyesi yok.
- Bir değişiklik önermeden önce onu **net getiri değişikliği** olarak, hiç görülmemiş veride ve rastgele girişe göre
  ölç.
- Canlı botların genlerini, `crypto.db` / `marketdesk.db` / `lab.db` verilerini kullanıcıya sormadan değiştirme
  ya da silme.
- Kullanıcıya Türkçe, sade ve dürüst yaz. Kullanıcı yazılımcı değil; belirsiz bir istek gelirse önce kendi
  anladığını söyleyip kısa bir soru sor.
