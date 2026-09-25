/* =================================================================
   SETTINGS  (frontend/src/ui/settings.js)
   Settings overlay for look & feel, autolock, and directory options
   ================================================================= */

import { icon } from '../icons.js';

export function createSettingsOverlay({ getSettings, onSaveSettings }) {
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

      <div class="settings-group">
        <label for="set-autolock">Auto-lock timer</label>
        <select class="settings-select" id="set-autolock">
          <option value="5">5 minutes</option>
          <option value="10">10 minutes (default)</option>
          <option value="15">15 minutes</option>
          <option value="30">30 minutes</option>
        </select>
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

  overlay.querySelector('#set-save').onclick = () => {
    const theme = overlay.querySelector('#set-theme').value;
    const fx = overlay.querySelector('#set-fx').value;
    const autolock = parseInt(overlay.querySelector('#set-autolock').value, 10);

    onSaveSettings?.({
      autolock_minutes: autolock,
      look: { theme, effects: fx }
    });
    closeSettings();
  };

  return {
    element: overlay,
    open() {
      const current = getSettings ? getSettings() : {};
      if (current.look?.theme) overlay.querySelector('#set-theme').value = current.look.theme;
      if (current.look?.effects) overlay.querySelector('#set-fx').value = current.look.effects;
      if (current.autolock_minutes) overlay.querySelector('#set-autolock').value = String(current.autolock_minutes);

      overlay.classList.add('open');
    },
    close: closeSettings
  };
}
