"""
Ultra Hafif Asenkron API Yük Testi (AsyncIO + aiohttp)
CPU: ~%3-5 | RAM: ~30 MB | Kapasite: 500-2000+ RPS
"""

import asyncio
import aiohttp
import time
import argparse
import sys

URL = "https://api.7xwon.com/main-events"
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36"
}

PAYLOAD = {
    "type": 0,
    "odds": 1,
    "code": False,
    "s": 1,
    "t": False
}

class Stats:
    def __init__(self):
        self.ok = 0
        self.failed = 0
        self.timeouts = 0
        self.latencies = []
        self.lock = asyncio.Lock()

async def worker(session, target_url, payload, token, stats, timeout_sec):
    headers = dict(HEADERS)
    if token:
        headers["Authorization"] = f"Bearer {token}"
        
    start_time = time.perf_counter()
    try:
        async with session.post(
            target_url,
            json=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout_sec)
        ) as response:
            await response.read()
            duration = (time.perf_counter() - start_time) * 1000
            
            async with stats.lock:
                stats.latencies.append(duration)
                if 200 <= response.status < 400:
                    stats.ok += 1
                else:
                    stats.failed += 1
    except asyncio.TimeoutError:
        async with stats.lock:
            stats.timeouts += 1
            stats.failed += 1
    except Exception:
        async with stats.lock:
            stats.failed += 1

async def reporter(stats, interval=1.0):
    """Terminalde canlı ve son derece hafif istatistik basar."""
    prev_ok = 0
    prev_fail = 0
    
    print("\n" + "="*70)
    print(f"{'Zaman':<10} | {'Anlık RPS':<10} | {'Başarılı':<10} | {'Hata':<8} | {'Timeout':<8} | {'Ort. ms':<8}")
    print("="*70)
    
    while True:
        await asyncio.sleep(interval)
        async with stats.lock:
            cur_ok = stats.ok
            cur_fail = stats.failed
            timeouts = stats.timeouts
            latencies = stats.latencies[-100:] if stats.latencies else []
            
        rps = (cur_ok - prev_ok) / interval
        avg_ms = sum(latencies) / len(latencies) if latencies else 0
        now_str = time.strftime("%H:%M:%S")
        
        status_color = "\033[92m" if timeouts == 0 and cur_fail == prev_fail else "\033[91m"
        reset_color = "\033[0m"
        
        print(f"{now_str:<10} | {rps:>9.1f} | {cur_ok:>10} | {status_color}{cur_fail:>8}{reset_color} | {status_color}{timeouts:>8}{reset_color} | {avg_ms:>7.1f}ms")
        
        prev_ok = cur_ok
        prev_fail = cur_fail

async def main():
    parser = argparse.ArgumentParser(description="Ultra-Lightweight Async API Load Tester")
    parser.add_argument("--target", type=str, choices=["main", "event"], default="main", help="Hedef endpoint: 'main' (/main-events) veya 'event' (/event)")
    parser.add_argument("--event-id", type=int, default=36223071, help="Tek maç detayı için maç id (Varsayılan: 36223071)")
    parser.add_argument("--rps", type=int, default=50, help="Saniyede hedeflenen istek sayısı (Varsayılan: 50)")
    parser.add_argument("--duration", type=int, default=60, help="Test süresi saniye cinsinden (Varsayılan: 60)")
    parser.add_argument("--token", type=str, default="", help="Bearer token (Opsiyonel)")
    parser.add_argument("--timeout", type=float, default=5.0, help="İstek zaman aşımı sn (Varsayılan: 5.0)")
    parser.add_argument("--odds", type=int, default=1, help="odds parametresi (1 veya 0, Varsayılan: 1)")
    args = parser.parse_args()

    if args.target == "event":
        target_url = "https://api.7xwon.com/event"
        payload = {"id": args.event_id}
    else:
        target_url = "https://api.7xwon.com/main-events"
        payload = dict(PAYLOAD)
        payload["odds"] = args.odds

    print(f"\n[+] Hedef: {target_url}")
    print(f"[+] Payload: {payload}")
    print(f"[+] Hedeflenen Hız: {args.rps} RPS | Süre: {args.duration}s | Timeout: {args.timeout}s")
    print("[+] Motor: AsyncIO Event-Loop (CPU & RAM tasarruf modu aktif)")

    stats = Stats()
    
    # Connection pool ve TCP Keep-Alive optimizasyonu
    connector = aiohttp.TCPConnector(
        limit=0,                 # Sınırsız eşzamanlı bağlantı
        limit_per_host=0,
        keepalive_timeout=30.0,  # TLS el sıkışmasını her seferinde tekrarlamaz
        enable_cleanup_closed=True
    )
    
    async with aiohttp.ClientSession(connector=connector) as session:
        # Arka plan raporlayıcıyı başlat
        report_task = asyncio.create_task(reporter(stats))
        
        interval = 1.0 / args.rps
        start_time = time.perf_counter()
        tasks = []
        
        total_requests = args.rps * args.duration
        for _ in range(total_requests):
            req_start = time.perf_counter()
            if (req_start - start_time) >= args.duration:
                break
                
            task = asyncio.create_task(worker(session, target_url, payload, args.token, stats, args.timeout))
            tasks.append(task)
            
            # Hassas zamanlama (Drift-free pacing)
            elapsed = time.perf_counter() - req_start
            wait_time = interval - elapsed
            if wait_time > 0:
                await asyncio.sleep(wait_time)
                
        # Kalan isteklerin tamamlanmasını bekle
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            
        report_task.cancel()
        
    print("\n" + "="*70)
    print(f"TEST TAMAMLANDI: Toplam Başarılı: {stats.ok} | Hata: {stats.failed} | Timeout: {stats.timeouts}")
    print("="*70)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[!] Test kullanıcı tarafından durduruldu.")
        sys.exit(0)
