/* =================================================================
   TOOLBAR  (frontend/src/ui/toolbar.js)
   Top glass header: brand, command palette trigger, view switcher,
   lock all, backup button, and theme switcher.
   ================================================================= */

import { icon } from '../icons.js';

export function createToolbar({ onViewChange, onLockAll, onBackup, onThemeChange, onOpenPalette }) {
  const header = document.createElement('header');
  header.className = 'toolbar glass';

  header.innerHTML = `
    <div class="brand">
      <svg width="28" height="28" viewBox="0 0 32 32" aria-hidden="true">
        <defs>
          <linearGradient id="lg" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0" style="stop-color: var(--plain)"/>
            <stop offset=".5" style="stop-color: var(--encrypted)"/>
            <stop offset="1" style="stop-color: var(--personal)"/>
          </linearGradient>
        </defs>
        <path d="M16 2.5 27.7 9.25v13.5L16 29.5 4.3 22.75V9.25z" fill="none" stroke="url(#lg)" stroke-width="2"/>
        <circle cx="16" cy="14" r="3.2" fill="url(#lg)"/>
        <path d="M16 16.5v5.5" stroke="url(#lg)" stroke-width="2.6" stroke-linecap="round"/>
      </svg>
      <span class="brand-name">Vault<b>Notes</b></span>
    </div>

    <button class="palette-trigger" id="open-palette" aria-label="Open command palette">
      ${icon('search', 16)}
      <span class="pt-text">Search notes or run a command…</span>
      <span class="kbd">Ctrl K</span>
    </button>

    <div class="tools">
      <div class="seg" id="seg" role="group" aria-label="View mode">
        <span class="thumb"></span>
        <button data-view="edit">${icon('edit', 14)}Edit</button>
        <button data-view="split">${icon('columns', 14)}Split</button>
        <button data-view="preview">${icon('eye', 14)}Preview</button>
      </div>
      <button class="btn" id="lock-all" title="Lock all vaults (Ctrl L)">
        ${icon('lock', 15)}<span>Lock all</span>
      </button>
      <button class="btn" id="backup-btn" title="Back up now (Ctrl B)">
        ${icon('cloud', 15)}<span>Back up</span>
      </button>
      <div class="themes" id="themes" role="group" aria-label="Theme">
        <button data-theme-id="nebula" title="Nebula" aria-label="Nebula theme"></button>
        <button data-theme-id="synthwave" title="Synthwave" aria-label="Synthwave theme"></button>
        <button data-theme-id="arctic" title="Arctic" aria-label="Arctic theme"></button>
      </div>
    </div>
  `;

  // Attach event listeners
  header.querySelector('#open-palette').onclick = () => onOpenPalette?.();
  header.querySelector('#lock-all').onclick = () => onLockAll?.();
  header.querySelector('#backup-btn').onclick = () => onBackup?.();

  const segButtons = header.querySelectorAll('#seg button');
  segButtons.forEach(btn => {
    btn.onclick = () => {
      const mode = btn.dataset.view;
      onViewChange?.(mode);
    };
  });

  const themeButtons = header.querySelectorAll('#themes button');
  themeButtons.forEach(btn => {
    btn.onclick = () => {
      const themeId = btn.dataset.themeId;
      onThemeChange?.(themeId);
    };
  });

  return header;
}

export function updateToolbarView(viewMode) {
  const views = ['edit', 'split', 'preview'];
  const segButtons = document.querySelectorAll('#seg button');
  segButtons.forEach(b => b.classList.toggle('on', b.dataset.view === viewMode));
  const thumb = document.querySelector('#seg .thumb');
  if (thumb) {
    const idx = views.indexOf(viewMode);
    thumb.style.transform = `translateX(${idx * 100}%)`;
  }
}

export function updateToolbarTheme(themeId) {
  const themeButtons = document.querySelectorAll('#themes button');
  themeButtons.forEach(b => b.classList.toggle('on', b.dataset.themeId === themeId));
}
