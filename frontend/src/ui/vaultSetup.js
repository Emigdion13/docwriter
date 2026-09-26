/* =================================================================
   FIRST-RUN VAULT SETUP
   The native Python side owns the folder/key save dialogs.  This overlay
   explains what will happen and starts that flow without accepting paths
   from browser JavaScript.
   ================================================================= */

import { icon } from '../icons.js';

export function createVaultSetupDialog({ onSetup, onChooseFolder }) {
  const overlay = document.createElement('div');
  overlay.className = 'overlay';
  overlay.id = 'ov-vault-setup';
  overlay.setAttribute('role', 'dialog');
  overlay.setAttribute('aria-modal', 'true');
  overlay.setAttribute('aria-labelledby', 'vault-setup-title');

  overlay.innerHTML = `
    <div class="dialog setup-dialog">
      <div class="setup-mark">${icon('shield', 30)}</div>
      <h3 id="vault-setup-title">Create your encrypted vaults</h3>
      <p class="dlg-sub" id="vault-setup-status">
        VaultNotes will create Encrypted and Personal. Each gets its own random
        key file, and key files are never put inside your notes folder.
      </p>
      <button class="btn setup-folder" id="setup-folder" type="button">
        ${icon('folder', 15)}Choose notes folder
      </button>
      <div class="setup-pass">
        <label class="field">
          <span>Passphrase for the Encrypted key (optional)</span>
          <div class="field-row">
            <input id="setup-pass-encrypted" type="password" autocomplete="off"
                   placeholder="Leave empty to keep the plain key file">
          </div>
        </label>
        <label class="field">
          <span>Passphrase for the Personal key (optional)</span>
          <div class="field-row">
            <input id="setup-pass-personal" type="password" autocomplete="off"
                   placeholder="Leave empty to keep the plain key file">
          </div>
        </label>
        <p class="setup-pass-note">
          A passphrase wraps the key file, so a stolen USB stick unlocks nothing
          (section 4.5).  There is no recovery: the passphrase is not stored.
        </p>
      </div>
      <ol class="setup-steps">
        <li><b>Encrypted</b> and <b>Personal</b> vault folders are created.</li>
        <li>Choose a safe place for each <code>.vnkey</code> file, such as a USB drive.</li>
        <li>Back up both key files. Without them, encrypted notes cannot be recovered.</li>
      </ol>
      <div class="setup-warning">
        ${icon('alert', 16)}<span>Anyone with a key file can unlock that vault. Keep the files separate from your notes backup.</span>
      </div>
      <div class="dlg-actions">
        <button class="btn" id="setup-later" type="button">Later</button>
        <button class="btn primary" id="setup-go" type="button">
          ${icon('key', 15)}Create vaults
        </button>
      </div>
    </div>
  `;

  const close = () => overlay.classList.remove('open');
  overlay.querySelector('#setup-later').onclick = close;
  overlay.querySelector('#setup-folder').onclick = async () => {
    const status = overlay.querySelector('#vault-setup-status');
    const result = await onChooseFolder?.();
    if (result?.ok) {
      status.textContent = `Notes folder selected: ${result.name}. Choose where to save each key file next.`;
    } else if (result?.message) {
      status.textContent = result.message;
    }
  };
  overlay.querySelector('#setup-go').onclick = async () => {
    const button = overlay.querySelector('#setup-go');
    const status = overlay.querySelector('#vault-setup-status');

    const passEncrypted = overlay.querySelector('#setup-pass-encrypted');
    const passPersonal = overlay.querySelector('#setup-pass-personal');
    // Both typed twice would be kinder, but the app never stores either one:
    // a wrong phrase simply fails to unlock, and the boxes clear on close.
    const passphrases = {};
    if (passEncrypted.value.trim()) passphrases.encrypted = passEncrypted.value;
    if (passPersonal.value.trim()) passphrases.personal = passPersonal.value;

    button.disabled = true;
    status.textContent = 'Choose a location for the Encrypted key file…';
    const result = await onSetup?.(passphrases);
    if (result?.error) {
      status.textContent = result.message || 'Vault setup could not be completed.';
      button.disabled = false;
      return;
    }
    passEncrypted.value = '';
    passPersonal.value = '';
    close();
    button.disabled = false;
  };

  return {
    element: overlay,
    open() {
      overlay.classList.add('open');
      setTimeout(() => overlay.querySelector('#setup-go')?.focus(), 60);
    },
    close
  };
}
