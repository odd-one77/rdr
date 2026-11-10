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
