# Claude Code Brifing Görevi — Piyasa Masası

Bu dosya, zamanlanmış bir Claude Code görevinin izleyeceği talimattır. Görevin: Piyasa Masası uygulamasının
topladığı ABD borsası verilerinden **keskin, işlemci (trader) odaklı bir Türkçe brifing** yazmak ve uygulamaya
geri yüklemek. Ücretli API yok; iş kullanıcının Claude aboneliğiyle bu görevde yapılır.

Her brifing türü için **ayrı bir zamanlanmış görev** kur ve türü görev isteminde açıkça yaz (aşağıdaki
"Zamanlama" bölümüne bak). Görev istemi örnekleri:

- `C:\Users\sefed\OneDrive\Masaüstü\trade\CLAUDE_BRIEF.md dosyasını oku ve adımlarını tür = sabah için eksiksiz uygula.`
- `C:\Users\sefed\OneDrive\Masaüstü\trade\CLAUDE_BRIEF.md dosyasını oku ve adımlarını tür = ogle için eksiksiz uygula.`
- `C:\Users\sefed\OneDrive\Masaüstü\trade\CLAUDE_BRIEF.md dosyasını oku ve adımlarını tür = aksam için eksiksiz uygula.`

İstemde tür yoksa komutu türsüz çalıştır (ET saatine göre seçilir, bkz. adım 1).

## Klasörler (sabit kural)

| Ne | Yol |
|---|---|
| Proje kökü | `C:\Users\sefed\OneDrive\Masaüstü\trade` |
| Veri klasörü | `%LOCALAPPDATA%\PiyasaMasasi` → `C:\Users\sefed\AppData\Local\PiyasaMasasi` (`MARKETDESK_DATA` ortam değişkeni tanımlıysa onun değeri) |
| Girdi (uygulama yazar) | `%LOCALAPPDATA%\PiyasaMasasi\claude\girdi\latest.md` ve `...\claude\girdi\<YYYY-AA-GG>_<tür>.md` |
| Çıktı (sen yazarsın) | `%LOCALAPPDATA%\PiyasaMasasi\claude\cikti\<YYYY-AA-GG>_<tür>.md` |

Tür (`<tür>`): `sabah` (piyasa öncesi), `ogle` (seans ortası), `aksam` (kapanış sonrası), `manual` (hafta sonu / anlık).
Tarih, ilgili **işlem gününün** ABD Doğu (ET) tarihidir; komut doğru yolu zaten yazdırır — yolu kendin uydurma.

## Adımlar

1. **Veri paketini oluştur.** Komut yalnızca proje kökünden çalışır; `cd` kısmını atlama. `<tür>` yerine
   istemdeki türü yaz (`sabah`, `ogle`, `aksam` veya `manual`):

   ```
   cd "/c/Users/sefed/OneDrive/Masaüstü/trade" && py -m marketdesk.export <tür>
   ```

   (PowerShell: `Set-Location "C:\Users\sefed\OneDrive\Masaüstü\trade"; py -m marketdesk.export <tür>`)
   Çalışma dizini proje kökü değilse `-m` çalışmaz; o durumda dosya yolunu doğrudan verin (dizinden bağımsız):
   `py "C:\Users\sefed\OneDrive\Masaüstü\trade\marketdesk\export.py" <tür>`
   İstemde tür yoksa `<tür>` yazma; tür ET saatine göre seçilir (10:30 ET öncesi `sabah`, kapanışa kadar —
   16:00 ET, erken kapanış günlerinde 13:00 ET — `ogle`, sonra `aksam`; işlem günü değilse `manual`).
   Komut şu satırları yazdırır:

   ```
   TUR: sabah
   ZAMAN: ...
   GIRDI: C:\...\claude\girdi\2026-10-26_sabah.md
   LATEST: C:\...\claude\girdi\latest.md
   CIKTI: C:\...\claude\cikti\2026-10-26_sabah.md
   SONRA: cd "C:/Users/sefed/OneDrive/Masaüstü/trade" && py -m marketdesk.export --import
   SONRA_PS: Set-Location "C:\Users\sefed\OneDrive\Masaüstü\trade"; py -m marketdesk.export --import
   ```

   **`CIKTI:` satırındaki yolu aynen not et** — brifingi tam olarak oraya yazacaksın.
   - `UYARI: tür saatle uyuşmuyor (ET'ye göre beklenen: X)` satırı çıkarsa devam et, ama brifingin **Dikkat**
     bölümüne "görev saati brifing türüyle uyuşmuyor (beklenen: X)" notunu ekle.
   - Komut `HATA:` yazar veya sıfır dışı kodla biterse brifing **yazma**; hatayı olduğu gibi raporla ve dur.
     ("... zaten var; üzerine yazılmadı" hatası hafta sonu / tatil gecelerinde beklenen bir korumadır: önceki
     işlem gününün brifingini ezmemek için çalışır. Kısaca bildir ve dur; `--zorla` kullanma.)

