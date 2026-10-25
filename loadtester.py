"""
Load Tester - a small GUI load/latency testing tool.

Standard library only (tkinter + urllib), so it runs with:
    python loadtester.py

Intended for testing APIs you own or are authorized to test.
"""

import json
import os
import sys
import time
import queue
import threading
import urllib.request
import urllib.error
from collections import Counter, deque

try:
    import requests
except ImportError:  # optional; stdlib fallback remains available
    requests = None

# PyInstaller bundles Tcl/Tk beside the executable. Point Tkinter at those
# files before importing it so the portable EXE does not depend on a local
# Python installation.
if getattr(sys, "frozen", False):
    _bundle = getattr(sys, "_MEIPASS", "")
    os.environ.setdefault("TCL_LIBRARY", os.path.join(_bundle, "tcl", "tcl8.6"))
    os.environ.setdefault("TK_LIBRARY", os.path.join(_bundle, "tcl", "tk8.6"))

import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext, messagebox


# --- palette (dark, neutral, single blue accent) ---------------------------
BG = "#15171c"
PANEL = "#1d2026"
BORDER = "#2b2f38"
FG = "#e6e8ec"
MUTED = "#8b909b"
ACCENT = "#4c8dff"
OK = "#3fb950"
WARN = "#e3b341"
ERR = "#f06a5d"

DEFAULT_PAYLOAD = {"type": 0, "odds": 1, "code": False, "s": 1, "t": False}


# ---------------------------------------------------------------------------
# Stats: thread-safe counters shared between worker threads and the UI poll.
# ---------------------------------------------------------------------------
class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        with self.lock:
            self.sent = 0
            self.done = 0
            self.ok = 0
            self.failed = 0
            self.bytes_received = 0
            self.status = Counter()
            self.latencies = []
            self.recent = deque(maxlen=200)  # for the live chart
            self.started_at = None

    def mark_sent(self):
        with self.lock:
            self.sent += 1

    def record(self, status, latency_ms, size, error):
        with self.lock:
            self.done += 1
            self.latencies.append(latency_ms)
            self.bytes_received += size or 0
            self.recent.append(latency_ms)
            if error is None and status is not None and 200 <= status < 400:
                self.ok += 1
            else:
                self.failed += 1
            key = f"{status}" if status is not None else "ERR"
            self.status[key] += 1

    def snapshot(self):
        with self.lock:
            lat = list(self.latencies)
            data = {
                "sent": self.sent,
                "done": self.done,
                "ok": self.ok,
                "failed": self.failed,
                "bytes_received": self.bytes_received,
                "status": dict(self.status),
                "recent": list(self.recent),
                "elapsed": (time.time() - self.started_at) if self.started_at else 0,
            }
        if lat:
            lat.sort()
            data["avg"] = sum(lat) / len(lat)
            data["min"] = lat[0]
            data["max"] = lat[-1]
            data["p50"] = _pct(lat, 50)
            data["p95"] = _pct(lat, 95)
            data["p99"] = _pct(lat, 99)
        else:
            for k in ("avg", "min", "max", "p50", "p95", "p99"):
                data[k] = 0.0
        return data


def _pct(sorted_values, pct):
    if not sorted_values:
        return 0.0
    idx = min(len(sorted_values) - 1,
              int(round((pct / 100.0) * (len(sorted_values) - 1))))
    return sorted_values[idx]


