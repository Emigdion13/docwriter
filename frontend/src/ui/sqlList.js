/* =================================================================
   SQL LIST  (frontend/src/ui/sqlList.js)
   The SQL space's middle column: the saved connections, each with the
   queries saved on it underneath.  Clicking a connection opens a query
   tab on it; clicking a saved query opens it on its connection and runs
   it there.
   ================================================================= */

import { icon } from '../icons.js';

export function createSqlList({ onOpen, onEdit, onNewServer, onNewSqlite, onRunQuery, onOpenQuery, onDeleteQuery }) {
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
      <input id="sql-filter" placeholder="Filter connections and queries" autocomplete="off" spellcheck="false" aria-label="Filter connections and saved queries">
    </label>
    <div class="cmd-scroll">
      <div class="cmd-rows" id="sql-conns" role="tree" aria-label="Connections and saved queries"></div>
    </div>
    <p class="sql-driver" id="sql-driver"></p>
  `;

  const filter = section.querySelector('#sql-filter');
  const folded = new Set(); // connection ids whose queries are hidden
  let last = { enabled: false, connections: [], queries: [], openQueries: [], driver: null };

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
    const query = e.target.closest('.sql-q');
    if (query) {
      const id = query.dataset.id;
      if (e.target.closest('.sql-q-open')) onOpenQuery?.(id);
      else if (e.target.closest('.sql-q-x')) onDeleteQuery?.(id);
      else if (e.target.closest('.sql-q-run')) onRunQuery?.(id);
      return;
    }
    const row = e.target.closest('.sql-row');
    if (!row) return;
    const id = row.dataset.id;
    if (e.target.closest('.sql-fold')) {
      if (folded.has(id)) folded.delete(id);
      else folded.add(id);
      render(last);
    } else if (e.target.closest('.sql-edit')) {
      onEdit?.(id);
    } else if (e.target.closest('.sql-open')) {
      onOpen?.(id);
    }
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

  function connectionRow(conn, count, open) {
    const el = document.createElement('div');
    el.className = 'cmd-row sql-row';
    el.setAttribute('role', 'treeitem');
    el.dataset.id = conn.id;
    if (count) el.setAttribute('aria-expanded', String(open));

    const fold = document.createElement('button');
    fold.type = 'button';
    fold.className = 'sql-fold';
    fold.tabIndex = count ? 0 : -1;
    fold.disabled = !count;
    fold.setAttribute('aria-label', open ? 'Hide saved queries' : 'Show saved queries');
    fold.title = count ? (open ? 'Hide saved queries' : 'Show saved queries') : '';
    fold.innerHTML = count ? icon('right', 11) : '';
    fold.classList.toggle('open', open);

    const opener = document.createElement('button');
    opener.type = 'button';
    opener.className = 'sql-open';
    opener.title = `Open a new query tab on ${conn.name}`;
    opener.innerHTML = `<span class="ic">${icon(conn.engine === 'sqlite' ? 'file' : 'database', 15)}</span><span class="txt"><span class="nm"></span><span class="sub"></span></span>`;
    // rule 12a: names and servers are text, never HTML
    opener.querySelector('.nm').textContent = conn.name;
    opener.querySelector('.sub').textContent = `${conn.engineName} · ${conn.where}`;

    el.append(fold, opener, iconButton('sql-edit', 'edit', `Edit ${conn.name}`));
    return el;
  }

  function queryRow(query, conn, isOpen) {
    const el = document.createElement('div');
    el.className = 'cmd-row sql-q';
    el.classList.toggle('open', isOpen);
    el.setAttribute('role', 'treeitem');
    el.dataset.id = query.id;

    const runner = document.createElement('button');
    runner.type = 'button';
    runner.className = 'sql-q-run';
    runner.title = `Run “${query.name}” on ${conn.name}`;
    runner.innerHTML = `<span class="ic">${icon('play', 11)}</span><span class="txt"><span class="nm"></span><span class="sub"></span></span>`;
    runner.querySelector('.nm').textContent = query.name;
    runner.querySelector('.sub').textContent = query.preview || '';

    el.append(
      runner,
      iconButton('sql-q-open', 'edit', `Open “${query.name}” without running it`),
      iconButton('sql-q-x', 'x', `Delete “${query.name}”`)
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
    last = { queries: [], openQueries: [], ...state };
    const q = filter.value.trim().toLowerCase();
    const has = (text) => !q || String(text || '').toLowerCase().includes(q);
    const open = new Set(last.openQueries);
    const rows = [];

    for (const conn of last.connections) {
      const own = last.queries.filter(query => query.connection === conn.id);
      const connMatches = has(`${conn.name} ${conn.where}`);
      // Filtering by a query's name shows it under its connection.
      const shown = connMatches ? own : own.filter(query => has(`${query.name} ${query.preview}`));
      if (!connMatches && !shown.length) continue;
      const unfolded = Boolean(q) || !folded.has(conn.id);
      rows.push(connectionRow(conn, own.length, unfolded));
      if (unfolded) rows.push(...shown.map(query => queryRow(query, conn, open.has(query.id))));
    }

    section.querySelector('#sql-conns').replaceChildren(
      ...(rows.length
        ? rows
        : [empty(q ? 'Nothing matches.' : 'Add a SQL Server connection with +, or a SQLite file.')])
    );
    const n = last.connections.length;
    const k = last.queries.length;
    section.querySelector('#sql-count').textContent =
      `${n} connection${n === 1 ? '' : 's'}${k ? ` · ${k} saved quer${k === 1 ? 'y' : 'ies'}` : ''}`;
    section.querySelector('#sql-driver').textContent = !last.enabled ? ''
      : last.driver ? `SQL Server driver: ${last.driver}`
        : 'No SQL Server ODBC driver found. Install ODBC Driver 18 for SQL Server to connect to SQL Server.';
    section.classList.toggle('is-off', !last.enabled);
    filter.disabled = !last.enabled;
    section.querySelector('#sql-add-server').disabled = !last.enabled;
    section.querySelector('#sql-add-sqlite').disabled = !last.enabled;
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
