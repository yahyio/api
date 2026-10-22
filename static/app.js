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

let timer = null;

document.querySelector('#apply-suggestion').addEventListener('click', () => {
  const s = window.SYSTEM_PROFILE.suggested;
  for (const key of ['rps', 'threads', 'duration', 'timeout']) {
    document.querySelector(`#${key}`).value = s[key];
  }
});

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
    if (elapsed >= duration) {
      noteEl.textContent = 'Sonuclar toplaniyor...';
    }
  }, 100);
}

function stopProgress(done) {
  if (timer) { clearInterval(timer); timer = null; }
  if (done) progressBar.style.width = '100%';
}

function tile(value, label, variant = '') {
  return `<div class="tile ${variant}"><div class="v">${value}</div><div class="k">${label}</div></div>`;
}

function renderResult(data) {
  const failVariant = data.failed > 0 ? 'err' : 'ok';
  tilesEl.innerHTML = [
    tile(`${data.successful}/${data.requests}`, 'Basarili', failVariant),
    tile(data.achieved_rps, 'Gercek RPS', 'accent'),
    tile(`${data.avg_ms} ms`, 'Ortalama', ''),
    tile(`${data.p95_ms} ms`, 'p95', 'warn'),
    tile(`${data.p99_ms} ms`, 'p99', 'warn'),
    tile(`${data.mib_per_second} MiB/s`, 'Veri hizi', ''),
  ].join('');

  statusesEl.innerHTML = Object.entries(data.statuses).map(([code, count]) => {
    const good = /^[23]\d\d$/.test(code);
    return `<span class="pill ${good ? 'good' : 'bad'}">${code}: ${count}</span>`;
  }).join('');

  if (data.error_sample) {
    errorEl.hidden = false;
    errorEl.textContent = data.error_sample;
  } else {
    errorEl.hidden = true;
  }
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

  const payload = Object.fromEntries(new FormData(form));
  const duration = +payload.duration;
  Object.assign(payload, {
    rps: +payload.rps,
    threads: +payload.threads,
    duration,
    timeout: +payload.timeout,
    body,
    confirm: document.querySelector('#confirm').checked,
  });

  // running state
  runButton.disabled = true;
  runButton.textContent = 'Test suruyor...';
  setBadge('running', 'calisiyor');
  titleEl.textContent = 'Test suruyor';
  noteEl.textContent = 'Istekler gonderiliyor...';
  tilesEl.innerHTML = '';
  statusesEl.innerHTML = '';
  errorEl.hidden = true;
  progressBar.style.width = '0%';
  startProgress(duration);

  try {
    const response = await fetch('/api/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Test baslatilamadi.');

    stopProgress(true);
    const hasFail = data.failed > 0;
    setBadge(hasFail ? 'error' : 'done', hasFail ? 'hata var' : 'tamam');
    titleEl.textContent = hasFail ? 'Test tamamlandi - hata var' : 'Test tamamlandi';
    noteEl.textContent = `${data.requests} istek - ${data.total_mib} MiB toplam veri`;
    renderResult(data);
  } catch (error) {
    stopProgress(false);
    setBadge('error', 'hata');
    titleEl.textContent = 'Test baslatilamadi';
    noteEl.textContent = error.message;
  } finally {
    runButton.disabled = false;
    runButton.textContent = 'Testi baslat';
  }
});
