# Piyasa Masası

ABD borsasında gün içi işlem yapanlar için Windows masaüstü uygulaması. Onlarca kaynaktan (CNBC, Reuters,
WSJ, Bloomberg, MarketWatch, basın bülteni servisleri, SEC, Fed, FDA, Google News, Reddit, StockTwits,
Alpaca, Finnhub…) haberleri toplar, **her kaynağı ayrı gösterir** ("kim ne diyor"), haberleri yerelde
etiketler ve puanlar, fırsat listesi çıkarır, makro/bilanço takvimini ve piyasa panosunu gösterir,
sabah / öğle / akşam Türkçe brifing yazar.

- Ücretli yapay zekâ API'si **yoktur**. Analiz kural tabanlıdır ve bilgisayarınızda çalışır.
- İsteğe bağlı eklentiler: **Ollama** (yerel dil modeli), **FinBERT** (yerel duygu modeli) ve
  **Claude Code** (kendi Claude aboneliğinizle zamanlanmış brifing).
- Tüm veriler tek bir SQLite veritabanında tutulur; ileride yazılacak gün içi işlem botu aynı veriyi okuyabilir.

> **Uyarı:** Bu uygulama bir bilgi ve haber toplama aracıdır. Gösterilen skorlar, fırsatlar ve brifingler
> otomatik üretilir, hatalı veya eksik olabilir. **Yatırım tavsiyesi değildir.** Alım-satım kararlarınızın
> sorumluluğu size aittir.

---

## Kurulum

