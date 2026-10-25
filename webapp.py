"""Browser-based control panel for controlled API capacity checks."""

from __future__ import annotations

import json
import os
import platform
import statistics
import threading
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests
from flask import Flask, Response, jsonify, render_template, request

try:
    import psutil
except ImportError:  # Docker installs psutil; retain a useful fallback.
    psutil = None


MAX_RPS = 100
MAX_DURATION = 3600
MAX_THREADS = 32
DEFAULT_URL = "https://api.7xwon.com/main-events"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/131.0 Safari/537.36"
)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024

# In-flight runs keyed by a client-supplied run id, so /api/stop can cancel a
# test while /api/run is still blocking in another request thread.
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

    # These values are deliberately client-side conservative. They only size
    # this tool's connection pool; they do not predict origin capacity.
    threads = min(MAX_THREADS, max(2, cores * 2))
    rps = min(MAX_RPS, max(5, cores * 10))
    if memory_gib is not None and memory_gib < 4:
        rps = min(rps, 20)
        threads = min(threads, 8)
    return {
        "os": f"{platform.system()} {platform.release()}",
        "cores": cores,
        "memory_gib": memory_gib,
        "suggested": {"rps": rps, "threads": threads, "duration": 20, "timeout": 10},
    }


def validate_config(data):
    url = str(data.get("url", "")).strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Geçerli bir http:// veya https:// URL gir.")

    method = str(data.get("method", "GET")).upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        raise ValueError("Geçersiz HTTP metodu.")

    try:
        rps = int(data.get("rps", 1))
        duration = int(data.get("duration", 10))
        threads = int(data.get("threads", 1))
        timeout = float(data.get("timeout", 10))
    except (TypeError, ValueError) as exc:
        raise ValueError("RPS, süre, thread ve timeout sayı olmalı.") from exc

    if not 1 <= rps <= MAX_RPS:
        raise ValueError(f"RPS 1–{MAX_RPS} arasında olmalı.")
    if not 1 <= duration <= MAX_DURATION:
        raise ValueError(f"Süre 1–{MAX_DURATION} saniye arasında olmalı.")
    if not 1 <= threads <= MAX_THREADS:
        raise ValueError(f"Thread 1–{MAX_THREADS} arasında olmalı.")
    if not 1 <= timeout <= 30:
        raise ValueError("Timeout 1–30 saniye arasında olmalı.")

    body = None
    if method in {"POST", "PUT", "PATCH"}:
        body = data.get("body") or None
        if body is not None and not isinstance(body, (dict, list)):
            raise ValueError("JSON body nesne veya dizi olmalı.")
    return url, method, rps, duration, threads, timeout, body


def one_request(session, url, method, headers, timeout, body):
    started = time.perf_counter()
    try:
        response = session.request(method, url, headers=headers, json=body, timeout=timeout)
        elapsed = (time.perf_counter() - started) * 1000
        return response.status_code, elapsed, len(response.content), None
    except requests.RequestException as exc:
        return None, (time.perf_counter() - started) * 1000, 0, str(exc)


def run_check(url, method, rps, duration, threads, timeout, token, body, state):
    stop_event = state.stop_event
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    local = threading.local()

    def work():
        if not hasattr(local, "session"):
            local.session = requests.Session()
        return one_request(local.session, url, method, headers, timeout, body)

    total = rps * duration
    started = time.perf_counter()
    futures = []
    results = []
    with ThreadPoolExecutor(max_workers=threads) as pool:
        for index in range(total):
            if stop_event.is_set():
                break
            due = started + (index / rps)
            pause = due - time.perf_counter()
            if pause > 0:
                # Interruptible wait: a stop is honoured within the pacing gap
                # instead of sleeping through it.
                if stop_event.wait(pause):
                    break
            futures.append(pool.submit(work))
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            state.record(*result)  # feed live counters consumed by /api/stream

    elapsed = max(time.perf_counter() - started, 1e-9)
    stopped = stop_event.is_set()
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
    # The confirmation is explicit so a pasted configuration cannot silently
    # start traffic against an endpoint.
    if data.get("confirm") is not True:
        return jsonify({"error": "Test sahipliği onayı gerekli."}), 400

    run_id = str(data.get("run_id", "")).strip()
    state = RunState(rps * duration)
    if run_id:
        with ACTIVE_RUNS_LOCK:
            ACTIVE_RUNS[run_id] = state
    try:
        result = run_check(
            url, method, rps, duration, threads, timeout,
            str(data.get("token", "")).strip(), body, state,
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
        return jsonify({"stopped": False, "error": "Aktif test bulunamadı."}), 404
    state.stop_event.set()
    return jsonify({"stopped": True})


@app.get("/api/stream")
def stream():
    run_id = request.args.get("run_id", "").strip()

    def gen():
        state = None
        for _ in range(60):  # wait up to ~3s for the run to register
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
