const form = document.querySelector('#test-form');
const runButton = document.querySelector('.run');
const titleEl = document.querySelector('#result-title');
const noteEl = document.querySelector('#result-note');
const badgeEl = document.querySelector('#status-badge');
const tilesEl = document.querySelector('#tiles');
const statusesEl = document.querySelector('#statuses');
const errorEl = document.querySelector('#error-sample');
const progressWrap = document.querySelector('#progress-wrap');
const progressBar = document.querySelector('#progress-bar');
const progressElapsed = document.querySelector('#progress-elapsed');
const progressTotal = document.querySelector('#progress-total');
const stopBtn = document.querySelector('#stop-btn');
const chartWrap = document.querySelector('#chart-wrap');
const chart = document.querySelector('#live-chart');
const chartMax = document.querySelector('#chart-max');
const ctx = chart.getContext('2d');

let timer = null;
let currentRunId = null;
let es = null;

document.querySelector('#apply-suggestion').addEventListener('click', () => {
  const s = window.SYSTEM_PROFILE.suggested;
  for (const key of ['rps', 'threads', 'duration', 'timeout']) {
    document.querySelector(`#${key}`).value = s[key];
  }
});

const presetMainBtn = document.querySelector('#preset-main');
if (presetMainBtn) {
  presetMainBtn.addEventListener('click', () => {
    document.querySelector('#url').value = 'https://api.7xwon.com/main-events';
    document.querySelector('#method').value = 'POST';
    document.querySelector('#body').value = JSON.stringify({
      type: 0,
      odds: 1,
      code: false,
      s: 1,
      t: false
    }, null, 2);
  });
}

const presetEventBtn = document.querySelector('#preset-event');
if (presetEventBtn) {
  presetEventBtn.addEventListener('click', () => {
    document.querySelector('#url').value = 'https://api.7xwon.com/event';
    document.querySelector('#method').value = 'POST';
    document.querySelector('#body').value = JSON.stringify({
      id: 36223071
    }, null, 2);
  });
}

function setBadge(state, text) {
  badgeEl.className = `badge ${state}`;
  badgeEl.textContent = text;
}

function startProgress(duration) {
  progressWrap.hidden = false;
  progressTotal.textContent = `${duration}s`;
  const started = performance.now();
  timer = setInterval(() => {
    const elapsed = (performance.now() - started) / 1000;
    const pct = Math.min(elapsed / duration, 1) * 100;
    progressBar.style.width = `${pct}%`;
    progressElapsed.textContent = `${elapsed.toFixed(1)}s`;
  }, 100);
}

function stopProgress(done) {
  if (timer) { clearInterval(timer); timer = null; }
  if (done) progressBar.style.width = '100%';
}

function tile(value, label, variant = '') {
  return `<div class="tile ${variant}"><div class="v">${value}</div><div class="k">${label}</div></div>`;
}

// Works for both a live snapshot ({done, ok}) and the final result ({requests, successful}).
function renderTiles(d) {
  const done = d.requests ?? d.done ?? 0;
  const ok = d.successful ?? d.ok ?? 0;
  const failed = d.failed ?? 0;
  tilesEl.innerHTML = [
    tile(`${ok}/${done}`, 'Basarili', failed > 0 ? 'err' : 'ok'),
    tile(d.achieved_rps ?? 0, 'Gercek RPS', 'accent'),
    tile(failed, 'Hata', failed > 0 ? 'err' : ''),
    tile(`${d.avg_ms ?? 0} ms`, 'Ortalama', ''),
    tile(`${d.p95_ms ?? 0} ms`, 'p95', 'warn'),
    tile(`${d.p99_ms ?? 0} ms`, 'p99', 'warn'),
  ].join('');
}

function renderStatuses(statuses) {
  statusesEl.innerHTML = Object.entries(statuses || {}).map(([code, count]) => {
    const good = /^[23]\d\d$/.test(code);
    return `<span class="pill ${good ? 'good' : 'bad'}">${code}: ${count}</span>`;
  }).join('');
}

