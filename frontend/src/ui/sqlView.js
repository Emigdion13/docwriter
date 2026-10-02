/* =================================================================
   SQL VIEW  (frontend/src/ui/sqlView.js)
   The SQL space's workspace: query tabs, each bound to one saved
   connection, with a SQL editor on top and the results below.  Python
   owns the connections and runs the SQL; a run answers at once and the
   result comes back as a sql_done event tagged with the tab's session.
   A tab can show a saved query: Ctrl+S saves it back, and a dot on the
   tab means its SQL or connection differs from what is saved.

   With kind: 'vt' the same view is the SQL - VT space: every tab is on
   the virtual tables' own database (connection id "vt"), and a result
   can be kept as a virtual table from either space.  A SQL tab can take a
   virtual table in as a temporary table (#name on SQL Server), to join it
   with the server's own tables.
   ================================================================= */

import { basicSetup } from 'codemirror';
import { EditorView, keymap } from '@codemirror/view';
import { Compartment, EditorState, Prec } from '@codemirror/state';
import { HighlightStyle, syntaxHighlighting } from '@codemirror/language';
import { acceptCompletion, closeCompletion } from '@codemirror/autocomplete';
import { tags as t } from '@lezer/highlight';

import { bridge, events } from '../bridge.js';
import { icon } from '../icons.js';
import { createResultGrid } from './resultGrid.js';
import { confirmAction, pickOne, promptText } from './dialogs.js';
import { catalogDialect, dialect } from './sqlComplete.js';

// The most tabs open at once (Python's MAX_SESSIONS).
export const MAX_TABS = 8;

const sqlHighlight = HighlightStyle.define([
  { tag: t.keyword, color: 'var(--sql)', fontWeight: '600' },
  { tag: [t.string, t.special(t.string)], color: 'var(--success)' },
  { tag: t.number, color: 'var(--warning)' },
  { tag: [t.lineComment, t.blockComment], color: 'var(--faint)', fontStyle: 'italic' },
  { tag: [t.typeName, t.standard(t.name)], color: 'var(--encrypted)' },
  { tag: t.operator, color: 'var(--muted)' },
  { tag: [t.special(t.name), t.variableName], color: 'var(--personal)' },
  { tag: t.bool, color: 'var(--warning)' },
  { tag: t.null, color: 'var(--danger)' }
]);

const sqlTheme = EditorView.theme({
  '&': { height: '100%', background: 'transparent', color: 'var(--text)', fontSize: '13px' },
  '.cm-scroller': { fontFamily: 'var(--font-mono)', lineHeight: '1.55' },
  '.cm-content': { caretColor: 'var(--sql)', padding: '10px 0' },
  '.cm-cursor': { borderLeftColor: 'var(--sql)', borderLeftWidth: '2px' },
  '.cm-gutters': { background: 'transparent', border: 'none', color: 'var(--faint)' },
  '.cm-activeLine': { background: 'color-mix(in srgb, var(--sql) 6%, transparent)' },
  '.cm-activeLineGutter': { background: 'transparent', color: 'var(--muted)' },
  '&.cm-focused .cm-selectionBackground, .cm-selectionBackground, ::selection': {
    background: 'color-mix(in srgb, var(--sql) 26%, transparent) !important'
  },
  '.cm-matchingBracket': { background: 'color-mix(in srgb, var(--sql) 22%, transparent)', outline: 'none' },
  '.cm-tooltip': { background: 'var(--surface)', border: '1px solid var(--glass-border)', borderRadius: '10px' },
  '.cm-tooltip-autocomplete ul li[aria-selected]': { background: 'color-mix(in srgb, var(--sql) 28%, transparent)', color: 'var(--text)' },
  '.cm-panels': { background: 'var(--surface)', color: 'var(--text)', borderColor: 'var(--glass-border)' },
  '.cm-searchMatch': { background: 'color-mix(in srgb, var(--warning) 30%, transparent)' }
});

