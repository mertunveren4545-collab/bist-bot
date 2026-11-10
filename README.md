# BIST Bot — Telegram'dan haberleşen Borsa İstanbul al-sat botu

Her gün BIST hisselerini tarar, teknik göstergelerle haberleri birlikte değerlendirir,
alım/satım kararı verir ve her şeyi Telegram'dan sana bildirir.

## Nasıl çalışır? (Günlük plan sistemi)

| Saat (İstanbul) | Ne gelir |
|---|---|
| ~10:20 | ☀️ **Sabah planı (günlük al-sat):** piyasa durumu, en güçlü 5 hisse ve **bugün alınacak en fazla 2 hisse** (lot, fiyat, zarar kes, kâr al). Her birinin altında **✅ Midas'ta aldım / ❌ Geç** butonu. |
| 10:00 – 17:30 | Her 10 dk'da bir **sadece elindeki hisseler** kontrol edilir: kâr al (+%2,5), zarar kes (−%1,5), iz süren stop olursa **🔴 SAT**. Gün içinde **yeni alım önerilmez.** |
| 17:30 | Elde kalan her hisse için **🔴 SAT** — günlük al-satta ertesi güne taşınmaz. |
| ~18:20 | 🌙 Gün sonu raporu |

- **Normal yazıyla konuşabilirsin:**
  - `ALARK aldım 53 bin` → lotu hesaplar, takibe alır, kâr al / zarar kes seviyelerini yazar
  - `THYAO 100 lot aldım 285,50` → fiyatı ve lotu sen verirsin
  - `ALARK sattım 107,30` → satışı kaydeder, kâr/zararı yazar
  - `ALARK` → elindeyse durum ve hedefler, değilse puanı
  - `aldım` → bekleyen tek alım sinyalini onaylar
- Bot sadece ✅ bastığın ya da "aldım" yazdığın hisseleri takip eder. ❌ dediğin satış uyarısını o gün tekrar sormaz.
- Midas'taki serbest bakiyeni `/nakit 50000` ile söyle; lotları buna göre hesaplar. Onay bekleyen alımların parasını ayrı tutar.
- Kendi kafana göre bir şey aldıysan `/aldim THYAO 100 285,50` yaz, bot onu da takibe alır.
- Botun haberi olmadan sattıysan `/sattim THYAO 295,25` yaz.
- Butona bastığında bot bunu bir sonraki kontrolde (en geç ~10 dk) işler. Komutlara da seans saatlerinde en geç 10 dk içinde cevap gelir; akşam yazdığın komut ertesi sabah cevaplanır.

**Puanlama (0-100):** Trend 30 + Momentum (RSI, MACD) 25 + Hacim 15 + Haber 30. 65 ve üstü alım adayı.
**Haberler:** Google Haberler (KAP dahil), Bloomberg HT, AA, Dünya — Türkçe olumlu/olumsuz kelime skoru.
Ağır olumsuz haber olan hisse alınmaz.
**Risk:** işlem başına sermayenin en fazla %1'i riske girer, tek hisseye en fazla %20, en fazla 5 hisse,
zarar kes = giriş − 2×ATR, kâr al = giriş + 4×ATR, iz süren stop, endeks zayıfsa alım yok.

## Kurulum — bilgisayara hiçbir şey kurmadan (GitHub)

Bot GitHub'ın ücretsiz sunucularında, hafta içi her 15 dakikada bir kendiliğinden çalışır.
Bilgisayarın kapalı olabilir.

1. **Telegram botu aç:** Telegram'da **@BotFather** → `/newbot` → isim ver → sonu `bot` ile biten kullanıcı adı ver → sana verdiği **token**'ı kopyala.
2. **Kendi botunu aç ve `/start` yaz.** (Bot seni böyle tanır; ayrıca chat id bulmana gerek yok.)
3. **Token'ı GitHub'a gir:** repo sayfası → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**
   - Name: `TELEGRAM_TOKEN`
   - Secret: token'ı yapıştır → **Add secret**
4. **İlk çalıştırma:** repo → **Actions** sekmesi → (istenirse "I understand… enable" butonuna bas) → **BIST Bot** → **Run workflow**.
   1-2 dakika içinde Telegram'a "Bot sana bağlandı" mesajı gelir.
