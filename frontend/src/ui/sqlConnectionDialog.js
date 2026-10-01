/* =================================================================
   SQL CONNECTION DIALOG  (frontend/src/ui/sqlConnectionDialog.js)
   Adds or edits one saved connection.  A password typed here goes to
   Python once, which keeps it in the Windows Credential Manager; it is
   never shown again.  A SQLite connection's file is picked in a native
   dialog, so this one never handles a path.
   ================================================================= */

import { bridge } from '../bridge.js';
import { icon } from '../icons.js';
import { confirmAction } from './dialogs.js';

export function createSqlConnectionDialog() {
  const overlay = document.createElement('div');
  overlay.className = 'overlay';
  overlay.id = 'ov-sql-conn';
  overlay.setAttribute('role', 'dialog');
  overlay.setAttribute('aria-modal', 'true');
  overlay.setAttribute('aria-labelledby', 'sqlc-title');
  overlay.style.setProperty('--c', 'var(--sql)');

  overlay.innerHTML = `
    <form class="dialog sqlc-dialog" autocomplete="off" novalidate>
      <h3 id="sqlc-title">New SQL Server connection</h3>
      <p class="dlg-sub" id="sqlc-sub"></p>

      <label class="field"><span>Name</span>
        <div class="field-row"><input id="sqlc-name" maxlength="100" placeholder="e.g. Dev ingest" required></div>
      </label>

      <div class="sqlc-server">
        <label class="field"><span>Server</span>
          <div class="field-row"><input id="sqlc-server" maxlength="256" placeholder="dev-sql01, dev-sql01\\SQLEXPRESS or host,1433"></div>
        </label>
        <label class="field"><span>Database</span>
          <div class="field-row"><input id="sqlc-db" maxlength="256" placeholder="Optional: the login's default database"></div>
        </label>
        <label class="field"><span>Sign in with</span>
          <select class="settings-select" id="sqlc-auth">
            <option value="windows">Windows (this PC's account)</option>
            <option value="sql">SQL Server login</option>
          </select>
        </label>
        <div class="sqlc-login">
          <label class="field"><span>User name</span>
            <div class="field-row"><input id="sqlc-user" maxlength="256"></div>
          </label>
          <label class="field"><span>Password</span>
            <div class="field-row"><input id="sqlc-pass" type="password" maxlength="1024"></div>
          </label>
        </div>
        <label class="check-row sqlc-check"><input type="checkbox" id="sqlc-encrypt"><span>Encrypt the connection</span></label>
        <label class="check-row sqlc-check"><input type="checkbox" id="sqlc-trust"><span>Trust the server certificate (for servers with a self-signed certificate)</span></label>
      </div>

      <div class="sqlc-file">
        <label class="field"><span>File</span>
          <div class="field-row">
            <input id="sqlc-path" readonly tabindex="-1">
            <button class="btn" id="sqlc-choose" type="button">${icon('folder', 15)}Choose…</button>
          </div>
        </label>
      </div>

      <p class="sqlc-result" id="sqlc-result" role="status"></p>

      <div class="dlg-actions">
        <button class="btn danger-ghost sqlc-delete" id="sqlc-delete" type="button">${icon('trash', 15)}Delete</button>
        <button class="btn" id="sqlc-test" type="button">${icon('sync', 15)}Test</button>
        <button class="btn" id="sqlc-cancel" type="button">Cancel</button>
        <button class="btn primary" id="sqlc-save" type="submit">${icon('check', 15)}Save</button>
      </div>
    </form>
  `;

  const form = overlay.querySelector('form');
  const $ = (id) => overlay.querySelector(id);
  const result = $('#sqlc-result');
  let editing = null;   // the connection being edited, or null for a new one
  let queryCount = 0;   // saved queries on it, which deleting it removes too
  let resolver = null;
  let busy = false;

  function close(value) {
    overlay.classList.remove('open');
    $('#sqlc-pass').value = '';
    if (resolver) {
      resolver(value);
      resolver = null;
    }
  }

  function setResult(text, kind = '') {
    result.textContent = text;
    result.className = `sqlc-result ${kind ? `is-${kind}` : ''}`;
  }

  function setBusy(on) {
    busy = on;
    for (const b of overlay.querySelectorAll('.dlg-actions .btn, #sqlc-choose')) b.disabled = on;
  }

  function engine() {
    return editing?.engine || 'mssql';
  }

  function paintAuth() {
    const sqlLogin = $('#sqlc-auth').value === 'sql';
    $('.sqlc-login').hidden = !sqlLogin;
    $('#sqlc-pass').placeholder = editing?.hasPassword ? 'Saved. Leave empty to keep it.' : '';
  }

  function fields() {
    return {
      ...(editing ? { id: editing.id } : {}),
      engine: engine(),
      name: $('#sqlc-name').value,
      server: $('#sqlc-server').value,
      database: $('#sqlc-db').value,
      auth: $('#sqlc-auth').value,
      username: $('#sqlc-user').value,
      encrypt: $('#sqlc-encrypt').checked,
      trust_cert: $('#sqlc-trust').checked
    };
  }

  function password() {
    const typed = $('#sqlc-pass').value;
    return $('#sqlc-auth').value === 'sql' && typed ? typed : null;
  }

  $('#sqlc-auth').addEventListener('change', paintAuth);
  $('#sqlc-cancel').onclick = () => close(null);
  overlay.addEventListener('mousedown', (e) => {
    if (e.target === overlay && !busy) close(null);
  });
  overlay.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !busy) {
      e.stopPropagation();
      close(null);
    }
  });

  $('#sqlc-test').onclick = async () => {
    setBusy(true);
    setResult('Connecting…');
    const res = await bridge.sql_test_connection(fields(), password());
    setBusy(false);
    if (res?.error) setResult(res.message || 'The connection failed.', 'error');
    else setResult(`Connected in ${res.elapsedMs < 1000 ? `${res.elapsedMs} ms` : `${(res.elapsedMs / 1000).toFixed(1)} s`}.`, 'ok');
  };

  $('#sqlc-choose').onclick = async () => {
    if (!editing) return;
    setBusy(true);
    const res = await bridge.sql_choose_sqlite_file(editing.id);
    setBusy(false);
    if (res?.error) {
      if (res.error !== 'cancelled') setResult(res.message || 'That file could not be used.', 'error');
      return;
    }
    editing = res.connection;
    $('#sqlc-path').value = editing.file;
    setResult('The connection now uses this file.', 'ok');
    overlay.dispatchEvent(new CustomEvent('sql-connections', { detail: res.connections }));
  };

  $('#sqlc-delete').onclick = async () => {
    if (!editing) return;
    const ok = await confirmAction({
      title: `Delete “${editing.name}”?`,
      message: `The connection${queryCount ? `, its ${queryCount} saved quer${queryCount === 1 ? 'y' : 'ies'}` : ''} and its saved password are removed, and its query tabs are disconnected. Your databases are not touched.`,
      confirmLabel: 'Delete',
      danger: true,
      iconName: 'trash'
    });
    if (!ok) return;
    const res = await bridge.sql_delete_connection(editing.id);
    if (res?.error) {
      setResult(res.message || 'The connection could not be deleted.', 'error');
      return;
    }
    close({ deleted: editing.id, connections: res.connections, queries: res.queries });
  };

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (busy) return;
    if (!$('#sqlc-name').value.trim()) {
      setResult('Give the connection a name.', 'error');
      $('#sqlc-name').focus();
      return;
    }
    if (engine() === 'mssql' && !$('#sqlc-server').value.trim()) {
      setResult('Which server? Type its name.', 'error');
      $('#sqlc-server').focus();
      return;
    }
    setBusy(true);
    const res = await bridge.sql_save_connection(fields(), password());
    setBusy(false);
    if (res?.error) {
      setResult(res.message || 'The connection was not saved.', 'error');
      return;
    }
    close({ connection: res.connection, connections: res.connections });
  });

  return {
    element: overlay,

    /**
     * Opens the dialog for a new SQL Server connection (no argument) or to
     * edit `connection`.  Resolves { connection, connections },
     * { deleted, connections, queries } or null.
     */
    open(connection = null, { queryCount: count = 0 } = {}) {
      editing = connection;
      queryCount = count;
      const isSqlite = engine() === 'sqlite';
      $('#sqlc-title').textContent = connection
        ? `Edit ${isSqlite ? 'SQLite' : 'SQL Server'} connection`
        : 'New SQL Server connection';
      $('#sqlc-sub').textContent = isSqlite
        ? 'A SQLite database file on this PC.'
        : 'Saved on this PC. A SQL login password goes in the Windows Credential Manager.';
      $('.sqlc-server').hidden = isSqlite;
      $('.sqlc-file').hidden = !isSqlite;
      $('#sqlc-test').hidden = false;
      $('#sqlc-delete').hidden = !connection;
      $('#sqlc-name').value = connection?.name || '';
      $('#sqlc-server').value = connection?.server || '';
      $('#sqlc-db').value = connection?.database || '';
      $('#sqlc-auth').value = connection?.auth || 'windows';
      $('#sqlc-user').value = connection?.username || '';
      $('#sqlc-pass').value = '';
      $('#sqlc-encrypt').checked = connection ? connection.encrypt : true;
      $('#sqlc-trust').checked = connection ? connection.trust_cert : false;
      $('#sqlc-path').value = connection?.file || '';
      setResult('');
      setBusy(false);
      paintAuth();
      overlay.classList.add('open');
      setTimeout(() => (isSqlite || !connection ? $('#sqlc-name') : $('#sqlc-server')).focus(), 60);
      return new Promise((resolve) => { resolver = resolve; });
    },

    close: () => close(null)
  };
}
