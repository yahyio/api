"""Browser-based control panel for controlled API capacity checks.
Optimized with AsyncIO & aiohttp for ultra-low PC CPU/RAM usage.
Supports multi-scenario simulations:
1. Main Events Polling (/main-events)
2. All Live Matches distributed attack (/event)
3. Cascading Abort / Stampede storm simulation
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import random
import statistics
import threading
import time
from collections import Counter, deque
from urllib.parse import urlparse

import aiohttp
from flask import Flask, Response, jsonify, render_template, request

try:
    import psutil
except ImportError:
    psutil = None


MAX_RPS = 1000
MAX_DURATION = 3600
MAX_THREADS = 100
DEFAULT_URL = "https://api.7xwon.com/main-events"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/131.0 Safari/537.36"
)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024

ACTIVE_RUNS: dict[str, "RunState"] = {}
ACTIVE_RUNS_LOCK = threading.Lock()


class RunState:
    """Live, thread-safe counters for one in-flight run, read by /api/stream."""

    def __init__(self, total):
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.total = total
        self.started = time.perf_counter()
        self.done = 0
        self.ok = 0
        self.failed = 0
        self.bytes = 0
        self.statuses = Counter()
        self.latencies = []
        self.recent = deque(maxlen=120)  # last N latencies for the live chart
        self.finished = False

    def record(self, status, latency, size, error):
        with self.lock:
            self.done += 1
            self.latencies.append(latency)
            self.recent.append(round(latency, 1))
            self.bytes += size
            if error is None and status is not None and 200 <= status < 400:
                self.ok += 1
            else:
                self.failed += 1
            self.statuses[str(status) if status is not None else "ERR"] += 1

    def snapshot(self):
        with self.lock:
            elapsed = max(time.perf_counter() - self.started, 1e-9)
            lat = sorted(self.latencies)

            def pct(percent):
                return round(lat[round((len(lat) - 1) * percent / 100)], 1) if lat else 0

            return {
                "total": self.total,
                "done": self.done,
                "ok": self.ok,
                "failed": self.failed,
                "achieved_rps": round(self.done / elapsed, 2),
                "avg_ms": round(statistics.mean(lat), 1) if lat else 0,
                "p95_ms": pct(95),
                "p99_ms": pct(99),
                "total_mib": round(self.bytes / 1024**2, 3),
                "statuses": dict(self.statuses),
                "recent": list(self.recent),
                "elapsed": round(elapsed, 1),
                "finished": self.finished,
                "stopped": self.stop_event.is_set(),
            }


def system_profile():
    cores = os.cpu_count() or 2
    memory_gib = None
    if psutil is not None:
        memory_gib = round(psutil.virtual_memory().total / 1024**3, 1)

    threads = min(MAX_THREADS, max(10, cores * 5))
    rps = min(MAX_RPS, max(50, cores * 25))
    return {
        "os": f"{platform.system()} {platform.release()}",
        "cores": cores,
        "memory_gib": memory_gib,
        "suggested": {"rps": rps, "threads": threads, "duration": 30, "timeout": 5},
    }


def validate_config(data):
    url = str(data.get("url", "")).strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Gecerli bir http:// veya https:// URL gir.")

    method = str(data.get("method", "GET")).upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        raise ValueError("Gecersiz HTTP metodu.")

    try:
        rps = int(data.get("rps", 1))
        duration = int(data.get("duration", 10))
        threads = int(data.get("threads", 1))
        timeout = float(data.get("timeout", 10))
    except (TypeError, ValueError) as exc:
        raise ValueError("RPS, sure, thread ve timeout sayi olmali.") from exc

    if not 1 <= rps <= MAX_RPS:
        raise ValueError(f"RPS 1-{MAX_RPS} arasinda olmali.")
    if not 1 <= duration <= MAX_DURATION:
        raise ValueError(f"Sure 1-{MAX_DURATION} saniye arasinda olmali.")
    if not 1 <= threads <= MAX_THREADS:
        raise ValueError(f"Thread 1-{MAX_THREADS} arasinda olmali.")
    if not 1 <= timeout <= 30:
        raise ValueError("Timeout 1-30 saniye arasinda olmali.")

    body = None
    if method in {"POST", "PUT", "PATCH"}:
        body = data.get("body") or None
        if body is not None and not isinstance(body, (dict, list)):
            raise ValueError("JSON body nesne veya dizi olmali.")
    return url, method, rps, duration, threads, timeout, body


async def fetch_live_event_ids(session, headers):
    """Fetches currently active match event IDs from /main-events to simulate real browsing."""
    try:
        url = "https://api.7xwon.com/main-events"
        payload = {"type": 0, "odds": 0, "code": False, "s": 1, "t": False}
        async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=6.0)) as resp:
            if resp.status == 200:
                data = await resp.json()
                items = []
                if isinstance(data, dict):
                    items = data.get("data") or data.get("events") or []
                    if isinstance(items, dict):
                        items = list(items.values())
                elif isinstance(data, list):
                    items = data
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
    # Fallback known match IDs
    return [36223071, 36223072, 36223073, 36223074, 36223075]


async def _async_worker(session, method, url, headers, body, timeout, sem, state, results, is_cascading=False):
    if state.stop_event.is_set():
        return
    async with sem:
        if state.stop_event.is_set():
            return
        started = time.perf_counter()
        try:
            client_timeout = aiohttp.ClientTimeout(total=timeout)
            kwargs = {"headers": headers, "timeout": client_timeout}
            if body is not None and method in {"POST", "PUT", "PATCH"}:
                kwargs["json"] = body

            async with session.request(method, url, **kwargs) as resp:
                size = 0
                error_msg = None
                if resp.status >= 400:
                    raw = await resp.read()
                    size = len(raw)
                    error_msg = f"HTTP {resp.status}"
                else:
                    async for chunk in resp.content.iter_chunked(16384):
                        size += len(chunk)
                
                elapsed = (time.perf_counter() - started) * 1000
                res = (resp.status, elapsed, size, error_msg)
                results.append(res)
                state.record(*res)
        except asyncio.TimeoutError:
            elapsed = (time.perf_counter() - started) * 1000
            error_desc = f"Timeout ({timeout:.1f}s) -> Stampede Abort" if is_cascading else f"Read timed out ({timeout}s)"
            res = (None, elapsed, 0, error_desc)
            results.append(res)
            state.record(*res)

            # If cascading abort simulation is active: spawn an immediate retry request
            # to simulate a client that aborts after 2.5s and immediately resends!
            if is_cascading and not state.stop_event.is_set():
                asyncio.create_task(_async_worker(session, method, url, headers, body, timeout, sem, state, results, False))

        except Exception as exc:
            elapsed = (time.perf_counter() - started) * 1000
            res = (None, elapsed, 0, str(exc))
            results.append(res)
            state.record(*res)


async def _async_run_check(url, method, rps, duration, concurrency, timeout, token, body, state, scenarios):
    headers = {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json"
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    sc_main = scenarios.get("main", True)
    sc_all_matches = scenarios.get("all_matches", False)
    sc_cascading = scenarios.get("cascading", False)

    # In cascading abort simulation, client timeout is strictly 2.5 seconds
    effective_timeout = 2.5 if sc_cascading else timeout

    sem = asyncio.Semaphore(concurrency)
    connector = aiohttp.TCPConnector(
        limit=0,
        limit_per_host=0,
        keepalive_timeout=30.0,
        enable_cleanup_closed=True
    )

    results = []
    total_requests = rps * duration
    interval = 1.0 / rps if rps > 0 else 0
    start_time = time.perf_counter()
    tasks = []

    async with aiohttp.ClientSession(connector=connector) as session:
        # Pre-fetch live event IDs if all_matches scenario is selected
        event_ids = []
        if sc_all_matches:
            event_ids = await fetch_live_event_ids(session, headers)

        for index in range(total_requests):
            if state.stop_event.is_set():
                break
            now = time.perf_counter()
            if (now - start_time) >= duration:
                break

            due = start_time + (index * interval)
            pause = due - now
            if pause > 0:
                await asyncio.sleep(pause)

            # Route request based on selected scenario checkboxes
            req_url = url
            req_body = body
            req_method = method

            if sc_main and sc_all_matches:
                # Alternate between /main-events and random match /event
                if index % 2 == 0:
                    req_url = "https://api.7xwon.com/main-events"
                    req_method = "POST"
                    req_body = body or {"type": 0, "odds": 1, "code": False, "s": 1, "t": False}
                else:
                    req_url = "https://api.7xwon.com/event"
                    req_method = "POST"
                    eid = random.choice(event_ids) if event_ids else 36223071
                    req_body = {"id": eid}
            elif sc_all_matches:
                req_url = "https://api.7xwon.com/event"
                req_method = "POST"
                eid = random.choice(event_ids) if event_ids else 36223071
                req_body = {"id": eid}
            elif sc_main:
                req_url = "https://api.7xwon.com/main-events"
                req_method = "POST"
                req_body = body or {"type": 0, "odds": 1, "code": False, "s": 1, "t": False}

            task = asyncio.create_task(
                _async_worker(session, req_method, req_url, headers, req_body, effective_timeout, sem, state, results, sc_cascading)
            )
            tasks.append(task)

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    elapsed = max(time.perf_counter() - start_time, 1e-9)
    stopped = state.stop_event.is_set()

    if not results:
        return {
            "requests": 0, "successful": 0, "failed": 0, "achieved_rps": 0,
            "avg_ms": 0, "p95_ms": 0, "p99_ms": 0, "response_bytes_avg": 0,
            "total_mib": 0, "mib_per_second": 0, "statuses": {},
            "error_sample": None, "stopped": stopped,
        }

    latencies = sorted(item[1] for item in results)
    statuses = {}
    for status, _, _, _ in results:
        key = str(status) if status is not None else "ERR"
        statuses[key] = statuses.get(key, 0) + 1

    successful = sum(200 <= status < 400 for status, _, _, _ in results if status is not None)
    bytes_received = sum(item[2] for item in results)
    errors = [item[3] for item in results if item[3]]
    percentile = lambda percent: latencies[round((len(latencies) - 1) * percent / 100)]

    return {
        "requests": len(results),
        "successful": successful,
        "failed": len(results) - successful,
        "achieved_rps": round(len(results) / elapsed, 2),
        "avg_ms": round(statistics.mean(latencies), 1),
        "p95_ms": round(percentile(95), 1),
        "p99_ms": round(percentile(99), 1),
        "response_bytes_avg": round(statistics.mean(item[2] for item in results), 1),
        "total_mib": round(bytes_received / 1024**2, 3),
        "mib_per_second": round(bytes_received / elapsed / 1024**2, 3),
        "statuses": statuses,
        "error_sample": errors[0][:220] if errors else None,
        "stopped": stopped,
    }


def run_check(url, method, rps, duration, threads, timeout, token, body, state, scenarios):
    return asyncio.run(_async_run_check(url, method, rps, duration, threads, timeout, token, body, state, scenarios))


@app.get("/")
def index():
    return render_template("index.html", default_url=DEFAULT_URL, profile=system_profile())


@app.get("/api/system")
def system():
    return jsonify(system_profile())


@app.post("/api/run")
def run():
    data = request.get_json(silent=True) or {}
    try:
        url, method, rps, duration, threads, timeout, body = validate_config(data)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    if data.get("confirm") is not True:
        return jsonify({"error": "Test sahipligi onayi gerekli."}), 400

    scenarios = data.get("scenarios") or {
        "main": bool(data.get("sc_main")),
        "all_matches": bool(data.get("sc_all_matches")),
        "cascading": bool(data.get("sc_cascading"))
    }

    run_id = str(data.get("run_id", "")).strip()
    state = RunState(rps * duration)
    if run_id:
        with ACTIVE_RUNS_LOCK:
            ACTIVE_RUNS[run_id] = state
    try:
        result = run_check(
            url, method, rps, duration, threads, timeout,
            str(data.get("token", "")).strip(), body, state, scenarios
        )
    finally:
        with state.lock:
            state.finished = True
        if run_id:
            with ACTIVE_RUNS_LOCK:
                ACTIVE_RUNS.pop(run_id, None)
    return jsonify(result)


@app.post("/api/stop")
def stop():
    data = request.get_json(silent=True) or {}
    run_id = str(data.get("run_id", "")).strip()
    with ACTIVE_RUNS_LOCK:
        state = ACTIVE_RUNS.get(run_id)
    if state is None:
        return jsonify({"stopped": False, "error": "Aktif test bulunamadi."}), 404
    state.stop_event.set()
    return jsonify({"stopped": True})


@app.get("/api/stream")
def stream():
    run_id = request.args.get("run_id", "").strip()

    def gen():
        state = None
        for _ in range(60):
            with ACTIVE_RUNS_LOCK:
                state = ACTIVE_RUNS.get(run_id)
            if state is not None:
                break
            time.sleep(0.05)
        if state is None:
            yield "event: end\ndata: {}\n\n"
            return
        while True:
            snapshot = state.snapshot()
            yield f"data: {json.dumps(snapshot)}\n\n"
            if snapshot["finished"]:
                break
            time.sleep(0.25)
        yield "event: end\ndata: {}\n\n"

    return Response(
        gen(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")), debug=False, threaded=True)