2. **Paketi oku.** `LATEST:` yolundaki `latest.md` dosyasını **sonuna kadar** oku. Paket ~45 KB'ın altında
   tutulur; yine de Read aracı dosyayı kısmen döndürürse (kesilme uyarısı, "exceeds maximum allowed tokens"
   vb.) `offset`/`limit` ile parça parça okumaya devam et — son bölüm "Kaynak dağılımı"dır. Bölüm sırası:
   başlık + veri tazeliği / **UYARI** satırları, "Bugünün 5 maddesi", Piyasa (vadeliler, endeksler, VIX, faiz,
   dolar, emtia, kripto, Korku & Açgözlülük), izleme listesi kotasyonları, hareketliler (sabah: piyasa öncesi
   gap'ler; akşam: seans + kapanış sonrası/AH hareketleri, "neden" satırlarıyla), kural tabanlı fırsat skorları
   (nedenler + riskler), makro takvim, bilançolar (BMO/AMC), izleme listesi haberleri, çok kaynaklı hikayeler
   (her yayıncının kendi başlığı, saati ve duygu tonu), sosyal medya ve en sonda önem sırasıyla haberler.

3. **(İsteğe bağlı) Bağlam ara.** En önemli 3 hikaye için kısa bir web araması yapabilirsin. Bulduğun ek
   bilgiyi kaynağın adıyla ve linkiyle ver; paketteki veriyle çelişiyorsa bunu açıkça yaz. Arama
   yapamıyorsan bu adımı atla — brifing yalnızca paketle de tamamlanmalı.

4. **Brifingi yaz** (aşağıdaki biçimde) ve `CIKTI:` yoluna **tek seferde** kaydet (Write aracı; dosyayı
   parça parça yazma, başka dosyaya dokunma).

5. **Uygulamaya yükle.** 1. adımdaki gibi proje kökünden çalıştır (komut çıktısındaki `SONRA:` / `SONRA_PS:`
   satırları da aynı komuttur):

   ```
   cd "/c/Users/sefed/OneDrive/Masaüstü/trade" && py -m marketdesk.export --import
   ```

   (PowerShell: `Set-Location "C:\Users\sefed\OneDrive\Masaüstü\trade"; py -m marketdesk.export --import`)
   Komut, yeni yazılmış dosyanın oturması için birkaç saniye bekler, sonra `ICE_AKTARILAN: <sayı>` ve son
   2 saatte değişen her çıktı dosyası için bir durum satırı yazar:
   - `YUKLU: <dosya adı>` — **başarı.** `CIKTI:` dosyanın adı bu satırda görünüyorsa iş tamamdır.
     Uygulama açıksa klasörü her dakika kendisi de içe aktarır; bu yüzden `ICE_AKTARILAN: 0` tek başına hata
     **değildir** — belirleyici olan `YUKLU` satırıdır.
   - `YUKLENMEDI: <dosya adı> (neden)` — "çok yeni" ise birkaç saniye bekleyip komutu bir kez daha çalıştır;
     "boş" / "içerik çok kısa" ise dosyayı yeniden yaz. Dosya adı `CIKTI:` ile aynı değilse yanlış yere
     yazmışsındır: doğru yola yaz.
   Uygulama açıksa brifing **Brifingler** sekmesinde "Claude" rozetiyle görünür.

## Brifing biçimi

İlk satır başlıktır: paketteki **"Önerilen başlık"** değerini aynen kullan, ör.
`# Sabah Brifingi — 26 Ekim 2026 Pazartesi`. Ardından şu bölümler (bu sırayla):

```
# <Önerilen başlık>

**Tek bakışta:** 2–3 cümlelik ana tema (bugün piyasayı ne yönetiyor?).

## Piyasa tonu
Vadeliler/endeksler, VIX, Korku & Açgözlülük, 10Y faiz, dolar, petrol — rakamlarla, 3–5 cümle.
Tonu tek kelimeyle de söyle (Pozitif / Negatif / Karışık) ve nedenini ver.

## Kim ne diyor
En büyük 3–5 çok kaynaklı hikaye. Her biri için: ne oldu (1 cümle), kaynakların yorum farkı / çelişkisi
(hangi kaynak olumlu, hangisi temkinli, kim ilk yazdı), piyasaya olası etkisi.
- **NVDA** · Başlık — *CNBC, Reuters (+2)* [link](url)

## En güçlü 5 fırsat
Her biri için ayrı madde (veya en fazla 5 sütunlu basit bir tablo: KOD | Yön | Katalizör | Fiyat/değişim | Risk):
- **KOD** · Yön: Yükseliş/Düşüş/Karışık · Katalizör (kaç kaynak) · Fiyat/değişim (pakette varsa)
  - Neden önemli: …
  - Riskler: … (tek kaynak, aşırı hareket, bilanço oynaklığı, sulandırma, düşük likidite vb.)
  - İzlenecek tetik: … (ör. açılışta hacim teyidi, 15:30 TR verisi, bilanço saati)

## Takvim
Bugünün (akşam brifinginde yarının) yüksek etkili verileri, Fed konuşmaları ve önemli bilançolar.
Saatler **TR saati, ET parantez içinde**: `- **15:30 TR** (08:30 ET) · TÜFE (Enflasyon) · Beklenti %0,3`
Bilançoları BMO (piyasa öncesi) / AMC (kapanış sonrası) diye ayır. Basit bir tablo da kullanabilirsin.

## İzleme listesi
Paketteki izleme listesinden haberi olan her hisse için bir satır: durum + en önemli haber + link.
Haberi olmayanları tek satırda topla.

## Dikkat
Makro/jeopolitik riskler, sosyal medyada aşırı kalabalıklaşan hisseler, veri tazeliği uyarıları.

---
_Yatırım tavsiyesi değildir. Claude tarafından Piyasa Masası veri paketinden üretilmiştir._
```

Türe göre vurgu:

- **sabah** — gece olanlar (son kapanıştan beri), vadeliler, bugünün takvimi ve BMO bilançoları,
  piyasa öncesi hareketliler (paketteki "PM" değişimleri ve "neden" satırları), açılışta izlenecekler.
- **ogle** — açılıştan beri endeks hareketi, en çok konuşulan hikayeler, öğleden sonra gelecek veriler ve
  Fed konuşmaları, bu akşamki AMC bilançoları.
- **aksam** — kapanış özeti, günün kazananları/kaybedenleri ve nedenleri, AMC bilançoları ve kapanış sonrası
  (AH) hareketler, yarının takvimi, "yarın için izlenecekler".
- **manual** — hafta sonu/anlık durum: son seanstan beri olanlar ve sonraki işlem gününe hazırlık.

## Yazım kuralları

- Dil **Türkçe**; kısa, net, işlemci dili. Gereksiz giriş cümlesi yok. Uzunluk: yaklaşık 400–900 kelime.
- **Sadece paketteki veriyi** (ve 3. adımda bulduğun, kaynağını verdiğin bilgiyi) kullan. Rakam, haber,
  fiyat seviyesi veya hedef fiyat **uydurma**. Veri yoksa "veri yok" de.
- Paket "makro takvim yüklenmedi" diyorsa (UYARI satırı veya takvim bölümünde "henüz yok" notu) **"önemli veri
  yok" yazma**; takvimin henüz yüklenmediğini söyle. Pakette sabit Fed listesinden gelen FOMC notu varsa onu
  "teyit edilmeli" diyerek kullanabilirsin.
- Her hikaye satırında kaynak adlarını ve linki ver: `- **KOD** · Başlık — *Kaynak1, Kaynak2 (+N)* [link](url)`.
  Linkleri paketten aynen kopyala. Paketteki `ref:<kimlik>` biçimli linkleri (uzun Google News adresleri
  yerine kısaltma) `[link](ref:<kimlik>)` olarak **aynen** yaz; uygulama içe aktarırken gerçek adrese çevirir.
- Sayılar Türkçe biçimde: `5.812,25`, `+%0,42`, `-%1,10`, `4,2 Mn`, `3,4 Tn $`, `228 bin`.
- Saatler TR saati, ET parantez içinde. (ABD yaz saati 1 Kasım 2026'da biter; fark 7 saatten 8 saate çıkar —
  paketteki saatler bunu zaten hesaba katar, kendin çevirme.)
- **Yatırım tavsiyesi verme:** "al", "sat", "hedef fiyat", "kesin kazanç" gibi ifadeler yok. "izlenebilir",
  "dikkat", "olası senaryo", "teyit beklenmeli" gibi ifadeler kullan. Son satırdaki uyarıyı silme.
- Paketin başındaki `UYARI` satırları varsa (eski veri, haber yok, takvim yüklenmedi vb.) brifingin en üstünde belirt.
- Markdown'ı sade tut: `#`, `##`, `###`, `-` listeler, `**kalın**`, `*italik*`, `[link](url)`, `---`.
  "En güçlü 5 fırsat" ve "Takvim" bölümlerinde en fazla 5 sütunlu basit `|` tabloları kullanabilirsin
  (uygulamanın görüntüleyicisi tabloları gösterir). HTML, resim ve kod bloğu kullanma.
- Proje dosyalarını veya başka dosyaları değiştirme, silme. Tek yazacağın dosya `CIKTI:` yolu.

## Son kontrol

- [ ] Dosya `CIKTI:` yolunda, ilk satırı `# <Önerilen başlık>`.
- [ ] Bölümler: Tek bakışta, Piyasa tonu, Kim ne diyor, En güçlü 5 fırsat, Takvim, İzleme listesi, Dikkat.
- [ ] Her fırsatın riskleri yazılı; hiçbir yerde al/sat tavsiyesi yok.
- [ ] Linkler paketten (`ref:` linkleri dahil, aynen); rakamlar paketle tutarlı.
- [ ] `--import` çalıştırıldı ve çıktıda `YUKLU: <CIKTI dosya adı>` satırı var.

## Zamanlama

Her tür için ayrı bir görev kur; görev isteminde türü yaz (yukarıdaki örnekler) — komut da o türü alır.
Türkiye yaz saati uygulamaz; ABD ise 1 Kasım'da ve Mart'ın ikinci pazarı saat değiştirir. Aşağıdaki TR saatleri
**her iki dönemde de** aynı türe denk gelir, saat değişiminde görevleri düzenlemen gerekmez:

| Tür | Görev saati (TR) | Günler (TR) | ET karşılığı (yaz saati / kış saati) |
|---|---|---|---|
| sabah | 16:15 | Pazartesi–Cuma | 09:15 / 08:15 (açılıştan önce) |
| ogle | 20:00 | Pazartesi–Cuma | 13:00 / 12:00 |
| aksam | 00:50 | **Salı–Cumartesi** | önceki günün 17:50 / 16:50 (kapanıştan sonra) |

- **aksam görevi gece yarısından sonra çalıştığı için günleri bir kaydırılmıştır:** Pazartesi seansının akşam
  brifingi Salı 00:50 TR'de, Cuma seansınınki Cumartesi 00:50 TR'de yazılır. Pazartesi 00:50 TR'de (ET'de Pazar)
  aksam çalıştırma — komut Cuma'nın brifingini ezmemek için `HATA` ile durur.
- Uygulamanın kendi kural tabanlı brifingleri `brief_times_et` ayarına göre (varsayılan 08:30 / 12:30 / 16:30 ET;
  erken kapanış günlerinde akşam brifingi kapanıştan 30 dk sonra) üretilir; Claude görevi kendi paketini
  oluşturduğu için onları beklemesi gerekmez.
- ABD tatillerinde görev `UYARI` (tür saatle uyuşmuyor) veya `HATA` (dosya zaten var) verebilir; bu beklenen
  davranıştır.

Uygulama (Piyasa Masası) bu saatlerde açık olmalı; kapalıysa paket eski veriyle oluşur ve pakette UYARI görünür.
