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
  const idOf = v => String(v || '').replace(/^bolero\s*/i, '');
  // Under a search result: the pack name when it differs, or the imported name when RDR took the pack's name.
  // Flag only: the selected person's pack carries a different name. Shown, never asked.
  const flagNote = c => c ? `<div class="bname differs"><span class="tag">Name differs</span>Bolero ${esc(c.number)} is named <b>${esc(c.pack_name)}</b>${c.expected ? `, not ${esc(c.expected)}` : ''}</div>` : '';
  function nameNote(p) {
    if (p.bolero_differs) return `<div class="bname differs"><span class="tag">Name differs</span>Bolero ${esc(idOf(p.bolero_id))} is named <b>${esc(p.bolero_name)}</b>${p.bolero_expected ? `, not ${esc(p.bolero_expected)}` : ''}</div>`;
    if (p.import_name && p.import_name !== p.name) return `<div class="bname">Name from Bolero ${esc(idOf(p.bolero_id))} · imported as ${esc(p.import_name)}</div>`;
    return '';
  }
  const searchEl = $('#personSearch'), resultsEl = $('#personResults');
  let timer = null;
  searchEl.addEventListener('input', () => { clearTimeout(timer); const q = searchEl.value.trim(); if (!q) { resultsEl.classList.add('hidden'); return; } timer = setTimeout(() => search(q), 150); });
  searchEl.addEventListener('blur', () => setTimeout(() => resultsEl.classList.add('hidden'), 150));
  async function search(q) {
    const d = await api(`/api/people/search?q=${encodeURIComponent(q)}`);
    resultsEl.innerHTML = d.results.map(p => `<div class="item" data-id="${p.id}"><div><div class="name">${esc(p.name)}</div><div class="meta">${esc([p.dept, p.role].filter(Boolean).join(' · '))}</div>${nameNote(p)}</div><div class="counts">${p.held_count ? p.held_count + ' out' : ''}</div></div>`).join('') + (d.packs || []).map(k => `<div class="empty"><span>Bolero ${esc(k.number)} is named <b>${esc(k.pack_name)}</b>. Nobody in RDR has this ID.</span><button class="btn sm" data-pack="${esc(k.number)}" data-pack-name="${esc(k.pack_name)}">Create</button></div>`).join('') + `<div class="empty"><span>${d.results.length ? '' : 'No match'}</span><button class="btn sm" data-new>New person</button></div>`;
    resultsEl.classList.remove('hidden');
    resultsEl.querySelectorAll('.item').forEach(el => el.onmousedown = () => selectPerson(+el.dataset.id));
    resultsEl.querySelector('[data-new]').onmousedown = () => openNewPerson(q);
    resultsEl.querySelectorAll('[data-pack]').forEach(el => el.onmousedown = () => openNewPerson(el.dataset.packName, el.dataset.pack));
  }
  async function selectPerson(id, opts = {}) {
    resultsEl.classList.add('hidden');
    const d = await api(`/api/people/${id}`);
    if (opts.auto && (state.person || !state.basket.every(x => x.status === 'out' && x.holder_id === id))) return;   // the list changed while loading
    if (opts.auto) state.autoPerson = true; else if (!state.person || state.person.id !== id) state.autoPerson = false;
    state.person = d.person; state.holdings = d.holdings; state.assignments = d.assignments; state.nameFlag = d.name_flag; searchEl.value = '';
    renderPerson(); renderBasket();
    if (d.name_check && !opts.auto) askBoleroName(d.person, d.name_check);
  }
  function askBoleroName(person, c) {
    const choose = async use => {
      try {
        const r = await post(`/api/people/${person.id}/bolero-name`, { use, pack_name: c.pack_name });
        closeModal();
        if (use) { toast('ok', 'Name updated', `${person.name} is now ${r.person.name}`); if (state.person && state.person.id === person.id) { state.person = r.person; renderPerson(); } }
      } catch (e) { closeModal(); toast('err', 'Could not save', e.message); }
    };
    modal('Name differs on Bolero', `<p>Bolero ${esc(c.number)} is named <b>${esc(c.pack_name)}</b> on the Bolero system.</p><p class="help">RDR has <b>${esc(person.name)}</b> from the import${c.expected ? `, which would be ${esc(c.expected)} on a pack` : ''}.</p>`,
      [{ label: 'Keep RDR name', onClick: () => choose(false) }, { label: 'Use Bolero name', cls: 'primary', onClick: () => choose(true) }]);
  }
  function clearPerson() { state.person = null; state.holdings = []; state.assignments = []; state.autoPerson = false; renderPerson(); renderBasket(); }
  function renderPerson() {
    const p = state.person, el = $('#personPanel');
    if (!p) { el.innerHTML = ''; return; }
    el.innerHTML = `<div class="row"><div class="grow"><b class="pname">${esc(p.name)}</b><div class="muted sm">${esc([p.dept, p.role].filter(Boolean).join(' · '))}</div>${flagNote(state.nameFlag)}</div>${p.bolero_id ? `<span class="bolero${p.faceplate ? ' faced' : ''}"${p.faceplate ? ` style="--face:#${hex(p.faceplate)}"` : ''}>Bolero ${esc(p.bolero_id.replace(/^bolero\s*/i, ''))}</span>` : ''}<button class="btn sm ghost" id="btnClearPerson">&#10005;</button></div>
      ${state.holdings.length ? `<div class="muted sm mt-s">Signed out · ${state.holdings.length}: ${state.holdings.map(d => esc(d.label || d.si)).join(', ')} <button class="btn sm" data-return-all>Return all</button></div>` : ''}
      ${state.assignments.length ? `<div class="muted sm mt-s">Reserved: ${state.assignments.map(d => esc(d.label || d.si)).join(', ')} <button class="btn sm" data-add-all>Add all</button></div>` : ''}`;
    $('#btnClearPerson').onclick = clearPerson;
    const ra = el.querySelector('[data-return-all]'); if (ra) ra.onclick = () => state.holdings.forEach(d => addToBasket(d, true));
    const aa = el.querySelector('[data-add-all]'); if (aa) aa.onclick = () => state.assignments.forEach(d => addToBasket(d, true));
  }
  function openNewPerson(prefill, boleroId = '') {
    modal('New person', `<div class="field"><label>Full name</label><input id="np_name" value="${esc(prefill)}"></div><div class="grid cols-2"><div class="field"><label>Department</label><input id="np_dept"></div><div class="field"><label>Role</label><input id="np_role"></div></div><div class="grid cols-2"><div class="field"><label>Phone</label><input id="np_phone"></div><div class="field"><label>Bolero ID</label><input id="np_bolero" value="${esc(boleroId)}" inputmode="numeric"></div></div>`,
      [{ label: 'Cancel' }, { label: 'Create', cls: 'primary', onClick: async root => { try { const d = await post('/api/people', { name: $('#np_name', root).value, dept: $('#np_dept', root).value, role: $('#np_role', root).value, phone: $('#np_phone', root).value, bolero_id: $('#np_bolero', root).value }); closeModal(); selectPerson(d.person.id); } catch (e) { toast('err', 'Could not create', e.message); } } }]);
  }

  // ---- basket ----------------------------------------------------------------
  let lastScan = { si: '', t: 0 };
  async function handleCode(raw, fromCamera = false) {
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
      if (fromCamera) { unknownToast(d.si); return; }   // a misread must not open a form: offer it instead
      beep(false); openQuickCreate(d.si);
    } catch (e) { beep(false); toast('err', 'Lookup failed', e.message); }
  }
  const offered = {};
  function unknownToast(si) {
    if (Date.now() - (offered[si] || 0) < 10000) return;   // label still in view: one offer is enough
    offered[si] = Date.now(); beep(false);
    const el = document.createElement('div'); el.className = 'toast warn';
    el.innerHTML = `<div class="t">Not in the database</div><div class="d mono">${esc(si)}</div><button class="btn sm mt-s" type="button">Add as new device</button>`;
    el.querySelector('button').onclick = () => { el.remove(); openQuickCreate(si); };
    $('#toasts').appendChild(el); setTimeout(() => { el.classList.add('hide'); setTimeout(() => el.remove(), 220); }, 6000);
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
      else if (d.status !== 'in') { cls = 'warn'; const why = [d.flag_reason, d.flag_note].filter(Boolean).join(' · '); note = why ? `${d.status}: ${why}` : d.status; }
      return `<li class="${cls}"><span class="idx">${i + 1}</span><span><div class="label">${esc(d.label || d.si)}${/bolero/i.test(d.type_name) && !/antenna/i.test(d.type_name) && d.linked_id ? ` <span class="bolero">ID ${esc(d.linked_id)}</span>` : ''}</div><div class="sub">${esc(d.type_name)} · <span class="si">${esc(d.si)}</span>${note ? ` · <span class="${cls === 'conflict' ? 'red' : 'amber'}">${esc(note)}</span>` : ''}</div></span><span class="badge ${esc(d.status)}">${esc(d.status)}</span><span class="remove" data-rm="${d.id}">&#10005;</span></li>`;
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
  // Our own loop, so the box on screen is exactly the part of the picture that is read: the camera runs at
  // up to 1080p, the box is mapped back through the object-fit crop to camera pixels, and that strip is read
  // at full resolution (small Code 128 labels need it). Native BarcodeDetector when the phone has one
  // (Android Chrome), ZXing otherwise (iPhone).
  const cam = { stream: null, track: null, busy: false, timer: null, detector: null, reader: null, canvas: document.createElement('canvas'), zoom: 1, flip: 0, last: { text: '', t: 0 }, votes: [] };
  // A camera read worth acting on: printable, at least 4 characters, no control or odd symbols.
  const plausible = t => { t = String(t || '').trim(); return t.length >= 4 && t.length <= 64 && /^[A-Za-z0-9][A-Za-z0-9 ._\/+-]*$/.test(t) ? t : null; };

  function guideCrop(video) {
    const vw = video.videoWidth, vh = video.videoHeight;
    const vr = video.getBoundingClientRect(), g = $('#scanGuide').getBoundingClientRect();
    if (!vw || !vh || !vr.width || !g.width) return null;
    const s = Math.max(vr.width / vw, vr.height / vh);              // object-fit: cover
    const ox = (vr.width - vw * s) / 2, oy = (vr.height - vh * s) / 2;
    const padX = g.width * 0.06, padY = g.height * 0.25;            // a little slack around the box
    let x0 = (g.left - vr.left - padX - ox) / s, x1 = (g.right - vr.left + padX - ox) / s;
    let y0 = (g.top - vr.top - padY - oy) / s, y1 = (g.bottom - vr.top + padY - oy) / s;
    x0 = Math.max(0, x0); y0 = Math.max(0, y0); x1 = Math.min(vw, x1); y1 = Math.min(vh, y1);
    return { x: Math.round(x0), y: Math.round(y0), w: Math.round(x1 - x0), h: Math.round(y1 - y0) };
  }

  function zxingReader() {
    if (cam.reader || !window.ZXing) return cam.reader;
    const Z = window.ZXing, F = Z.BarcodeFormat, hints = new Map();
    hints.set(Z.DecodeHintType.POSSIBLE_FORMATS, [F.CODE_128, F.EAN_13, F.QR_CODE, F.DATA_MATRIX]);   // all carry a check: Code 39, ITF and Codabar misread shadows
    hints.set(Z.DecodeHintType.TRY_HARDER, true);
    cam.reader = new Z.MultiFormatReader(); cam.reader.setHints(hints);
    return cam.reader;
  }

  async function readFrame() {
    const video = $('#camVideo'), crop = guideCrop(video);
    if (!crop || crop.w < 20 || crop.h < 10) return null;
    const scale = Math.min(1, 1600 / crop.w);
    const c = cam.canvas; c.width = Math.round(crop.w * scale); c.height = Math.round(crop.h * scale);
    const ctx = c.getContext('2d', { willReadFrequently: true });
    ctx.drawImage(video, crop.x, crop.y, crop.w, crop.h, 0, 0, c.width, c.height);
    if (cam.detector) {
      const found = await cam.detector.detect(c);
      return found.length ? found[0].rawValue : null;
    }
    const Z = window.ZXing, reader = zxingReader();
    if (!reader) return null;
    const lum = new Z.HTMLCanvasElementLuminanceSource(c);
    cam.flip = 1 - cam.flip;                                        // two binarizers in turn: global suits 1D labels, hybrid suits uneven light
    const bin = cam.flip ? new Z.GlobalHistogramBinarizer(lum) : new Z.HybridBinarizer(lum);
    try { return reader.decodeWithState(new Z.BinaryBitmap(bin)).getText(); }
    catch (e) { return null; }                                      // nothing in this frame
    finally { reader.reset(); }
  }

  function scanLoop() {
    clearTimeout(cam.timer);
    cam.timer = setTimeout(async () => {
      const video = $('#camVideo');
      if (cam.stream && !document.hidden && video.readyState >= 2 && !cam.busy && !$('#modalRoot').firstChild) {   // no reading behind an open dialog
        cam.busy = true;
        try {
          const text = plausible(await readFrame());
          const now = Date.now();
          if (text) {
            // Shadows and print can fool a reader for one frame; a real label reads the same several frames running.
            cam.votes = cam.votes.filter(v => now - v.t < 1500); cam.votes.push({ text, t: now });
            const agree = cam.votes.filter(v => v.text === text).length;
            if (agree >= 3 && !(text === cam.last.text && now - cam.last.t < 2500)) {   // the same label is seen many times a second
              cam.last = { text, t: now }; cam.votes = [];
              const g = $('#scanGuide'); g.classList.add('hit'); setTimeout(() => g.classList.remove('hit'), 400);
              handleCode(text, true);
            }
          }
        } catch (e) { /* keep going */ }
        cam.busy = false;
      }
      scanLoop();
    }, 90);
  }

  async function setupDetector() {
    if (!('BarcodeDetector' in window)) return;
    try {
      const have = await BarcodeDetector.getSupportedFormats();
      const want = ['code_128', 'ean_13', 'qr_code', 'data_matrix'].filter(f => have.includes(f));
      if (want.includes('code_128')) cam.detector = new BarcodeDetector({ formats: want });
    } catch (e) { cam.detector = null; }
  }

  function cameraControls() {
    const caps = cam.track && cam.track.getCapabilities ? cam.track.getCapabilities() : {};
    if (caps.focusMode && caps.focusMode.includes('continuous')) cam.track.applyConstraints({ advanced: [{ focusMode: 'continuous' }] }).catch(() => {});
    const tb = $('#btnTorch');
    if (caps.torch) { tb.classList.remove('hidden'); let on = false; tb.onclick = async () => { on = !on; try { await cam.track.applyConstraints({ advanced: [{ torch: on }] }); tb.classList.toggle('on', on); } catch (e) {} }; }
    const zb = $('#btnZoom');
    if (caps.zoom && caps.zoom.max >= 1.5) {                         // small labels: zoom in rather than hold the phone too close to focus
      const steps = [1, 2, 3].filter(z => z <= caps.zoom.max).map(z => Math.max(caps.zoom.min || 1, z));
      zb.classList.remove('hidden');
      zb.onclick = async () => {
        const next = steps[(steps.indexOf(cam.zoom) + 1) % steps.length] || steps[0];
        try { await cam.track.applyConstraints({ advanced: [{ zoom: next }] }); cam.zoom = next; zb.textContent = `Zoom ${next}×`; } catch (e) {}
      };
    }
  }

  async function startCamera() {
    const msg = $('#scanMsg'), video = $('#camVideo');
    if (!window.RDR.secure) return;
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) { msg.textContent = 'This browser cannot access the camera.'; return; }
    if (cam.stream) return;
    try {
      cam.stream = await navigator.mediaDevices.getUserMedia({ audio: false, video: { facingMode: { ideal: 'environment' }, width: { ideal: 1920 }, height: { ideal: 1080 } } });
      cam.track = cam.stream.getVideoTracks()[0];
      cam.track.addEventListener('ended', () => { cam.stream = null; });
      video.srcObject = cam.stream;
      await video.play().catch(() => {});
      await setupDetector();
      if (!cam.detector && !window.ZXing) { msg.textContent = 'The barcode reader did not load. Reload the page, or type SI numbers below.'; return; }
      msg.classList.add('hidden');
      cameraControls();
      scanLoop();
    } catch (e) {
      cam.stream = null;
      msg.classList.remove('hidden');
      msg.textContent = 'No camera available. Allow camera access for this site and reload, or type SI numbers below.'; msg.title = e && e.message ? e.message : String(e);
    }
  }
  // iPhones stop the camera when the page is in the background: start it again on return.
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) return;
    if (!cam.stream || !cam.track || cam.track.readyState === 'ended') { cam.stream = null; cam.zoom = 1; $('#btnZoom').textContent = 'Zoom 1×'; startCamera(); }
  });

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
