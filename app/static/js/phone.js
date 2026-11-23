/* RDR phone view: camera barcode scanning + a compact sign in / out flow. */
(function () {
  'use strict';
  const $ = (sel, root = document) => root.querySelector(sel);
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const hex = v => /^[0-9a-f]{6}$/i.test(v || '') ? v : '';   // faceplate colours go into style attributes
  const TYPES = window.RDR.types; const TECH = window.RDR.tech;
  if (!TECH) { location.replace('/start?next=/m'); return; }   // no tech and site picked: nothing here can work
  const state = { person: null, holdings: [], assignments: [], basket: [], autoPerson: false };

  async function api(path, opts = {}) {
    const r = await fetch(path, Object.assign({ headers: { 'Content-Type': 'application/json', 'X-RDR-Site': String(TECH.site_id) }, cache: 'no-store' }, opts));
    if (r.status === 401) { location.href = '/start?next=/m'; throw new Error('Session expired'); }
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || data.error || `Request failed (${r.status})`);
    return data;
  }
  const post = (path, body) => api(path, { method: 'POST', body: JSON.stringify(body) });
  function toast(kind, title, detail = '', ms = 3500) {
    const el = document.createElement('div'); el.className = `toast ${kind}`;
    el.innerHTML = `<div class="t">${esc(title)}</div>${detail ? `<div class="d">${esc(detail)}</div>` : ''}`;
    $('#toasts').appendChild(el); setTimeout(() => { el.classList.add('hide'); setTimeout(() => el.remove(), 220); }, ms);
  }
  function beep(ok = true) {
    try { const ctx = new (window.AudioContext || window.webkitAudioContext)(); const o = ctx.createOscillator(); const g = ctx.createGain();
      o.frequency.value = ok ? 1200 : 300; g.gain.value = 0.08; o.connect(g); g.connect(ctx.destination); o.start(); setTimeout(() => { o.stop(); ctx.close(); }, ok ? 90 : 220); } catch (e) { /* no audio */ }
    if (navigator.vibrate) navigator.vibrate(ok ? 40 : [60, 40, 60]);
  }
  function modal(title, bodyHtml, buttons) {
    const root = $('#modalRoot');
    root.innerHTML = `<div class="modal-back"><div class="modal"><div class="panel-head"><h2>${esc(title)}</h2><button class="btn sm ghost" data-close>&#10005;</button></div><div class="panel-body">${bodyHtml}</div><div class="foot">${buttons.map((b, i) => `<button class="btn ${b.cls || ''}" data-i="${i}">${esc(b.label)}</button>`).join('')}</div></div></div>`;
    root.querySelector('[data-close]').onclick = closeModal;
    buttons.forEach((b, i) => { root.querySelector(`[data-i="${i}"]`).onclick = () => b.onClick ? b.onClick(root) : closeModal(); });
    return root;
  }
  function closeModal() { $('#modalRoot').innerHTML = ''; }

  // ---- person --------------------------------------------------------------
  const searchEl = $('#personSearch'), resultsEl = $('#personResults');
  let timer = null;
  searchEl.addEventListener('input', () => { clearTimeout(timer); const q = searchEl.value.trim(); if (!q) { resultsEl.classList.add('hidden'); return; } timer = setTimeout(() => search(q), 150); });
  searchEl.addEventListener('blur', () => setTimeout(() => resultsEl.classList.add('hidden'), 150));
  async function search(q) {
    const d = await api(`/api/people/search?q=${encodeURIComponent(q)}`);
    resultsEl.innerHTML = d.results.map(p => `<div class="item" data-id="${p.id}"><div><div class="name">${esc(p.name)}</div><div class="meta">${esc([p.dept, p.role].filter(Boolean).join(' · '))}</div></div><div class="counts">${p.held_count ? p.held_count + ' out' : ''}</div></div>`).join('') + `<div class="empty"><span>${d.results.length ? '' : 'No match'}</span><button class="btn sm" data-new>New person</button></div>`;
    resultsEl.classList.remove('hidden');
    resultsEl.querySelectorAll('.item').forEach(el => el.onmousedown = () => selectPerson(+el.dataset.id));
    resultsEl.querySelector('[data-new]').onmousedown = () => openNewPerson(q);
  }
  async function selectPerson(id, opts = {}) {
    resultsEl.classList.add('hidden');
    const d = await api(`/api/people/${id}`);
    if (opts.auto && (state.person || !state.basket.every(x => x.status === 'out' && x.holder_id === id))) return;   // the list changed while loading
    if (opts.auto) state.autoPerson = true; else if (!state.person || state.person.id !== id) state.autoPerson = false;
    state.person = d.person; state.holdings = d.holdings; state.assignments = d.assignments; searchEl.value = '';
    renderPerson(); renderBasket();
  }
  function clearPerson() { state.person = null; state.holdings = []; state.assignments = []; state.autoPerson = false; renderPerson(); renderBasket(); }
  function renderPerson() {
    const p = state.person, el = $('#personPanel');
    if (!p) { el.innerHTML = ''; return; }
    el.innerHTML = `<div class="row"><div class="grow"><b class="pname">${esc(p.name)}</b><div class="muted sm">${esc([p.dept, p.role].filter(Boolean).join(' · '))}</div></div>${p.bolero_id ? `<span class="bolero${p.faceplate ? ' faced' : ''}"${p.faceplate ? ` style="--face:#${hex(p.faceplate)}"` : ''}>Bolero ${esc(p.bolero_id.replace(/^bolero\s*/i, ''))}</span>` : ''}<button class="btn sm ghost" id="btnClearPerson">&#10005;</button></div>
      ${state.holdings.length ? `<div class="muted sm mt-s">Signed out · ${state.holdings.length}: ${state.holdings.map(d => esc(d.label || d.si)).join(', ')} <button class="btn sm" data-return-all>Return all</button></div>` : ''}
      ${state.assignments.length ? `<div class="muted sm mt-s">Reserved: ${state.assignments.map(d => esc(d.label || d.si)).join(', ')} <button class="btn sm" data-add-all>Add all</button></div>` : ''}`;
    $('#btnClearPerson').onclick = clearPerson;
    const ra = el.querySelector('[data-return-all]'); if (ra) ra.onclick = () => state.holdings.forEach(d => addToBasket(d, true));
    const aa = el.querySelector('[data-add-all]'); if (aa) aa.onclick = () => state.assignments.forEach(d => addToBasket(d, true));
  }
  function openNewPerson(prefill) {
    modal('New person', `<div class="field"><label>Full name</label><input id="np_name" value="${esc(prefill)}"></div><div class="grid cols-2"><div class="field"><label>Department</label><input id="np_dept"></div><div class="field"><label>Role</label><input id="np_role"></div></div><div class="field"><label>Phone</label><input id="np_phone"></div>`,
      [{ label: 'Cancel' }, { label: 'Create', cls: 'primary', onClick: async root => { try { const d = await post('/api/people', { name: $('#np_name', root).value, dept: $('#np_dept', root).value, role: $('#np_role', root).value, phone: $('#np_phone', root).value }); closeModal(); selectPerson(d.person.id); } catch (e) { toast('err', 'Could not create', e.message); } } }]);
  }

  // ---- basket ----------------------------------------------------------------
  let lastScan = { si: '', t: 0 };
  async function handleCode(raw) {
    const si = String(raw || '').trim();
    if (!si) return;
    const now = Date.now();
    if (si === lastScan.si && now - lastScan.t < 2500) return;   // camera re-reads the same label many times a second
    lastScan = { si, t: now };
    try {
      const d = await api(`/api/devices/lookup?si=${encodeURIComponent(si)}`);
      if (d.found) { addToBasket(d.device); return; }
      const r = await api(`/api/devices/search?q=${encodeURIComponent(si)}`);   // typed partial SI
      if (r.results.length === 1) { addToBasket(r.results[0]); return; }
      if (r.results.length > 1) {
        modal('Which device?', `<ul class="conflict-list">${r.results.map(x => `<li><span><b>${esc(x.label || x.si)}</b> <span class="muted">· ${esc(x.type_name)}</span><div class="muted mono sm">${esc(x.si)}</div></span><button class="btn sm" data-pick="${x.id}">Add</button></li>`).join('')}</ul>`, [{ label: 'Cancel' }]);
        document.querySelectorAll('[data-pick]').forEach(b => b.onclick = () => { closeModal(); addToBasket(r.results.find(x => x.id === +b.dataset.pick)); });
        return;
      }
      beep(false); openQuickCreate(d.si);
    } catch (e) { beep(false); toast('err', 'Lookup failed', e.message); }
  }
  function addToBasket(device, quiet = false) {
    if (state.basket.some(x => x.id === device.id)) { if (!quiet) { beep(false); toast('warn', 'Already in the list', device.label || device.si, 1200); } return; }
    state.basket.push(device); beep(true); renderBasket();
    // as on the kiosk: a device that is out brings up its holder, mixed devices go back to bulk sign in
    const theirs = d => d.status === 'out' && d.holder_id && d.holder_id === device.holder_id;
    if (!state.person) {
      if (device.status === 'out' && device.holder_id && state.basket.every(theirs)) selectPerson(device.holder_id, { auto: true }).catch(() => {});
    } else if (state.autoPerson && !(device.status === 'out' && device.holder_id === state.person.id)) {
      clearPerson(); toast('ok', 'Back to sign in', 'Devices from more than one person.', 2000);
    }
  }
  function renderBasket() {
    const p = state.person;
    $('#basket').innerHTML = state.basket.map((d, i) => {
      let cls = '', note = '';
      if (d.status === 'out') { if (p && d.holder_id === p.id) { cls = 'warn'; note = 'already with this person'; } else { cls = 'conflict'; note = `out to ${d.holder_name}`; } }
      else if (d.status !== 'in') { cls = 'warn'; note = d.status; }
      return `<li class="${cls}"><span class="idx">${i + 1}</span><span><div class="label">${esc(d.label || d.si)}</div><div class="sub">${esc(d.type_name)} · <span class="si">${esc(d.si)}</span>${note ? ` · <span class="${cls === 'conflict' ? 'red' : 'amber'}">${esc(note)}</span>` : ''}</div></span><span class="badge ${esc(d.status)}">${esc(d.status)}</span><span class="remove" data-rm="${d.id}">&#10005;</span></li>`;
    }).join('');
    $('#basket').querySelectorAll('[data-rm]').forEach(el => el.onclick = () => { state.basket = state.basket.filter(x => x.id !== +el.dataset.rm); renderBasket(); });
    const n = state.basket.length;
    $('#basketEmpty').classList.toggle('hidden', n > 0); $('#basketCount').textContent = n;
    $('#btnSignIn').disabled = !n; $('#btnSignOut').disabled = !n || !p;
    $('#btnSignOut').textContent = p ? `Sign out to ${p.name.split(' ')[0]}` : 'Sign out';
  }
  $('#btnClear').onclick = () => { state.basket = []; renderBasket(); };
  $('#btnAddManual').onclick = () => { handleCode($('#scanInput').value); $('#scanInput').value = ''; };
  $('#scanInput').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); handleCode($('#scanInput').value); $('#scanInput').value = ''; } });

  function openQuickCreate(si) {
    modal('New device', `<p class="help"><span class="mono">${esc(si)}</span> is not in the database yet.</p><div class="field"><label>Type</label><select id="qc_type">${TYPES.map(t => `<option value="${t.id}">${esc(t.name)}</option>`).join('')}</select></div><div class="field"><label>Label</label><input id="qc_label" placeholder="Bolero 61"></div>`,
      [{ label: 'Cancel' }, { label: 'Create and add', cls: 'primary', onClick: async root => { try { const d = await post('/api/devices/quick', { si, type_id: +$('#qc_type', root).value, label: $('#qc_label', root).value }); closeModal(); addToBasket(d.device); } catch (e) { toast('err', 'Could not create', e.message); } } }]);
  }

  const NICE = { issued: ['ok', 'Signed out'], reassigned: ['warn', 'Reassigned'], reissued: ['warn', 'Restored and signed out'], already_held: ['warn', 'Already with this person'], returned: ['ok', 'Signed in'], already_in: ['warn', 'Already in stock'], restored: ['ok', 'Restored'], conflict: ['err', 'Not processed'], missing: ['err', 'Unknown device'] };
  function announce(results) {
    const grouped = {}; results.forEach(r => (grouped[r.outcome] = grouped[r.outcome] || []).push(r.label || r.si));
    Object.entries(grouped).forEach(([o, items]) => { const [k, t] = NICE[o] || ['warn', o]; toast(k, `${t} · ${items.length}`, items.join(', ')); });
  }
  $('#btnSignIn').onclick = async () => {
    try { const d = await post('/api/signin', { device_ids: state.basket.map(x => x.id) }); announce(d.results); state.basket = []; if (state.person) await selectPerson(state.person.id); else renderBasket(); } catch (e) { toast('err', 'Sign in failed', e.message); }
  };
  $('#btnSignOut').onclick = () => doSignOut([]);
  async function doSignOut(force) {
    const person = state.person; if (!person) return;
    try {
      const d = await post('/api/signout', { person_id: person.id, device_ids: state.basket.map(x => x.id), force_ids: force });
      const conflicts = d.results.filter(r => r.outcome === 'conflict');
      announce(d.results.filter(r => r.outcome !== 'conflict'));
      if (conflicts.length) {
        state.basket = state.basket.filter(x => conflicts.some(c => c.id === x.id)); renderBasket();
        modal('Devices not available', `<ul class="conflict-list">${conflicts.map(c => `<li><span><b>${esc(c.label || c.si)}</b><div class="red sm">${esc(c.message)}</div></span><label class="check"><input type="checkbox" data-force="${c.id}"> ${c.status === 'out' ? 'Reassign' : 'Issue anyway'}</label></li>`).join('')}</ul>`,
          [{ label: 'Leave' }, { label: 'Apply', cls: 'primary', onClick: async root => { const f = [...root.querySelectorAll('[data-force]:checked')].map(x => +x.dataset.force); closeModal(); if (!f.length) return; state.basket = state.basket.filter(x => f.includes(x.id)); await doSignOut(f); } }]);
        return;
      }
      state.basket = []; clearPerson();
    } catch (e) { toast('err', 'Sign out failed', e.message); }
  }

  // ---- camera ------------------------------------------------------------------
  async function startCamera() {
    const msg = $('#scanMsg');
    if (!window.RDR.secure || !window.Html5Qrcode) return;
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) { msg.textContent = 'This browser cannot access the camera.'; return; }
    const formats = window.Html5QrcodeSupportedFormats ? [
      Html5QrcodeSupportedFormats.CODE_128, Html5QrcodeSupportedFormats.CODE_39, Html5QrcodeSupportedFormats.EAN_13, Html5QrcodeSupportedFormats.EAN_8,
      Html5QrcodeSupportedFormats.QR_CODE, Html5QrcodeSupportedFormats.DATA_MATRIX, Html5QrcodeSupportedFormats.ITF, Html5QrcodeSupportedFormats.UPC_A,
    ] : undefined;
    const scanner = new Html5Qrcode('reader', { formatsToSupport: formats, verbose: false, experimentalFeatures: { useBarCodeDetectorIfSupported: true } });
    try {
      await scanner.start({ facingMode: 'environment' }, { fps: 12, qrbox: (w, h) => ({ width: Math.round(w * 0.7), height: Math.round(h * 0.36) }), aspectRatio: 4 / 3 }, text => handleCode(text), () => {});
      msg.classList.add('hidden');
      const caps = scanner.getRunningTrackCapabilities ? scanner.getRunningTrackCapabilities() : {};
      if (caps && caps.torch) { const b = $('#btnTorch'); b.classList.remove('hidden'); let on = false; b.onclick = async () => { on = !on; try { await scanner.applyVideoConstraints({ advanced: [{ torch: on }] }); } catch (e) {} }; }
    } catch (e) {
      msg.textContent = 'No camera available. Allow camera access for this site and reload, or type SI numbers below.'; msg.title = e && e.message ? e.message : String(e);
    }
  }

  // ---- live configuration: admin edits show up here without a reload ------------
  let configVersion = null;
  const setLive = (id, text) => { const el = document.getElementById(id); if (!el) return; if (el.textContent !== text) { el.textContent = text; el.classList.remove('live-swap'); void el.offsetWidth; el.classList.add('live-swap'); } };
  async function pollConfig() {
    try {
      const c = await api('/api/config');
      if (window.RDRLock) RDRLock.apply(c.away || null);
      if (c.version === configVersion) return;
      configVersion = c.version;
      if (!c.tech.active || !c.site.active) { location.href = '/start'; return; }
      setLive('evName', c.event.name || '');
      const show = document.getElementById('evShow'); if (show) { show.style.display = c.event.show_name ? '' : 'none'; setLive('evShow', c.event.show_name || ''); }
      const logo = document.getElementById('eventLogo'), sep = document.getElementById('logoSep');
      if (logo) { if (c.event.logo_path) { if (logo.getAttribute('src') !== c.event.logo_path) logo.src = c.event.logo_path; logo.style.display = ''; if (sep) sep.style.display = ''; } else { logo.style.display = 'none'; if (sep) sep.style.display = 'none'; } }
      TECH.display_name = c.tech.display_name || TECH.display_name; TECH.site_name = c.site.name || TECH.site_name;
      setLive('techName', TECH.display_name); setLive('siteName', TECH.site_name); setLive('drawerTitle', `${TECH.display_name} · ${TECH.site_name}`);
      const scope = document.getElementById('scopeLabel'); if (scope && state.scope === 'site') setLive('scopeLabel', `${TECH.site_name} stock`);
      if (typeof TYPES !== 'undefined') { TYPES.length = 0; c.types.forEach(t => TYPES.push(t)); }
      
    } catch (e) { /* server away; try again */ }
  }
  setInterval(pollConfig, 4000); pollConfig();
  renderBasket(); startCamera();
})();
