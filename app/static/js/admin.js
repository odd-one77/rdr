/* Small helpers for the admin pages: confirms, toggles, person pickers. */
(function () {
  'use strict';
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  // On a narrow screen the menu is a scrolling strip: keep the current page in view.
  const here = document.querySelector('.sidenav a.on');
  if (here) { const nav = here.parentElement; if (nav.scrollWidth > nav.clientWidth) nav.scrollLeft += here.getBoundingClientRect().left - nav.getBoundingClientRect().left - 12; }
  document.addEventListener('click', e => {
    const c = e.target.closest('[data-confirm]');
    if (c && !confirm(c.dataset.confirm)) { e.preventDefault(); e.stopPropagation(); return; }
    if (e.target.closest('[data-print]')) { window.print(); return; }
    const t = e.target.closest('[data-toggle]');
    if (t) { e.preventDefault(); const el = document.querySelector(t.dataset.toggle); if (el) el.classList.toggle('hidden'); }
  });
  // Filter forms apply themselves: selects/dates/checkboxes on change, text after a short pause.
  document.querySelectorAll('form.filters').forEach(form => {
    let t = null;
    const go = () => { form.requestSubmit ? form.requestSubmit() : form.submit(); };
    form.addEventListener('change', e => { if (e.target.matches('select, input[type=checkbox], input[type=date]')) go(); });
    form.addEventListener('input', e => { if (e.target.matches('input[type=search], input[type=text]')) { clearTimeout(t); t = setTimeout(go, 350); } });
    form.addEventListener('submit', () => { const f = form.querySelector('input[type=search], input[type=text]'); if (f) sessionStorage.setItem('rdr-filter-focus', f.name); });
    const focusName = sessionStorage.getItem('rdr-filter-focus');
    if (focusName) { const f = form.querySelector(`[name="${focusName}"]`); if (f) { f.focus(); const v = f.value; f.value = ''; f.value = v; } sessionStorage.removeItem('rdr-filter-focus'); }
  });
  // Direct edit: a toggle at the top turns table cells into inputs that save when you leave them.
  const toggle = document.querySelector('[data-direct-edit]');
  if (toggle) {
    const key = 'rdr-direct-edit-' + toggle.dataset.directEdit;
    const table = document.querySelector('table.editable, table.autosave');
    let dirty = false;
    const flash = (el, ok) => { el.classList.remove('saved', 'failed'); void el.offsetWidth; el.classList.add(ok ? 'saved' : 'failed'); };
    async function save(endpoint, id, field, value, el) {
      try {
        const r = await fetch(endpoint.replace('{id}', id), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ field, value }) });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(d.detail || 'Save failed');
        flash(el, true); return d.value;
      } catch (e) { flash(el, false); alert(e.message); return null; }
    }
    function apply(on) {
      document.body.classList.toggle('direct-edit', on);
      if (!table) return;
      if (table.classList.contains('editable')) {
        table.querySelectorAll('td[data-field]').forEach(td => {
          if (on && !td.querySelector('input, select')) {
            // data-value holds the raw value when the cell shows something else (a name for an id, a chip)
            const opts = document.getElementById('opt-' + td.dataset.field);
            td._html = td.innerHTML;
            td.dataset.orig = td.dataset.value !== undefined ? td.dataset.value : td.textContent.trim();
            const inp = document.createElement(opts ? 'select' : 'input');
            if (opts) inp.innerHTML = opts.innerHTML;
            inp.value = td.dataset.orig;
            td.replaceChildren(inp);
            const commit = async () => {
              if (inp.value.trim() === td.dataset.orig) return;
              const v = await save(table.dataset.endpoint, td.closest('tr').dataset.id, td.dataset.field, inp.value, td);
              if (v !== null) { td.dataset.orig = v; inp.value = v; dirty = true; } else inp.value = td.dataset.orig;
            };
            if (opts) inp.addEventListener('change', commit);
            else {
              inp.addEventListener('blur', commit);
              inp.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); inp.blur(); } if (e.key === 'Escape') { inp.value = td.dataset.orig; inp.blur(); } });
            }
          } else if (!on && td.querySelector('input, select')) td.innerHTML = td._html;
        });
        // the plain table is drawn by the server: after an edit, fetch it again instead of rebuilding it here
        if (!on && dirty) location.reload();
      } else if (table.classList.contains('autosave')) {
        table.querySelectorAll('tr[data-id] input[name], tr[data-id] select[name]').forEach(inp => {
          if (inp.dataset.bound) return; inp.dataset.bound = '1';
          const handler = async () => {
            if (!document.body.classList.contains('direct-edit')) return;
            const value = inp.type === 'checkbox' ? (inp.checked ? '1' : '0') : inp.value;
            await save(table.dataset.endpoint, inp.closest('tr').dataset.id, inp.name, value, inp.closest('td'));
          };
          inp.addEventListener('change', handler);
        });
      }
    }
    toggle.checked = localStorage.getItem(key) === '1';
    apply(toggle.checked);
    toggle.addEventListener('change', () => { localStorage.setItem(key, toggle.checked ? '1' : '0'); apply(toggle.checked); });
  }
  // Stock transfer: every scan moves one device into the chosen site, in the order scanned.
  const tfScan = document.getElementById('tfScan');
  if (tfScan) {
    const site = document.getElementById('tfSite'), list = document.getElementById('tfList'), count = document.getElementById('tfCount');
    const none = list.querySelector('.none');
    let moved = 0, queue = Promise.resolve();
    const setCount = n => { moved = n; count.textContent = n; };
    const post = async (si, to) => {
      const r = await fetch('/admin/transfer/scan', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ si, site_id: to }) });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.detail || 'Transfer failed');
      return d;
    };
    function add(d) {
      const tr = document.createElement('tr'); tr.className = 'row-new';
      if (d.outcome === 'moved') tr.innerHTML = `<td><a href="/admin/devices/${d.id}"><b>${esc(d.label || d.si)}</b></a>${d.holder_name ? ` <span class="amber">out with ${esc(d.holder_name)}</span>` : ''}</td><td class="mono">${esc(d.si)}</td><td>${esc(d.type)}</td><td class="muted">${esc(d.from_site)}</td><td>${esc(d.to_site)}</td><td><span class="badge in">Moved</span></td><td class="right"><button class="btn sm ghost">Undo</button></td>`;
      else if (d.outcome === 'already') tr.innerHTML = `<td><a href="/admin/devices/${d.id}"><b>${esc(d.label || d.si)}</b></a></td><td class="mono">${esc(d.si)}</td><td>${esc(d.type)}</td><td class="muted">${esc(d.from_site)}</td><td>${esc(d.to_site)}</td><td><span class="badge neutral">Already there</span></td><td></td>`;
      else tr.innerHTML = `<td class="muted">${esc(d.message || 'No device with this SI')}</td><td class="mono">${esc(d.si)}</td><td></td><td></td><td></td><td><span class="badge lost">Not moved</span></td><td></td>`;
      const undo = tr.querySelector('button');
      if (undo) undo.onclick = async () => {
        undo.disabled = true;
        try {
          await post(d.si, d.from_site_id == null ? '' : String(d.from_site_id));
          tr.querySelector('.badge').outerHTML = '<span class="badge neutral">Undone</span>'; undo.remove(); setCount(moved - 1);
        } catch (e) { undo.disabled = false; alert(e.message); }
        tfScan.focus();
      };
      none.remove(); list.prepend(tr);
    }
    const scan = si => { queue = queue.then(async () => {
      try { const d = await post(si, site.value); add(d); if (d.outcome === 'moved') setCount(moved + 1); }
      catch (e) { add({ outcome: 'failed', si, message: e.message }); }
    }); };
    const take = text => text.split(/[\s,;]+/).map(s => s.trim()).filter(Boolean).forEach(scan);
    tfScan.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); take(tfScan.value); tfScan.value = ''; } });
    tfScan.addEventListener('paste', e => {
      const text = (e.clipboardData || window.clipboardData).getData('text');
      if (/[\r\n]/.test(text.trim())) { e.preventDefault(); take(text); tfScan.value = ''; }
    });
    site.addEventListener('change', () => tfScan.focus());
    document.getElementById('tfClear').onclick = () => { list.replaceChildren(none); setCount(0); tfScan.focus(); };
  }
  // Person pickers: a search input + hidden id + results dropdown.
  document.querySelectorAll('[data-person-picker]').forEach(input => {
    const box = input.closest('.search-box');
    const hidden = box.querySelector('input[type=hidden]');
    const results = box.querySelector('.results');
    let timer = null;
    input.addEventListener('input', () => {
      hidden.value = '';
      clearTimeout(timer);
      const q = input.value.trim();
      if (!q) { results.classList.add('hidden'); return; }
      timer = setTimeout(async () => {
        const r = await fetch(`/admin/people-search?q=${encodeURIComponent(q)}`);
        const d = await r.json();
        results.innerHTML = d.results.map(p => `<div class="item" data-id="${p.id}" data-name="${esc(p.name)}"><div><div class="name">${esc(p.name)}</div><div class="meta">${esc([p.dept, p.role, p.phone, p.external_id].filter(Boolean).join(' · '))}</div></div></div>`).join('') || '<div class="empty">No match</div>';
        results.classList.remove('hidden');
        results.querySelectorAll('.item').forEach(el => el.onmousedown = () => { hidden.value = el.dataset.id; input.value = el.dataset.name; results.classList.add('hidden'); });
      }, 150);
    });
    input.addEventListener('blur', () => setTimeout(() => results.classList.add('hidden'), 150));
    input.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); const f = results.querySelector('.item'); if (f) f.onmousedown(); } });
  });
})();
