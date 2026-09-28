/* =================================================================
   SETTINGS  (frontend/src/ui/settings.js)
   Settings overlay: theme, effects, editor font size, auto-lock,
   notes folder, and the Google Drive backup controls (section 7 "Backup").
   ================================================================= */

import { icon } from '../icons.js';

const FONT_SIZES = [11, 12, 13, 13.5, 14, 15, 16];

export function createSettingsOverlay({
  getSettings,
  onSaveSettings,
  onChooseFolder,
  getBackup,
  onSaveBackup,
  onChooseClientSecret,
  onConnectDrive,
  onDisconnectDrive,
  onBackupNow,
  onRestoreDrive,
  onPruneDrive
}) {
  const overlay = document.createElement('div');
  overlay.className = 'overlay';
  overlay.id = 'ov-settings';
  overlay.setAttribute('role', 'dialog');
  overlay.setAttribute('aria-modal', 'true');
  overlay.setAttribute('aria-labelledby', 'settings-title');

  overlay.innerHTML = `
    <div class="settings-box">
      <h3 id="settings-title">Settings</h3>

      <div class="settings-group">
        <label for="set-theme">Theme</label>
        <select class="settings-select" id="set-theme">
          <option value="nebula">Nebula (Dark default)</option>
          <option value="synthwave">Synthwave (Dark neon)</option>
          <option value="arctic">Arctic (Light)</option>
        </select>
      </div>

      <div class="settings-group">
        <label for="set-fx">Effects & Animations</label>
        <select class="settings-select" id="set-fx">
          <option value="full">Full (Blur, glow, living aurora)</option>
          <option value="lite">Lite (No blur, still background)</option>
          <option value="off">Off (No animations)</option>
        </select>
      </div>

      <div class="settings-grid">
        <div class="settings-group">
          <label for="set-font">Editor font size</label>
          <select class="settings-select" id="set-font">
            ${FONT_SIZES.map(px => `<option value="${px}">${px}px</option>`).join('')}
          </select>
        </div>

        <div class="settings-group">
          <label for="set-autolock">Auto-lock timer</label>
          <select class="settings-select" id="set-autolock">
            <option value="1">1 minute (testing)</option>
            <option value="5">5 minutes</option>
            <option value="10">10 minutes (default)</option>
            <option value="15">15 minutes</option>
            <option value="30">30 minutes</option>
          </select>
        </div>
      </div>

      <div class="settings-group">
        <label for="set-folder">Notes folder</label>
        <div class="field-row">
          <input id="set-folder" readonly aria-label="Notes folder">
          <button class="btn" id="set-browse" type="button">Choose…</button>
        </div>
        <p class="settings-hint">Switching folders locks all vaults. Key files must stay outside it.</p>
      </div>

      <div class="settings-group">
        <label for="set-drive-interval">Google Drive backup</label>
        <div class="drive-state" id="set-drive-state">
          <span class="dot"></span>
          <span class="grow" id="set-drive-text">Not connected</span>
          <span class="when" id="set-drive-when"></span>
        </div>

        <div class="settings-grid" style="margin-top:10px">
          <div class="settings-group" style="margin-bottom:0">
            <label for="set-drive-interval">Every</label>
            <select class="settings-select" id="set-drive-interval">
              <option value="15">15 minutes</option>
              <option value="30">30 minutes</option>
              <option value="60">1 hour</option>
              <option value="180">3 hours</option>
              <option value="720">12 hours</option>
              <option value="1440">1 day</option>
            </select>
          </div>
          <div class="settings-group" style="margin-bottom:0">
            <label for="set-drive-enabled">While the app is open</label>
            <label class="check-row" for="set-drive-enabled">
              <input type="checkbox" id="set-drive-enabled">
              <span>Back up automatically</span>
            </label>
          </div>
        </div>

        <div class="drive-actions">
          <button class="btn" id="set-drive-client" type="button">
            ${icon('file', 15)}Choose client_secret.json…
          </button>
          <button class="btn primary" id="set-drive-connect" type="button">
            ${icon('cloud', 15)}Connect Google Drive
          </button>
          <button class="btn" id="set-drive-now" type="button">
            ${icon('sync', 15)}Back up now
          </button>
          <button class="btn" id="set-drive-restore" type="button">
            ${icon('upload', 15)}Restore from Drive…
          </button>
          <button class="btn" id="set-drive-prune" type="button">
            ${icon('trash', 15)}Clean up Drive…
          </button>
          <button class="btn" id="set-drive-disconnect" type="button">Disconnect</button>
        </div>
        <p class="settings-hint">
          Encrypted notes are uploaded as ciphertext, and <code>*.vnkey</code> key
          files are never uploaded. Notes you delete stay in Drive until you press
          <b>Clean up Drive…</b>, which only removes files you deleted here more than
          30 days ago.
        </p>
      </div>

      <div class="dlg-actions">
        <button class="btn" id="set-cancel" type="button">Close</button>
        <button class="btn primary" id="set-save" type="button">
          ${icon('check', 15)}Save
        </button>
      </div>
    </div>
  `;

  const closeSettings = () => overlay.classList.remove('open');

  overlay.addEventListener('mousedown', (e) => {
    if (e.target === overlay) closeSettings();
  });

  overlay.querySelector('#set-cancel').onclick = closeSettings;

  const driveState = overlay.querySelector('#set-drive-state');
  const driveText = overlay.querySelector('#set-drive-text');
  const driveWhen = overlay.querySelector('#set-drive-when');

  /** One place turns the engine's snapshot into the card's wording. */
  function paintDrive(backup) {
    const data = backup || {};
    const connected = Boolean(data.connected);
    driveState.classList.toggle('on', connected);
    overlay.querySelector('#set-drive-client').style.display = connected ? 'none' : '';
    overlay.querySelector('#set-drive-connect').style.display = connected ? 'none' : '';
    overlay.querySelector('#set-drive-disconnect').style.display = connected ? '' : 'none';
    overlay.querySelector('#set-drive-prune').style.display = connected && data.has_folder ? '' : 'none';
    if (!connected) {
      driveText.textContent = data.client_secret
        ? 'Not connected · sign in once to back up'
        : 'No client_secret.json yet · choose the file you downloaded from Google Cloud';
    } else {
      const auto = data.enabled ? `every ${data.interval_minutes} min` : 'automatic backup off';
      driveText.textContent = `Connected · ${auto} · ${data.folder_name || 'VaultNotes Backup'}`;
    }
    driveWhen.textContent = data.last_backup ? `last: ${String(data.last_backup).slice(11, 16)}` : '';
    overlay.querySelector('#set-drive-enabled').checked = Boolean(data.enabled);
    const interval = String(data.interval_minutes ?? 60);
    const select = overlay.querySelector('#set-drive-interval');
    if ([...select.options].some(o => o.value === interval)) select.value = interval;
    else select.value = '60';
  }

  async function runDriveAction(fn, refresh = true) {
    if (!fn) return;
    const result = await fn();
    if (refresh && getBackup) paintDrive(getBackup());
    return result;
  }

  overlay.querySelector('#set-drive-client').onclick = () => runDriveAction(onChooseClientSecret);
  overlay.querySelector('#set-drive-connect').onclick = () => runDriveAction(onConnectDrive);
  overlay.querySelector('#set-drive-disconnect').onclick = () => runDriveAction(onDisconnectDrive);
  overlay.querySelector('#set-drive-now').onclick = () => runDriveAction(onBackupNow);
  overlay.querySelector('#set-drive-restore').onclick = () => runDriveAction(onRestoreDrive);
  overlay.querySelector('#set-drive-prune').onclick = () => runDriveAction(onPruneDrive);

  overlay.querySelector('#set-browse').onclick = async () => {
    const result = await onChooseFolder?.();
    if (result?.ok) {
      const current = getSettings ? getSettings() : {};
      if (current.notes_root) overlay.querySelector('#set-folder').value = current.notes_root;
    }
  };

  overlay.querySelector('#set-save').onclick = () => {
    const theme = overlay.querySelector('#set-theme').value;
    const fx = overlay.querySelector('#set-fx').value;
    const fontSize = parseFloat(overlay.querySelector('#set-font').value);
    const autolock = parseInt(overlay.querySelector('#set-autolock').value, 10);

    const interval = parseInt(overlay.querySelector('#set-drive-interval').value, 10);
    const enabled = overlay.querySelector('#set-drive-enabled').checked;
    const before = getBackup ? getBackup() : {};
    const backupChanged = before.enabled !== enabled || Number(before.interval_minutes) !== interval;

    onSaveSettings?.({
      autolock_minutes: autolock,
      look: { theme, effects: fx, editor_font_size: fontSize }
    });
    if (backupChanged) onSaveBackup?.({ enabled, interval_minutes: interval });
    closeSettings();
  };

  return {
    element: overlay,
    open() {
      const current = getSettings ? getSettings() : {};
      if (current.look?.theme) overlay.querySelector('#set-theme').value = current.look.theme;
      if (current.look?.effects) overlay.querySelector('#set-fx').value = current.look.effects;
      const px = String(current.look?.editor_font_size ?? '13.5');
      const fontSelect = overlay.querySelector('#set-font');
      if ([...fontSelect.options].some(o => o.value === px)) fontSelect.value = px;
      if (current.autolock_minutes) overlay.querySelector('#set-autolock').value = String(current.autolock_minutes);
      overlay.querySelector('#set-folder').value = current.notes_root || '';
      paintDrive(getBackup ? getBackup() : null);

      overlay.classList.add('open');
    },
    close: closeSettings,
    /** Called after a Drive action so the card shows the fresh snapshot. */
    paintDrive
  };
}