# ---------------------------------------------------------------------------
# Engine: a scheduler paces requests at target RPS; a pool of workers sends
# them concurrently. Both run until duration elapses or stop is requested.
# ---------------------------------------------------------------------------
class Engine:
    def __init__(self, cfg, stats, log_fn, done_fn):
        self.cfg = cfg
        self.stats = stats
        self.log = log_fn
        self.done_fn = done_fn
        self.stop_event = threading.Event()
        self.jobs = queue.Queue(maxsize=max(cfg["threads"] * 4, 16))
        self._threads = []

    def start(self):
        self.stats.reset()
        with self.stats.lock:
            self.stats.started_at = time.time()
        for i in range(self.cfg["threads"]):
            t = threading.Thread(target=self._worker, args=(i + 1,), daemon=True)
            t.start()
            self._threads.append(t)
        threading.Thread(target=self._scheduler, daemon=True).start()

    def stop(self):
        self.stop_event.set()

    # scheduler: enqueue one job every 1/rps seconds for the duration.
    def _scheduler(self):
        rps = self.cfg["rps"]
        interval = 1.0 / rps if rps > 0 else 0
        duration = self.cfg["duration"]
        start = time.time()
        while not self.stop_event.is_set():
            if duration and (time.time() - start) >= duration:
                break
            try:
                self.jobs.put(1, timeout=0.5)
                self.stats.mark_sent()
            except queue.Full:
                continue
            if interval:
                time.sleep(interval)
        # signal workers to drain and exit
        for _ in self._threads:
            try:
                self.jobs.put(None, timeout=1)
            except queue.Full:
                pass
        for t in self._threads:
            t.join(timeout=self.cfg["timeout"] + 2)
        elapsed = time.time() - start
        self.log(f"Bitti. Sure: {elapsed:.1f}s", "muted")
        self.done_fn()

    def _worker(self, wid):
        data = self.cfg["body"]
        headers = self.cfg["headers"]
        url = self.cfg["url"]
        method = self.cfg["method"]
        timeout = self.cfg["timeout"]
        # A session per worker keeps connections alive.  This removes repeated
        # TCP/TLS setup from the measurement while remaining thread-safe.
        session = requests.Session() if requests is not None else None
        try:
            while True:
                item = self.jobs.get()
                if item is None:
                    self.jobs.task_done()
                    break
                status, latency, _size, error = _do_request(
                    url, method, data, headers, timeout, session=session)
                self.stats.record(status, latency, _size, error)
                if error:
                    self.log(f"T{wid} HATA: {error}", "err")
                    if "HTTP 403" in error and "1010" in error:
                        self.stop_event.set()
                self.jobs.task_done()
        finally:
            if session is not None:
                session.close()


def _do_request(url, method, data, headers, timeout, session=None):
    if requests is not None:
        return _do_request_requests(url, method, data, headers, timeout, session)

    body = data if method in ("POST", "PUT", "PATCH") else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            size = 0
            while True:
                chunk = resp.read(16384)
                if not chunk:
                    break
                size += len(chunk)
            return resp.status, (time.perf_counter() - start) * 1000, size, None
    except urllib.error.HTTPError as e:
        try:
            detail = e.read(500).decode("utf-8", errors="replace").strip()
        except Exception:  # noqa: BLE001
            detail = ""
        message = f"HTTP {e.code}"
        if detail:
            message += f": {detail}"
        return e.code, (time.perf_counter() - start) * 1000, 0, message
    except Exception as e:  # noqa: BLE001
        return None, (time.perf_counter() - start) * 1000, 0, str(e)


