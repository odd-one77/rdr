// Away-from-desk lock, shared by the kiosk and the phone view. The server holds the state per site;
// pages call RDRLock.apply(away) from their config poll and RDRLock.start(...) from their menu.
(function () {
  const $ = (s, r = document) => r.querySelector(s);
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const headers = () => ({ 'Content-Type': 'application/json', 'X-RDR-Site': String((window.RDR && window.RDR.tech && window.RDR.tech.site_id) || '') });
  let shown = null, timer = null;

  function ensure() {
    let el = $('#lockScreen');
    if (el) return el;
    el = document.createElement('div'); el.id = 'lockScreen'; el.className = 'lock-screen';
    el.innerHTML = `<div class="lock-card"><div class="k">Away from desk</div><div class="big" id="lockLabel"></div><div class="sub" id="lockSub"></div>
      <form id="lockForm" autocomplete="off"><input type="password" id="lockPw" placeholder="Admin password to reopen" autocomplete="current-password"><button class="btn primary" type="submit">Reopen</button></form><div class="err" id="lockErr"></div></div>`;
    document.body.appendChild(el);
    $('#lockForm', el).onsubmit = async e => {
      e.preventDefault();
      const pw = $('#lockPw', el), err = $('#lockErr', el);
      err.textContent = '';
      try {
        const r = await fetch('/api/away/unlock', { method: 'POST', headers: headers(), body: JSON.stringify({ password: pw.value }) });
        if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(d.detail || 'Wrong password'); }
        pw.value = ''; apply(null);
        if (window.RDRLock.onUnlock) window.RDRLock.onUnlock();
      } catch (ex) { err.textContent = ex.message; pw.select(); }
    };
    return el;
  }
  function words(a) {
    if (!a) return ['', ''];
    const until = a.until ? ` (until ${a.until_hm})` : '';
    const label = a.overdue ? 'Back any moment' : a.label;
    return [label, [a.note, a.until && !a.overdue ? `until ${a.until_hm}` : '', a.tech_name ? `locked by ${a.tech_name}` : ''].filter(Boolean).join(' · ') || ' '];
  }
  function apply(a) {
    const el = ensure();
    const on = !!a;
    if (on) {
      const [l, s] = words(a);
      $('#lockLabel', el).textContent = l; $('#lockSub', el).textContent = s;
      if (!shown) { el.classList.add('open'); document.body.classList.add('locked'); setTimeout(() => { const pw = $('#lockPw', el); if (pw) pw.focus(); }, 80); }
    } else if (shown) { el.classList.remove('open'); document.body.classList.remove('locked'); }
    shown = on ? a : null;
  }
  async function start(opts) {
    const r = await fetch('/api/away', { method: 'POST', headers: headers(), body: JSON.stringify(opts || {}) });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'Could not set away');
    apply(d.away);
    return d.away;
  }
  window.RDRLock = { apply, start, isLocked: () => !!shown, esc };
})();
