/* =================================================================
   UNLOCK DIALOG  (frontend/src/ui/unlockDialog.js)
   Signature unlock ring animation and real native-key unlock flow.
   ================================================================= */

import { icon } from '../icons.js';
import { isCalm } from './effects.js';

const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms));

/**
 * The unlock dialog: key file choice, the M10 passphrase box for a wrapped key
 * file (section 4.5), and the ring animation around the real unlock call.
 */
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
      <label class="field" id="unlock-pass-field" hidden>
        <span>Key file passphrase</span>
        <div class="field-row">
          <input id="unlock-pass" type="password" autocomplete="off"
                 placeholder="Protects the key file, never stored">
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
  overlay.querySelector('#unlock-browse').onclick = async () => {
    const result = await onBrowseKey?.(currentSpace?.id);
    const statusEl = overlay.querySelector('#unlock-status');
    const keyInput = overlay.querySelector('#unlock-key');
    if (result?.ok) {
      keyInput.value = result.name || 'Selected key file';
      statusEl.textContent = 'Key selected. It will be checked against this vault.';
    } else if (result?.message) {
      statusEl.textContent = result.message;
    }
  };

  overlay.querySelector('#unlock-go').onclick = async () => {
    if (!currentSpace) return;
    const dialog = overlay.querySelector('#unlock-dialog');
    const statusEl = overlay.querySelector('#unlock-status');
    const goBtn = overlay.querySelector('#unlock-go');
    const passField = overlay.querySelector('#unlock-pass-field');
    const passInput = overlay.querySelector('#unlock-pass');
    const passphrase = passInput.value.trim();

    const wait = ms => sleep(isCalm() ? 0 : ms);

    goBtn.disabled = true;
    dialog.classList.add('working');

    // A wrapped key file needs its passphrase before anything can be checked.
    if (!passphrase && currentSpace.key_wrapped) {
      passField.hidden = false;
      statusEl.textContent = 'This key file is protected. Enter its passphrase to unlock.';
      goBtn.disabled = false;
      passInput.focus();
      return;
    }

    statusEl.textContent = 'Checking the key matches this vault…';
    await wait(450);

    const noteCount = currentSpace.note_count ?? currentSpace.notes?.length ?? 0;
    statusEl.textContent = `Decrypting ${noteCount} notes into memory…`;
    await wait(350);

    // Python performs the actual file read, vault-id check, verifier check and
    // decryption.  The UI never receives a key path from the browser.
    const result = await onUnlockComplete?.(currentSpace.id, passphrase);
    if (result?.error || result?.ok === false) {
      dialog.classList.remove('working', 'done');
      statusEl.textContent = result.message || 'Unable to unlock this vault.';
      goBtn.disabled = false;
      if (result.error === 'passphrase_required' || result.error === 'wrong_passphrase') {
        // The key file turned out to be protected (or the phrase was wrong):
        // ask in the dialog, which is the only place a passphrase may be typed.
        passField.hidden = false;
        currentSpace.key_wrapped = true;
        passInput.focus();
        if (result.error === 'wrong_passphrase') passInput.select();
      }
      return;
    }

    dialog.classList.add('done');
    statusEl.textContent = 'Unlocked';
    await wait(380);

    closeDialog();
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

      const passField = overlay.querySelector('#unlock-pass-field');
      const passInput = overlay.querySelector('#unlock-pass');
      // The passphrase box appears only for a wrapped key file; the typed
      // passphrase never outlives this dialog.
      passField.hidden = !space.key_wrapped;
      passInput.value = '';

      titleEl.textContent = `Unlock ${space.name}`;
      keyInput.value = space.key_path
        ? space.key_path.split(/[\\/]/).pop()
        : `Choose ${space.name.toLowerCase()}.vnkey…`;
      statusEl.textContent = space.key_wrapped
        ? 'This key file is protected by a passphrase. Neither is ever uploaded.'
        : 'Your key file stays on this computer. It is never uploaded.';
      goBtn.disabled = false;

      overlay.classList.add('open');
      setTimeout(() => goBtn.focus(), 60);
    },
    close: closeDialog
  };
}
