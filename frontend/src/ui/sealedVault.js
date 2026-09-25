/* =================================================================
   SEALED VAULT HERO  (frontend/src/ui/sealedVault.js)
   Renders the sci-fi locked vault screen with rotating orb rings & HUD
   ================================================================= */

import { icon } from '../icons.js';

export function createSealedHero({ onUnlock }) {
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
    <div class="hud-tag">VAULT SEALED</div>
    <h2><span id="sealed-name">Personal</span> is locked</h2>
    <p>Its notes are encrypted on disk. Load <b id="sealed-key">personal.vnkey</b> to decrypt them in memory. Nothing decrypted is ever written to disk.</p>
    <button class="btn primary big" id="sealed-unlock">
      ${icon('key', 17)}Unlock with key file
    </button>
    <div class="hud">
      <span>CIPHER<b>AES-256-GCM</b></span>
      <span>KEY<b id="sealed-key2">PERSONAL.VNKEY</b></span>
      <span>AUTO-LOCK<b>10 MIN</b></span>
    </div>
  `;

  div.querySelector('#sealed-unlock').onclick = () => onUnlock?.();
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

export function updateSealedDetails(space) {
  const nameEl = document.getElementById('sealed-name');
  const keyEl = document.getElementById('sealed-key');
  const keyEl2 = document.getElementById('sealed-key2');

  if (nameEl) nameEl.textContent = space.name;
  const keyFileName = space.key_path ? space.key_path.split(/[\\/]/).pop() : `${space.name.toLowerCase()}.vnkey`;
  if (keyEl) keyEl.textContent = keyFileName;
  if (keyEl2) keyEl2.textContent = keyFileName.toUpperCase();
}
