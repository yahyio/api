const form = document.querySelector('#test-form');
const runButton = document.querySelector('.run');
const resultTitle = document.querySelector('#result-title');
const resultNote = document.querySelector('#result-note');
const metrics = document.querySelector('#metrics');

document.querySelector('#apply-suggestion').addEventListener('click', () => {
  const s = window.SYSTEM_PROFILE.suggested;
  for (const key of ['rps', 'threads', 'duration', 'timeout']) document.querySelector(`#${key}`).value = s[key];
});

function showMetrics(data) {
  const entries = [
    ['Başarılı', `${data.successful} / ${data.requests}`],
    ['Gerçek RPS', data.achieved_rps],
    ['Ortalama', `${data.avg_ms} ms`],
    ['p95', `${data.p95_ms} ms`],
    ['p99', `${data.p99_ms} ms`],
    ['Ortalama yanıt', `${(data.response_bytes_avg / 1024).toFixed(1)} KiB`],
    ['Toplam veri', `${data.total_mib} MiB`],
    ['Veri hızı', `${data.mib_per_second} MiB/s`],
    ['Durumlar', Object.entries(data.statuses).map(([code, count]) => `${code}: ${count}`).join(', ')],
  ];
  metrics.innerHTML = entries.map(([key, value]) => `<div><dt>${key}</dt><dd>${value}</dd></div>`).join('');
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const bodyText = form.body.value.trim();
  let body = null;
  try { body = bodyText ? JSON.parse(bodyText) : null; }
  catch { resultTitle.textContent = 'JSON body geçersiz'; resultNote.textContent = 'Body alanını düzeltip tekrar dene.'; return; }

  runButton.disabled = true;
  runButton.textContent = 'Ölçüm sürüyor…';
  resultTitle.textContent = 'Ölçüm sürüyor';
  resultNote.textContent = 'İstekler zaman sınırlı olarak gönderiliyor.';
  metrics.innerHTML = '';
  const payload = Object.fromEntries(new FormData(form));
  Object.assign(payload, { rps: +payload.rps, threads: +payload.threads, duration: +payload.duration, timeout: +payload.timeout, body, confirm: document.querySelector('#confirm').checked });
  try {
    const response = await fetch('/api/run', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload) });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Ölçüm başlatılamadı.');
    resultTitle.textContent = data.failed ? 'Ölçüm tamamlandı — hata var' : 'Ölçüm tamamlandı';
    resultNote.textContent = data.error_sample || 'Yanıt metrikleri aşağıda.';
    showMetrics(data);
  } catch (error) { resultTitle.textContent = 'Ölçüm başlatılamadı'; resultNote.textContent = error.message; }
  finally { runButton.disabled = false; runButton.textContent = 'Kontrollü ölçümü başlat'; }
});
