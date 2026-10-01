/* =================================================================
   VT LIST  (frontend/src/ui/vtList.js)
   The SQL - VT space's middle column: the virtual tables, results kept in
   vt.db after their connection closed.  Clicking one opens it in a query
   tab; the pencil renames it and ✕ drops it.
   ================================================================= */

import { icon } from '../icons.js';

function when(stamp) {
  if (!stamp) return '';
  const date = new Date(stamp);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

export function createVtList({ onOpen, onNewQuery, onPush, onRename, onDelete }) {
  const section = document.createElement('section');
  section.className = 'cmdlist vtlist glass';
  section.setAttribute('aria-label', 'Virtual tables');

  section.innerHTML = `
    <div class="nl-head">
      <div>
        <div class="nl-title">Virtual tables</div>
        <div class="nl-count" id="vt-count">0 tables</div>
      </div>
      <div class="nl-actions">
        <button class="btn icon small" id="vt-new" type="button" aria-label="New query on the virtual tables" title="New query on the virtual tables (Ctrl+Shift+T)">
          ${icon('plus', 15)}
        </button>
      </div>
    </div>
    <label class="search">
      ${icon('search', 15)}
      <input id="vt-filter" placeholder="Filter tables and columns" autocomplete="off" spellcheck="false" aria-label="Filter virtual tables">
    </label>
    <div class="cmd-scroll">
      <div class="cmd-rows" id="vt-rows" role="list" aria-label="Virtual tables"></div>
    </div>
    <p class="sql-driver" id="vt-where">Kept in vt.db on this PC, outside your notes folder.</p>
  `;

  const filter = section.querySelector('#vt-filter');
  let last = { enabled: false, tables: [] };

  filter.addEventListener('input', () => render(last));
  filter.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && filter.value) {
      e.stopPropagation();
      filter.value = '';
      render(last);
    }
  });

  section.querySelector('#vt-new').onclick = () => onNewQuery?.();

  section.addEventListener('click', (e) => {
    const row = e.target.closest('.vt-row');
    if (!row) return;
    const name = row.dataset.name;
    if (e.target.closest('.vt-push')) onPush?.(name);
    else if (e.target.closest('.vt-rename')) onRename?.(name);
    else if (e.target.closest('.vt-x')) onDelete?.(name);
    else if (e.target.closest('.vt-open')) onOpen?.(name);
  });

  function iconButton(className, name, label) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `btn icon small ${className}`;
    button.setAttribute('aria-label', label);
    button.title = label;
    button.innerHTML = icon(name, 14); // a fixed icon, never data
    return button;
  }

  function row(table) {
    const el = document.createElement('div');
    el.className = 'cmd-row vt-row';
    el.setAttribute('role', 'listitem');
    el.dataset.name = table.name;

    const open = document.createElement('button');
    open.type = 'button';
    open.className = 'sql-open vt-open';
    open.innerHTML = `<span class="ic">${icon('table', 15)}</span><span class="txt"><span class="nm"></span><span class="sub"></span></span>`;
    // rule 12a: names, columns and SQL are text, never HTML
    open.querySelector('.nm').textContent = table.name;
    const size = `${table.rows.toLocaleString()} row${table.rows === 1 ? '' : 's'} · ${table.columns.length} col${table.columns.length === 1 ? '' : 's'}`;
    open.querySelector('.sub').textContent = table.source ? `${size} · from ${table.source.name}` : size;
    open.title = [
      `Open ${table.name}`,
      table.source ? `From ${table.source.name}${table.source.where ? ` (${table.source.where})` : ''}${table.created ? `, ${when(table.created)}` : ''}` : 'Made in SQL - VT',
      table.query ? table.query : '',
      `Columns: ${table.columns.join(', ')}`
    ].filter(Boolean).join('\n');

    el.append(
      open,
      iconButton('vt-push', 'upload', `Use ${table.name} in the open SQL tab (as #${table.name})`),
      iconButton('vt-rename', 'edit', `Rename ${table.name}`),
      iconButton('vt-x', 'x', `Delete ${table.name}`)
    );
    return el;
  }

  function empty(message) {
    const el = document.createElement('div');
    el.className = 'cmd-empty';
    el.textContent = message;
    return el;
  }

  function render(state) {
    last = { tables: [], ...state };
    const q = filter.value.trim().toLowerCase();
    const has = (t) => !q || `${t.name} ${t.columns.join(' ')} ${t.source?.name || ''}`.toLowerCase().includes(q);
    const shown = last.tables.filter(has);
    section.querySelector('#vt-rows').replaceChildren(
      ...(shown.length
        ? shown.map(row)
        : [empty(q ? 'No table matches.'
          : 'No virtual tables yet. Run a query in SQL, then press the table button above its result to keep it here.')])
    );
    const n = last.tables.length;
    section.querySelector('#vt-count').textContent = `${n} table${n === 1 ? '' : 's'}`;
    section.classList.toggle('is-off', !last.enabled);
    filter.disabled = !last.enabled;
    section.querySelector('#vt-new').disabled = !last.enabled;
  }

  return {
    element: section,
    render,
    focusFilter() {
      filter.focus();
      filter.select();
    }
  };
}
