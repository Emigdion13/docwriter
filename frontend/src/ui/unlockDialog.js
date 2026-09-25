/* =================================================================
   UNLOCK DIALOG  (frontend/src/ui/unlockDialog.js)
   Signature unlock ring animation and vault decryption flow.
   ================================================================= */

import { icon } from '../icons.js';
import { isCalm } from './effects.js';

const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms));

export function createUnlockDialog({ onUnlockComplete, onBrowseKey }) {
  const overlay = document.createElement('div');
  overlay.className = 'overlay';
  overlay.id = 'ov-unlock';
  overlay.setAttribute('role', 'dialog');
  overlay.setAttribute('aria-modal', 'true');
  overlay.setAttribute('aria-labelledby', 'unlock-title');

  overlay.innerHTML = `
    <div class="dialog" id="unlock-dialog">
      <div class="unlock-orb">
        <svg viewBox="0 0 120 120" aria-hidden="true">
          <circle class="u-bg" cx="60" cy="60" r="48"/>
          <circle class="u-fg" cx="60" cy="60" r="48"/>
        </svg>
        <span class="u-ic u-key">${icon('key', 30)}</span>
        <span class="u-ic u-open">${icon('unlock', 30)}</span>
      </div>
      <h3 id="unlock-title">Unlock Vault</h3>
      <p class="dlg-sub" id="unlock-status">Your key file stays on this computer. It is never uploaded.</p>
      <label class="field">
        <span>Key file</span>
        <div class="field-row">
          <input id="unlock-key" readonly>
          <button class="btn" id="unlock-browse" type="button">Browse…</button>
        </div>
      </label>
      <label class="check-row">
        <input type="checkbox" id="unlock-remember" checked> Remember where this key file is
      </label>
      <div class="dlg-actions">
        <button class="btn" id="unlock-cancel" type="button">Cancel</button>
        <button class="btn primary" id="unlock-go" type="button">
          ${icon('unlock', 15)}Unlock
        </button>
      </div>
    </div>
  `;

  let currentSpace = null;

  const closeDialog = () => {
    overlay.classList.remove('open');
  };

  overlay.addEventListener('mousedown', (e) => {
    if (e.target === overlay) closeDialog();
  });

  overlay.querySelector('#unlock-cancel').onclick = closeDialog;
  overlay.querySelector('#unlock-browse').onclick = () => onBrowseKey?.();

  overlay.querySelector('#unlock-go').onclick = async () => {
    if (!currentSpace) return;
    const dialog = overlay.querySelector('#unlock-dialog');
    const statusEl = overlay.querySelector('#unlock-status');
    const goBtn = overlay.querySelector('#unlock-go');

    const wait = ms => sleep(isCalm() ? 0 : ms);

    goBtn.disabled = true;
    dialog.classList.add('working');

    statusEl.textContent = 'Checking the key matches this vault…';
    await wait(450);

    const noteCount = currentSpace.note_count ?? currentSpace.notes?.length ?? 4;
    statusEl.textContent = `Decrypting ${noteCount} notes into memory…`;
    await wait(550);

    dialog.classList.add('done');
    statusEl.textContent = 'Unlocked';
    await wait(380);

    closeDialog();
    onUnlockComplete?.(currentSpace.id);
  };

  return {
    element: overlay,
    open(space) {
      currentSpace = space;
      const dialog = overlay.querySelector('#unlock-dialog');
      const titleEl = overlay.querySelector('#unlock-title');
      const keyInput = overlay.querySelector('#unlock-key');
      const statusEl = overlay.querySelector('#unlock-status');
      const goBtn = overlay.querySelector('#unlock-go');

      dialog.classList.remove('working', 'done');
      dialog.style.setProperty('--c', `var(${space.colorVar || '--accent'})`);

      titleEl.textContent = `Unlock ${space.name}`;
      keyInput.value = space.key_path || `E:\\keys\\${space.name.toLowerCase()}.vnkey`;
      statusEl.textContent = 'Your key file stays on this computer. It is never uploaded.';
      goBtn.disabled = false;

      overlay.classList.add('open');
      setTimeout(() => goBtn.focus(), 60);
    },
    close: closeDialog
  };
}
