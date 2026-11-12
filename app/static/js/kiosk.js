/* RDR kiosk front end. Plain JS, no build step. */
(function () {
  'use strict';
  const $ = (sel, root = document) => root.querySelector(sel);
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const TECH = window.RDR.tech;
  if (!TECH) { location.replace('/start'); return; }   // no tech and site picked: nothing here can work
  const TYPES = window.RDR.types;
  const idOf = v => String(v || '').replace(/^bolero\s*/i, '');
  const isPack = d => /bolero/i.test(d.type_name || d.type || '') && !/antenna/i.test(d.type_name || d.type || '');
  const isRadio = n => /radio/i.test(n || '') && !/adapter/i.test(n || '');
  const isHeadset = n => /headset/i.test(n || '') && !/adapter/i.test(n || '');
  // What sits beside a name: Bolero (person's ID or the held pack), Radio (held radio), both, or nothing.
  function chips(p, holdings) {
    const out = [];
    const pack = (holdings || []).find(isPack);
    const bol = p.bolero_id ? idOf(p.bolero_id) : (pack ? idOf(pack.linked_id || pack.label) : (p.held_bolero ? idOf(p.held_bolero) : ''));
    const face = p.faceplate ? ` style="--face:#${esc(p.faceplate)}"` : '';
    if (bol || pack) out.push(`<span class="bolero${p.faceplate ? ' faced' : ''}"${face} title="Bolero ID${p.faceplate ? ' · faceplate colour' : ''}">${bol ? 'Bolero ' + esc(bol) : 'Bolero'}</span>`);
    const radio = (holdings || []).find(d => isRadio(d.type_name));
    const radioLabel = radio ? (radio.label || 'Radio') : (p.radio_label || '');
    if (radioLabel) out.push(`<span class="bolero radio" title="Radio">${esc(radioLabel)}</span>`);
    return out.join(' ');
  }
  const ACTIONS = {
    out: 'OUT', in: 'IN', in_noop: 'Scan (already in)', out_noop: 'Scan (already out)', out_reassign: 'OUT (reassigned)',
    swap_out: 'Swap returned', swap_in: 'Swap issued', lost: 'LOST', damaged: 'DAMAGED', restored: 'Restored',
    created: 'Created', imported: 'Imported', assigned: 'Reserved', unassigned: 'Unreserved', transfer: 'Transfer',
  };

  const state = { person: null, holdings: [], assignments: [], basket: [], scope: 'site', result: null, collector: null, recipients: [], served: [], lastPerson: null };

  // ---- api ---------------------------------------------------------------
  async function api(path, opts = {}) {
    const r = await fetch(path, Object.assign({ headers: { 'Content-Type': 'application/json', 'X-RDR-Site': String(TECH.site_id) }, cache: 'no-store' }, opts));
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
    setTimeout(() => { el.classList.add('hide'); setTimeout(() => el.remove(), 220); }, ms);
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
      $('#summary').innerHTML = `<div class="chip total"><span class="name">All devices</span><span class="nums"><span class="in">${t.n_in} in</span><span class="out">${t.n_out} out</span>${bad ? `<span class="bad">${bad} !</span>` : ''}</span></div>` +
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
      const brief = d => ({ si: d.si, label: d.label, type: d.type_name || d.type, linked_id: d.linked_id || '' });
      post('/api/kiosk/state', { state: {
        person: state.person ? { name: state.person.name, dept: state.person.dept, role: state.person.role, bolero_id: state.person.bolero_id, faceplate: state.person.faceplate || '', radio: (state.holdings.find(d => /radio/i.test(d.type_name)) || {}).label || '' } : null,
        // Someone collecting for others: the mirror stays on the collector and lists each person's devices under their name.
        collect: state.collector ? { name: state.collector.name, people: state.recipients.map(r => ({ name: r.name, bolero_id: r.bolero_id || '', faceplate: r.faceplate || '', items: state.basket.filter(x => x.for && x.for.id === r.id).map(brief) })) } : null,
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
      resultsEl.innerHTML = (state.collector ? `<div class="empty top"><span>${esc(state.collector.name)} is collecting. Pick who these devices are for.</span></div>` : '') +
        d.results.map(p => `<div class="item" data-id="${p.id}"><div><div class="name">${esc(p.name)} ${chips(p, null)}</div><div class="meta">${esc([p.dept, p.role, p.phone].filter(Boolean).join(' · '))}</div></div><div class="counts">${p.held_count ? `${p.held_count} out` : ''}</div></div>`).join('') +
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
      if (state.collector && id === state.collector.id) { toast('warn', `${d.person.name.split(' ')[0]} is the collector`, 'Pick the people they are collecting for, or press Exit.'); return; }
      if (state.person && state.person.id !== id) state.lastPerson = { id: state.person.id, name: state.person.name };
      state.person = d.person; state.holdings = d.holdings; state.assignments = d.assignments; state.result = null;
      searchEl.value = '';
      if (state.collector && !state.recipients.some(r => r.id === id)) {
        state.recipients.push({ id, name: d.person.name, bolero_id: d.person.bolero_id || '', faceplate: d.person.faceplate || '' });
        const added = d.assignments.filter(a => !state.basket.some(b => b.id === a.id));
        added.forEach(a => addToBasket(a, true));
        toast('ok', `Added ${d.person.name.split(' ')[0]}`, added.length ? `${added.length} reserved device${added.length > 1 ? 's' : ''} in the list. Scan anything else for them.` : 'Nothing reserved. Scan their devices.', 3000);
      }
      renderPerson(); renderBasket(); pushState(); updatePrev();
      if (state.collector) searchEl.focus(); else focusScan();
    } catch (e) { toast('err', 'Could not load person', e.message); }
  }
  function clearPerson() {
    if (state.person) state.lastPerson = { id: state.person.id, name: state.person.name };
    state.person = null; state.holdings = []; state.assignments = [];
    renderPerson(); renderBasket(); pushState(); updatePrev();
    if (state.collector) { searchEl.focus(); }
  }
  function startCollecting(p) {
    state.collector = { id: p.id, name: p.name }; state.served = []; state.recipients = [];
    state.person = null; state.holdings = []; state.assignments = []; state.basket = state.basket.filter(x => x.for);
    renderPerson(); renderBasket(); pushState();
    searchEl.value = ''; searchEl.placeholder = 'Add a person they are collecting for'; searchEl.focus();
  }
  function exitCollecting() {
    const c = state.collector;
    state.collector = null; state.served = []; state.recipients = [];
    state.basket.forEach(x => { delete x.for; });
    searchEl.placeholder = 'Name, phone or ID';
    renderPerson(); renderBasket(); pushState();
    if (c) toast('ok', 'Stopped collecting', `${c.name} is no longer recorded as collector`, 2500);
    focusScan();
  }
  function renderCollectStrip() {
    const el = $('#collectStrip'), c = state.collector;
    el.classList.toggle('hidden', !c);
    if (!c) return;
    const active = state.person ? state.person.id : null;
    const chips = state.recipients.map(r => `<span class="rcpt${r.id === active ? ' active' : ''}" data-rcpt="${r.id}" title="Scan for ${esc(r.name)}">${esc(r.name)} <b>${state.basket.filter(x => x.for && x.for.id === r.id).length}</b><span class="x" data-rm-rcpt="${r.id}" title="Remove">&#10005;</span></span>`).join('');
    const done = state.served.map(x => `<span class="chip-done">${esc(x.name.split(' ')[0])} ${x.count} &#10003;</span>`).join(' ');
    el.innerHTML = `<div class="row"><div class="grow"><div class="t">${esc(c.name)} is collecting for</div><div class="d rcpts">${chips || '<span class="dim">Search and add each person. Their reserved devices join the list.</span>'}${done ? ` <span class="dim">Done:</span> ${done}` : ''}</div></div><button class="btn sm" id="btnExitCollect">Exit</button></div>`;
    $('#btnExitCollect').onclick = exitCollecting;
    el.querySelectorAll('[data-rcpt]').forEach(x => x.onclick = e => { if (e.target.dataset.rmRcpt) return; selectPerson(+x.dataset.rcpt); });
    el.querySelectorAll('[data-rm-rcpt]').forEach(x => x.onclick = () => removeRecipient(+x.dataset.rmRcpt));
  }
  function removeRecipient(id) {
    state.recipients = state.recipients.filter(r => r.id !== id);
    state.basket = state.basket.filter(x => !(x.for && x.for.id === id));
    if (state.person && state.person.id === id) { state.person = null; state.holdings = []; state.assignments = []; }
    renderPerson(); renderBasket(); pushState(); searchEl.focus();
  }
  function updatePrev() {
    const b = $('#btnPrev'), lp = state.lastPerson;
    const show = lp && (!state.person || state.person.id !== lp.id);
    b.classList.toggle('hidden', !show);
    if (show) b.textContent = `Back to ${lp.name.split(' ')[0]}`;
  }
  $('#btnPrev').onclick = () => { if (state.lastPerson) selectPerson(state.lastPerson.id); };

  // "Bolero · Pro-D1 headset" -> [{text, type, pack, matches(device)}], matched against the device types list.
  function allocationItems(p) {
    if (!p || !p.allocation) return [];
    const norm = x => String(x || '').toLowerCase().replace(/headset|beltpack|bp|pack/g, ' ').replace(/[^a-z0-9]+/g, ' ').trim();
    return p.allocation.split(/[·,/;+]| and /).map(x => x.trim()).filter(Boolean).map(text => {
      const key = norm(text), isPackText = /bolero|beltpack|\bbp\b|\bpack\b/i.test(text);
      // a bare "Radio" or "Headset" is met by any model; anything more specific must be that type
      const any = key === 'radio' ? isRadio : (!key && /headset/i.test(text) ? isHeadset : null);
      const named = TYPES.map(t => ({ t, k: norm(t.name) })).filter(x => x.k);
      const closest = (list, dir) => list.sort((a, b) => dir * (a.k.length - b.k.length))[0];
      let type = null;
      if (any) type = TYPES.find(t => t.name.toLowerCase() === text.toLowerCase()) || null;
      else if (key) type = (named.find(x => x.k === key) || closest(named.filter(x => x.k.includes(key)), 1) || closest(named.filter(x => key.includes(x.k)), -1) || {}).t || null;
      if (!type && !any && isPackText) type = TYPES.find(t => /bolero/i.test(t.name) && !/headset/i.test(t.name)) || null;
      const pack = !!(isPackText || (type && isPack({ type_name: type.name })));
      const matches = d => any ? any(d.type_name) : (type ? d.type_id === type.id || d.type_name === type.name : (key ? norm(d.type_name).includes(key) : false));
      return { text, type, pack, matches, any: !!any };
    });
  }

  function renderPerson() {
    const panel = $('#personPanel');
    const p = state.person;
    renderCollectStrip();
    if (!p) {
      panel.innerHTML = state.collector
        ? `<div class="empty-state">Add the people <b>${esc(state.collector.name)}</b> is collecting for.<span class="dim">Each person's reserved devices join the list. Scan anything extra while their name is selected. One sign out covers everyone.</span></div>`
        : `<div class="empty-state">Search for a person to sign devices out.<span class="dim">To sign in, just scan. No name needed.</span></div>`;
      $('#btnAddAssigned').classList.add('hidden');
      return;
    }
    const hold = state.holdings, pre = state.assignments;
    const line1 = [p.dept, p.role].filter(Boolean).map(esc).join(' · ');
    const line2 = [p.phone ? `<span class="mono">${esc(p.phone)}</span>` : '', p.external_id ? `<span class="si">ID ${esc(p.external_id)}</span>` : ''].filter(Boolean).join(' · ');
    // Assigned list: one row per device, led by its type, then linked ID and SI.
    const devRow = (d, actions) => `<li>
          <span class="badge ${d.status}">${esc(d.status)}</span>
          <span class="txt"><div class="label">${esc(d.type_name)}${d.linked_id ? ` <span class="bolero${p.faceplate && isPack(d) ? ' faced' : ''}"${p.faceplate && isPack(d) ? ` style="--face:#${esc(p.faceplate)}"` : ''}>ID ${esc(d.linked_id)}</span>` : ''}</div><div class="type">${d.label && d.label !== d.type_name ? esc(d.label) + ' · ' : ''}<span class="si">${esc(d.si)}</span>${d.holder_name && (!p || d.holder_id !== p.id) ? ` · <span class="amber">with ${esc(d.holder_name)}</span>` : ''}</div></span>
          <span class="actions">${actions}</span></li>`;
    const byType = [...hold].sort((a, b) => (a.type_name > b.type_name ? 1 : a.type_name < b.type_name ? -1 : 0));
    const hasPack = hold.some(isPack);
    const myId = idOf(p.bolero_id);
    const reservedPack = pre.find(d => isPack(d) && (!d.linked_id || idOf(d.linked_id) === myId));
    const plannedRow = p.bolero_id && !hasPack ? `<li class="pending"><span class="badge neutral">plan</span><span class="txt"><div class="label">Bolero Beltpack <span class="bolero${p.faceplate ? ' faced' : ''}"${p.faceplate ? ` style="--face:#${esc(p.faceplate)}"` : ''}>ID ${esc(myId)}</span></div><div class="type">${reservedPack ? `Reserved: ${esc(reservedPack.label || reservedPack.si)} · <span class="si">${esc(reservedPack.si)}</span>. Scan it to sign out.` : 'No pack linked yet. Scan one for this person.'}</div></span></li>` : '';
    // Everything else on the allocation line (headset, radio, …) that the person does not hold yet.
    const allocRows = allocationItems(p).filter(a => !(a.pack && (hasPack || p.bolero_id)) && !hold.some(d => a.matches(d))).map(a =>
      `<li class="pending"><span class="badge neutral">plan</span><span class="txt"><div class="label">${esc(a.type ? a.type.name : a.text)}</div><div class="type">${a.type || a.any ? 'From allocation. Scan one for this person.' : `From allocation: ${esc(a.text)}`}</div></span></li>`).join('');
    panel.innerHTML = `
      <div class="person-card">
        <div class="row"><div class="name grow">${esc(p.name)}</div>${chips(p, hold)}<button class="btn sm ghost" id="btnEditPerson" title="Edit details">Edit</button><button class="btn sm ghost" id="btnClearPerson" title="Clear">&#10005;</button></div>
        ${line1 ? `<div class="meta">${line1}</div>` : ''}${line2 ? `<div class="meta">${line2}</div>` : ''}
        ${p.allocation ? `<div class="meta alloc">Allocation: <b>${esc(p.allocation)}</b></div>` : ''}
        <div class="meta pack-live" id="packLive" data-number="${p.bolero_id ? esc(idOf(p.bolero_id)) : ''}"></div>
        ${state.collector ? `<div class="meta collector">Scans go to <b>${esc(p.name)}</b>, collected by <b>${esc(state.collector.name)}</b></div>` : ''}
      </div>
      <div class="section-title"><h3>Assigned <span class="mono">${hold.length}</span></h3>${hold.length ? '<button class="btn sm ghost" data-return-all>Return all</button>' : ''}</div>
      <ul class="dev-list">${plannedRow}${allocRows}${byType.map(d => devRow(d, `<button class="btn sm" data-swap="${d.id}" title="Swap for another device">Swap</button><button class="btn sm ghost" data-flag="${d.id}" title="Mark lost or damaged">!</button><button class="btn sm ghost" data-add="${d.id}" title="Add to list">+</button>`)).join('') || (plannedRow || allocRows ? '' : '<li class="empty">Nothing assigned.</li>')}</ul>
      ${pre.length ? `<div class="section-title"><h3>Reserved devices <span class="mono">${pre.length}</span></h3><button class="btn sm ghost" data-add-all>Add all</button></div>
      <ul class="dev-list">${pre.map(d => devRow(d, `<button class="btn sm" data-add="${d.id}">Add</button>`)).join('')}</ul>` : ''}
      ${state.collector ? '' : `<div class="collect-cta"><button class="btn block" id="btnForSomeone">Picking up for other people?</button><div class="dim">Their own devices stay with them. The people you pick next receive the devices.</div></div>`}`;
    $('#btnClearPerson').onclick = clearPerson;
    $('#btnEditPerson').onclick = () => openEditPerson(p);
    refreshPackLive();
    const fs = $('#btnForSomeone'); if (fs) fs.onclick = () => startCollecting(p);
    renderCollectStrip();
    if (state.collector) searchEl.placeholder = 'Add another person they are collecting for';
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
      <div class="grid cols-2"><div class="field"><label>Phone</label><input id="np_phone"></div><div class="field"><label>Bolero ID (optional)</label><input id="np_bolero" placeholder="61"></div></div>
      <div class="field"><label>Unique ID (optional)</label><input id="np_ext"></div>`,
      [{ label: 'Cancel' }, { label: 'Create and select', cls: 'primary', onClick: async root => {
        try {
          const d = await post('/api/people', { name: $('#np_name', root).value, dept: $('#np_dept', root).value, role: $('#np_role', root).value, phone: $('#np_phone', root).value, external_id: $('#np_ext', root).value, bolero_id: $('#np_bolero', root).value });
          closeModal(); toast('ok', 'Person created', d.person.name); selectPerson(d.person.id);
        } catch (e) { toast('err', 'Could not create', e.message); }
      } }]);
  }
  $('#btnNewPerson').onclick = () => openNewPerson(searchEl.value.trim());

  function openEditPerson(p) {
    modal('Edit details', `
      <div class="field"><label>Full name</label><input id="ep_name" value="${esc(p.name)}"></div>
      <div class="grid cols-2"><div class="field"><label>Department</label><input id="ep_dept" value="${esc(p.dept || '')}"></div><div class="field"><label>Role</label><input id="ep_role" value="${esc(p.role || '')}"></div></div>
      <div class="grid cols-2"><div class="field"><label>Phone</label><input id="ep_phone" value="${esc(p.phone || '')}"></div><div class="field"><label>Bolero ID</label><input id="ep_bolero" value="${esc(p.bolero_id || '')}" placeholder="61"></div></div>
      <div class="grid cols-2"><div class="field"><label>Unique ID</label><input id="ep_ext" value="${esc(p.external_id || '')}"></div><div class="field"><label>Allocation</label><input id="ep_alloc" value="${esc(p.allocation || '')}" placeholder="Bolero · Pro-D1 headset"></div></div>`,
      [{ label: 'Cancel' }, { label: 'Save', cls: 'primary', onClick: async root => {
        try {
          await post(`/api/people/${p.id}`, { name: $('#ep_name', root).value, dept: $('#ep_dept', root).value, role: $('#ep_role', root).value, phone: $('#ep_phone', root).value, bolero_id: $('#ep_bolero', root).value, external_id: $('#ep_ext', root).value, allocation: $('#ep_alloc', root).value });
          closeModal(); toast('ok', 'Details saved'); await selectPerson(p.id);
        } catch (e) { toast('err', 'Could not save', e.message); }
      } }]);
  }

  // ---- scanning / basket -------------------------------------------------
  const scanEl = $('#scanInput');
  function focusScan() { if (!isModalOpen()) scanEl.focus(); }
  // Typing part of an SI (the last four digits, say) lists matching devices; a scanner's Enter still goes straight through.
  const scanResults = $('#scanResults');
  let scanTimer = null, scanHits = [], scanIdx = -1, scanSeq = 0;
  function hideScanResults() { scanResults.classList.add('hidden'); scanHits = []; scanIdx = -1; }
  function paintScanActive() { scanResults.querySelectorAll('.item').forEach((el, i) => el.classList.toggle('active', i === scanIdx)); }
  scanEl.addEventListener('input', () => {
    clearTimeout(scanTimer);
    const q = scanEl.value.trim();
    if (q.length < 2) { hideScanResults(); return; }
    scanTimer = setTimeout(() => searchDevices(q), 160);
  });
  scanEl.addEventListener('blur', () => setTimeout(hideScanResults, 150));
  async function searchDevices(q) {
    const seq = ++scanSeq;
    try {
      const d = await api(`/api/devices/search?q=${encodeURIComponent(q)}`);
      if (seq !== scanSeq || scanEl.value.trim() !== q) return;   // a newer keystroke or a scan happened meanwhile
      scanHits = d.results; scanIdx = scanHits.length ? 0 : -1;
      scanResults.innerHTML = scanHits.map(x => `<div class="item" data-id="${x.id}"><div><div class="name">${esc(x.label || x.si)} <span class="muted fw-n">· ${esc(x.type_name)}</span>${x.linked_id ? ` <span class="bolero">ID ${esc(x.linked_id)}</span>` : ''}</div><div class="meta"><span class="mono">${esc(x.si)}</span>${x.holder_name ? ` · with ${esc(x.holder_name)}` : ''}${x.site_name ? ` · ${esc(x.site_name)}` : ''}</div></div><span class="badge ${x.status}">${esc(x.status)}</span></div>`).join('')
        + `<div class="empty"><span>${scanHits.length ? 'Not it?' : 'No device matches “' + esc(q) + '”'}</span><button class="btn sm" data-create>+ Create “${esc(q)}”</button></div>`;
      scanResults.classList.remove('hidden'); paintScanActive();
      scanResults.querySelectorAll('.item').forEach(el => el.onmousedown = () => pickScanHit(scanHits.find(x => x.id === +el.dataset.id)));
      scanResults.querySelector('[data-create]').onmousedown = () => { hideScanResults(); openQuickCreate(q.toUpperCase()); scanEl.value = ''; };
    } catch (e) { /* ignore */ }
  }
  function pickScanHit(x) { if (!x) return; hideScanResults(); scanEl.value = ''; addToBasket(x); focusScan(); }
  scanEl.addEventListener('keydown', async e => {
    if (!scanResults.classList.contains('hidden')) {
      if (e.key === 'ArrowDown') { scanIdx = Math.min(scanIdx + 1, scanHits.length - 1); paintScanActive(); e.preventDefault(); return; }
      if (e.key === 'ArrowUp') { scanIdx = Math.max(scanIdx - 1, 0); paintScanActive(); e.preventDefault(); return; }
      if (e.key === 'Escape') { hideScanResults(); return; }
    }
    if (e.key !== 'Enter') return;
    e.preventDefault();
    clearTimeout(scanTimer); scanSeq++;
    const si = scanEl.value.trim();
    scanEl.value = '';
    if (!si) return;
    try {
      const d = await api(`/api/devices/lookup?si=${encodeURIComponent(si)}`);
      if (d.found) { hideScanResults(); addToBasket(d.device); return; }       // exact SI (a scan) goes straight in
      if (scanHits.length && scanIdx >= 0) { pickScanHit(scanHits[scanIdx]); return; }   // typed: take the highlighted match
      const r = await api(`/api/devices/search?q=${encodeURIComponent(si)}`);
      if (r.results.length === 1) { hideScanResults(); addToBasket(r.results[0]); return; }
      if (r.results.length > 1) { scanEl.value = si; searchDevices(si); return; }
      hideScanResults(); openQuickCreate(d.si);
    } catch (err) { toast('err', 'Lookup failed', err.message); }
  });
  function addById(id) {
    const known = state.holdings.concat(state.assignments).find(d => d.id === id);
    if (known) addToBasket(known);
  }
  let justAdded = null;
  function addToBasket(device, quiet = false) {
    if (state.basket.some(d => d.id === device.id)) { if (!quiet) toast('warn', 'Already in the list', device.label || device.si, 1500); return; }
    if (state.collector) {
      if (!state.person) { toast('warn', 'Who is it for?', 'Add or pick a person on the strip first.'); return; }
      device = Object.assign({}, device, { for: { id: state.person.id, name: state.person.name } });
    }
    state.basket.push(device); state.result = null; justAdded = device.id;
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
        else { cls = 'conflict'; note = `Out to ${d.holder_name}`; }
      } else if (d.status === 'lost' || d.status === 'damaged') { cls = 'warn'; note = `Marked ${d.status}`; }
      if (d.site_name && d.site_name !== TECH.site_name) note += (note ? ' · ' : '') + `${d.site_name} stock`;
      const isPack = /bolero/i.test(d.type_name) && !/antenna/i.test(d.type_name);
      if (isPack && p && d.status !== 'out') {
        const mine = (p.bolero_id || '').replace(/^bolero\s*/i, '');
        if (d.linked_id && mine && d.linked_id !== mine) { cls = cls || 'warn'; note += (note ? ' · ' : '') + `configured as ${d.linked_id}, re-ID to ${mine}`; }
        else if (d.linked_id && !mine) note += (note ? ' · ' : '') + `configured as ${d.linked_id}, ${p.name.split(' ')[0]} takes this ID`;
        else if (!d.linked_id && mine) note += (note ? ' · ' : '') + `set pack to ID ${mine}`;
      }
      if (d.assigned_person_name && (!p || d.assigned_person_id !== p.id)) note += (note ? ' · ' : '') + `reserved for ${d.assigned_person_name}`;
      return `<li class="${cls}${d.id === justAdded ? ' new' : ''}"><span class="idx">${i + 1}</span>
        <span class="txt"><div class="label">${esc(d.label || d.si)}${d.for && state.collector ? ` <span class="for${p && d.for.id === p.id ? ' active' : ''}">for ${esc(d.for.name.split(' ')[0])}</span>` : ''}</div><div class="sub">${esc(d.type_name)} · <span class="si">${esc(d.si)}</span>${note ? ` · <span class="${cls === 'conflict' ? 'red' : 'amber'}">${esc(note)}</span>` : ''}</div></span>
        <span class="badge ${d.status}">${esc(d.status)}</span>
        <span class="remove" data-rm="${d.id}" title="Remove">&#10005;</span></li>`;
    }).join('');
    ul.querySelectorAll('[data-rm]').forEach(el => el.onclick = () => removeFromBasket(+el.dataset.rm));
    justAdded = null;
    $('#basketEmpty').classList.toggle('hidden', state.basket.length > 0);
    $('#basketCount').textContent = state.basket.length;
    const n = state.basket.length;
    $('#btnSignIn').disabled = !n;
    const forIds = [...new Set(state.basket.filter(x => x.for).map(x => x.for.id))];
    if (state.collector) {
      $('#btnSignOut').disabled = !n || !forIds.length;
      $('#btnSignOut').innerHTML = forIds.length > 1 ? `Sign out for ${forIds.length} people <small>collected by ${esc(state.collector.name.split(' ')[0])}</small>` : `Sign out${p ? ` to ${esc(p.name.split(' ')[0])}` : ''} <small>collected by ${esc(state.collector.name.split(' ')[0])}</small>`;
      $('#actionHint').textContent = !n ? (state.recipients.length ? `Scan extra devices for ${p ? p.name : 'the selected person'}, or add more people.` : `Add the people ${state.collector.name.split(' ')[0]} is collecting for.`) : `${n} device${n > 1 ? 's' : ''} for ${forIds.length} ${forIds.length === 1 ? 'person' : 'people'} ready.`;
    } else {
      $('#btnSignOut').disabled = !n || !p;
      $('#btnSignOut').innerHTML = p ? `Sign out to ${esc(p.name.split(' ')[0])}` : 'Sign out';
      $('#actionHint').textContent = !n ? (p ? `Scan the devices for ${p.name}.` : 'Scan devices to sign in. Pick a person first to sign out.')
        : (p ? `${n} device${n > 1 ? 's' : ''} ready.` : `${n} device${n > 1 ? 's' : ''} ready to sign in. Pick a person to sign out instead.`);
    }
    renderCollectStrip();
  }

  function openQuickCreate(si) {
    modal('New device', `
      <p class="help"><span class="mono">${esc(si)}</span> is not in the database yet.</p>
      <div class="field"><label>SI number</label><input id="qc_si" value="${esc(si)}" class="mono"></div>
      <div class="grid cols-2">
        <div class="field"><label>Type</label><select id="qc_type">${TYPES.map(t => `<option value="${t.id}">${esc(t.name)}</option>`).join('')}</select></div>
        <div class="field"><label>Label</label><input id="qc_label" placeholder="Bolero 61"></div>
      </div>
      <label class="check"><input type="checkbox" id="qc_site"> This device belongs to ${esc(TECH.site_name)} only</label>`,
      [{ label: 'Cancel' }, { label: 'Create and add', cls: 'primary', onClick: async root => {
        try {
          const d = await post('/api/devices/quick', { si: $('#qc_si', root).value, type_id: +$('#qc_type', root).value, label: $('#qc_label', root).value, site_specific: $('#qc_site', root).checked });
          closeModal(); toast('ok', 'Device created', `${d.device.label || d.device.si} · ${d.device.type_name}`); addToBasket(d.device); refreshSummary();
        } catch (e) { toast('err', 'Could not create', e.message); }
      } }]);
    setTimeout(() => { const l = $('#qc_label'); if (l) l.focus(); }, 50);
  }

  // ---- sign out / in -----------------------------------------------------
  const OK_OUTCOMES = ['issued', 'reassigned', 'reissued', 'returned', 'restored'];
  const NICE = { issued: ['ok', 'Signed out'], reassigned: ['warn', 'Reassigned'], reissued: ['warn', 'Restored and signed out'], already_held: ['warn', 'Already with this person'], returned: ['ok', 'Signed in'], already_in: ['warn', 'Already in stock, scan logged'], restored: ['ok', 'Restored to stock'], conflict: ['err', 'Not processed'], missing: ['err', 'Unknown device'] };

  function announce(results) {
    const linked = results.filter(r => r.outcome === 'issued' && r.linked_id).map(r => `${r.label || r.si} is Bolero ${r.linked_id}`);
    if (linked.length) toast('ok', 'Bolero ID linked', linked.join(', '), 5000);
    const grouped = {};
    results.forEach(r => (grouped[r.outcome] = grouped[r.outcome] || []).push(r.label || r.si));
    Object.entries(grouped).forEach(([o, items]) => { const [k, t] = NICE[o] || ['warn', o]; toast(k, `${t} · ${items.length}`, items.join(', '), 5000); });
  }
  function setResult(kind, results, personName, groups) {
    const ok = results.filter(r => OK_OUTCOMES.includes(r.outcome));
    const brief = r => ({ si: r.si, label: r.label, type: r.type, linked_id: r.linked_id || '' });
    state.result = ok.length ? { kind, person: personName || null, personInfo: state.person && !(personName || '').includes(',') ? { bolero_id: state.person.bolero_id, faceplate: state.person.faceplate || '' } : null, items: ok.map(brief), ts: Date.now() } : null;
    // Collected for others: the summary is addressed to the collector, with each person's devices under their name.
    if (state.result && groups) {
      state.result.collector = state.collector ? state.collector.name : '';
      state.result.groups = groups.map(g => ({ name: g.name, bolero_id: g.bolero_id, faceplate: g.faceplate, items: g.results.filter(r => OK_OUTCOMES.includes(r.outcome)).map(brief) })).filter(g => g.items.length);
    }
    if (state.result) setTimeout(() => { if (state.result && Date.now() - state.result.ts >= 11500) { state.result = null; pushState(); } }, 12000);
  }
  async function refreshPerson() {
    if (!state.person) return;
    try { const d = await api(`/api/people/${state.person.id}`); state.holdings = d.holdings; state.assignments = d.assignments; } catch (e) { /* ignore */ }
  }

  $('#btnSignIn').onclick = async () => {
    if (!state.basket.length) return;
    try {
      const d = await post('/api/signin', { device_ids: state.basket.map(x => x.id) });
      announce(d.results);
      setResult('in', d.results, state.person ? state.person.name : null);
      state.basket = [];
      if (state.person) clearPerson(); else { renderBasket(); pushState(); }
      refreshSummary(); refreshFeed(); focusScan();
    } catch (e) { toast('err', 'Sign in failed', e.message); }
  };

  $('#btnSignOut').onclick = () => doSignOut([]);
  async function doSignOut(forceIds) {
    if (!state.basket.length) return;
    // One group per person: the selected person normally, each recipient when someone is collecting.
    const groups = {};
    if (state.collector) state.basket.filter(x => x.for).forEach(x => (groups[x.for.id] = groups[x.for.id] || { person: x.for, ids: [] }).ids.push(x.id));
    else if (state.person) groups[state.person.id] = { person: { id: state.person.id, name: state.person.name }, ids: state.basket.map(x => x.id) };
    const list = Object.values(groups);
    if (!list.length) return;
    try {
      const results = [];
      for (const g of list) {
        const d = await post('/api/signout', { person_id: g.person.id, device_ids: g.ids, force_ids: forceIds, collector_id: state.collector ? state.collector.id : null });
        d.results.forEach(r => { r.for = g.person; results.push(r); });
      }
      const conflicts = results.filter(r => r.outcome === 'conflict');
      announce(results.filter(r => r.outcome !== 'conflict'));
      const names = list.map(g => g.person.name);
      const groups = state.collector ? list.map(g => { const r = state.recipients.find(x => x.id === g.person.id) || {}; return { name: g.person.name, bolero_id: r.bolero_id || '', faceplate: r.faceplate || '', results: results.filter(x => x.for.id === g.person.id) }; }) : null;
      setResult('out', results, names.length > 1 ? names.map(n => n.split(' ')[0]).join(', ') : names[0], groups);
      refreshSummary(); refreshFeed();
      if (state.collector) list.forEach(g => {
        const n = results.filter(r => r.for.id === g.person.id && ['issued', 'reassigned', 'reissued'].includes(r.outcome)).length;
        const hit = state.served.find(x => x.id === g.person.id);
        if (hit) hit.count += n; else state.served.push({ id: g.person.id, name: g.person.name, count: n });
      });
      if (conflicts.length) {
        state.basket = state.basket.filter(x => conflicts.some(c => c.id === x.id));
        if (state.collector) state.recipients = state.recipients.filter(r => state.basket.some(x => x.for && x.for.id === r.id));
        await refreshPerson(); renderPerson(); renderBasket(); pushState();
        openConflicts(conflicts, list.length === 1 ? list[0].person : null);
        return;
      }
      state.basket = [];
      if (state.collector) {
        state.recipients = [];
        clearPerson();
        toast('ok', `Done for ${names.map(n => n.split(' ')[0]).join(', ')}`, `Add more people ${state.collector.name.split(' ')[0]} is collecting for, or press Exit.`, 4000);
        searchEl.focus();
      } else { clearPerson(); focusScan(); }
    } catch (e) { toast('err', 'Sign out failed', e.message); }
  }
  function openConflicts(conflicts, person) {
    modal('Devices not available', `
      <p class="help">Tick the ones to give ${person ? `to <b>${esc(person.name)}</b>` : 'out'} anyway. The rest stay in the list.</p>
      <ul class="conflict-list">${conflicts.map(c => `<li>
        <span><div class="t">${esc(c.label || c.si)} <span class="si">${esc(c.si)}</span>${!person && c.for ? ` <span class="muted">for ${esc(c.for.name)}</span>` : ''}</div><div class="red sm">${esc(c.message)}</div></span>
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
      <p class="help">${esc(state.person.name)} returns <b>${esc(old.label || old.si)}</b> and gets the device you scan now.</p>
      <div class="field"><label>Scan replacement SI</label><input id="sw_si" class="mono" placeholder="Scan the replacement" autocomplete="off"></div>
      <div class="grid cols-2">
        <div class="field"><label>Reason</label><select id="sw_reason"><option>Damaged</option><option>Faulty</option><option>Battery issue</option><option>Wrong device</option><option>Lost</option><option>Other</option></select></div>
        <div class="field"><label>Returned device becomes</label><select id="sw_status"><option value="damaged">Damaged</option><option value="in">Back in stock</option><option value="lost">Lost</option></select></div>
      </div>
      <div class="field"><label>Note</label><input id="sw_note" placeholder="Optional"></div>`,
      [{ label: 'Cancel' }, { label: 'Swap', cls: 'primary', onClick: root => submitSwap(root, old) }]);
    $('#sw_si').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); submitSwap($('#modalRoot'), old); } });
    $('#sw_reason').onchange = e => { if (e.target.value === 'Lost') $('#sw_status').value = 'lost'; if (e.target.value === 'Wrong device') $('#sw_status').value = 'in'; };
  }
  async function submitSwap(root, old) {
    const newSi = $('#sw_si', root).value.trim();
    if (!newSi) { toast('warn', 'Scan the replacement first'); return; }
    try {
      const d = await post('/api/swap', { old_device_id: old.id, new_si: newSi, reason: $('#sw_reason', root).value, note: $('#sw_note', root).value, old_status: $('#sw_status', root).value });
      closeModal(); toast('ok', 'Swapped', `${d.new.label || d.new.si} replaces ${d.old.label || d.old.si}`);
      state.result = { kind: 'out', person: state.person.name, personInfo: { bolero_id: state.person.bolero_id, faceplate: state.person.faceplate || '' }, items: [{ si: d.new.si, label: d.new.label, type: d.new.type, linked_id: d.new.linked_id || '' }], ts: Date.now() };
      await selectPerson(state.person.id); refreshSummary(); refreshFeed();
    } catch (e) { toast('err', 'Swap failed', e.message); }
  }
  function openFlag(d) {
    if (!d) return;
    modal(`Flag ${d.label || d.si}`, `
      <p class="help">The device comes off <b>${esc(state.person.name)}</b>'s list and the change is logged against them.</p>
      <div class="grid cols-2">
        <div class="field"><label>Status</label><select id="fl_status"><option value="damaged">Damaged</option><option value="lost">Lost</option></select></div>
        <div class="field"><label>Reason</label><input id="fl_reason" placeholder="Cable cut, dropped"></div>
      </div>
      <div class="field"><label>Note</label><input id="fl_note" placeholder="Optional"></div>`,
      [{ label: 'Cancel' }, { label: 'Save', cls: 'danger', onClick: async root => {
        try {
          const status = $('#fl_status', root).value;
          await post(`/api/devices/${d.id}/status`, { status, reason: $('#fl_reason', root).value, note: $('#fl_note', root).value });
          closeModal(); toast('warn', `Marked ${status}`, d.label || d.si);
          await selectPerson(state.person.id); refreshSummary(); refreshFeed();
        } catch (e) { toast('err', 'Could not save', e.message); }
      } }]);
  }

  // ---- keyboard: a barcode scanner is just a fast keyboard -------------
  document.addEventListener('keydown', e => {
    if (e.key === 'F2') { e.preventDefault(); searchEl.focus(); searchEl.select(); return; }
    if (e.key === 'F4') { e.preventDefault(); focusScan(); return; }
    if (e.key === 'F3') { e.preventDefault(); if (state.lastPerson) selectPerson(state.lastPerson.id); return; }
    if (e.key === 'Escape') { if (isModalOpen()) closeModal(); else if (state.collector && !drawer.classList.contains('open')) exitCollecting(); return; }
    if (isModalOpen()) return;
    const t = e.target;
    const typing = t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT');
    if (!typing && e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) scanEl.focus();
  });

  // ---- Bolero link: where is this person's pack, and can we flash it --------------
  let packLiveTimer = null;
  async function refreshPackLive() {
    clearTimeout(packLiveTimer);
    const el = $('#packLive');
    if (!el) return;
    const n = el.dataset.number;
    if (!n) {
      try { const i = await api('/api/bolero/pack/0'); el.innerHTML = i.configured ? '<span class="dim">No Bolero ID on this person. Nothing to flash.</span>' : ''; if (!i.configured) el.remove(); } catch (e) { el.remove(); }
      return;
    }
    try {
      const i = await api(`/api/bolero/pack/${encodeURIComponent(n)}`);
      if (!i.configured) { el.remove(); return; }
      let txt;
      if (!i.ok) txt = `<span class="dim">Bolero link: ${esc(i.error)}</span>`;
      else if (!i.known) txt = `<span class="amber">No Bolero ${esc(n)} on the system</span>`;
      else if (i.charger) txt = `Bolero ${esc(n)} is in <b>${esc(i.charger.name)}, bay ${esc(String(i.charger.bay))}</b>${i.battery != null ? ` · ${i.battery}%` : ''}${i.can_identify ? ` <button class="btn sm" id="btnFlash">Flash it</button>` : ''}`;
      else if (i.online) txt = `Bolero ${esc(n)} is <b class="green">on</b>${i.battery != null ? ` · ${i.battery}%` : ''}${i.antenna ? ` · ${esc(String(i.antenna))}` : ''} <span class="dim">· not in a charger</span>`;
      else if (i.online === false) txt = `Bolero ${esc(n)} is <b>off</b>${i.charging ? ' (charging)' : ''}${!i.charger_data ? ' · not seen in a networked charger' : ' · not in a charger'}`;
      else txt = `<span class="dim">Bolero ${esc(n)}: no status yet</span>`;
      el.innerHTML = txt;
      // First look at a newly selected person: flash their pack straight away so the tech can find it.
      if (i.ok && i.known && i.charger && i.can_identify && el.dataset.flashed !== '1') {
        el.dataset.flashed = '1';
        try { const r = await post(`/api/bolero/pack/${encodeURIComponent(n)}/identify`, {}); toast('ok', `Flashing Bolero ${n}`, `in ${i.charger.name}, bay ${i.charger.bay} for ${r.seconds} seconds`); } catch (e) { toast('err', 'Could not flash', e.message); }
      }
      const fb = $('#btnFlash'); if (fb) fb.onclick = async () => { fb.disabled = true; try { const r = await post(`/api/bolero/pack/${encodeURIComponent(n)}/identify`, {}); toast('ok', `Flashing Bolero ${n}`, `in the charger for ${r.seconds} seconds`); } catch (e) { toast('err', 'Could not flash', e.message); } setTimeout(() => { fb.disabled = false; }, 3000); };
    } catch (e) { /* link away */ }
    packLiveTimer = setTimeout(refreshPackLive, 4000);
  }

  // ---- menu drawer -----------------------------------------------------------
  const drawer = $('#drawer'), drawerBack = $('#drawerBack');
  if (drawer) {
    const open = () => { drawer.classList.add('open'); drawerBack.classList.add('open'); };
    const close = () => { drawer.classList.remove('open'); drawerBack.classList.remove('open'); focusScan(); };
    $('#btnMenu').onclick = open;
    drawerBack.onclick = close;
    $('#btnDrawerClose').onclick = close;
    document.addEventListener('keydown', e => { if (e.key === 'Escape' && drawer.classList.contains('open')) close(); });
  }


  // ---- away from desk -------------------------------------------------------------
  const PRESETS = [['Be right back', {}], ['5 min', { minutes: 5 }], ['10 min', { minutes: 10 }], ['15 min', { minutes: 15 }], ['30 min', { minutes: 30 }], ['1 hour', { minutes: 60 }]];
  function openAway() {
    modal('Away from desk', `
      <p class="help">Locks this desk behind the admin password. The mirror keeps the PGM feed and shows when you are back.</p>
      <div class="preset-grid">${PRESETS.map(([l], i) => `<button class="btn" data-preset="${i}">${esc(l)}</button>`).join('')}</div>
      <div class="grid cols-2 mt-s"><div class="field"><label>Or back at</label><input type="time" id="aw_until"></div><div class="field"><label>Note for the mirror (optional)</label><input id="aw_note" placeholder="Gone to stage, call 1234"></div></div>`,
      [{ label: 'Cancel' }, { label: 'Lock until the time above', cls: 'primary', onClick: async root => {
        const until = $('#aw_until', root).value;
        if (!until) { toast('warn', 'Pick a time', 'Or use one of the buttons above.'); return; }
        await goAway({ until, note: $('#aw_note', root).value });
      } }]);
    document.querySelectorAll('#modalRoot [data-preset]').forEach(b => b.onclick = () => goAway(Object.assign({ note: ($('#aw_note') || {}).value || '' }, PRESETS[+b.dataset.preset][1])));
  }
  async function goAway(opts) {
    try { const a = await RDRLock.start(opts); closeModal(); drawer.classList.remove('open'); drawerBack.classList.remove('open'); toast('ok', 'Desk locked', a.label, 2500); pushState(); }
    catch (e) { toast('err', 'Could not lock', e.message); }
  }
  RDRLock.onUnlock = () => { toast('ok', 'Desk reopened', '', 2000); focusScan(); pushState(); };
  const menuAway = $('#menuAway'); if (menuAway) menuAway.onclick = e => { e.preventDefault(); openAway(); };

  // ---- live configuration: admin edits show up here without a reload ------------
  let configVersion = null;
  const setLive = (id, text) => { const el = document.getElementById(id); if (!el) return; if (el.textContent !== text) { el.textContent = text; el.classList.remove('live-swap'); void el.offsetWidth; el.classList.add('live-swap'); } };
  async function pollConfig() {
    try {
      const c = await api('/api/config');
      RDRLock.apply(c.away || null);
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
      if (typeof refreshSummary === 'function') refreshSummary();
    } catch (e) { /* server away; try again */ }
  }
  setInterval(pollConfig, 4000); pollConfig();

  // ---- boot ----------------------------------------------------------------
  renderBasket(); refreshSummary(); refreshFeed(); pushState();
  setInterval(refreshFeed, 10000); setInterval(refreshSummary, 15000);
  focusScan();
})();
