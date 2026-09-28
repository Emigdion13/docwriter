/* =================================================================
   SEALED VAULT HERO  (frontend/src/ui/sealedVault.js)
   Renders the sci-fi locked vault screen with rotating orb rings & HUD
   ================================================================= */

import { icon } from '../icons.js';

export function createSealedHero({ onUnlock, onChooseFolder, onSetup }) {
  const div = document.createElement('div');
  div.className = 'sealed-hero';

  div.innerHTML = `
    <div class="orb">
      <svg viewBox="0 0 200 200" aria-hidden="true">
        <circle class="o1" cx="100" cy="100" r="92"/>
        <circle class="o2" cx="100" cy="100" r="74"/>
        <circle class="o3" cx="100" cy="100" r="56"/>
      </svg>
      <span class="orb-ic">${icon('lock', 38)}</span>
    </div>
    <div class="hud-tag" id="sealed-tag">VAULT SEALED</div>
    <h2 id="sealed-title">Personal is locked</h2>
    <p id="sealed-locked-text">Its notes are encrypted on disk. Load <b id="sealed-key">personal.vnkey</b> to decrypt them in memory. Nothing decrypted is ever written to disk.</p>
    <p id="sealed-missing-text" hidden></p>
    <button class="btn primary big" id="sealed-unlock">
      ${icon('key', 17)}Unlock with key file
    </button>
    <div class="sealed-actions" id="sealed-missing-actions" hidden>
      <button class="btn primary big" id="sealed-folder">
        ${icon('folder', 17)}Choose notes folder…
      </button>
      <button class="btn big" id="sealed-setup">
        ${icon('key', 17)}Create vaults…
      </button>
    </div>
    <div class="hud">
      <span>CIPHER<b>AES-256-GCM</b></span>
      <span>KEY<b id="sealed-key2">PERSONAL.VNKEY</b></span>
      <span>AUTO-LOCK<b>10 MIN</b></span>
    </div>
  `;

  div.querySelector('#sealed-unlock').onclick = () => onUnlock?.();
  div.querySelector('#sealed-folder').onclick = () => onChooseFolder?.();
  div.querySelector('#sealed-setup').onclick = () => onSetup?.();
  return div;
}

export function createEmptyHero({ onNewNote }) {
  const div = document.createElement('div');
  div.className = 'empty-hero';

  div.innerHTML = `
    <div class="orb">
      <span class="orb-ic">${icon('file', 36)}</span>
    </div>
    <h2>Nothing here yet</h2>
    <p>Create a note to get started. Link notes together by typing [[ and a title.</p>
    <button class="btn primary big" id="empty-new">
      ${icon('plus', 17)}New note
    </button>
  `;

  div.querySelector('#empty-new').onclick = () => onNewNote?.();
  return div;
}

export function updateSealedDetails(space, notesRoot = '') {
  const byId = (id) => document.getElementById(id);
  // A vault missing from this notes folder is not "locked": saying so sent
  // people to create new, empty vaults when their notes folder had changed.
  const missing = space.created === false;

  const title = byId('sealed-title');
  if (title) title.textContent = missing ? `${space.name} isn't in this notes folder` : `${space.name} is locked`;
  const tag = byId('sealed-tag');
  if (tag) tag.textContent = missing ? 'NOT SET UP' : 'VAULT SEALED';

  const missingText = byId('sealed-missing-text');
  if (missingText) {
    missingText.textContent =
      `VaultNotes found no ${space.name} vault in ${notesRoot || 'the current notes folder'}. ` +
      'If you moved your notes, choose the folder they are in now. Create vaults only if you ' +
      'never had any: new vaults start empty and get new key files.';
    missingText.hidden = !missing;
  }
  const lockedText = byId('sealed-locked-text');
  if (lockedText) lockedText.hidden = missing;
  const unlock = byId('sealed-unlock');
  if (unlock) unlock.hidden = missing;
  const actions = byId('sealed-missing-actions');
  if (actions) actions.hidden = !missing;

  const keyFileName = space.key_path ? space.key_path.split(/[\\/]/).pop() : `${space.name.toLowerCase()}.vnkey`;
  const keyEl = byId('sealed-key');
  if (keyEl) keyEl.textContent = keyFileName;
  const keyEl2 = byId('sealed-key2');
  if (keyEl2) keyEl2.textContent = keyFileName.toUpperCase();
}