Gerekenler: Windows 10/11, Python 3.12+ (`py` komutu), Microsoft Edge WebView2 (Windows 11'de hazır gelir).

```
cd "C:\Users\sefed\OneDrive\Masaüstü\trade"
py -m pip install -r requirements.txt
```

## Çalıştırma

| Nasıl | Ne yapar |
|---|---|
| `run.bat` (çift tıklayın) | Uygulamayı masaüstü penceresinde açar. Pencere kapanınca uygulama da kapanır (arka planda çalışan bir kopya varsa yalnızca ona pencere açar). |
| `run_server_only.bat` | Yalnızca arka plan sunucusu; tarayıcıda `http://127.0.0.1:8765/` adresini açın. Ctrl+C ile kapatın. |
| `py -m marketdesk.main` | `run.bat` ile aynı. |
| `py -m marketdesk.main --no-window --port 8799` | Pencere açmadan, belirli portta çalıştırır. |
| `py -m marketdesk.main --edge` | Pencere için pywebview yerine Microsoft Edge "uygulama modu" kullanır. |
| `py -m marketdesk.main --browser` | Varsayılan tarayıcıda açar. |
| `py -m marketdesk.main --kisayol` | Masaüstü kısayolu + Windows açılışında **arka planda** çalıştıran başlangıç kısayolu oluşturur (aşağıya bakın). |
| `py -m marketdesk.main --masaustu-kisayol` | Yalnızca masaüstüne simgeli `Piyasa Masası` kısayolu koyar: çift tıklayınca konsol penceresi olmadan uygulama penceresi açılır (Windows açılışında otomatik başlatma yok). |
| `py -m marketdesk.main --kisayol-kaldir` | Bu iki kısayolu kaldırır. |
| `py -m marketdesk.main --kapat` | Çalışan (ör. arka plandaki) uygulamayı tamamen kapatır. |

- Pencere sırası: pywebview (Edge WebView2) → olmazsa Edge uygulama penceresi → olmazsa varsayılan tarayıcı.
- Haber başlıklarındaki bağlantılar sistem tarayıcınızda açılır.
- Aynı veri klasörü için yalnızca **tek kopya** çalışır (`instance.lock`). Uygulama zaten açıksa ikinci kez
  başlatmak (ör. `run.bat`'a iki kez tıklamak) yeni bir sunucu açmaz, çalışana pencere açar.
- Ayarlardaki port (varsayılan 8765) başka bir program tarafından kullanılıyorsa sonraki birkaç porttan
  biri, o da olmazsa boş bir port seçilir; kullanılan port `instance.json` dosyasına yazılır. Hiçbir port
  açılamazsa uygulama Türkçe bir hata mesajıyla durur.
- Siyah konsol penceresi günlük (log) satırlarını gösterir; kapatırsanız uygulama da kapanır.

Açılıştan sonraki ~20 saniye içinde tüm kaynaklar ilk kez çekilir; sonra her kaynak kendi aralığında
(1 dk – 1 saat) otomatik yenilenir. Hata veren kaynak bir sonraki turda tekrar denenir (art arda
hatalarda bekleme süresi uzar, en fazla 30 dk).

Klavye: `1`–`9` sekmeler, `/` arama kutusu.

### Arka planda çalıştırma (isteğe bağlı)

Pencere kapanınca veri toplama da durur; uygulama kapalıyken zamanlanmış brifingler yazılmaz (4 saatten fazla
geciken brifing atlanır) ve gece haberleri kaçabilir. Sürekli toplamak için bir kez şunu çalıştırın:

```
py -m marketdesk.main --kisayol
```

- **Başlangıç klasörüne** `Piyasa Masası (arka plan)` kısayolu eklenir: Windows'ta oturum açınca uygulama
  pencere ve konsol açmadan (`pythonw -m marketdesk.main --no-window`) arka planda çalışır.
- **Masaüstüne** `Piyasa Masası` kısayolu eklenir: arka plandaki uygulamaya pencere açar. Bu pencereyi
  kapatmak veri toplamayı **durdurmaz**.
- Arka plandaki uygulamayı tamamen kapatmak için: `py -m marketdesk.main --kapat` (ya da Görev
  Yöneticisi'nde `pythonw` işlemi). Kısayolları kaldırmak için: `py -m marketdesk.main --kisayol-kaldir`.
- Bu seçenek isteğe bağlıdır; `run.bat` her zamanki gibi çalışır (pencere kapanınca uygulama da kapanır).

## Veriler nerede?

Veriler OneDrive **dışında** tutulur (SQLite + OneDrive senkronizasyonu kilitlenme sorunu çıkarır, API
anahtarlarınız da buluta gitmez):

```
%LOCALAPPDATA%\PiyasaMasasi\          (ör. C:\Users\sefed\AppData\Local\PiyasaMasasi)
  marketdesk.db        SQLite veritabanı (haberler, takvim, anlık görüntüler, brifingler, kaynak durumu)
  config.json          Ayarlar ve API anahtarları (yalnızca bu bilgisayarda)
  marketdesk.log       Günlük dosyası (sorun giderirken bakın; 5 MB'ta döner, en fazla 3 yedek: .log.1–.log.3)
  instance.lock, instance.json   Tek kopya kilidi ve çalışan kopyanın portu/PID'i
  symbols.json         ABD hisse sembolü listesi (nasdaqtrader.com, haftada bir yenilenir)
  claude\girdi\        Claude Code için dışa aktarılan veri paketleri (latest.md + tarihli dosyalar)
  claude\cikti\        Claude Code'un yazdığı brifingler (uygulama dakikada bir içe alır)
  webview\, edge-profile\   Pencere tarayıcı profili (filtre/sekme tercihleri)
```

Farklı bir klasör kullanmak için `MARKETDESK_DATA` ortam değişkenini ayarlayın. Haberler ve takvim 21 gün,
brifingler 90 gün, fırsat sinyali geçmişi (`signals`) 180 gün saklanır (günlük otomatik temizlik). Ayarları sıfırlamak için uygulama kapalıyken `config.json`
dosyasını silin.

## API anahtarları (isteğe bağlı, ücretsiz)

Anahtar gerektiren kaynaklar, anahtar girilene kadar **"Ayar gerekli"** durumunda bekler; uygulamanın geri
kalanı anahtarsız da çalışır. Hesapları **kendiniz** açmalısınız; anahtarları **Kaynaklar & Ayarlar →
API anahtarları** bölümüne girin. Anahtarlar yalnızca `config.json` dosyasında saklanır ve arayüzde bir daha
gösterilmez (yalnızca "girildi" bilgisi görünür).

- **Alpaca** — Benzinga kaynaklı gerçek zamanlı haber akışı, günün hareketlileri, izleme listesi fiyatları.
  Ücretsiz kayıt: <https://app.alpaca.markets/signup> → giriş yaptıktan sonra **Paper Trading** hesabında
  *API Keys* → *Generate New Keys*. "API Key ID" ve "Secret Key" değerlerini girin. Ücretsiz planda fiyat
  verisi IEX borsasındandır.
- **Finnhub** — genel piyasa ve şirket haberleri, birleşme haberleri, bilanço takvimi.
  Ücretsiz kayıt: <https://finnhub.io/register> → panodaki *API Key* değerini girin
  (ücretsiz plan: dakikada 60 istek).

### SEC e-posta adresi neden gerekli?

SEC EDGAR (8-K, Form 4, S-1/424B, 13D/13G, 6-K bildirimleri, SEC duyuruları) hesap istemez; ancak SEC'in
"adil erişim" kuralı her isteğin `User-Agent` başlığında **ad ve e-posta** bulunmasını zorunlu tutar,
aksi halde istekler engellenir (HTTP 403). Ayarlar'daki **SEC e-posta** alanına örneğin
`Ad Soyad ad@ornek.com` yazın (yalnızca e-posta yazarsanız başına "PiyasaMasasi" eklenir). Bu bilgi
yalnızca SEC'e gönderilir. Uygulama SEC'e saniyede 10 istekten az gönderir.

## İsteğe bağlı: Ollama (yerel dil modeli ile brifing)

1. <https://ollama.com/download> adresinden Ollama'yı kurun.
2. Komut satırında modeli indirin: `ollama pull qwen2.5:7b` (~4,7 GB; Türkçesi iyi, 16 GB RAM önerilir).
3. Ollama arka planda çalışırken uygulama onu kendiliğinden bulur (`http://127.0.0.1:11434`).
   Ayarlar'da model adını boş bırakırsanız yüklü ilk model kullanılır.

Brifing saatlerinde kural tabanlı brifingin yanında bir de **Ollama** brifingi yazılır; Brifingler
sekmesinden elle de üretebilirsiniz. Yerel modeller hata yapabilir; rakamları kaynaktan doğrulayın.

## İsteğe bağlı: FinBERT (yerel finans duygu modeli)

```
py -m pip install transformers torch
```

Ardından Ayarlar'da **FinBERT kullan** seçeneğini açın. İlk kullanımda `ProsusAI/finbert` modeli
(~450 MB) Hugging Face'ten indirilir. Kurulu değilse uygulama sözlük tabanlı duygu analiziyle devam eder.

## Claude Code brifing köprüsü

Claude aboneliğiniz varsa daha derin bir Türkçe brifingi Claude Code'a yazdırabilirsiniz (ek API ücreti yok):

1. Uygulama her brifing saatinde veri paketini `claude\girdi\latest.md` dosyasına yazar
   (Brifingler → **Claude için dışa aktar** düğmesiyle istediğiniz an da yazdırabilirsiniz).
2. Claude Code'da **her brifing türü için ayrı bir zamanlanmış görev** oluşturun; istemde türü yazın:
   - `C:\Users\sefed\OneDrive\Masaüstü\trade\CLAUDE_BRIEF.md dosyasını oku ve adımlarını tür = sabah için eksiksiz uygula.`
   - `C:\Users\sefed\OneDrive\Masaüstü\trade\CLAUDE_BRIEF.md dosyasını oku ve adımlarını tür = ogle için eksiksiz uygula.`
   - `C:\Users\sefed\OneDrive\Masaüstü\trade\CLAUDE_BRIEF.md dosyasını oku ve adımlarını tür = aksam için eksiksiz uygula.`

   Görev saatleri (Türkiye saati):

   | Tür | Saat (TR) | Günler |
   |---|---|---|
   | sabah | 16:15 | Pazartesi–Cuma |
   | ogle | 20:00 | Pazartesi–Cuma |
   | aksam | 00:50 | **Salı–Cumartesi** (gece yarısından sonra çalıştığı için bir gün kaydırılmış) |

   Bu saatler ABD yaz saatinde de kış saatinde de doğru brifinge denk gelir; ABD saat değişiminde görevleri
   düzenlemeniz gerekmez (ayrıntı: `CLAUDE_BRIEF.md` → "Zamanlama").
3. Görev `py -m marketdesk.export <tür>` ile paketi hazırlar, okur, brifingi `claude\cikti\` klasörüne yazar ve
   `py -m marketdesk.export --import` ile uygulamaya yükler. Uygulama açıksa bu klasörü zaten dakikada bir
   tarar; brifing **Claude** etiketiyle Brifingler sekmesinde görünür.

Ayrıntılı talimat: [`CLAUDE_BRIEF.md`](CLAUDE_BRIEF.md).

Uygulamanın kendi (kural tabanlı) brifing saatleri ABD Doğu saatiyle (ET) ayarlanır; varsayılan 08:30 / 12:30 /
16:30 ET. Türkiye saati karşılığı ABD yaz saatinde (Mart–Kasım başı) 15:30 / 19:30 / 23:30, ABD kış saatinde
16:30 / 20:30 / 00:30 olur; uygulama bunu kendisi hesaplar.
Hafta sonu ve NYSE tatillerinde zamanlanmış brifing yazılmaz (elle "Şimdi oluştur" her zaman çalışır).

## Veritabanı şeması (gün içi işlem botu için)

Dosya: `%LOCALAPPDATA%\PiyasaMasasi\marketdesk.db` (SQLite, WAL modu). Bot okurken uygulama yazmaya devam
edebilir; salt-okunur bağlanın: `sqlite3.connect("file:...marketdesk.db?mode=ro", uri=True)`.
Tüm zamanlar **unix saniyesi (UTC)**. JSON alanlar metin olarak saklanır.

**`news`** — her haber tek satır (aynı URL tekrar eklenmez)

| Sütun | Açıklama |
|---|---|
| `id` | Kalıcı kimlik (normalleştirilmiş URL'nin SHA1'i, 20 karakter) |
| `source`, `source_name` | Kaynak anahtarı (`reuters`, `sec_8k`, `alpaca_news`…) ve görünen adı |
| `source_type` | `news` / `press` / `filing` / `official` / `social` / `api` |
| `title`, `summary`, `url`, `author` | Başlık, düz metin özet (≤ 600 karakter), bağlantı, yazar |
| `published_at`, `fetched_at` | Yayın zamanı ve uygulamanın haberi ilk gördüğü zaman |
| `tickers` | JSON liste, ör. `["NVDA","AMD"]`; analizin sırası korunur, **ilk sembol haberin asıl konusudur** (ayrıca `extra.subject_tickers`) |
| `categories` | JSON liste: `earnings`, `guidance_up`, `analyst_down`, `mna`, `fda`, `offering`, `halt`… (etiketler `GET /api/meta`) |
| `sentiment`, `sentiment_label` | −1…+1 ve `pos` / `neg` / `neu` |
| `importance` | 0–100 önem puanı |
| `cluster_id` | Aynı hikâyenin farklı kaynaklardaki haberleri aynı kimliği taşır (kümenin ilk haberi) |
| `extra` | JSON: kaynağa özel alanlar (`publisher` = normalleştirilmiş yayıncı adı, SEC form türü, CIK, işlem durdurmalarında `reason_code`/`reason_tr`/`halt_ts`/`resume_ts`…) ve analiz alanları (`title_tickers`, `subject_tickers`, `title_categories`, `<kategori>_outcome`, `junk`…; ayrıntı `SPEC.md`) |

**`calendar_events`** — `kind` (`macro` / `earnings` / `ipo`), `title`, `country`, `ts`, `all_day`, `impact`
(`high`/`medium`/`low`), `forecast`, `previous`, `actual`, `ticker`, `source`, `extra` (JSON:
`title_tr`, bilançolarda `time` = `bmo`/`amc`/`unknown`, `eps_forecast`, `company`, `market_cap`, açıklandıktan
sonra `eps_actual`, `surprise_pct`), `updated_at`. `source = nasdaq_econ` satırları ForexFactory'nin henüz
yayımlamadığı günler için Nasdaq'tan alınan **geçici** ABD verileridir (`extra.provisional = true`); FF o günü
yayımlayınca silinir.

**`snapshots`** — `key`, `data` (JSON), `updated_at`. Anahtarlar: `quotes` (vadeli, endeks, VIX, faiz, döviz,
emtia, kripto, izleme listesi; fiyat, değişim, mini grafik), `movers` / `movers_alpaca` (yükselenler,
düşenler, en aktifler), `alpaca_snapshots`, `fear_greed`, `social` (Reddit/ApeWisdom, StockTwits),
`opportunities` (hisse bazlı fırsat skorları + Türkçe gerekçeler; her 2 dakikada **üzerine yazılır**, geçmiş
için `signals` tablosuna bakın), `halts` (devam eden resmi işlem durdurmaları: `items: [{symbol, name, market,
reason_code, reason_tr, halt_ts, resume_ts, resume_quote_ts}]`, `luld_today: {SEMBOL: bugünkü LULD molası}`),
`movers_why` (hareketli hisselerin "neden" haberi: `items: {SEMBOL: {title,
url, source_name, publisher, published_at, categories, sentiment_label, source_count, news_id}}`),
`brief_runs` (zamanlayıcı iç durumu).

**`briefs`** — `id`, `kind` (`sabah`/`ogle`/`aksam`/`manual`), `author` (`kural`/`ollama`/`claude`),
`created_at`, `title`, `body_md` (Markdown).

**`source_status`** — kaynak başına `last_run`, `last_ok`, `last_error` (Türkçe), `last_warning` (son başarılı
çalışmadaki kısmi hata, ör. akışlardan biri alınamadı), `items_last`, `items_total`.

**`signals`** — fırsat skorlarının geçmişi (bot için geriye dönük test ve "T anından beri yeni sinyaller").
Birincil anahtar `(ts, ticker)`; `ts` skorun hesaplandığı an. Seans açıkken (piyasa öncesi/açık/sonrası) her
10 dakikada, piyasa kapalıyken saatte bir listedeki **tüm** hisseler yazılır; arada yalnızca listeye yeni giren,
skoru 8 puan ya da daha fazla değişen veya yönü dönen hisseler yazılır.

| Sütun | Açıklama |
|---|---|
| `ts`, `ticker` | Hesaplama zamanı (unix sn) ve sembol |
| `score`, `direction` | 0–100 skor; `long` / `short` / `mixed` |
| `session` | `premarket` / `open` / `afterhours` / `closed` (ts anındaki seans) |
| `price`, `change_pct`, `volume`, `rel_volume` | Fiyat, ilgili seansın % değişimi, hacim, hacim / ortalama hacim |
| `categories` | JSON: haber kategorileri (anahtarlar `GET /api/meta`) |
| `reason_codes` | JSON, makine okunur gerekçeler: kategori anahtarları + `gap` (≥ %3 hareket), `rvol` (≥ 2× hacim), `earnings_today`, `earnings_next`, `earnings_surprise` (EPS sürprizi ≥ %5), `halt`, `social`, `watchlist` |
| `news_ids`, `news_count`, `n_sources` | En önemli haberlerin `news.id` listesi (≤ 12), haber ve kaynak sayısı |
| `first_news_ts` | Katalizörün ilk haber zamanı (bu haberlerin en eskisi) |
| `earnings`, `in_watchlist` | 0/1: bugün bilanço, izleme listesinde |
| `schema_version` | Satırın şema sürümü (şu an `1`; alan anlamı değişirse artar) |

### Bot arayüzü

- Doğrudan SQLite (salt-okunur) ya da uygulama açıkken `GET /api/signals?since=<ts>&ticker=&min_score=&limit=`
  → `{schema_version, items, now}`. `since` verilirse en eski satırlar önce gelir; bot aldığı en büyük `ts`
  ile devam eder (bir hesaplamanın satırları tek işlemde yazılır, yarım parti görülmez). `since` olmadan en
  yeniler önce gelir. `until`, `limit` (≤ 5000) da desteklenir.
- Gerekçeler için Türkçe metni (`opportunities.items[].reasons`) ayrıştırmayın; `reason_codes` kullanın.
- `schema_version` değişmeden alan adları/anlamları değişmez; yeni alanlar eklenebilir.

Örnek sorgu — son 1 saatin önemli, NVDA içeren haberleri:

```sql
SELECT published_at, source_name, title, importance, sentiment
FROM news
WHERE published_at >= strftime('%s','now') - 3600
  AND tickers LIKE '%"NVDA"%' AND importance >= 60
ORDER BY published_at DESC;
```

Uygulama açıkken aynı veriler yerel JSON API'den de alınabilir (yalnızca `127.0.0.1`):
`/api/news`, `/api/news/by_source`, `/api/clusters`, `/api/ticker/<SEMBOL>`, `/api/opportunities`,
`/api/market`, `/api/calendar`, `/api/briefs`, `/api/sources`, `/api/alerts?since=<ts>`, `/api/status`,
`/api/meta`, `/api/config`, `/api/signals`. Ayrıntılar `SPEC.md` dosyasında.

## Test Laboratuvarı (4 bot + hakem, 5 yıllık geçmiş)

**Test Laboratuvarı** sayfası dört kural tabanlı botu ve onların oylarından karar veren bir **hakemi** 5 yıllık
geçmiş veride dener. Her şey ücretsiz kaynaklardan gelir ve ayrı bir dosyada tutulur (`lab.db`, ~100–300 MB).

- **Veri (bir kez indirilir, sonra her işlem günü kapanıştan sonra kendiliğinden güncellenir):**
  ~460 hisse (piyasa değeri en büyükler + en çok işlem görenler); Yahoo günlük fiyatlar (5 yıl), Nasdaq bilanço
  takvimi (EPS gerçekleşen / beklenen, 5 yıl), SEC EDGAR bildirimleri (8-K madde kodları, hisse arzı, 13D, Form 4;
  Ayarlar'da SEC e-postası gerekir), Finnhub haber başlıkları (ücretsiz plan ≈ son 12 ay; Ayarlar'da Finnhub anahtarı
  gerekir; günde onlarca haberi olan dev şirketlerde haftanın en yeni ~240 başlığı alınır). İndirme kaldığı yerden
  devam eder; uygulama kapanırsa bir sonraki açılışta "indir" ile sürdürülür.
- **Botlar** (karar anı: günün açılışı; yalnızca o ana kadarki bilgi): momentum / kırılım, ortalamaya dönüş (RSI 2),
  açılış boşluğu, olay / haber (bilanço sürprizi, gece gelen 8-K ve hisse arzı bildirimleri, gece haberlerinin tonu).
- **İşlem modeli:** açılışta giriş, zarar durdur / kâr al / kapanışta çıkış (varsayılan %2 / %4, taraf başına 5 baz
  puan kayma). Günlük veride aynı gün hem zarar durdur hem kâr al seviyesine değildiyse zarar sayılır.
- **Hakem:** (1) ağırlıklı oy — her botun ağırlığı eğitim yıllarındaki net getirisinden; (2) öğrenen model —
  scikit-learn lojistik regresyon, "yükseliş işlemi kazanır mı / düşüş işlemi kazanır mı" olasılıklarını botların
  oylarından ve piyasa bağlamından tahmin eder, eşiği geçen tarafa girer (gerekirse botların tersine).
- **Test:** hakem son 12 ay hariç yıllarla eğitilir, hiç görmediği son 12 ayda ölçülür; günde en fazla 5 işlemli
  portföy simülasyonu SPY'yi elde tutmakla karşılaştırılır. Bugünkü hisse listesi kullanıldığı için (aradan borsadan
  çıkanlar yok) sonuçlar biraz iyimserdir.
- **Canlı kullanım (isteğe bağlı, varsayılan kapalı):** Sanal İşlem → "Hakem bot açılışta sanal işlem yapsın".
  İşlem günlerinde 09:35–09:55 ET arasında hakem o günün seçimlerini sanal emir olarak açar (en fazla 3), kapanıştan
  5 dk önce kapatılır. Gerçek emir gönderilmez.
- API: `GET /api/lab`, `POST /api/lab/download` (`{"update": true}` = yalnızca son günler), `POST /api/lab/stop`,
  `POST /api/lab/test` (`sl_pct`, `tp_pct`, `slippage_bps`), `POST /api/lab/today` (bugünün kararları, emir açmaz).

## Sinyal Karnesi

**Sinyal Karnesi** sayfası, Fırsatlar sinyallerinin sonradan gerçekte ne kazandırdığını ölçer (fırsat skoru
değişmez; yalnızca sonuçları kaydedilir). Her `signals` satırı, giriş gününün kapanışından ~20 dk sonra Yahoo
5 dakikalık fiyatlarıyla değerlendirilir:

- Seans içindeki sinyal bir sonraki 5 dk barının açılışında, seans dışındaki (piyasa öncesi, akşam, hafta sonu)
  sinyal ilk açılışta "alınmış" sayılır; getiri 30 dk, 60 dk ve kapanışta, ayrıca gün içi en iyi / en kötü an
  (MFE / MAE) ve aynı aralıktaki SPY getirisi kaydedilir. Düşüş sinyallerinde fiyatın düşmesi kazançtır.
- Sanal İşlem botunun çıkış kuralı (kâr al / zarar durdur / gün sonu) aynı fiyat yoluna uygulanır.
- Sonuçlar skor aralığına, gerekçe koduna, yöne ve giriş zamanına göre gruplanır. Varsayılan sayım "hisse başına
  günde ilk sinyal"dir (bir botun göreceği gibi). 20'den az örnekli gruplar "az veri" olarak işaretlenir.
- Veri: `signal_outcomes` tablosu. API: `GET /api/scorecard?days=&min_score=&direction=&unit=first|all`,
  `POST /api/scorecard/run`. Yahoo 5 dk verisi ~60 gün geriye gider; uygulama kapalıyken kaydedilmemiş sinyaller
  değerlendirilemez.

## Sanal İşlem (paper trading)

Sol menüdeki **Sanal İşlem** sayfası (kısayol `9`) sanal parayla ABD hissesi alıp satmanızı sağlar. Hiçbir emir
gerçek bir aracı kuruma gitmez; hesap, kayıt ya da API anahtarı gerekmez.

- **Fiyat:** Yahoo'nun son işlem fiyatı. Piyasa ve stop emirleri bu fiyata ayarlanabilir bir kayma (varsayılan
  5 baz puan) eklenerek, limit emirleri limit fiyatı ya da daha iyisiyle gerçekleşir. Emir defteri, kısmi dolum
  ve açığa satış ödünç maliyeti yoktur; yalnızca tam hisse işlenir.
- **Seans:** Piyasa emirleri yalnızca normal seansta (09:30–16:00 ET) gerçekleşir; piyasa kapalıyken verilen emir
  açılışta işlenir. "Uzatılmış seans" işaretli limit emirleri piyasa öncesi / sonrası da gerçekleşebilir.
  Günlük emirlerin süresi seans sonunda dolar.
- **Risk limitleri (Ayarlar panelinde):** tek hissede en fazla özsermayenin %20'si, en fazla 5 pozisyon,
  kaldıraç yok, günlük %3 zararda o gün yeni pozisyon açılmaz.
- **Bot (kural tabanlı, yapay zekâ yok, varsayılan kapalı):** seans açıkken 09:45–15:30 ET arasında, Fırsatlar
  skorunu, günlük hareketi, zaman ayarlı göreli hacmi ve fiyatı süzerek girer; pozisyon büyüklüğünü işlem başına
  risk (%0,5) ve zarar durdur (%2) ile hesaplar, kâr al (%4) koyar, aynı hisseye günde bir kez girer ve
  kapanıştan 5 dk önce kendi pozisyonlarını kapatır. Her kararı **Motor günlüğü**ne yazar.
- **Veri:** `paper_account`, `paper_orders`, `paper_positions`, `paper_equity`, `paper_log` tabloları
  (`marketdesk.db`). API: `GET /api/paper`, `GET /api/paper/quote?symbol=`, `POST /api/paper/order`,
  `/cancel`, `/close`, `/position`, `/settings`, `/reset`.

Sanal sonuçlar gerçek getiriyi garanti etmez: gerçek piyasada dolum fiyatı, kayma ve likidite farklıdır.
Bir stratejiyi değerlendirmek için en az birkaç hafta ve onlarca işlem gerekir.

## Mikro hesap ve Gerçekçi hesap (100 $)

- **Mikro hesap** (`micro_paper.py`): ana hesaptan ayrı 100 $'lık sanal hesap; 1–10 $ arası hareketli küçük hisseleri
  alır (işlem başına ≤ 40 $, ≤ 3 pozisyon, kapanıştan 5 dk önce hepsi kapanır). Ayarlarını canlı öğrenme yazar.
  5 dakikadan eski (durdurulmuş) fiyatla işlem yapmaz.
- **Gerçekçi hesap** (`real_sim.py`): gerçek bir ABD nakit hesabının kurallarıyla çalışan 100 $'lık sanal hesap
  (gerçek emir yok). Karma: evrim, fırsat ve mikro botuna birer yuva (özsermayenin 1/3'ü). Kaldıraç ve açığa satış
  yok; alışlar yalnızca takası tamamlanmış nakitle, satış geliri ertesi iş günü (T+1); kesirli hisse (emir ≥ 5 $);
  emir sinyalden sonraki motor adımında (≥ 15 sn) dolar; makas + likiditeye göre kayma, SEC ücreti ve FINRA TAF;
  durdurulmuş hisse satılamaz; günlük %5 zararda yeni işlem yok. Sanal İşlem sayfasında sanal mikro ve ana hesapla
  karşılaştırılır. API: `GET /api/paper/real`, `POST /api/paper/real/settings` (`{"enabled": bool}`), `/reset`.

## Canlı öğrenme, ileri test ve rejim karnesi

- **Canlı öğrenme** (`lab/learn.py`): her seans fırsat ve mikro botunun 48'er ayar çeşidi gölgede yarışır; gece
  kazananlar klonlanır, kaybedenler elenir, en iyisi (otomatik uygulama açıksa) sanal hesaba yazılır. Evrim kolunun
  botları da her gece canlı sonuçlarına göre emekliye ayrılır / terfi eder / klonlanır.
- **Sabit bölümler:** canlı öğrenmenin sınavları turnuvanın kilitli kasasını kullanır (geçmiş < kilitli kasa < canlı
  dönem); kasa her gece kaymaz ve canlı dönemle çakışmaz.
- **İleri test:** her gün, bir önceki gece seçilmiş ayarın o günkü sonucu aynı günün ortalama / medyan çeşidiyle
  karşılaştırılır (Test Laboratuvarı → Canlı öğrenme → "İleri test"). 20 günden az veri "az veri" sayılır.
- **Rejim karnesi:** her günün rejimi açılışta belirlenir — korku (VIX ≥ 20), sakin yükseliş (hisselerin yarısından
  fazlası 50 günlük ortalamasının üstünde), sakin zayıf. Her botun geçmiş ve canlı sonucu rejime göre ayrı tutulur;
  hakem bota bugünün rejimindeki karnesine göre ağırlık verir, o rejimde zarar eden bot oy veremez.

## Sinir ağı evrimi (ayrı popülasyon)

Test Laboratuvarı → 7. Her bot küçük bir sinir ağıdır (31 gösterge + haber var/yok → 12 nöron → skor) ve bir çıkış
kuralı taşır; her gün en yüksek skorlu en fazla 5 hisseyi açılışta alır (yalnızca yükseliş).

**Yaşayarak evrim (`neuro.run_walk`):** 40 ağlık popülasyon 2017'den bugüne **gün gün** yaşar. Her gün her ağ
seçimlerini yapar; her 5 işlem gününde bir, en az 60 gündür yaşayan ağlar yalnızca **kapanmış** işlemlerine (son
~250 gün) göre puanlanır: en zayıf beşte biri elenir, en iyi beşte birinden çaprazlama + mutasyonla yeni ağlar doğar,
birkaç rastgele ağ katılır. Kimse bir sonucu gerçekleşmeden göremez; girdi ölçekleme de yalnızca ilk yıldan
hesaplanır. Her gün o ana kadar en iyi görünen ağın (şampiyon) seçimleri kaydedilir: bu **sicil**, canlıda yaşasaydık
ne olurdu sorusunun dürüst cevabıdır (ilk yıl okul dönemi, sayılmaz). E hisse grubu aynı ağlarca gölgede işlenir,
elemede hiç kullanılmaz. Sonunda en iyi ağlar (son yıl + E grubu şartlarıyla) popülasyona katılır.

**Popülasyon:** en fazla 12 aktif ağ. Her gece canlı sonucu zararda olanlar emekliye ayrılır, en iyi 2 ağın klonu
sınavı geçerse eklenir, yer kalmazsa en zayıf çıkar. Ağlar gölgede çalışır (emir açmaz). Haber göstergeleri yalnızca
son ~12 ayda var. Bir çalıştırma ~20–40 dk sürer. API: `POST /api/lab/nn_evolve` (`runs`, `population`),
`POST /api/lab/nn_today` (bugünün seçimleri), durum `GET /api/lab` → `nn`, `nn_report`, `nn_population`, `nn_night`.
Eski toplu yöntem (`neuro.run`, bütün eğitim dönemine tek not) yedek olarak duruyor.

## Sorun giderme

- **Pencere boş/açılmıyor:** `run.bat --edge` deneyin; WebView2 için <https://developer.microsoft.com/microsoft-edge/webview2/>.
- **Bir kaynak "Hata" gösteriyor:** Kaynaklar sekmesinde hata metnine bakın (ör. "HTTP 403 — erişim engellendi",
  "Zaman aşımı"). Siteler zaman zaman erişimi kısıtlar; kaynak otomatik olarak yeniden denenir, isterseniz kapatabilirsiniz.
- **Ayrıntılı günlük:** `py -m marketdesk.main --debug`, ardından `marketdesk.log` dosyasına bakın.