function drawChart(values) {
  const w = chart.clientWidth || 560;
  const h = 150;
  if (chart.width !== w) chart.width = w;
  chart.height = h;
  ctx.clearRect(0, 0, w, h);
  if (!values || !values.length) { chartMax.textContent = ''; return; }
  const top = Math.max(...values, 1);
  const pad = 6;
  const step = (w - 2 * pad) / Math.max(values.length - 1, 1);

  ctx.strokeStyle = '#2b2f38';
  ctx.lineWidth = 1;
  for (const frac of [0.25, 0.5, 0.75]) {
    const y = pad + (h - 2 * pad) * frac;
    ctx.beginPath();
    ctx.moveTo(pad, y);
    ctx.lineTo(w - pad, y);
    ctx.stroke();
  }

  ctx.strokeStyle = '#4c8dff';
  ctx.lineWidth = 2;
  ctx.beginPath();
  values.forEach((v, i) => {
    const x = pad + i * step;
    const y = h - pad - (h - 2 * pad) * (v / top);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.stroke();
  chartMax.textContent = `max ${Math.round(top)} ms`;
}

function openStream(runId) {
  if (!window.EventSource) return;
  es = new EventSource(`/api/stream?run_id=${encodeURIComponent(runId)}`);
  es.onmessage = (event) => {
    let snap;
    try { snap = JSON.parse(event.data); } catch { return; }
    if (!snap || snap.finished === undefined) return;
    renderTiles(snap);
    renderStatuses(snap.statuses);
    drawChart(snap.recent);
    noteEl.textContent = `${snap.done} istek - ${snap.total_mib} MiB`;
  };
  es.addEventListener('end', () => { if (es) { es.close(); es = null; } });
  es.onerror = () => { /* the awaited /api/run still delivers the final result */ };
}

function closeStream() {
  if (es) { es.close(); es = null; }
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();

  const bodyText = form.body.value.trim();
  let body = null;
  if (bodyText) {
    try { body = JSON.parse(bodyText); }
    catch {
      setBadge('error', 'hata');
      titleEl.textContent = 'JSON body gecersiz';
      noteEl.textContent = 'Body alanini duzeltip tekrar dene.';
      return;
    }
  }

  currentRunId = (crypto.randomUUID && crypto.randomUUID()) || String(Date.now() + Math.random());
  const payload = Object.fromEntries(new FormData(form));
  const duration = +payload.duration;
  
  const scenarios = {
    main: !!document.querySelector('#sc_main')?.checked,
    all_matches: !!document.querySelector('#sc_all_matches')?.checked,
    cascading: !!document.querySelector('#sc_cascading')?.checked
  };

  Object.assign(payload, {
    rps: +payload.rps,
    threads: +payload.threads,
    duration,
    timeout: +payload.timeout,
    body,
    confirm: document.querySelector('#confirm').checked,
    run_id: currentRunId,
    scenarios,
  });

  // running state: swap the start button for a stop button
  runButton.hidden = true;
  stopBtn.hidden = false;
  stopBtn.disabled = false;
  stopBtn.textContent = 'Durdur';
  setBadge('running', 'calisiyor');
  titleEl.textContent = 'Test suruyor';
  noteEl.textContent = 'Istekler gonderiliyor...';
  tilesEl.innerHTML = '';
  statusesEl.innerHTML = '';
  errorEl.hidden = true;
  chartWrap.hidden = false;
  drawChart([]);
  progressBar.style.width = '0%';
  startProgress(duration);
  openStream(currentRunId);

  try {
    const response = await fetch('/api/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Test baslatilamadi.');

    stopProgress(true);
    if (data.stopped) {
      setBadge('idle', 'durduruldu');
      titleEl.textContent = 'Test durduruldu';
      noteEl.textContent = data.requests
        ? `${data.requests} istek tamamlandi - ${data.total_mib} MiB`
        : 'Test veri toplanmadan durduruldu.';
    } else {
      const hasFail = data.failed > 0;
      setBadge(hasFail ? 'error' : 'done', hasFail ? 'hata var' : 'tamam');
      titleEl.textContent = hasFail ? 'Test tamamlandi - hata var' : 'Test tamamlandi';
      noteEl.textContent = `${data.requests} istek - ${data.total_mib} MiB toplam veri`;
    }
    if (data.requests) {
      renderTiles(data);
      renderStatuses(data.statuses);
    }
    if (data.error_sample) {
      errorEl.hidden = false;
      errorEl.textContent = data.error_sample;
    }
  } catch (error) {
    stopProgress(false);
    setBadge('error', 'hata');
    titleEl.textContent = 'Test baslatilamadi';
    noteEl.textContent = error.message;
  } finally {
    closeStream();
    currentRunId = null;
    stopBtn.hidden = true;
    runButton.hidden = false;
    runButton.disabled = false;
    runButton.textContent = 'Testi baslat';
  }
});

stopBtn.addEventListener('click', async () => {
  if (!currentRunId) return;
  stopBtn.disabled = true;
  stopBtn.textContent = 'Durduruluyor...';
  noteEl.textContent = 'Test durduruluyor...';
  try {
    await fetch('/api/stop', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ run_id: currentRunId }),
    });
  } catch { /* the pending /api/run will still resolve with partial data */ }
});
