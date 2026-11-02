"""
Gercek Kullanici Davranisi Simulatörü (1000 Kullanici Stres & Kapasite Testi)
Sitedeki gercek insan trafigini taklit eder:
- %50 Ana bülten polling (/api/main-events)
- %30 Rastgele farkli canli maclarin detaylarina girme (/api/event)
- %10 Kullanici profil / bakiye kontrolü (/api/user)
- %10 Ozel oranlar ve mac listesi sorgulama (/api/special-odds & /api/matches)

Her istek benzersiz zaman damgasiyla (Cache-Bust) sunucunun veritabanini ve backend CPU'sunu calistirir.
"""

import asyncio
import time
import argparse
import sys
import random
from curl_cffi.requests import AsyncSession
from rich.console import Console
from rich.table import Table
from rich.live import Live
from rich.panel import Panel
from rich.text import Text
from rich import box

try:
    import undetected_chromedriver as uc
except ImportError:
    uc = None

API_BASE = "https://www.7xwon.com/api"


def get_fresh_cookie_and_ua():
    """Arka planda gercek tarayici ile 7xwon'u acip taze cf_clearance cerezini ve UA'yi ceker."""
    if not uc:
        return "", ""
    try:
        driver = uc.Chrome(headless=False)
        driver.get("https://www.7xwon.com")
        time.sleep(5)
        cookies = driver.get_cookies()
        ua = driver.execute_script("return navigator.userAgent")
        driver.quit()
        cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies])
        return cookie_str, ua
    except Exception:
        return "", ""


class Metrics:
    def __init__(self, target_rps, duration, mode):
        self.target_rps = target_rps
        self.duration = duration
        self.mode = mode
        self.total_sent = 0
        self.ok = 0
        self.failed = 0
        self.timeouts = 0
        self.status_codes = {}
        self.latencies = []
        self.start_time = time.time()
        self.last_sec_ok = 0
        self.last_sec_fail = 0
        self.instant_rps = 0.0
        self.lock = asyncio.Lock()

    async def record(self, status, latency_ms, error=None):
        async with self.lock:
            self.total_sent += 1
            if status == 200:
                self.ok += 1
                self.last_sec_ok += 1
            else:
                self.failed += 1
                self.last_sec_fail += 1
                if error and "timeout" in str(error).lower():
                    self.timeouts += 1

            code_str = str(status) if status else "ERR"
            self.status_codes[code_str] = self.status_codes.get(code_str, 0) + 1
            self.latencies.append(latency_ms)
            if len(self.latencies) > 500:
                self.latencies.pop(0)

    def calc_stats(self):
        elapsed = max(time.time() - self.start_time, 0.001)
        avg_ms = sum(self.latencies) / len(self.latencies) if self.latencies else 0.0
        sorted_l = sorted(self.latencies) if self.latencies else [0]
        p50 = sorted_l[int(len(sorted_l) * 0.50)]
        p95 = sorted_l[int(len(sorted_l) * 0.95)]
        p99 = sorted_l[int(len(sorted_l) * 0.99)]
        success_rate = (self.ok / self.total_sent * 100) if self.total_sent else 0.0
        return {
            "elapsed": elapsed,
            "avg_ms": avg_ms,
            "p50": p50,
            "p95": p95,
            "p99": p99,
            "success_rate": success_rate,
            "overall_rps": self.total_sent / elapsed
        }


def make_dashboard(m: Metrics):
    s = m.calc_stats()
    rem = max(0, int(m.duration - s["elapsed"]))

    table = Table(
        title=f"[bold bright_blue]7XWON GERCEK KULLANICI YUK PANELİ[/]  |  Mod: [bold cyan]{m.mode.upper()}[/]  |  Hedef: [bold yellow]{m.target_rps} RPS[/]",
        box=box.ROUNDED,
        header_style="bold bright_white",
        expand=True
    )

    table.add_column("Anlik RPS", justify="center", style="bold green" if m.instant_rps > 0 else "yellow")
    table.add_column("Gonderilen", justify="center", style="white")
    table.add_column("Basarili (200)", justify="center", style="bold green")
    table.add_column("Hata", justify="center", style="bold red" if m.failed > 0 else "dim")
    table.add_column("Basari %", justify="center", style="green" if s["success_rate"] >= 95 else "red")
    table.add_column("Ort. Latency", justify="center", style="cyan bold")
    table.add_column("Medyan (p50)", justify="center", style="blue")
    table.add_column("Gecikme (p95)", justify="center", style="yellow bold" if s["p95"] < 400 else "bold red")
    table.add_column("Max Gecikme (p99)", justify="center", style="red bold")
    table.add_column("Kalan", justify="center", style="bold magenta")

    table.add_row(
        f"{m.instant_rps:.1f} /s",
        str(m.total_sent),
        str(m.ok),
        str(m.failed),
        f"%{s['success_rate']:.1f}",
        f"{s['avg_ms']:.1f} ms",
        f"{s['p50']:.1f} ms",
        f"{s['p95']:.1f} ms",
        f"{s['p99']:.1f} ms",
        f"{rem} sn"
    )

    codes = "   ".join([f"[{'bold green' if k=='200' else 'bold red'}]{k}[/]: {v}" for k, v in m.status_codes.items()]) or "Bekleniyor..."
    footer = Text.from_markup(f"  [bold]HTTP Kod Dagilimi:[/] {codes}  |  [bold yellow]Backend Yük Durumu:[/] {'NORMAL' if s['p95'] < 300 else 'SUNUCU YAVAŞLIYOR (Darboğaz)' if s['p95'] < 1000 else 'KRİTİK GECİKME'}")

    return Panel(
        table,
        subtitle=footer,
        border_style="bright_blue",
        padding=(0, 1)
    )


