/* =================================================================
   SQL VIEW  (frontend/src/ui/sqlView.js)
   The SQL space's workspace: query tabs, each bound to one saved
   connection, with a SQL editor on top and the results below.  Python
   owns the connections and runs the SQL; a run answers at once and the
   result comes back as a sql_done event tagged with the tab's session.
   ================================================================= */

import { basicSetup } from 'codemirror';
import { EditorView, keymap } from '@codemirror/view';
import { Compartment, EditorState, Prec } from '@codemirror/state';
import { HighlightStyle, syntaxHighlighting } from '@codemirror/language';
import { closeCompletion } from '@codemirror/autocomplete';
import { sql, MSSQL, SQLite } from '@codemirror/lang-sql';
import { tags as t } from '@lezer/highlight';

import { bridge, events } from '../bridge.js';
import { icon } from '../icons.js';
import { createResultGrid } from './resultGrid.js';

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

function dialect(engine) {
  return sql({ dialect: engine === 'sqlite' ? SQLite : MSSQL, upperCaseKeywords: true });
}

function plural(n, word) {
  return `${n.toLocaleString()} ${word}${n === 1 ? '' : 's'}`;
}

function seconds(ms) {
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(ms < 10_000 ? 2 : 1)} s`;
}

export function createSqlView({ onStateChanged, notify, onNewConnection }) {
  const root = document.createElement('div');
  root.className = 'ws-sql';
  root.innerHTML = `
    <div class="term-head">
      <div class="crumb"><span class="crumb-dot"></span><span>SQL</span><span class="sep">/</span><span id="sql-where">Query</span></div>
      <div class="term-actions">
        <span class="meta" id="sql-status"></span>
        <select class="settings-select sql-conn" id="sql-conn" aria-label="Connection for this tab" title="Connection for this tab"></select>
        <button class="btn sql-run" id="sql-run" type="button" title="Run (Ctrl+Enter or F5). With text selected, runs only that.">
          ${icon('play', 14)}<span>Run</span>
        </button>
        <button class="btn icon" id="sql-stop" type="button" aria-label="Stop the query" title="Stop the query">
          ${icon('stop', 15)}
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
        <p>Pick a connection on the left to open a query tab.</p>
        <button class="btn" id="sql-empty-new" type="button">${icon('plus', 15)}<span>New SQL Server connection…</span></button>
      </div>
    </div>
    <div class="term-off">
      <div class="term-off-ic">${icon('database', 40)}</div>
      <span class="hud-tag">SQL</span>
      <h2>Query your databases inside VaultNotes</h2>
      <p>
        Save SQL Server and SQLite connections, then write and run queries in
        tabs, one connection per tab, with the results in a grid below.
      </p>
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
  const statusEl = root.querySelector('#sql-status');
  const whereEl = root.querySelector('#sql-where');

  let info = { enabled: false, driver: null, connections: [] };
  let visible = false;
  let editorShare = 0.42; // of the pane's height, shared by every tab

  /* Every tab: { key, connectionId, conn, sessionId, connecting, running, doneRun,
     status, unread, pane, editor, lang, grid, results, messages, error,
     cancelled, elapsedMs, view }. */
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

  function tabLabel(tab) {
    const same = tabs.filter(other => other.connectionId === tab.connectionId);
    return same.length > 1 ? `${connName(tab)} ${same.indexOf(tab) + 1}` : connName(tab);
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
      el.dataset.key = String(tab.key);

      const pick = document.createElement('button');
      pick.type = 'button';
      pick.className = 'term-tab-pick';
      pick.setAttribute('role', 'tab');
      pick.setAttribute('aria-selected', String(tab === active));
      pick.tabIndex = tab === active ? 0 : -1;
      pick.innerHTML = `${icon('database', 13)}<span class="nm"></span><span class="dot"></span>`; // fixed markup only
      pick.querySelector('.nm').textContent = label;
      pick.title = connection(tab.connectionId)?.where ? `${label} · ${connection(tab.connectionId).where}` : label;

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
    connSelect.hidden = !active;
    whereEl.textContent = !info.enabled ? 'Off' : active ? tabLabel(active) : 'No query tab';
    statusEl.textContent = active?.status || '';
    runButton.disabled = !info.enabled || !active || Boolean(active.running);
    stopButton.disabled = !active?.running;
    runButton.hidden = stopButton.hidden = !active;
    paintTabs();
    onStateChanged?.(publicState());
  }

  function publicState() {
    return {
      enabled: info.enabled,
      driver: info.driver,
      connections: info.connections,
      tabs: tabs.length,
      running: tabs.filter(tab => tab.running).length,
      active: active ? { connectionId: active.connectionId, name: connName(active), running: Boolean(active.running) } : null
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

  function buildTab(connectionId, text = '') {
    const tab = {
      key: ++tabSeq, connectionId, conn: connection(connectionId) || null, sessionId: null,
      connecting: null, running: null, doneRun: null, status: '', unread: false,
      results: [], messages: [], error: null, cancelled: false, elapsedMs: null, view: null
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
            { key: 'Mod-Shift-t', run: () => { newTab(); return true; } },
            { key: 'Mod-Shift-w', run: () => { closeTab(tab); return true; } },
            { key: 'Mod-s', run: () => true } // saved queries come later; no browser "Save page"
          ])),
          basicSetup,
          tab.lang.of(dialect(tab.conn?.engine)),
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
      return true;
    })();
    try {
      return await tab.connecting;
    } finally {
      tab.connecting = null;
      paint();
    }
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

  async function newTab(connectionId = active?.connectionId || info.connections[0]?.id, text = '') {
    if (!info.enabled) return false;
    if (!connection(connectionId)) {
      notify?.('Add a connection first.');
      return false;
    }
    if (tabs.length >= MAX_TABS) {
      notify?.(`Up to ${MAX_TABS} query tabs can be open at once. Close one first.`);
      return false;
    }
    const tab = buildTab(connectionId, text);
    tabs.push(tab);
    activate(tab);
    return connect(tab);
  }

  function closeTab(tab) {
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

  /* ---- running ---- */

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
    let res = await bridge.sql_run(tab.sessionId, text);
    if (res?.error === 'not_open') {
      // The connection dropped (or the app restarted it): connect again once.
      tab.sessionId = null;
      if (await connect(tab)) res = await bridge.sql_run(tab.sessionId, text);
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
  root.querySelector('#sql-enable').onclick = () => enable();
  root.querySelector('#sql-empty-new').onclick = () => onNewConnection?.();

  /* Switching a tab's connection keeps its SQL and connects it again. */
  connSelect.addEventListener('change', async () => {
    const tab = active;
    const wanted = connSelect.value;
    if (!tab || wanted === tab.connectionId || !connection(wanted)) return;
    if (tab.sessionId) bridge.sql_close(tab.sessionId);
    tab.sessionId = null;
    tab.connectionId = wanted;
    tab.conn = connection(wanted);
    tab.editor.dispatch({ effects: tab.lang.reconfigure(dialect(tab.conn.engine)) });
    paint();
    await connect(tab);
    tab.editor.focus();
  });

  function setConnections(list) {
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
    if (res.sessions?.length && !tabs.length) {
      // The page was reloaded while tabs were connected: start clean.
      await bridge.sql_close();
    }
    setConnections(res.connections);
    return publicState();
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

  async function disable() {
    await bridge.sql_disable();
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
    cancel: () => cancel()
  };
}