/* Statements that change what tables and columns exist. */
const CHANGES_SCHEMA = /\b(create|alter|drop)\s+(table|view)\b|\bselect\b[^;]*\binto\s+(?!#|@)[\w\[\]."]+\s+from\b|\bsp_rename\b/i;

/* The virtual tables' own database, as the tabs of the SQL - VT space see it. */
const VT_CONNECTION = {
  id: 'vt', name: 'Virtual tables', engine: 'sqlite', engineName: 'SQLite', where: 'vt.db',
  server: '', database: '', auth: 'windows', username: '', file: '', hasPassword: false
};

const VT_NAME = /^[A-Za-z_][A-Za-z0-9_]{0,62}$/;

/* A name to offer for a result kept as a virtual table: the saved query's,
   else the table it reads from, else "result". */
function suggestVtName(text, savedName, index, count) {
  const from = /\bfrom\s+([\w.\[\]"`]+)/i.exec(text || '')?.[1]?.split('.').pop();
  let name = (savedName || from || 'result').replace(/[\[\]"`]/g, '').toLowerCase()
    .replace(/[^a-z0-9_]+/g, '_').replace(/^_+|_+$/g, '') || 'result';
  if (/^[0-9]/.test(name)) name = `t_${name}`;
  if (count > 1) name += `_${index + 1}`;
  return name.slice(0, 63);
}

function plural(n, word) {
  return `${n.toLocaleString()} ${word}${n === 1 ? '' : 's'}`;
}

function seconds(ms) {
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(ms < 10_000 ? 2 : 1)} s`;
}

export function createSqlView({ kind = 'sql', onStateChanged, notify, onNewConnection, onEnable, onTablesChanged }) {
  const vt = kind === 'vt';
  const root = document.createElement('div');
  root.className = `ws-sql ${vt ? 'ws-vt' : 'ws-query'}`;
  root.innerHTML = `
    <div class="term-head">
      <div class="crumb"><span class="crumb-dot"></span><span>${vt ? 'SQL - VT' : 'SQL'}</span><span class="sep">/</span><span id="sql-where">Query</span></div>
      <div class="term-actions">
        <span class="meta" id="sql-status"></span>
        <select class="settings-select sql-conn" id="sql-conn" aria-label="Connection for this tab" title="Connection for this tab"></select>
        <button class="btn sql-run" id="sql-run" type="button" title="Run (Ctrl+Enter or F5). With text selected, runs only that.">
          ${icon('play', 14)}<span>Run</span>
        </button>
        <button class="btn icon" id="sql-stop" type="button" aria-label="Stop the query" title="Stop the query">
          ${icon('stop', 15)}
        </button>
        <button class="btn icon" id="sql-use" type="button" aria-label="Use a virtual table in this tab" title="Use a virtual table in this tab: copies it into this connection as a temporary table (#name)">
          ${icon('table', 15)}
        </button>
        <button class="btn icon" id="sql-save" type="button" aria-label="Save this query" title="Save this query on its connection (Ctrl+S; Ctrl+Shift+S saves a copy)">
          ${icon('save', 15)}
        </button>
      </div>
    </div>
    <div class="term-tabs">
      <div class="term-tablist" id="sql-tablist" role="tablist" aria-label="Query tabs"></div>
      <button class="btn icon small term-new" id="sql-new" type="button" aria-label="New query tab" title="New query tab on this connection (Ctrl+Shift+T)">
        ${icon('plus', 15)}
      </button>
    </div>
    <div class="sql-body" id="sql-body">
      <div class="term-empty sql-empty">
        <p>${vt ? 'Open a virtual table on the left, or write a query across them.' : 'Pick a connection on the left to open a query tab.'}</p>
        <button class="btn" id="sql-empty-new" type="button">${icon('plus', 15)}<span>${vt ? 'New query' : 'New SQL Server connection…'}</span></button>
      </div>
    </div>
    <div class="term-off">
      <div class="term-off-ic">${icon('database', 40)}</div>
      <span class="hud-tag">SQL</span>
      ${vt ? `<h2>Virtual tables</h2>
      <p>
        Keep a query's result as a table here, then query it after you
        disconnect, and join it with results from other servers.
      </p>` : `<h2>Query your databases inside VaultNotes</h2>
      <p>
        Save SQL Server and SQLite connections, then write and run queries in
        tabs, one connection per tab, with the results in a grid below.
      </p>`}
      <p class="term-warn">
        ${icon('shield', 15)}
        <span>Queries run with your sign-in and can change data, so the SQL
        space is off until you allow it. Windows will ask you to confirm.</span>
      </p>
      <button class="btn primary" id="sql-enable" type="button">${icon('database', 16)}Turn on the SQL space…</button>
    </div>
  `;

  const body = root.querySelector('#sql-body');
  const tabList = root.querySelector('#sql-tablist');
  const newButton = root.querySelector('#sql-new');
  const connSelect = root.querySelector('#sql-conn');
  const runButton = root.querySelector('#sql-run');
  const stopButton = root.querySelector('#sql-stop');
  const saveButton = root.querySelector('#sql-save');
  const useButton = root.querySelector('#sql-use');
  const statusEl = root.querySelector('#sql-status');
  const whereEl = root.querySelector('#sql-where');

  let info = { enabled: false, driver: null, connections: vt ? [VT_CONNECTION] : [], queries: [], tables: [] };
  let firstRefresh = true;
  let visible = false;
  let editorShare = 0.42; // of the pane's height, shared by every tab

  /* Every tab: { key, connectionId, conn, sessionId, connecting, running, doneRun,
     status, unread, pane, editor, lang, grid, results, messages, error,
     cancelled, elapsedMs, view, saved, dirty }.  saved is the saved query the
     tab shows, { id, name, text, connection } as last saved, or null. */
  const tabs = [];
  let active = null;
  let tabSeq = 0;

  function connection(id) {
    return info.connections.find(c => c.id === id);
  }

  function tabBySession(id) {
    return id ? tabs.find(tab => tab.sessionId === id) : undefined;
  }

  function tabByKey(key) {
    return tabs.find(tab => String(tab.key) === key);
  }

  function connName(tab) {
    return connection(tab.connectionId)?.name || tab.conn?.name || 'Deleted connection';
  }

  /* A saved query's name, else the connection's: "Dev", "Dev 2"... */
  function tabLabel(tab) {
    if (tab.saved) return tab.saved.name;
    if (vt) return tab.title || `Query ${tabs.filter(other => !other.title).indexOf(tab) + 1}`;
    const same = tabs.filter(other => !other.saved && other.connectionId === tab.connectionId);
    return same.length > 1 ? `${connName(tab)} ${same.indexOf(tab) + 1}` : connName(tab);
  }

  function isDirty(tab) {
    return Boolean(tab.saved)
      && (tab.editor.state.doc.toString() !== tab.saved.text || tab.connectionId !== tab.saved.connection);
  }

  function setDirty(tab) {
    const dirty = isDirty(tab);
    if (dirty === tab.dirty) return;
    tab.dirty = dirty;
    paint();
  }

  /* ---- painting ---- */

  function paintTabs() {
    tabList.replaceChildren(...tabs.map((tab) => {
      const label = tabLabel(tab);
      const el = document.createElement('div');
      el.className = 'term-tab';
      el.classList.toggle('on', tab === active);
      el.classList.toggle('unread', tab.unread);
      el.classList.toggle('busy', Boolean(tab.running || tab.connecting));
      el.classList.toggle('ended', !connection(tab.connectionId));
      el.classList.toggle('dirty', tab.dirty);
      el.dataset.key = String(tab.key);

      const pick = document.createElement('button');
      pick.type = 'button';
      pick.className = 'term-tab-pick';
      pick.setAttribute('role', 'tab');
      pick.setAttribute('aria-selected', String(tab === active));
      pick.tabIndex = tab === active ? 0 : -1;
      pick.innerHTML = `${icon(tab.saved ? 'save' : 'database', 13)}<span class="nm"></span><span class="dot"></span>`; // fixed markup only
      pick.querySelector('.nm').textContent = label;
      const where = connection(tab.connectionId)?.where;
      pick.title = [label, tab.saved ? connName(tab) : '', where || '', tab.dirty ? 'not saved' : '']
        .filter(Boolean).join(' · ');

      const close = document.createElement('button');
      close.type = 'button';
      close.className = 'term-tab-x';
      close.setAttribute('aria-label', `Close ${label}`);
      close.title = 'Close tab (Ctrl+Shift+W)';
      close.tabIndex = -1;
      close.innerHTML = icon('x', 12);

      el.append(pick, close);
      return el;
    }));
    newButton.disabled = !info.enabled || tabs.length >= MAX_TABS || !(active ? connection(active.connectionId) : info.connections[0]);
    root.classList.toggle('no-tabs', !tabs.length);
  }

  function paint() {
    root.classList.toggle('is-off', !info.enabled);
    const options = info.connections.map((c) => {
      const option = document.createElement('option');
      option.value = c.id;
      option.textContent = `${c.name} · ${c.where}`;
      return option;
    });
    if (active && !connection(active.connectionId)) {
      const gone = document.createElement('option');
      gone.value = active.connectionId;
      gone.textContent = 'Deleted connection';
      options.unshift(gone);
    }
    connSelect.replaceChildren(...options);
    connSelect.value = active?.connectionId || '';
    connSelect.disabled = !info.enabled || !active || Boolean(active.running);
    connSelect.hidden = !active || vt;
    whereEl.textContent = !info.enabled ? 'Off'
      : !active ? 'No query tab'
        : active.saved ? `${connName(active)} / ${active.saved.name}${active.dirty ? ' •' : ''}` : tabLabel(active);
    statusEl.textContent = active?.status || '';
    runButton.disabled = !info.enabled || !active || Boolean(active.running);
    stopButton.disabled = !active?.running;
    saveButton.disabled = !info.enabled || !active || !connection(active.connectionId);
    runButton.hidden = stopButton.hidden = !active;
    saveButton.hidden = !active || vt;
    useButton.hidden = !active || vt;
    useButton.disabled = !info.enabled || !active || Boolean(active.running) || !connection(active.connectionId);
    paintTabs();
    onStateChanged?.(publicState());
  }

  function publicState() {
    return {
      enabled: info.enabled,
      driver: info.driver,
      connections: info.connections,
      queries: info.queries,
      tables: info.tables,
      tabs: tabs.length,
      running: tabs.filter(tab => tab.running).length,
      openQueries: tabs.filter(tab => tab.saved).map(tab => tab.saved.id),
      active: active ? {
        connectionId: active.connectionId,
        connectionName: connName(active),
        engine: connection(active.connectionId)?.engine || null,
        name: tabLabel(active),
        running: Boolean(active.running),
        savedId: active.saved?.id || null,
        dirty: active.dirty
      } : null
    };
  }

  function setStatus(tab, text) {
    tab.status = text;
    if (tab === active) statusEl.textContent = text;
  }

  /* ---- one tab's results ---- */

  function paintResults(tab) {
    const bar = tab.pane.querySelector('.sql-rtabs');
    const meta = tab.pane.querySelector('.sql-rmeta');
    const copy = tab.pane.querySelector('.sql-copy');
    const keep = tab.pane.querySelector('.sql-keep');
    const msgs = tab.pane.querySelector('.sql-msgs');
    const empty = tab.pane.querySelector('.sql-rempty');

    const buttons = tab.results.map((result, i) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'sql-rtab';
      b.dataset.view = String(i);
      b.textContent = tab.results.length > 1 ? `Result ${i + 1}` : 'Result';
      const count = document.createElement('span');
      count.className = 'c';
      count.textContent = result.total.toLocaleString();
      b.appendChild(count);
      return b;
    });
    const hasMessages = tab.messages.length || tab.error || tab.cancelled;
    if (hasMessages || tab.results.length) {
      const m = document.createElement('button');
      m.type = 'button';
      m.className = 'sql-rtab';
      m.classList.toggle('is-error', Boolean(tab.error));
      m.dataset.view = 'messages';
      m.textContent = tab.error ? 'Error' : 'Messages';
      buttons.push(m);
    }
    for (const b of buttons) {
      const on = b.dataset.view === String(tab.view);
      b.classList.toggle('on', on);
      b.setAttribute('aria-selected', String(on));
      b.setAttribute('role', 'tab');
    }
    bar.replaceChildren(...buttons);

    const showing = typeof tab.view === 'number' ? tab.results[tab.view] : null;
    tab.grid.element.hidden = !showing;
    msgs.hidden = tab.view !== 'messages';
    empty.hidden = Boolean(buttons.length);
    copy.hidden = !showing;
    keep.hidden = !showing;

    if (showing) {
      meta.textContent = `${plural(showing.total, 'row')} · ${showing.columns.length} col${showing.columns.length === 1 ? '' : 's'}`;
    } else {
      meta.textContent = tab.elapsedMs != null ? seconds(tab.elapsedMs) : '';
    }

    if (tab.view === 'messages') {
      const lines = [];
      if (tab.error) {
        const err = document.createElement('div');
        err.className = 'sql-msg is-error';
        err.textContent = tab.error;
        lines.push(err);
      }
      if (tab.cancelled) {
        const c = document.createElement('div');
        c.className = 'sql-msg is-warn';
        c.textContent = 'The query was stopped.';
        lines.push(c);
      }
      for (const text of tab.messages) {
        const line = document.createElement('div');
        line.className = 'sql-msg';
        line.textContent = text;
        lines.push(line);
      }
      if (tab.elapsedMs != null) {
        const done = document.createElement('div');
        done.className = 'sql-msg is-faint';
        done.textContent = `Finished in ${seconds(tab.elapsedMs)}.`;
        lines.push(done);
      }
      msgs.replaceChildren(...lines);
    }
  }

  function showView(tab, view) {
    tab.view = view;
    if (typeof view === 'number' && tab.results[view]) {
      tab.grid.setResult({ ...tab.results[view], rows: tab.results[view].rows });
    }
    paintResults(tab);
  }

  function showError(tab, message) {
    tab.error = message;
    tab.results = [];
    tab.messages = [];
    tab.cancelled = false;
    tab.elapsedMs = null;
    tab.grid.clear();
    showView(tab, 'messages');
  }

  /* ---- tabs ---- */

  function applyEditorShare() {
    root.style.setProperty('--sql-editor-share', `${Math.round(editorShare * 1000) / 10}%`);
  }

  function buildTab(connectionId, text = '', saved = null) {
    const tab = {
      key: ++tabSeq, connectionId, conn: connection(connectionId) || null, sessionId: null,
      connecting: null, running: null, doneRun: null, status: '', unread: false,
      results: [], messages: [], error: null, cancelled: false, elapsedMs: null, view: null,
      saved, dirty: false
    };
    tab.pane = document.createElement('div');
    tab.pane.className = 'sql-pane';
    tab.pane.innerHTML = `
      <div class="sql-editor"></div>
      <div class="sql-split" role="separator" aria-orientation="horizontal" title="Drag to resize"></div>
      <div class="sql-results">
        <div class="sql-rbar">
          <div class="sql-rtabs" role="tablist" aria-label="Results"></div>
          <span class="sql-rmeta"></span>
          <button class="btn icon small sql-keep" type="button" aria-label="Keep as a virtual table" title="Keep this result as a virtual table in SQL - VT">
            ${icon('table', 14)}
          </button>
          <button class="btn icon small sql-copy" type="button" aria-label="Copy this result" title="Copy this result with its header, to paste into Excel">
            ${icon('copy', 14)}
          </button>
        </div>
        <div class="sql-rbody">
          <div class="sql-msgs" hidden></div>
          <div class="sql-rempty">
            <span>Write a query and press <span class="kbd">Ctrl Enter</span> or <span class="kbd">F5</span>.
            With text selected, only the selection runs.</span>
          </div>
        </div>
      </div>
    `;
    body.appendChild(tab.pane);

    tab.grid = createResultGrid({
      fetchRows: async (offset, limit) => {
        if (!tab.sessionId || typeof tab.view !== 'number') return null;
        const res = await bridge.sql_rows(tab.sessionId, tab.view, offset, limit);
        return res?.error ? null : res.rows;
      },
      notify
    });
    tab.grid.element.hidden = true;
    tab.pane.querySelector('.sql-rbody').prepend(tab.grid.element);

    tab.lang = new Compartment();
    tab.editor = new EditorView({
      parent: tab.pane.querySelector('.sql-editor'),
      state: EditorState.create({
        doc: text,
        extensions: [
          Prec.highest(keymap.of([
            { key: 'Mod-Enter', run: () => { run(tab); return true; } },
            { key: 'F5', run: () => { run(tab); return true; } },
            // Tab takes the highlighted suggestion; with none open it does what it did.
            { key: 'Tab', run: acceptCompletion },
            { key: 'Mod-Shift-t', run: () => { newTab(); return true; } },
            { key: 'Mod-Shift-w', run: () => { closeTab(tab); return true; } },
            // Saved queries belong to connections; the VT space has none.
            { key: 'Mod-s', run: () => { if (!vt) save(tab); return true; } },
            { key: 'Mod-Shift-s', run: () => { if (!vt) saveAs(tab); return true; } }
          ])),
          EditorView.updateListener.of((update) => {
            if (update.docChanged) setDirty(tab);
          }),
          basicSetup,
          tab.lang.of(vt ? vtDialect() : dialect(tab.conn?.engine)),
          syntaxHighlighting(sqlHighlight),
          sqlTheme,
          EditorView.contentAttributes.of({ 'aria-label': 'SQL', spellcheck: 'false' })
        ]
      })
    });

    tab.pane.querySelector('.sql-rtabs').addEventListener('click', (e) => {
      const b = e.target.closest('.sql-rtab');
      if (!b) return;
      showView(tab, b.dataset.view === 'messages' ? 'messages' : Number(b.dataset.view));
    });

    tab.pane.querySelector('.sql-copy').onclick = () => copyResult(tab);
    tab.pane.querySelector('.sql-keep').onclick = () => keepAsVt(tab);

    tab.pane.querySelector('.sql-split').addEventListener('mousedown', (e) => {
      e.preventDefault();
      const box = tab.pane.getBoundingClientRect();
      const move = (ev) => {
        editorShare = Math.max(0.12, Math.min(0.85, (ev.clientY - box.top) / box.height));
        applyEditorShare();
      };
      const up = () => {
        window.removeEventListener('mousemove', move);
        window.removeEventListener('mouseup', up);
        tab.grid.refresh();
      };
      window.addEventListener('mousemove', move);
      window.addEventListener('mouseup', up);
    });

    paintResults(tab);
    return tab;
  }

  async function connect(tab) {
    if (tab.connecting) return tab.connecting;
    tab.connecting = (async () => {
      setStatus(tab, 'Connecting…');
      paintTabs();
      const res = await bridge.sql_open(tab.connectionId);
      if (!tabs.includes(tab)) {
        if (res?.id) bridge.sql_close(res.id);
        return false;
      }
      if (res?.error) {
        setStatus(tab, 'Not connected');
        showError(tab, `Could not connect: ${res.message || 'unknown error'}`);
        return false;
      }
      tab.sessionId = res.id;
      tab.conn = res.connection;
      setStatus(tab, '');
      if (tab.pushed?.length) {
        // A new connection starts with no temporary tables.
        notify?.(`Connected again, so ${tab.pushed.map(p => p.target).join(', ')} ${tab.pushed.length === 1 ? 'is' : 'are'} gone. Use ${tab.pushed.length === 1 ? 'it' : 'them'} again if you need ${tab.pushed.length === 1 ? 'it' : 'them'}.`);
        tab.pushed = [];
      }
      loadCatalog(tab); // not awaited: the tab is usable while the names load
      return true;
    })();
    try {
      return await tab.connecting;
    } finally {
      tab.connecting = null;
      paint();
    }
  }

  /* Reads the connected database's table and column names into the tab's
     autocomplete.  The VT space has its own list (see vtDialect()). */
  async function loadCatalog(tab) {
    if (vt || !tab.sessionId) return;
    const session = tab.sessionId;
    const res = await bridge.sql_schema(session);
    if (!tabs.includes(tab) || tab.sessionId !== session || res?.error) return;
    tab.editor.dispatch({
      effects: tab.lang.reconfigure(catalogDialect(tab.conn?.engine, res.tables))
    });
    if (res.truncated) notify?.('This database is very large: autocomplete knows only its first tables.');
  }

  function activate(tab) {
    if (!tab) return;
    active = tab;
    tab.unread = false;
    for (const other of tabs) other.pane.classList.toggle('on', other === tab);
    paint();
    if (!visible) return;
    requestAnimationFrame(() => {
      tab.grid.refresh();
      if (active === tab) tab.editor.focus();
    });
  }

  function cycle(step) {
    if (tabs.length < 2) return;
    const i = tabs.indexOf(active);
    activate(tabs[(i + step + tabs.length) % tabs.length]);
  }

  /* Opens a tab and connects it; resolves the tab (null when none opened). */
  async function openTab(connectionId, text = '', saved = null) {
    if (!info.enabled) return null;
    if (!connection(connectionId)) {
      notify?.('Add a connection first.');
      return null;
    }
    if (tabs.length >= MAX_TABS) {
      notify?.(`Up to ${MAX_TABS} query tabs can be open at once. Close one first.`);
      return null;
    }
    const tab = buildTab(connectionId, text, saved);
    tabs.push(tab);
    activate(tab);
    tab.ready = await connect(tab);
    return tab;
  }

  async function newTab(connectionId = vt ? 'vt' : active?.connectionId || info.connections[0]?.id, text = '') {
    return Boolean((await openTab(connectionId, text))?.ready);
  }

  async function closeTab(tab) {
    if (!tabs.includes(tab)) return;
    if (tab.dirty) {
      const ok = await confirmAction({
        title: `Close “${tab.saved.name}” without saving?`,
        message: 'Your changes to this saved query are lost. The saved version stays as it was.',
        confirmLabel: 'Close without saving',
        iconName: 'save'
      });
      if (!ok) return;
    }
    const i = tabs.indexOf(tab);
    if (i < 0) return;
    tabs.splice(i, 1);
    if (tab.sessionId) bridge.sql_close(tab.sessionId);
    tab.sessionId = null;
    tab.editor.destroy();
    tab.pane.remove();
    if (active === tab) {
      active = null;
      activate(tabs[Math.min(i, tabs.length - 1)]);
    }
    paint();
  }

  function closeAll() {
    for (const tab of tabs.splice(0)) {
      tab.sessionId = null;
      tab.editor.destroy();
      tab.pane.remove();
    }
    active = null;
  }

  /* ---- saved queries ---- */

  function link(tab, query) {
    tab.saved = { id: query.id, name: query.name, text: query.text, connection: query.connection };
    tab.dirty = isDirty(tab);
  }

  /* Ctrl+S: saves a saved query's tab in place; any other tab asks for a name. */
  async function save(tab = active) {
    if (!tab || !info.enabled) return false;
    if (!tab.saved) return saveAs(tab);
    const res = await bridge.sql_save_query({
      id: tab.saved.id, connection: tab.connectionId, name: tab.saved.name, text: tab.editor.state.doc.toString()
    });
    if (res?.error === 'not_found' && connection(tab.connectionId)) {
      // Deleted meanwhile: save it as a new one instead.
      tab.saved = null;
      return saveAs(tab);
    }
    if (res?.error) {
      notify?.(res.message || 'The query was not saved.');
      return false;
    }
    link(tab, res.query);
    setQueries(res.queries);
    notify?.(`Saved “${res.query.name}” on ${connName(tab)}.`);
    return true;
  }

  /* Ctrl+Shift+S: saves the tab's SQL as a new query on its connection. */
  async function saveAs(tab = active) {
    if (!tab || !info.enabled) return false;
    if (!connection(tab.connectionId)) {
      notify?.('Pick a connection for this tab first.');
      return false;
    }
    const text = tab.editor.state.doc.toString();
    if (!text.trim()) {
      notify?.('Write some SQL first.');
      return false;
    }
    const name = await promptText({
      title: tab.saved ? 'Save a copy' : 'Save query',
      message: `It is saved on ${connName(tab)}: clicking it in the list opens it and runs it there.`,
      value: tab.saved ? `${tab.saved.name} (copy)` : '',
      placeholder: 'e.g. Failed batches today',
      iconName: 'save',
      colorVar: '--sql'
    });
    if (!name || !tabs.includes(tab)) return false;
    const res = await bridge.sql_save_query({ connection: tab.connectionId, name, text: tab.editor.state.doc.toString() });
    if (res?.error) {
      notify?.(res.message || 'The query was not saved.');
      return false;
    }
    link(tab, res.query);
    setQueries(res.queries);
    notify?.(`Saved “${res.query.name}” on ${connName(tab)}.`);
    return true;
  }

  /* Opens a saved query in a tab on its own connection; with run, runs it.
     A tab that already shows it is reused, edits and all. */
  async function openSaved(queryId, { run: andRun = false } = {}) {
    if (!info.enabled) return false;
    let tab = tabs.find(t => t.saved?.id === queryId);
    if (tab) {
      activate(tab);
    } else {
      const res = await bridge.sql_get_query(queryId);
      if (res?.error) {
        notify?.(res.message || 'That saved query could not be opened.');
        return false;
      }
      const { id, name, text, connection: on } = res.query;
      tab = await openTab(on, text, { id, name, text, connection: on });
      if (!tab?.ready) return false;
    }
    return andRun ? run(tab) : true;
  }

  /* The saved queries changed (saved, renamed, moved or deleted). */
  function setQueries(list) {
    info.queries = Array.isArray(list) ? list : [];
    for (const tab of tabs) {
      if (!tab.saved) continue;
      const now = info.queries.find(q => q.id === tab.saved.id);
      if (!now) {
        tab.saved = null; // deleted: the tab keeps its SQL as an unsaved query
        tab.dirty = false;
      } else {
        tab.saved.name = now.name;
      }
    }
    paint();
  }

  /* ---- running ---- */

  function hasSelection(tab) {
    return tab.editor.state.selection.ranges.some(r => !r.empty);
  }

  function queryText(tab) {
    const { state } = tab.editor;
    const picked = state.selection.ranges.filter(r => !r.empty).map(r => state.sliceDoc(r.from, r.to));
    return picked.length ? picked.join('\n') : state.doc.toString();
  }

  async function run(tab = active) {
    if (!tab || tab.running || !info.enabled) return false;
    closeCompletion(tab.editor);
    const text = queryText(tab);
    if (!text.trim()) {
      notify?.('Write some SQL first.');
      return false;
    }
    tab.running = 'starting';
    paint();
    if (!tab.sessionId && !(await connect(tab))) {
      tab.running = null;
      paint();
      return false;
    }
    setStatus(tab, 'Running…');
    tab.ranText = text;
    // Only the saved SQL itself counts as a run of the saved query.
    const savedId = tab.saved && !tab.dirty && !hasSelection(tab) ? tab.saved.id : null;
    let res = await bridge.sql_run(tab.sessionId, text, savedId);
    if (res?.error === 'not_open') {
      // The connection dropped (or the app restarted it): connect again once.
      tab.sessionId = null;
      if (await connect(tab)) res = await bridge.sql_run(tab.sessionId, text, savedId);
    }
    if (!tabs.includes(tab)) return false;
    if (res?.error) {
      tab.running = null;
      setStatus(tab, '');
      if (res.error !== 'not_open') showError(tab, res.message || 'The query could not be started.');
      paint();
      return false;
    }
    if (tab.doneRun !== res.run) tab.running = res.run;
    paint();
    return true;
  }

  async function cancel(tab = active) {
    if (!tab?.running || !tab.sessionId) return;
    setStatus(tab, 'Stopping…');
    await bridge.sql_cancel(tab.sessionId);
  }

  async function copyResult(tab) {
    if (typeof tab.view !== 'number' || !tab.sessionId) return;
    const result = tab.results[tab.view];
    if (result.total > 200_000) notify?.('Copying a large result…');
    const res = await bridge.sql_copy(tab.sessionId, tab.view);
    if (res?.error) {
      notify?.(res.message || 'The result could not be copied.');
      return;
    }
    try {
      await navigator.clipboard.writeText(res.text);
      notify?.(`Copied ${plural(result.total, 'row')} with the header.`);
    } catch {
      notify?.('Copying was blocked.');
    }
  }

  events.on('sql_push_progress', (data) => {
    const tab = tabBySession(data?.session);
    if (!tab || tab.running !== 'push') return;
    setStatus(tab, `Copying ${data.table}… ${plural(data.rows, 'row')}`);
  });

  /* Copies a virtual table into the tab's own connection as a temporary
     table, so its queries can join it with the server's tables. */
  async function pushVt(tab = active, name = null) {
    if (vt || !tab || !info.enabled) return false;
    if (tab.running) {
      notify?.('Wait for the query to finish first.');
      return false;
    }
    if (!name) {
      const engine = connection(tab.connectionId)?.engine;
      name = await pickOne({
        title: 'Use a virtual table',
        message: `It is copied into ${connName(tab)} as ${engine === 'sqlite' ? 'a temporary table' : '#name'}, for this tab's queries. It lasts while the tab stays connected.`,
        items: info.tables.map(t => ({
          id: t.name,
          label: t.name,
          sub: `${plural(t.rows, 'row')} · ${t.columns.length} col${t.columns.length === 1 ? '' : 's'}${t.source ? ` · from ${t.source.name}` : ''}`,
          icon: 'table'
        })),
        colorVar: '--vt',
        empty: 'No virtual tables yet. Keep a result with the table button above it first.'
      });
      if (!name || !tabs.includes(tab)) return false;
    }
    tab.running = 'push';
    paint();
    if (!tab.sessionId && !(await connect(tab))) {
      tab.running = null;
      paint();
      return false;
    }
    setStatus(tab, `Copying ${name}…`);
    const res = await bridge.sql_push_vt(tab.sessionId, name);
    tab.running = null;
    if (!tabs.includes(tab)) return false;
    if (res?.error) {
      setStatus(tab, res.error === 'cancelled' ? 'Stopped' : '');
      paint();
      if (res.error !== 'cancelled') notify?.(res.message || 'The virtual table could not be copied.');
      else notify?.(`Stopped copying ${name}; nothing was kept.`);
      return false;
    }
    tab.pushed = [...(tab.pushed || []).filter(p => p.target !== res.target), { table: res.table, target: res.target }];
    setStatus(tab, `${res.target} ready · ${plural(res.rows, 'row')}`);
    // An empty tab gets a query to start from.
    if (!tab.editor.state.doc.length) {
      tab.editor.dispatch({ changes: { from: 0, insert: `SELECT *\nFROM ${res.target}` } });
    }
    paint();
    notify?.(`Copied ${plural(res.rows, 'row')} into ${res.target} on ${connName(tab)}. Use ${res.target} in this tab's SQL.`);
    if (tab === active && visible) tab.editor.focus();
    return true;
  }

  events.on('sql_progress', (data) => {
    const tab = tabBySession(data?.session);
    if (!tab || !tab.running) return;
    setStatus(tab, `Running… ${plural(data.rows, 'row')}`);
  });

  events.on('sql_done', (data) => {
    const tab = tabBySession(data?.session);
    if (!tab) return;
    tab.doneRun = data.run;
    tab.running = null;
    tab.results = data.results || [];
    tab.messages = data.messages || [];
    tab.error = data.error || null;
    tab.cancelled = Boolean(data.cancelled);
    tab.elapsedMs = data.elapsedMs ?? null;
    const rows = tab.results.reduce((sum, r) => sum + r.total, 0);
    setStatus(tab, tab.error ? 'Error' : tab.cancelled ? 'Stopped' : `${plural(rows, 'row')} · ${seconds(tab.elapsedMs || 0)}`);
    // An error is shown first; the results that came before it stay one click away.
    if (tab.results.length && !tab.error) {
      showView(tab, 0);
    } else {
      tab.grid.clear();
      showView(tab, tab.error || tab.cancelled || tab.messages.length ? 'messages' : null);
    }
    if (tab !== active) tab.unread = true;
    paint();
    // SQL in the VT space may have made, changed or dropped virtual tables.
    if (vt) onTablesChanged?.();
    // A CREATE, ALTER or DROP: autocomplete should know the new names.
    else if (!tab.error && CHANGES_SCHEMA.test(tab.ranText || '')) loadCatalog(tab);
  });

  /* ---- the strip and the header ---- */

  tabList.addEventListener('click', (e) => {
    const tab = tabByKey(e.target.closest('.term-tab')?.dataset.key);
    if (!tab) return;
    if (e.target.closest('.term-tab-x')) closeTab(tab);
    else activate(tab);
  });
  tabList.addEventListener('mousedown', (e) => {
    if (e.button === 1) e.preventDefault();
  });
  tabList.addEventListener('auxclick', (e) => {
    if (e.button !== 1) return;
    const tab = tabByKey(e.target.closest('.term-tab')?.dataset.key);
    if (tab) closeTab(tab);
  });
  tabList.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
      e.preventDefault();
      cycle(e.key === 'ArrowRight' ? 1 : -1);
      tabList.querySelector('.term-tab.on .term-tab-pick')?.focus();
    } else if (e.key === 'Delete') {
      e.preventDefault();
      closeTab(active);
    }
  });

  newButton.onclick = () => newTab();
  runButton.onclick = () => run();
  stopButton.onclick = () => cancel();
  saveButton.onclick = (e) => (e.shiftKey ? saveAs() : save());
  useButton.onclick = () => pushVt();
  root.querySelector('#sql-enable').onclick = () => (onEnable ? onEnable() : enable());
  root.querySelector('#sql-empty-new').onclick = () => (vt ? newTab('vt') : onNewConnection?.());

  /* Switching a tab's connection keeps its SQL and connects it again. */
  connSelect.addEventListener('change', async () => {
    const tab = active;
    const wanted = connSelect.value;
    if (!tab || wanted === tab.connectionId || !connection(wanted)) return;
    if (tab.sessionId) bridge.sql_close(tab.sessionId);
    tab.sessionId = null;
    tab.connectionId = wanted;
    tab.conn = connection(wanted);
    // The old database's names go; connect() loads the new database's.
    tab.editor.dispatch({ effects: tab.lang.reconfigure(dialect(tab.conn.engine)) });
    tab.dirty = isDirty(tab); // a saved query moves only when it is saved again
    paint();
    await connect(tab);
    tab.editor.focus();
  });

  function setConnections(list) {
    if (vt) return; // always the virtual tables' own database
    info.connections = Array.isArray(list) ? list : [];
    for (const tab of tabs) {
      const now = connection(tab.connectionId);
      if (!now) {
        // Deleted: Python closed its connection already.
        tab.sessionId = null;
        tab.running = null;
        setStatus(tab, 'Connection deleted');
      } else if (tab.conn?.engine !== now.engine) {
        tab.editor.dispatch({ effects: tab.lang.reconfigure(dialect(now.engine)) });
      }
      if (now) tab.conn = now;
    }
    paint();
  }

  async function refresh() {
    const res = await bridge.sql_state();
    if (res?.error) return publicState();
    info = { ...info, enabled: res.enabled, driver: res.driver };
    if (firstRefresh && res.sessions?.length && !tabs.length) {
      // The page was reloaded while tabs were connected: start clean.
      await bridge.sql_close();
    }
    firstRefresh = false;
    setConnections(res.connections);
    setQueries(res.queries);
    setTables(res.vtables);
    return publicState();
  }

  /* ---- virtual tables ---- */

  /* The virtual tables' names and columns, for the VT space's autocomplete. */
  function vtDialect() {
    return catalogDialect('sqlite', info.tables.map(t => ({ schema: '', name: t.name, columns: t.columns })));
  }

  /* The virtual tables changed: the VT space's editors complete the new names. */
  function setTables(list) {
    info.tables = Array.isArray(list) ? list : [];
    if (vt) {
      for (const tab of tabs) tab.editor.dispatch({ effects: tab.lang.reconfigure(vtDialect()) });
    }
    paint();
  }

  /* Copies a result into vt.db under a name, so it outlives the connection. */
  async function keepAsVt(tab) {
    if (typeof tab.view !== 'number' || !tab.sessionId) return false;
    if (tab.running) {
      notify?.('Wait for the query to finish first.');
      return false;
    }
    const index = tab.view;
    const result = tab.results[index];
    const name = await promptText({
      title: 'Keep as a virtual table',
      message: `${plural(result.total, 'row')} from ${connName(tab)}. They stay in SQL - VT after you disconnect; query them by this name.`,
      value: suggestVtName(queryText(tab), tab.saved?.name, index, tab.results.length),
      placeholder: 'pending_batches',
      confirmLabel: 'Keep',
      iconName: 'table',
      colorVar: '--vt',
      maxLength: 63
    });
    if (!name || !tabs.includes(tab)) return false;
    if (!VT_NAME.test(name)) {
      notify?.('Use letters, digits and _ only, starting with a letter or _ (like pending_batches).');
      return false;
    }
    if (result.total > 100_000) notify?.(`Copying ${plural(result.total, 'row')}…`);
    let res = await bridge.sql_save_vt(tab.sessionId, index, name, false);
    if (res?.error === 'exists') {
      const ok = await confirmAction({
        title: `Replace ${name}?`,
        message: `There is already a virtual table named ${name}. It is replaced with this result.`,
        confirmLabel: 'Replace',
        danger: true,
        iconName: 'table'
      });
      if (!ok) return false;
      res = await bridge.sql_save_vt(tab.sessionId, index, name, true);
    }
    if (res?.error) {
      notify?.(res.message || 'The result could not be kept.');
      return false;
    }
    notify?.(`Kept ${plural(res.table.rows, 'row')} as ${res.table.name} in SQL - VT.`);
    onTablesChanged?.(res.tables);
    return true;
  }

  async function enable() {
    const res = await bridge.sql_enable();
    if (res?.error) {
      if (res.error !== 'cancelled') notify?.(res.message || 'The SQL space could not be turned on.');
      return false;
    }
    info.enabled = true;
    await refresh();
    return true;
  }

  /* told: Python was already told (the other SQL view turned it off). */
  async function disable({ told = false } = {}) {
    if (!told) await bridge.sql_disable();
    info.enabled = false;
    closeAll();
    paint();
  }

  applyEditorShare();

  return {
    element: root,
    refresh,
    enable,
    disable,
    setConnections,
    setQueries,
    setTables,
    getState: publicState,

    show() {
      visible = true;
      paint();
      if (!active) return;
      requestAnimationFrame(() => {
        active?.grid.refresh();
        active?.editor.focus();
      });
    },

    hide() {
      visible = false;
    },

    /** Opens a query tab on a connection (with text, already filled in). */
    newTab: (connectionId, text) => newTab(connectionId, text),
    closeTab: () => closeTab(active),
    cycle,
    run: () => run(),
    cancel: () => cancel(),
    save: () => save(),
    saveAs: () => saveAs(),
    /** Opens a saved query on its connection; { run: true } runs it too. */
    openSaved,

    /** Copies virtual table `name` (or one picked) into the open tab's connection. */
    pushVt: (name) => pushVt(active, name),

    /** A virtual table was renamed: tabs titled after it follow (their SQL does not). */
    retitle(oldName, newName) {
      for (const tab of tabs) if (tab.title === oldName) tab.title = newName;
      paint();
    },

    /** Opens a tab with SQL already in it (titled, in the VT space), and runs it. */
    async openAndRun(connectionId, text, title = '') {
      const tab = await openTab(connectionId, text);
      if (!tab) return false;
      tab.title = title;
      paint();
      return tab.ready ? run(tab) : false;
    }
  };
}