async def worker(session: AsyncSession, method: str, url: str, payload: dict | None, headers: dict, metrics: Metrics, timeout: float):
    t0 = time.perf_counter()
    status = None
    err = None
    try:
        kwargs = {"headers": headers, "timeout": timeout, "impersonate": "chrome120"}
        if payload is not None and method == "POST":
            kwargs["json"] = payload
        
        if method == "POST":
            resp = await session.post(url, **kwargs)
        else:
            resp = await session.get(url, **kwargs)
        status = resp.status_code
    except Exception as e:
        err = e
    lat = (time.perf_counter() - t0) * 1000
    await metrics.record(status, lat, err)


async def main():
    parser = argparse.ArgumentParser(description="Multi-Scenario Realistic Load Tester")
    parser.add_argument("--rps", type=int, default=100, help="Saniyede istek (Orn: 50, 100, 200, 500)")
    parser.add_argument("--duration", type=int, default=60, help="Sure (saniye)")
    parser.add_argument("--token", type=str, default="sGbBgXsmnX78Gp2BYZnz", help="Bearer token")
    parser.add_argument("--cookie", type=str, default="", help="Cookie / cf_clearance")
    parser.add_argument("--timeout", type=float, default=6.0, help="Istek timeout")
    parser.add_argument("--concurrency", type=int, default=100, help="Es zamanli worker sayisi")
    args = parser.parse_args()

    console = Console()
    cookie = args.cookie
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"

    if not cookie:
        console.print("[cyan][*] Cloudflare kontrol ediliyor, taze oturum aliniyor...[/]")
        fresh_cookie, fresh_ua = get_fresh_cookie_and_ua()
        if fresh_cookie:
            cookie = fresh_cookie
            ua = fresh_ua or ua
            console.print("[green][+] Cloudflare oturumu basariyla alindi![/]")
        else:
            console.print("[yellow][!] Otomatik oturum pas gecildi, standart basliklar kullaniliyor...[/]")

    headers = {
        "Origin": "https://www.7xwon.com",
        "Referer": "https://www.7xwon.com/",
        "User-Agent": ua,
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*"
    }
    if args.token:
        headers["Authorization"] = f"Bearer {args.token}"
    if cookie:
        headers["Cookie"] = cookie

    metrics = Metrics(args.rps, args.duration, "GERÇEK KULLANICI DAĞILIMI")

    # Canli mac ID'lerini cek
    event_ids = [36246068, 36245965, 36269546]
    async with AsyncSession() as s:
        try:
            r = await s.post(f"{API_BASE}/main-events", json={"type": 0, "odds": 0, "code": False, "s": 1, "t": False}, headers=headers, impersonate="chrome120", timeout=5)
            if r.status_code == 200:
                data = r.json().get("data", [])
                eids = [x.get("eventid") or x.get("id") for x in data if isinstance(x, dict)]
                event_ids = [e for e in eids if e] or event_ids
                console.print(f"[green][+] {len(event_ids)} adet canlı maç simülasyon havuzuna alındı.[/]")
        except Exception:
            pass

    sem = asyncio.Semaphore(args.concurrency)
    interval = 1.0 / max(args.rps, 1)

    async def bounded_worker(sess, method, url, body):
        async with sem:
            await worker(sess, method, url, body, headers, metrics, args.timeout)

    async def ticker():
        while True:
            await asyncio.sleep(1.0)
            metrics.instant_rps = metrics.last_sec_ok + metrics.last_sec_fail
            metrics.last_sec_ok = 0
            metrics.last_sec_fail = 0

    ticker_task = asyncio.create_task(ticker())

    with Live(make_dashboard(metrics), refresh_per_second=4, console=console) as live:
        async with AsyncSession() as session:
            start = time.time()
            tasks = []
            while (time.time() - start) < args.duration:
                t_start = time.perf_counter()
                
                # Gercek Kullanici Trafik Simülasyonu (Ağır backend sorguları)
                dice = random.random()
                ts = time.time_ns()
                
                if dice < 0.50:
                    # %50: Ana Liste (Cache-Bust ile doğrudan DB sorgusu)
                    method = "POST"
                    url = f"{API_BASE}/main-events?_t={ts}"
                    body = {"type": 0, "odds": 1, "code": False, "s": 1, "t": False, "_cb": ts}
                elif dice < 0.80:
                    # %30: Farklı canlı maçların detaylarını çekme (Ağır hesaplama)
                    method = "POST"
                    target_id = random.choice(event_ids)
                    url = f"{API_BASE}/event?_t={ts}"
                    body = {"id": target_id, "_cb": ts}
                elif dice < 0.90:
                    # %10: Kullanıcı profil ve bakiye verisi
                    method = "GET"
                    url = f"{API_BASE}/user?_t={ts}"
                    body = None
                else:
                    # %10: Özel oranlar bülteni
                    method = "POST"
                    url = f"{API_BASE}/special-odds?_t={ts}"
                    body = {"type": 0, "_cb": ts}

                tasks.append(asyncio.create_task(bounded_worker(session, method, url, body)))
                live.update(make_dashboard(metrics))

                elapsed = time.perf_counter() - t_start
                delay = interval - elapsed
                if delay > 0:
                    await asyncio.sleep(delay)

                if len(tasks) > 800:
                    tasks = [t for t in tasks if not t.done()]

            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    ticker_task.cancel()
    console.print("\n[bold green][TAMAMLANDI] Gercek Kullanici Stres Testi Bitti![/]")
    console.print(f"Toplam Istek: {metrics.total_sent} | Basarili: [green]{metrics.ok}[/] | Hata: [red]{metrics.failed}[/]")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nTest kullanici tarafindan durduruldu.")
        sys.exit(0)