def _do_request_requests(url, method, data, headers, timeout, session=None):
    """Use Requests with stream=True to prevent massive RAM and GC spikes on local PC."""
    body = data if method in ("POST", "PUT", "PATCH") else None
    start = time.perf_counter()
    try:
        client = session or requests
        response = client.request(
            method, url, data=body, headers=headers, timeout=timeout, stream=True)
        size = 0
        if response.status_code >= 400:
            raw = response.raw.read(500)
            detail = raw.decode("utf-8", errors="replace").strip()
            message = f"HTTP {response.status_code}"
            if detail:
                message += f": {detail}"
            return response.status_code, (time.perf_counter() - start) * 1000, len(raw), message

        # Stream chunks without loading entire payload into RAM
        for chunk in response.iter_content(chunk_size=16384):
            size += len(chunk)
        return response.status_code, (time.perf_counter() - start) * 1000, size, None
    except requests.RequestException as e:
        return None, (time.perf_counter() - start) * 1000, 0, str(e)


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
class App:
    def __init__(self, root):
        self.root = root
        self.stats = Stats()
        self.engine = None
        self.running = False

        root.title("Load Tester")
        root.configure(bg=BG)
        root.geometry("1040x680")
        root.minsize(900, 600)

        self._init_style()
        self._build_layout()
        self._tick()

    def _init_style(self):
        s = ttk.Style()
        try:
            s.theme_use("clam")
        except tk.TclError:
            pass
        s.configure(".", background=PANEL, foreground=FG, fieldbackground=PANEL,
                    bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER)
        s.configure("TFrame", background=PANEL)
        s.configure("Bg.TFrame", background=BG)
        s.configure("TLabel", background=PANEL, foreground=FG)
        s.configure("Muted.TLabel", background=PANEL, foreground=MUTED)
        s.configure("Title.TLabel", background=BG, foreground=FG,
                    font=("Segoe UI", 13, "bold"))
        s.configure("TEntry", foreground=FG, fieldbackground="#0f1115", insertcolor=FG)
        s.configure("TCombobox", foreground=FG, fieldbackground="#0f1115")
        s.configure("Accent.TButton", background=ACCENT, foreground="#ffffff",
                    font=("Segoe UI", 10, "bold"), borderwidth=0, padding=8)
        s.map("Accent.TButton",
              background=[("active", "#3f78dd"), ("disabled", "#2b2f38")])
        s.configure("Stop.TButton", background=ERR, foreground="#ffffff",
                    font=("Segoe UI", 10, "bold"), borderwidth=0, padding=8)
        s.map("Stop.TButton",
              background=[("active", "#d2574b"), ("disabled", "#2b2f38")])

    def _build_layout(self):
        root = self.root
        header = ttk.Frame(root, style="Bg.TFrame")
        header.pack(fill="x", padx=16, pady=(14, 8))
        ttk.Label(header, text="Load Tester", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="kendi servislerin icin yuk / latency testi",
                  background=BG, foreground=MUTED).pack(side="left", padx=12)

        body = ttk.Frame(root, style="Bg.TFrame")
        body.pack(fill="both", expand=True, padx=16, pady=(0, 14))
        body.columnconfigure(0, weight=0, minsize=380)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        self._build_config(body)
        self._build_results(body)

    # --- left: configuration ---
    def _build_config(self, parent):
        card = tk.Frame(parent, bg=PANEL, highlightbackground=BORDER,
                        highlightthickness=1, bd=0)
        card.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        pad = {"padx": 14, "pady": (0, 4)}

        def field_label(text):
            ttk.Label(card, text=text, style="Muted.TLabel",
                      font=("Segoe UI", 9)).pack(anchor="w", padx=14, pady=(12, 2))

        field_label("URL")
        self.url_var = tk.StringVar(value="https://api.7xwon.com/main-events")
        ttk.Entry(card, textvariable=self.url_var).pack(fill="x", **pad)

        row = ttk.Frame(card)
        row.pack(fill="x", padx=14, pady=(10, 0))
        mcol = ttk.Frame(row)
        mcol.pack(side="left")
        ttk.Label(mcol, text="Method", style="Muted.TLabel",
                  font=("Segoe UI", 9)).pack(anchor="w")
        self.method_var = tk.StringVar(value="POST")
        ttk.Combobox(mcol, textvariable=self.method_var, width=8, state="readonly",
                     values=["GET", "POST", "PUT", "PATCH", "DELETE"]).pack()

        field_label("User-Agent (opsiyonel)")
        self.ua_var = tk.StringVar(value="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36")
        ttk.Entry(card, textvariable=self.ua_var).pack(fill="x", **pad)

        field_label("Bearer token (opsiyonel)")
        self.token_var = tk.StringVar(value="")
        ttk.Entry(card, textvariable=self.token_var, show="•").pack(fill="x", **pad)

        nums = ttk.Frame(card)
        nums.pack(fill="x", padx=14, pady=(12, 0))
        self.rps_var = tk.StringVar(value="5")
        self.dur_var = tk.StringVar(value="30")
        self.thr_var = tk.StringVar(value="8")
        self.to_var = tk.StringVar(value="10")
        self._num_field(nums, "RPS", self.rps_var, 0)
        self._num_field(nums, "Sure (s)", self.dur_var, 1)
        self._num_field(nums, "Thread", self.thr_var, 2)
        self._num_field(nums, "Timeout (s)", self.to_var, 3)
        ttk.Label(card, text="Sure 0 = durdurana kadar", style="Muted.TLabel",
                  font=("Segoe UI", 8)).pack(anchor="w", padx=14, pady=(6, 0))

        hrow = ttk.Frame(card)
        hrow.pack(fill="x", padx=14, pady=(12, 2))
        ttk.Label(hrow, text="Body (JSON)", style="Muted.TLabel",
                  font=("Segoe UI", 9)).pack(side="left")
        ttk.Button(hrow, text="Dosyadan", command=self._load_payload).pack(side="right")
        self.body_text = scrolledtext.ScrolledText(
            card, height=7, bg="#0f1115", fg=FG, insertbackground=FG,
            relief="flat", font=("Consolas", 10), highlightthickness=1,
            highlightbackground=BORDER)
        self.body_text.pack(fill="x", padx=14, pady=(2, 4))
        self.body_text.insert("1.0", json.dumps(DEFAULT_PAYLOAD, indent=2))

        btns = ttk.Frame(card)
        btns.pack(fill="x", padx=14, pady=14, side="bottom")
        self.start_btn = ttk.Button(btns, text="Baslat", style="Accent.TButton",
                                    command=self._start)
        self.start_btn.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.stop_btn = ttk.Button(btns, text="Durdur", style="Stop.TButton",
                                   command=self._stop, state="disabled")
        self.stop_btn.pack(side="left", fill="x", expand=True, padx=(6, 0))

    def _num_field(self, parent, label, var, col):
        parent.columnconfigure(col, weight=1)
        cell = ttk.Frame(parent)
        cell.grid(row=0, column=col, sticky="ew", padx=(0 if col == 0 else 6, 0))
        ttk.Label(cell, text=label, style="Muted.TLabel",
                  font=("Segoe UI", 9)).pack(anchor="w")
        ttk.Entry(cell, textvariable=var, width=6).pack(fill="x")

    # --- right: results ---
    def _build_results(self, parent):
        wrap = ttk.Frame(parent, style="Bg.TFrame")
        wrap.grid(row=0, column=1, sticky="nsew")
        wrap.rowconfigure(2, weight=1)
        wrap.columnconfigure(0, weight=1)

        tiles = ttk.Frame(wrap, style="Bg.TFrame")
        tiles.grid(row=0, column=0, sticky="ew")
        self.tiles = {}
        specs = [("sent", "Gonderilen", FG), ("ok", "Basarili", OK),
                 ("failed", "Hata", ERR), ("rps", "Gercek RPS", ACCENT),
                 ("avg", "Ort ms", FG), ("p95", "p95 ms", WARN),
                 ("p99", "p99 ms", WARN)]
        for i, (key, label, color) in enumerate(specs):
            tiles.columnconfigure(i, weight=1)
            self.tiles[key] = self._tile(tiles, label, color, i)

        chart_card = tk.Frame(wrap, bg=PANEL, highlightbackground=BORDER,
                              highlightthickness=1)
        chart_card.grid(row=1, column=0, sticky="ew", pady=12)
        ttk.Label(chart_card, text="Latency (son istekler)", style="Muted.TLabel",
                  font=("Segoe UI", 9)).pack(anchor="w", padx=12, pady=(8, 0))
        self.canvas = tk.Canvas(chart_card, height=150, bg="#0f1115",
                                highlightthickness=0)
        self.canvas.pack(fill="x", padx=12, pady=10)

        log_card = tk.Frame(wrap, bg=PANEL, highlightbackground=BORDER,
                            highlightthickness=1)
        log_card.grid(row=2, column=0, sticky="nsew")
        bar = ttk.Frame(log_card)
        bar.pack(fill="x", padx=12, pady=(8, 0))
        ttk.Label(bar, text="Log", style="Muted.TLabel",
                  font=("Segoe UI", 9)).pack(side="left")
        self.status_line = tk.StringVar(value="hazir")
        ttk.Label(bar, textvariable=self.status_line, style="Muted.TLabel",
                  font=("Consolas", 9)).pack(side="right")
        self.log_text = scrolledtext.ScrolledText(
            log_card, bg="#0f1115", fg=MUTED, relief="flat",
            font=("Consolas", 9), highlightthickness=0, height=8)
        self.log_text.pack(fill="both", expand=True, padx=12, pady=10)
        self.log_text.tag_config("err", foreground=ERR)
        self.log_text.tag_config("ok", foreground=OK)
        self.log_text.tag_config("muted", foreground=MUTED)
        self.log_text.tag_config("accent", foreground=ACCENT)

    def _tile(self, parent, label, color, col):
        card = tk.Frame(parent, bg=PANEL, highlightbackground=BORDER,
                        highlightthickness=1)
        card.grid(row=0, column=col, sticky="ew", padx=(0 if col == 0 else 6, 0))
        val = tk.Label(card, text="0", bg=PANEL, fg=color,
                       font=("Segoe UI", 16, "bold"))
        val.pack(anchor="w", padx=10, pady=(8, 0))
        tk.Label(card, text=label, bg=PANEL, fg=MUTED,
                 font=("Segoe UI", 8)).pack(anchor="w", padx=10, pady=(0, 8))
        return val

    # --- actions ---
    def _load_payload(self):
        path = filedialog.askopenfilename(
            filetypes=[("JSON", "*.json"), ("All", "*.*")])
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            json.loads(content)  # validate
            self.body_text.delete("1.0", "end")
            self.body_text.insert("1.0", content)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Hata", f"JSON okunamadi: {e}")

    def _build_cfg(self):
        try:
            rps = float(self.rps_var.get())
            duration = int(self.dur_var.get())
            threads = max(1, int(self.thr_var.get()))
            timeout = float(self.to_var.get())
        except ValueError:
            messagebox.showerror("Hata", "RPS / Sure / Thread / Timeout sayi olmali.")
            return None

        url = self.url_var.get().strip()
        if not url.startswith(("http://", "https://")):
            messagebox.showerror("Hata", "Gecerli bir URL gir (http:// veya https://).")
            return None

        method = self.method_var.get()
        headers = {"Accept": "application/json"}
        user_agent = self.ua_var.get().strip()
        if user_agent:
            headers["User-Agent"] = user_agent
        body = None
        if method in ("POST", "PUT", "PATCH"):
            raw = self.body_text.get("1.0", "end").strip()
            if raw:
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError as e:
                    messagebox.showerror("Hata", f"Body gecerli JSON degil: {e}")
                    return None
                body = json.dumps(parsed).encode("utf-8")
                headers["Content-Type"] = "application/json"
        token = self.token_var.get().strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"

        return {"url": url, "method": method, "headers": headers, "body": body,
                "rps": rps, "duration": duration, "threads": threads,
                "timeout": timeout}

    def _start(self):
        if self.running:
            return
        cfg = self._build_cfg()
        if not cfg:
            return
        self.log_text.delete("1.0", "end")
        self._log(f"Baslatiliyor: {cfg['method']} {cfg['url']} @ {cfg['rps']} RPS, "
                  f"{cfg['threads']} thread", "accent")
        self.running = True
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.engine = Engine(cfg, self.stats, self._log_threadsafe, self._on_done)
        self.engine.start()

    def _stop(self):
        if self.engine:
            self.engine.stop()
            self._log("Durduruluyor...", "muted")
        self.stop_btn.config(state="disabled")

    def _on_done(self):
        # called from the scheduler thread -> marshal back to the UI thread
        self.root.after(0, self._finish)

    def _finish(self):
        self.running = False
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")

    # --- logging ---
    def _log(self, msg, tag="muted"):
        ts = time.strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{ts}] {msg}\n", tag)
        self.log_text.see("end")

    def _log_threadsafe(self, msg, tag="muted"):
        self.root.after(0, lambda: self._log(msg, tag))

    # --- periodic UI refresh ---
    def _tick(self):
        snap = self.stats.snapshot()
        real_rps = snap["done"] / snap["elapsed"] if snap["elapsed"] > 0.2 else 0
        self.tiles["sent"].config(text=str(snap["sent"]))
        self.tiles["ok"].config(text=str(snap["ok"]))
        self.tiles["failed"].config(text=str(snap["failed"]))
        self.tiles["rps"].config(text=f"{real_rps:.1f}")
        self.tiles["avg"].config(text=f"{snap['avg']:.0f}")
        self.tiles["p95"].config(text=f"{snap['p95']:.0f}")
        self.tiles["p99"].config(text=f"{snap['p99']:.0f}")

        mb = snap["bytes_received"] / (1024 * 1024)
        mbps = (mb / snap["elapsed"]) if snap["elapsed"] > 0 else 0

        status_txt = "  ".join(f"{k}:{v}" for k, v in sorted(snap["status"].items()))
        state = "calisiyor" if self.running else "hazir"
        transfer = f"{mb:.2f} MB  |  {mbps:.2f} MB/s"
        self.status_line.set(f"{state}  |  {transfer}  |  {status_txt}" if status_txt else f"{state}  |  {transfer}")

        self._draw_chart(snap["recent"], snap.get("max", 0))
        self.root.after(250, self._tick)

    def _draw_chart(self, values, vmax):
        c = self.canvas
        c.delete("all")
        w = c.winfo_width() or 600
        h = c.winfo_height() or 150
        if not values:
            return
        top = max(vmax, max(values), 1.0)
        pad = 6
        n = len(values)
        step = (w - 2 * pad) / max(n - 1, 1)
        for frac in (0.25, 0.5, 0.75):  # gridlines
            y = pad + (h - 2 * pad) * frac
            c.create_line(pad, y, w - pad, y, fill=BORDER)
        pts = []
        for i, v in enumerate(values):
            x = pad + i * step
            y = h - pad - (h - 2 * pad) * (v / top)
            pts.extend((x, y))
        if len(pts) >= 4:
            c.create_line(*pts, fill=ACCENT, width=2, smooth=True)
        c.create_text(w - pad, pad + 4, text=f"max {top:.0f} ms",
                      fill=MUTED, anchor="ne", font=("Consolas", 8))


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
