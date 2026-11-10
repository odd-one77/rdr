/* Small helpers for the admin pages: confirms, toggles, person pickers. */
(function () {
  'use strict';
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  document.addEventListener('click', e => {
    const c = e.target.closest('[data-confirm]');
    if (c && !confirm(c.dataset.confirm)) { e.preventDefault(); e.stopPropagation(); return; }
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
          if (on && !td.querySelector('input')) {
            const text = td.textContent.trim(); td.dataset.orig = text;
            td.innerHTML = `<input value="${text.replace(/"/g, '&quot;')}">`;
            const inp = td.querySelector('input');
            const commit = async () => { if (inp.value.trim() === td.dataset.orig) return; const v = await save(table.dataset.endpoint, td.closest('tr').dataset.id, td.dataset.field, inp.value, td); if (v !== null) { td.dataset.orig = v; inp.value = v; } else inp.value = td.dataset.orig; };
            inp.addEventListener('blur', commit);
            inp.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); inp.blur(); } if (e.key === 'Escape') { inp.value = td.dataset.orig; inp.blur(); } });
          } else if (!on && td.querySelector('input')) {
            const v = td.querySelector('input').value;
            td.textContent = v;
            if (td.dataset.field === 'name') td.innerHTML = `<a href="/admin/people/${td.closest('tr').dataset.id}"><b>${v.replace(/</g, '&lt;')}</b></a>`;
          }
        });
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
