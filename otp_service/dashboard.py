"""Простой дашборд событий безопасности (бонус). Данные: GET /security/events."""

DASHBOARD_HTML = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OTP: события безопасности</title>
<style>
  :root { --bg:#f6f7f9; --card:#fff; --fg:#1d2330; --muted:#6b7280; --line:#e5e7eb;
          --ok:#15803d; --warn:#b45309; --bad:#b91c1c; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#111318; --card:#1a1d24; --fg:#e7e9ee; --muted:#9aa1ad; --line:#2a2f3a;
            --ok:#4ade80; --warn:#fbbf24; --bad:#f87171; }
  }
  * { box-sizing:border-box; }
  body { margin:0; font:14px/1.45 system-ui, sans-serif; background:var(--bg); color:var(--fg); }
  main { max-width:1100px; margin:0 auto; padding:24px 16px; }
  h1 { font-size:20px; margin:0 0 4px; }
  p.sub { color:var(--muted); margin:0 0 20px; }
  .tiles { display:grid; grid-template-columns:repeat(auto-fill,minmax(170px,1fr)); gap:12px; margin-bottom:20px; }
  .tile { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px 14px; }
  .tile b { display:block; font-size:24px; font-variant-numeric:tabular-nums; }
  .tile span { color:var(--muted); font-size:12px; }
  .table-wrap { overflow-x:auto; background:var(--card); border:1px solid var(--line); border-radius:10px; }
  table { width:100%; border-collapse:collapse; }
  th, td { text-align:left; padding:8px 12px; border-bottom:1px solid var(--line); white-space:nowrap; }
  th { color:var(--muted); font-weight:600; font-size:12px; }
  td.ev-ok { color:var(--ok); } td.ev-warn { color:var(--warn); } td.ev-bad { color:var(--bad); }
  code { font-size:12px; color:var(--muted); }
</style>
</head>
<body>
<main>
  <h1>События безопасности OTP</h1>
  <p class="sub">Обновляется каждые 2 секунды. Коды в журнал не пишутся, номера замаскированы.</p>
  <div class="tiles" id="tiles"></div>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Время</th><th>Событие</th><th>Номер</th><th>IP</th><th>Детали</th></tr></thead>
      <tbody id="rows"></tbody>
    </table>
  </div>
</main>
<script>
const LABELS = {
  otp_issued: "Выдано кодов", verify_success: "Успешных проверок", verify_failed: "Неудачных вводов",
  number_locked: "Блокировок номера", send_rate_limited: "Отказов send (лимит)",
  send_rejected_locked: "send при блокировке", verify_rejected_locked: "verify при блокировке",
  verify_ip_rate_limited: "Отказов по IP-лимиту", verify_bad_request: "Ошибок формата"
};
const CLS = { otp_issued:"ev-ok", verify_success:"ev-ok", verify_failed:"ev-warn",
  send_rate_limited:"ev-warn", verify_bad_request:"ev-warn", number_locked:"ev-bad",
  send_rejected_locked:"ev-bad", verify_rejected_locked:"ev-bad", verify_ip_rate_limited:"ev-bad" };
function esc(s) { return String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }
async function refresh() {
  try {
    const r = await fetch("/security/events?limit=200");
    const data = await r.json();
    document.getElementById("tiles").innerHTML = Object.keys(LABELS).map(k =>
      `<div class="tile"><b>${data.counts[k] || 0}</b><span>${LABELS[k]}</span></div>`).join("");
    document.getElementById("rows").innerHTML = data.events.map(e => {
      const {ts, event, msisdn, ip, ...rest} = e;
      return `<tr><td>${esc(ts)}</td><td class="${CLS[event]||""}">${esc(event)}</td>
        <td>${esc(msisdn)}</td><td>${esc(ip)}</td><td><code>${esc(JSON.stringify(rest))}</code></td></tr>`;
    }).join("");
  } catch (err) { /* сервис недоступен — попробуем позже */ }
}
refresh(); setInterval(refresh, 2000);
</script>
</body>
</html>
"""
