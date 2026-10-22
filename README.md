# Load Tester

Kendi API'n / servisin için basit, arayüzlü yük ve latency test aracı.
Tkinter ve `requests` kullanır. `requests`, daha uyumlu HTTP istemcisi ve
ayrıntılı yanıt ölçümü için kullanılır.

## Çalıştırma

```bash
pip install requests
python loadtester.py
```

## Windows EXE

Python kurmadan çalıştırmak için `dist/LoadTesterClean.exe` dosyasını kullanabilirsin.
Uygulama tek dosyadır; ilk açılışta geçici dosyaları hazırlaması birkaç saniye sürebilir.
Her test bilgisayarında token'ı uygulama içinden gir; token dosyaya kaydedilmez.

## Web arayüzü ve Coolify

`webapp.py`, aynı aracı tarayıcıdan yönetmek için web arayüzünü başlatır.
Makinenin CPU/RAM bilgisinden güvenli başlangıç RPS ve worker önerisi üretir.
Bu öneri hedef API'nin kapasite tahmini değildir; yalnızca test istemcisinin
kontrollü başlangıç ayarıdır.

Coolify'da:

1. Build pack olarak **Dockerfile** seç.
2. Uygulama portu olarak `8080` ayarla.
3. Deploy'dan sonra normal Coolify alan adını aç.

Arayüz token'ı tarayıcı isteği süresince kullanır; token dosyaya veya veritabanına
kaydedilmez. Coolify tarafında uygulamayı private network, IP allowlist veya
Basic Auth ile koruman önerilir.

(Python 3.8+ ve tkinter gerekir. Windows'ta resmi Python kurulumu tkinter'ı içerir.)

## Kullanım

1. **URL** — test edilecek endpoint (sahibi olduğun / izinli olduğun).
2. **Method** — GET / POST / PUT / PATCH / DELETE.
3. **Bearer token** — gerekiyorsa; `Authorization: Bearer ...` olarak eklenir.
4. **RPS** — saniyedeki hedef istek sayısı.
5. **Süre (s)** — test süresi; `0` = Durdur'a basana kadar.
6. **Thread** — eşzamanlı istek gönderen worker sayısı.
7. **Timeout (s)** — istek başına zaman aşımı.
8. **Body (JSON)** — POST/PUT/PATCH için gövde; dosyadan da yükleyebilirsin.

**Baslat** ile test başlar. Canlı olarak görürsün:

- Gönderilen / Başarılı / Hata sayıları
- Gerçek RPS
- Ortalama, p95, p99 latency (ms)
- Son isteklerin latency grafiği
- HTTP status kırılımı ve hata logu
- Alınan toplam veri ve MB/s bilgisi

## Not

Bu araç yalnızca sahibi olduğun ya da test izni bulunan servisler için
tasarlanmıştır.
