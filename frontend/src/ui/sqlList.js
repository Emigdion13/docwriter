/* =================================================================
   SQL LIST  (frontend/src/ui/sqlList.js)
   The SQL space's middle column: the saved connections.  Clicking one
   opens a query tab on it; the pencil edits it.
   ================================================================= */

import { icon } from '../icons.js';

export function createSqlList({ onOpen, onEdit, onNewServer, onNewSqlite }) {
  const section = document.createElement('section');
  section.className = 'cmdlist sqllist glass';
  section.setAttribute('aria-label', 'Connections');

  section.innerHTML = `
    <div class="nl-head">
      <div>
        <div class="nl-title">Connections</div>
        <div class="nl-count" id="sql-count">0 connections</div>
      </div>
      <div class="nl-actions">
        <button class="btn icon small" id="sql-add-server" type="button" aria-label="New SQL Server connection" title="New SQL Server connection">
          ${icon('plus', 15)}
        </button>
        <button class="btn icon small" id="sql-add-sqlite" type="button" aria-label="Add a SQLite file" title="Add a SQLite file">
          ${icon('file', 15)}
        </button>
      </div>
    </div>
    <label class="search">
      ${icon('search', 15)}
      <input id="sql-filter" placeholder="Filter connections" autocomplete="off" spellcheck="false" aria-label="Filter connections">
    </label>
    <div class="cmd-scroll">
      <div class="cmd-rows" id="sql-conns" role="list" aria-label="Connections"></div>
    </div>
    <p class="sql-driver" id="sql-driver"></p>
  `;

  const filter = section.querySelector('#sql-filter');
  let last = { enabled: false, connections: [], driver: null };

  filter.addEventListener('input', () => render(last));
  filter.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && filter.value) {
      e.stopPropagation();
      filter.value = '';
      render(last);
    }
  });

  section.querySelector('#sql-add-server').onclick = () => onNewServer?.();
  section.querySelector('#sql-add-sqlite').onclick = () => onNewSqlite?.();

  section.addEventListener('click', (e) => {
    const row = e.target.closest('.sql-row');
    if (!row) return;
    const id = row.dataset.id;
    if (e.target.closest('.sql-edit')) onEdit?.(id);
    else if (e.target.closest('.sql-open')) onOpen?.(id);
  });

  function row(conn) {
    const el = document.createElement('div');
    el.className = 'cmd-row sql-row';
    el.setAttribute('role', 'listitem');
    el.dataset.id = conn.id;

    const open = document.createElement('button');
    open.type = 'button';
    open.className = 'sql-open';
    open.title = `Open a query tab on ${conn.name}`;
    open.innerHTML = `<span class="ic">${icon(conn.engine === 'sqlite' ? 'file' : 'database', 15)}</span><span class="txt"><span class="nm"></span><span class="sub"></span></span>`;
    // rule 12a: names and servers are text, never HTML
    open.querySelector('.nm').textContent = conn.name;
    open.querySelector('.sub').textContent = `${conn.engineName} · ${conn.where}`;

    const edit = document.createElement('button');
    edit.type = 'button';
    edit.className = 'btn icon small sql-edit';
    edit.setAttribute('aria-label', `Edit ${conn.name}`);
    edit.title = 'Edit connection';
    edit.innerHTML = icon('edit', 14);

    el.append(open, edit);
    return el;
  }

  function empty(message) {
    const el = document.createElement('div');
    el.className = 'cmd-empty';
    el.textContent = message;
    return el;
  }

  function render(state) {
    last = state;
    const q = filter.value.trim().toLowerCase();
    const matches = (c) => !q || `${c.name} ${c.where}`.toLowerCase().includes(q);
    const shown = state.connections.filter(matches);
    section.querySelector('#sql-conns').replaceChildren(
      ...(shown.length
        ? shown.map(row)
        : [empty(q ? 'No connection matches.' : 'Add a SQL Server connection with +, or a SQLite file.')])
    );
    const n = state.connections.length;
    section.querySelector('#sql-count').textContent = `${n} connection${n === 1 ? '' : 's'}`;
    section.querySelector('#sql-driver').textContent = !state.enabled ? ''
      : state.driver ? `SQL Server driver: ${state.driver}`
        : 'No SQL Server ODBC driver found. Install ODBC Driver 18 for SQL Server to connect to SQL Server.';
    section.classList.toggle('is-off', !state.enabled);
    filter.disabled = !state.enabled;
    section.querySelector('#sql-add-server').disabled = !state.enabled;
    section.querySelector('#sql-add-sqlite').disabled = !state.enabled;
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
