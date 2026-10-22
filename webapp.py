"""Browser-based control panel for controlled API capacity checks."""

from __future__ import annotations

import os
import platform
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests
from flask import Flask, jsonify, render_template, request

try:
    import psutil
except ImportError:  # Docker installs psutil; retain a useful fallback.
    psutil = None


MAX_RPS = 100
MAX_DURATION = 60
MAX_THREADS = 32
DEFAULT_URL = "https://api.7xwon.com/main-events"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/131.0 Safari/537.36"
)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024


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


def run_check(url, method, rps, duration, threads, timeout, token, body):
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
    with ThreadPoolExecutor(max_workers=threads) as pool:
        for index in range(total):
            due = started + (index / rps)
            pause = due - time.perf_counter()
            if pause > 0:
                time.sleep(pause)
            futures.append(pool.submit(work))
        results = [future.result() for future in as_completed(futures)]

    elapsed = time.perf_counter() - started
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
    result = run_check(url, method, rps, duration, threads, timeout, str(data.get("token", "")).strip(), body)
    return jsonify(result)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")), debug=False)