5. Telegram'dan `/nakit 50000` yazarak Midas'taki serbest bakiyeni bildir. Bitti.

Bundan sonra bot hafta içi her sabah kendisi başlar. Durdurmak için Telegram'dan `/durdur`,
tamamen kapatmak için Actions → BIST Bot → "…" → **Disable workflow**.

**Ücret:** Gizli (private) repoda GitHub ayda 2.000 dakika ücretsiz veriyor; bu bot ayda yaklaşık 1.100-1.400 dakika kullanır. (Diğer gizli repolarının Actions kullanımı da aynı kotadan düşer.)

## 3 çalışma modu (config.yaml → mode)

| Mod | Ne olur |
|---|---|
| `midas` (varsayılan) | Yukarıdaki günlük plan sistemi. Emri sen Midas'ta girersin. |
| `paper` | Sanal parayla, sana sormadan alır satar (test için). |
| `algolab` | Deniz Yatırım API ile tam otomatik (şablon; API dokümanı gerekir). |

> Midas bireysel yatırımcıya otomatik emir bağlantısı vermediği için emri bot değil sen girersin.

## Telegram komutları

| Komut | İşlev |
|---|---|
| `/durum` | Mod, nakit, açık pozisyon sayısı |
| `/portfoy` | Hisseler, maliyet, anlık kâr/zarar, stop ve hedefler |
| `/tara` | Şimdi tara, en iyi 10 hisse |
| `/hisse THYAO` | Tek hissenin puan detayı |
| `/haber THYAO` | Son 48 saatin haberleri ve skoru |
| `/sat THYAO` | Pozisyonu hemen kapat |
| `/islemler` | Son 15 işlem |
| `/rapor` | Gün sonu raporunu şimdi gönder |
| `/fiyat 12 285,50` | Midas'ta farklı fiyattan dolduysa kaydı düzelt |
| `/nakit 50000` | Midas'taki serbest bakiyeni bildir |
| `/aldim KOD LOT FİYAT` | Kendi aldığın hisseyi takibe ekle |
| `/sattim KOD FİYAT` | Kendi sattığın hisseyi kayıttan düş |
| `/durdur` / `/devam` | Yeni alımları durdur / başlat (stop takibi her zaman sürer) |

Bot sadece senin chat id'nden gelen komutlara cevap verir.

## Ayarlar
Her şey `config.yaml` içinde ve Türkçe açıklamalı: hisse listesi, puan eşikleri, risk yüzdeleri,
saatler, haber kaynakları. Resmi tatil günlerini `holidays:` altına `- 2027-01-01` şeklinde ekleyebilirsin.
Haber kelime listesi `bot/news.py` içinde (POSITIVE / NEGATIVE).

## Dosyalar
```
run_once.py          GitHub'ın 15 dk'da bir çalıştırdığı dosya
.github/workflows/   GitHub zamanlayıcısı
main.py              Telegram komutları (+ istersen bilgisayarda sürekli çalıştırma)
backtest.py          geriye dönük test
config.yaml          ayarlar
bot/engine.py        karar motoru (tarama, alım, satım, raporlar)
bot/strategy.py      puanlama
bot/risk.py          lot, stop, hedef, BIST fiyat adımları
bot/news.py          haber toplama + duygu skoru
bot/data.py          fiyat verisi (Yahoo Finance)
bot/telegram.py      Telegram bağlantısı
bot/brokers/         paper / midas / algolab
data/bot.db          portföy ve işlem kayıtları (otomatik oluşur)
data/bot.log         günlük kayıtlar
```

## Bilmen gerekenler
- Yahoo verisi BIST için **~15 dakika gecikmeli**. Günlük/swing işlem için yeterli, gün içi hızlı al-sat için değil.
  Daha hızlı veri istersen `bot/data.py` içindeki 2 fonksiyonu başka kaynakla değiştirmen yeterli.
- Haber skoru kelime tabanlıdır, ironi ya da bağlam anlamaz. Sinyali aldığında `/haber KOD` ile göz at.
- Bot yalnızca alım yönlü çalışır (açığa satış yok).
- Bu bir yatırım tavsiyesi değildir. Geçmiş test sonuçları geleceği garanti etmez.
  İlk haftalarda küçük tutarlarla başla; istersen önce `paper` moduyla sanal dene.
