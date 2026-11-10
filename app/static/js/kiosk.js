/* RDR kiosk front end. Plain JS, no build step. */
(function () {
  'use strict';
  const $ = (sel, root = document) => root.querySelector(sel);
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const TECH = window.RDR.tech;
  const TYPES = window.RDR.types;
  const ACTIONS = {
    out: 'OUT', in: 'IN', in_noop: 'Scan (already in)', out_noop: 'Scan (already out)', out_reassign: 'OUT (reassigned)',
    swap_out: 'Swap returned', swap_in: 'Swap issued', lost: 'LOST', damaged: 'DAMAGED', restored: 'Restored',
    created: 'Created', imported: 'Imported', assigned: 'Pre-assigned', unassigned: 'Unassigned',
  };

  const state = { person: null, holdings: [], assignments: [], plan: [], basket: [], scope: 'site', result: null };

  // ---- api ---------------------------------------------------------------
  async function api(path, opts = {}) {
    const r = await fetch(path, Object.assign({ headers: { 'Content-Type': 'application/json' }, cache: 'no-store' }, opts));
    if (r.status === 401) { location.href = '/start'; throw new Error('Session expired'); }
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || data.error || `Request failed (${r.status})`);
    return data;
  }
  const post = (path, body) => api(path, { method: 'POST', body: JSON.stringify(body) });

  // ---- toasts / modals ---------------------------------------------------
  function toast(kind, title, detail = '', ms = 4000) {
    const el = document.createElement('div');
    el.className = `toast ${kind}`;
    el.innerHTML = `<div class="t">${esc(title)}</div>${detail ? `<div class="d">${esc(detail)}</div>` : ''}`;
    $('#toasts').appendChild(el);
    setTimeout(() => el.remove(), ms);
  }
  function modal(title, bodyHtml, buttons) {
    closeModal();
    const root = $('#modalRoot');
    root.innerHTML = `<div class="modal-back"><div class="modal"><div class="panel-head"><h2>${esc(title)}</h2><button class="btn sm ghost" data-close>&#10005;</button></div>
      <div class="panel-body">${bodyHtml}</div><div class="foot">${buttons.map((b, i) => `<button class="btn ${b.cls || ''}" data-i="${i}">${esc(b.label)}</button>`).join('')}</div></div></div>`;
    root.querySelector('[data-close]').onclick = closeModal;
    root.querySelector('.modal-back').addEventListener('click', e => { if (e.target.classList.contains('modal-back')) closeModal(); });
    buttons.forEach((b, i) => { root.querySelector(`[data-i="${i}"]`).onclick = () => b.onClick ? b.onClick(root) : closeModal(); });
    const first = root.querySelector('input, select, textarea');
    if (first) setTimeout(() => first.focus(), 30);
    return root;
  }
  function closeModal() { $('#modalRoot').innerHTML = ''; focusScan(); }
  const isModalOpen = () => !!$('#modalRoot .modal');

  // ---- clock / summary / feed -------------------------------------------
  const clock = () => { $('#clock').textContent = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }); };
  setInterval(clock, 1000); clock();

  async function refreshSummary() {
    try {
      const d = await api(`/api/summary?scope=${state.scope}`);
      const s = state.scope === 'site' ? d.site : d.all;
      const t = s.totals;
      const bad = t.n_lost + t.n_damaged;
      $('#summary').innerHTML = `<div class="chip total"><span class="name">All items</span><span class="nums"><span class="in">${t.n_in} in</span><span class="out">${t.n_out} out</span>${bad ? `<span class="bad">${bad} !</span>` : ''}</span></div>` +
        s.types.map(x => `<div class="chip"><span class="name">${esc(x.name)}</span><span class="nums"><span class="in">${x.n_in}</span><span class="out">${x.n_out}</span>${(x.n_lost + x.n_damaged) ? `<span class="bad">${x.n_lost + x.n_damaged}</span>` : ''}</span></div>`).join('');
    } catch (e) { /* ignore */ }
  }
  async function refreshFeed() {
    try {
      const d = await api(`/api/recent?limit=40&scope=${state.scope}`);
      $('#feed').innerHTML = d.transactions.map(x => {
        const label = ACTIONS[x.action] || x.action;
        return `<li class="act-${x.action}"><span class="ts">${esc(x.ts.slice(11, 16))}</span><span class="txt"><div class="l1"><span class="what">${esc(label)}</span> · ${esc(x.label || x.si || '')}</div><div class="l2">${esc([x.person_name, x.tech_name, x.note].filter(Boolean).join(' · ') || x.si || '')}</div></span></li>`;
      }).join('') || '<li><span class="ts"></span><span class="dim">No activity yet.</span></li>';
    } catch (e) { /* ignore */ }
  }
  document.querySelectorAll('.seg [data-scope]').forEach(b => b.onclick = () => {
    state.scope = b.dataset.scope;
    document.querySelectorAll('.seg [data-scope]').forEach(x => x.classList.toggle('on', x === b));
    $('#scopeLabel').textContent = state.scope === 'site' ? `${TECH.site_name} stock` : 'All sites';
    $('#feedScope').textContent = state.scope === 'site' ? 'this site' : 'all sites';
    refreshSummary(); refreshFeed();
  });

  // ---- mirror push -------------------------------------------------------
  let pushTimer = null;
  function pushState() {
    clearTimeout(pushTimer);
    pushTimer = setTimeout(() => {
      const brief = d => ({ si: d.si, label: d.label, type: d.type_name || d.type });
      post('/api/kiosk/state', { state: {
        person: state.person ? { name: state.person.name, dept: state.person.dept, role: state.person.role, bolero_id: state.person.bolero_id } : null,
        holdings: state.holdings.map(brief),
        basket: state.basket.map(brief),
        result: state.result,
      } }).catch(() => {});
    }, 150);
  }

  // ---- person search -----------------------------------------------------
  const searchEl = $('#personSearch'), resultsEl = $('#personResults');
  let searchTimer = null, activeIdx = -1, lastResults = [];
  searchEl.addEventListener('input', () => {
    clearTimeout(searchTimer);
    const q = searchEl.value.trim();
    if (!q) { hideResults(); return; }
    searchTimer = setTimeout(() => doSearch(q), 120);
  });
  searchEl.addEventListener('keydown', e => {
    if (e.key === 'Enter') {
      e.preventDefault();
      if (resultsEl.classList.contains('hidden')) { if (searchEl.value.trim()) doSearch(searchEl.value.trim()); return; }
      if (lastResults.length) selectPerson(lastResults[Math.max(activeIdx, 0)].id); else openNewPerson(searchEl.value.trim());
      return;
    }
    if (resultsEl.classList.contains('hidden')) return;
    if (e.key === 'ArrowDown') { activeIdx = Math.min(activeIdx + 1, lastResults.length - 1); paintActive(); e.preventDefault(); }
    else if (e.key === 'ArrowUp') { activeIdx = Math.max(activeIdx - 1, 0); paintActive(); e.preventDefault(); }
    else if (e.key === 'Escape') hideResults();
  });
  searchEl.addEventListener('blur', () => setTimeout(hideResults, 150));
  function hideResults() { resultsEl.classList.add('hidden'); activeIdx = -1; }
  function paintActive() { resultsEl.querySelectorAll('.item').forEach((el, i) => el.classList.toggle('active', i === activeIdx)); }
  async function doSearch(q) {
    try {
      const d = await api(`/api/people/search?q=${encodeURIComponent(q)}`);
      lastResults = d.results; activeIdx = d.results.length ? 0 : -1;
      resultsEl.innerHTML = d.results.map(p => `<div class="item" data-id="${p.id}"><div><div class="name">${esc(p.name)}</div><div class="meta">${esc([p.dept, p.role, p.phone].filter(Boolean).join(' · '))}</div></div><div class="counts">${p.bolero_id ? `Bolero ${esc(p.bolero_id.replace(/^bolero\s*/i, ''))}` : ''}${p.held_count ? `${p.bolero_id ? ' · ' : ''}${p.held_count} out` : ''}</div></div>`).join('') +
        `<div class="empty"><span>${d.results.length ? 'Not listed?' : 'No match for “' + esc(q) + '”'}</span><button class="btn sm" data-new>+ Create “${esc(q)}”</button></div>`;
      resultsEl.classList.remove('hidden'); paintActive();
      resultsEl.querySelectorAll('.item').forEach(el => el.onmousedown = () => selectPerson(+el.dataset.id));
      resultsEl.querySelector('[data-new]').onmousedown = () => openNewPerson(q);
    } catch (e) { toast('err', 'Search failed', e.message); }
  }

  async function selectPerson(id) {
    hideResults();
    try {
      const d = await api(`/api/people/${id}`);
      state.person = d.person; state.holdings = d.holdings; state.assignments = d.assignments; state.plan = d.plan || []; state.result = null;
      searchEl.value = '';
      renderPerson(); renderBasket(); pushState(); focusScan();
    } catch (e) { toast('err', 'Could not load person', e.message); }
  }
  function clearPerson() { state.person = null; state.holdings = []; state.assignments = []; state.plan = []; renderPerson(); renderBasket(); pushState(); }

  function renderPerson() {
    const panel = $('#personPanel');
    const p = state.person;
    if (!p) {
      panel.innerHTML = `<div class="empty-state">Type a name to start. Items can be signed in without a person.</div>`;
      $('#btnAddAssigned').classList.add('hidden');
      return;
    }
    const hold = state.holdings, pre = state.assignments, plan = state.plan;
    const line1 = [p.dept, p.role].filter(Boolean).map(esc).join(' · ');
    const line2 = [p.phone ? `<span class="mono">${esc(p.phone)}</span>` : '', p.external_id ? `<span class="si">ID ${esc(p.external_id)}</span>` : ''].filter(Boolean).join(' · ');
    const devRow = (d, actions) => `<li>
          <span class="badge ${d.status}">${esc(d.status)}</span>
          <span class="txt"><div class="label">${esc(d.label || d.si)}</div><div class="type">${esc(d.type_name)} · <span class="si">${esc(d.si)}</span>${d.holder_name && (!p || d.holder_id !== p.id) ? ` · <span class="amber">with ${esc(d.holder_name)}</span>` : ''}</div></span>
          <span class="actions">${actions}</span></li>`;
    panel.innerHTML = `
      <div class="person-card">
        <div class="row"><div class="name grow">${esc(p.name)}</div>${p.bolero_id ? `<span class="bolero" title="This person's Bolero ID">Bolero ${esc(p.bolero_id.replace(/^bolero\s*/i, ''))}</span>` : ''}<button class="btn sm ghost" id="btnClearPerson" title="Clear">&#10005;</button></div>
        ${line1 ? `<div class="meta">${line1}</div>` : ''}${line2 ? `<div class="meta">${line2}</div>` : ''}
      </div>
      ${plan.length ? `<div class="section-title"><h3>Plan</h3></div>
      <ul class="plan">${plan.map(r => `<li class="${r.held >= r.qty ? 'done' : ''}"><span class="tick">${r.held >= r.qty ? '&#10003;' : ''}</span><span class="txt">${r.qty > 1 ? r.qty + ' × ' : ''}${esc(r.type_name)}${/bolero/i.test(r.type_name) && p.bolero_id ? ` <span class="muted">· ID ${esc(p.bolero_id.replace(/^bolero\s*/i, ''))}</span>` : ''}${r.note ? ` <span class="dim">· ${esc(r.note)}</span>` : ''}</span><span class="mono count">${r.held}/${r.qty}</span></li>`).join('')}</ul>` : ''}
      <div class="section-title"><h3>Signed out <span class="mono">${hold.length}</span></h3>${hold.length ? '<button class="btn sm ghost" data-return-all>Return all</button>' : ''}</div>
      <ul class="dev-list">${hold.map(d => devRow(d, `<button class="btn sm" data-swap="${d.id}" title="Swap for another item">Swap</button><button class="btn sm ghost" data-flag="${d.id}" title="Mark lost or damaged">!</button><button class="btn sm ghost" data-add="${d.id}" title="Add to list">+</button>`)).join('') || '<li class="empty">Nothing signed out.</li>'}</ul>
      ${pre.length ? `<div class="section-title"><h3>Reserved devices <span class="mono">${pre.length}</span></h3><button class="btn sm ghost" data-add-all>Add all</button></div>
      <ul class="dev-list">${pre.map(d => devRow(d, `<button class="btn sm" data-add="${d.id}">+ Add</button>`)).join('')}</ul>` : ''}`;
    $('#btnClearPerson').onclick = clearPerson;
    panel.querySelectorAll('[data-add]').forEach(b => b.onclick = () => addById(+b.dataset.add));
    const addAll = panel.querySelector('[data-add-all]'); if (addAll) addAll.onclick = () => pre.forEach(d => addToBasket(d, true));
    const retAll = panel.querySelector('[data-return-all]'); if (retAll) retAll.onclick = () => hold.forEach(d => addToBasket(d, true));
    panel.querySelectorAll('[data-swap]').forEach(b => b.onclick = () => openSwap(hold.find(d => d.id === +b.dataset.swap)));
    panel.querySelectorAll('[data-flag]').forEach(b => b.onclick = () => openFlag(hold.find(d => d.id === +b.dataset.flag)));
    $('#btnAddAssigned').classList.toggle('hidden', !pre.length);
  }
  $('#btnAddAssigned').onclick = () => state.assignments.forEach(d => addToBasket(d, true));

  function openNewPerson(prefill = '') {
    modal('New person', `
      <div class="field"><label>Full name</label><input id="np_name" value="${esc(prefill)}"></div>
      <div class="grid cols-2"><div class="field"><label>Department</label><input id="np_dept"></div><div class="field"><label>Role</label><input id="np_role"></div></div>
      <div class="grid cols-2"><div class="field"><label>Phone</label><input id="np_phone"></div><div class="field"><label>Bolero ID (optional)</label><input id="np_bolero" placeholder="e.g. 61"></div></div>
      <div class="field"><label>Unique ID (optional)</label><input id="np_ext"></div>`,
      [{ label: 'Cancel' }, { label: 'Create & select', cls: 'primary', onClick: async root => {
        try {
          const d = await post('/api/people', { name: $('#np_name', root).value, dept: $('#np_dept', root).value, role: $('#np_role', root).value, phone: $('#np_phone', root).value, external_id: $('#np_ext', root).value, bolero_id: $('#np_bolero', root).value });
          closeModal(); toast('ok', 'Person created', d.person.name); selectPerson(d.person.id);
        } catch (e) { toast('err', 'Could not create', e.message); }
      } }]);
  }
  $('#btnNewPerson').onclick = () => openNewPerson(searchEl.value.trim());

  // ---- scanning / basket -------------------------------------------------
  const scanEl = $('#scanInput');
  function focusScan() { if (!isModalOpen()) scanEl.focus(); }
  scanEl.addEventListener('keydown', async e => {
    if (e.key !== 'Enter') return;
    e.preventDefault();
    const si = scanEl.value.trim();
    scanEl.value = '';
    if (!si) return;
    try {
      const d = await api(`/api/devices/lookup?si=${encodeURIComponent(si)}`);
      if (d.found) addToBasket(d.device); else openQuickCreate(d.si);
    } catch (err) { toast('err', 'Lookup failed', err.message); }
  });
  function addById(id) {
    const known = state.holdings.concat(state.assignments).find(d => d.id === id);
    if (known) addToBasket(known);
  }
  function addToBasket(device, quiet = false) {
    if (state.basket.some(d => d.id === device.id)) { if (!quiet) toast('warn', 'Already in the list', device.label || device.si, 1500); return; }
    state.basket.push(device); state.result = null;
    renderBasket(); pushState();
  }
  function removeFromBasket(id) { state.basket = state.basket.filter(d => d.id !== id); renderBasket(); pushState(); }
  function clearBasket() { state.basket = []; renderBasket(); pushState(); }
  $('#btnClear').onclick = () => { clearBasket(); focusScan(); };

  function renderBasket() {
    const ul = $('#basket');
    const p = state.person;
    ul.innerHTML = state.basket.map((d, i) => {
      let cls = '', note = '';
      if (d.status === 'out') {
        if (p && d.holder_id === p.id) { cls = 'warn'; note = 'Already with this person'; }
        else { cls = 'conflict'; note = `OUT to ${d.holder_name}`; }
      } else if (d.status === 'lost' || d.status === 'damaged') { cls = 'warn'; note = `Marked ${d.status.toUpperCase()}`; }
      if (d.site_name && d.site_name !== TECH.site_name) note += (note ? ' · ' : '') + `${d.site_name} stock`;
      if (d.assigned_person_name && (!p || d.assigned_person_id !== p.id)) note += (note ? ' · ' : '') + `pre-assigned to ${d.assigned_person_name}`;
      return `<li class="${cls}"><span class="idx">${i + 1}</span>
        <span class="txt"><div class="label">${esc(d.label || d.si)}</div><div class="sub">${esc(d.type_name)} · <span class="si">${esc(d.si)}</span>${note ? ` · <span class="${cls === 'conflict' ? 'red' : 'amber'}">${esc(note)}</span>` : ''}</div></span>
        <span class="badge ${d.status}">${esc(d.status)}</span>
        <span class="remove" data-rm="${d.id}" title="Remove">&#10005;</span></li>`;
    }).join('');
    ul.querySelectorAll('[data-rm]').forEach(el => el.onclick = () => removeFromBasket(+el.dataset.rm));
    $('#basketEmpty').classList.toggle('hidden', state.basket.length > 0);
    $('#basketCount').textContent = state.basket.length;
    const n = state.basket.length;
    $('#btnSignIn').disabled = !n;
    $('#btnSignOut').disabled = !n || !p;
    $('#btnSignOut').innerHTML = p ? `&#8593; Sign OUT to ${esc(p.name.split(' ')[0])}` : '&#8593; Sign OUT';
    $('#actionHint').textContent = !n ? (p ? `Scan the items for ${p.name}.` : 'Pick a person and scan items to sign out. Scan items alone to sign in.')
      : (p ? `${n} item${n > 1 ? 's' : ''} ready.` : `${n} item${n > 1 ? 's' : ''} ready to sign in. Pick a person to sign out instead.`);
  }

  function openQuickCreate(si) {
    modal('New device', `
      <p class="help"><span class="mono">${esc(si)}</span> is not in the database yet.</p>
      <div class="field"><label>SI number</label><input id="qc_si" value="${esc(si)}" class="mono"></div>
      <div class="grid cols-2">
        <div class="field"><label>Type</label><select id="qc_type">${TYPES.map(t => `<option value="${t.id}">${esc(t.name)}</option>`).join('')}</select></div>
        <div class="field"><label>Label (e.g. Bolero 61)</label><input id="qc_label"></div>
      </div>
      <label class="check"><input type="checkbox" id="qc_site"> This item belongs to ${esc(TECH.site_name)} only</label>`,
      [{ label: 'Cancel' }, { label: 'Create & add', cls: 'primary', onClick: async root => {
        try {
          const d = await post('/api/devices/quick', { si: $('#qc_si', root).value, type_id: +$('#qc_type', root).value, label: $('#qc_label', root).value, site_specific: $('#qc_site', root).checked });
          closeModal(); toast('ok', 'Device created', `${d.device.label || d.device.si} · ${d.device.type_name}`); addToBasket(d.device); refreshSummary();
        } catch (e) { toast('err', 'Could not create', e.message); }
      } }]);
    setTimeout(() => { const l = $('#qc_label'); if (l) l.focus(); }, 50);
  }

  // ---- sign out / in -----------------------------------------------------
  const OK_OUTCOMES = ['issued', 'reassigned', 'reissued', 'returned', 'restored'];
  const NICE = { issued: ['ok', 'Signed OUT'], reassigned: ['warn', 'Reassigned'], reissued: ['warn', 'Restored & signed out'], already_held: ['warn', 'Already with this person'], returned: ['ok', 'Signed IN'], already_in: ['warn', 'Already in stock (scan logged)'], restored: ['ok', 'Restored to stock'], conflict: ['err', 'Not processed'], missing: ['err', 'Unknown device'] };

  function announce(results) {
    const grouped = {};
    results.forEach(r => (grouped[r.outcome] = grouped[r.outcome] || []).push(r.label || r.si));
    Object.entries(grouped).forEach(([o, items]) => { const [k, t] = NICE[o] || ['warn', o]; toast(k, `${t} (${items.length})`, items.join(', '), 5000); });
  }
  function setResult(kind, results, personName) {
    const ok = results.filter(r => OK_OUTCOMES.includes(r.outcome));
    state.result = ok.length ? { kind, person: personName || null, items: ok.map(r => ({ si: r.si, label: r.label, type: r.type })), ts: Date.now() } : null;
    if (state.result) setTimeout(() => { if (state.result && Date.now() - state.result.ts >= 11500) { state.result = null; pushState(); } }, 12000);
  }
  async function refreshPerson() {
    if (!state.person) return;
    try { const d = await api(`/api/people/${state.person.id}`); state.holdings = d.holdings; state.assignments = d.assignments; state.plan = d.plan || []; } catch (e) { /* ignore */ }
  }

  $('#btnSignIn').onclick = async () => {
    if (!state.basket.length) return;
    try {
      const d = await post('/api/signin', { device_ids: state.basket.map(x => x.id) });
      announce(d.results);
      setResult('in', d.results, state.person ? state.person.name : null);
      state.basket = [];
      await refreshPerson();
      renderPerson(); renderBasket(); pushState(); refreshSummary(); refreshFeed(); focusScan();
    } catch (e) { toast('err', 'Sign in failed', e.message); }
  };

  $('#btnSignOut').onclick = () => doSignOut([]);
  async function doSignOut(forceIds) {
    if (!state.basket.length || !state.person) return;
    const person = state.person;
    try {
      const d = await post('/api/signout', { person_id: person.id, device_ids: state.basket.map(x => x.id), force_ids: forceIds });
      const conflicts = d.results.filter(r => r.outcome === 'conflict');
      announce(d.results.filter(r => r.outcome !== 'conflict'));
      setResult('out', d.results, person.name);
      refreshSummary(); refreshFeed();
      if (conflicts.length) {
        state.basket = state.basket.filter(x => conflicts.some(c => c.id === x.id));
        await refreshPerson(); renderPerson(); renderBasket(); pushState();
        openConflicts(conflicts, person);
        return;
      }
      state.basket = [];
      await refreshPerson();
      const missing = state.plan.filter(r => r.held < r.qty).map(r => `${r.qty - r.held} × ${r.type_name}`);
      if (missing.length) toast('warn', 'Still to issue', missing.join(', '), 6000);
      clearPerson(); focusScan();
    } catch (e) { toast('err', 'Sign out failed', e.message); }
  }
  function openConflicts(conflicts, person) {
    modal('Items not available', `
      <p class="help">Tick the ones to give to <b>${esc(person.name)}</b> anyway. The rest stay in the list.</p>
      <ul class="conflict-list" style="list-style:none;padding:0;margin:0">${conflicts.map(c => `<li>
        <span><div style="font-weight:800">${esc(c.label || c.si)} <span class="si">${esc(c.si)}</span></div><div class="red" style="font-size:13px">${esc(c.message)}</div></span>
        <label class="check"><input type="checkbox" data-force="${c.id}"> ${c.status === 'out' ? 'Reassign' : 'Issue anyway'}</label></li>`).join('')}</ul>`,
      [{ label: 'Leave in list' }, { label: 'Apply', cls: 'primary', onClick: async root => {
        const force = [...root.querySelectorAll('[data-force]:checked')].map(x => +x.dataset.force);
        closeModal();
        if (!force.length) return;
        state.basket = state.basket.filter(x => force.includes(x.id));
        await doSignOut(force);
      } }]);
  }

  // ---- swap / lost / damaged ---------------------------------------------
  function openSwap(old) {
    if (!old) return;
    modal(`Swap ${old.label || old.si}`, `
      <p class="help">${esc(state.person.name)} returns <b>${esc(old.label || old.si)}</b> and gets the item you scan now.</p>
      <div class="field"><label>Scan replacement SI</label><input id="sw_si" class="mono" placeholder="Scan…" autocomplete="off"></div>
      <div class="grid cols-2">
        <div class="field"><label>Reason</label><select id="sw_reason"><option>Damaged</option><option>Faulty</option><option>Battery issue</option><option>Wrong item</option><option>Lost</option><option>Other</option></select></div>
        <div class="field"><label>Returned item becomes</label><select id="sw_status"><option value="damaged">Damaged</option><option value="in">Back in stock (OK)</option><option value="lost">Lost</option></select></div>
      </div>
      <div class="field"><label>Note</label><input id="sw_note" placeholder="Optional"></div>`,
      [{ label: 'Cancel' }, { label: 'Swap', cls: 'primary', onClick: root => submitSwap(root, old) }]);
    $('#sw_si').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); submitSwap($('#modalRoot'), old); } });
    $('#sw_reason').onchange = e => { if (e.target.value === 'Lost') $('#sw_status').value = 'lost'; if (e.target.value === 'Wrong item') $('#sw_status').value = 'in'; };
  }
  async function submitSwap(root, old) {
    const newSi = $('#sw_si', root).value.trim();
    if (!newSi) { toast('warn', 'Scan the replacement first'); return; }
    try {
      const d = await post('/api/swap', { old_device_id: old.id, new_si: newSi, reason: $('#sw_reason', root).value, note: $('#sw_note', root).value, old_status: $('#sw_status', root).value });
      closeModal(); toast('ok', 'Swapped', `${d.old.label || d.old.si} → ${d.new.label || d.new.si}`);
      state.result = { kind: 'out', person: state.person.name, items: [{ si: d.new.si, label: d.new.label, type: d.new.type }], ts: Date.now() };
      await selectPerson(state.person.id); refreshSummary(); refreshFeed();
    } catch (e) { toast('err', 'Swap failed', e.message); }
  }
  function openFlag(d) {
    if (!d) return;
    modal(`Flag ${d.label || d.si}`, `
      <p class="help">The item comes off <b>${esc(state.person.name)}</b>'s list and the change is logged against them.</p>
      <div class="grid cols-2">
        <div class="field"><label>Status</label><select id="fl_status"><option value="damaged">Damaged</option><option value="lost">Lost</option></select></div>
        <div class="field"><label>Reason</label><input id="fl_reason" placeholder="e.g. cable cut, dropped"></div>
      </div>
      <div class="field"><label>Note</label><input id="fl_note" placeholder="Optional"></div>`,
      [{ label: 'Cancel' }, { label: 'Save', cls: 'danger', onClick: async root => {
        try {
          const status = $('#fl_status', root).value;
          await post(`/api/devices/${d.id}/status`, { status, reason: $('#fl_reason', root).value, note: $('#fl_note', root).value });
          closeModal(); toast('warn', `Marked ${status}`, d.label || d.si);
          await selectPerson(state.person.id); refreshSummary(); refreshFeed();
        } catch (e) { toast('err', 'Failed', e.message); }
      } }]);
  }

  // ---- keyboard: a barcode scanner is just a fast keyboard -------------
  document.addEventListener('keydown', e => {
    if (e.key === 'F2') { e.preventDefault(); searchEl.focus(); searchEl.select(); return; }
    if (e.key === 'F4') { e.preventDefault(); focusScan(); return; }
    if (e.key === 'Escape') { if (isModalOpen()) closeModal(); return; }
    if (isModalOpen()) return;
    const t = e.target;
    const typing = t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT');
    if (!typing && e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) scanEl.focus();
  });

  // ---- boot ----------------------------------------------------------------
  renderBasket(); refreshSummary(); refreshFeed(); pushState();
  setInterval(refreshFeed, 15000); setInterval(refreshSummary, 30000);
  focusScan();
})();
