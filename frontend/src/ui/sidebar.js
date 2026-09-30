/* =================================================================
   SIDEBAR  (frontend/src/ui/sidebar.js)
   Left navigation panel: Spaces list, New vault button, Google Drive card
   ================================================================= */

import { icon, spaceIcon } from '../icons.js';

export function createSidebar({ onSelectSpace, onNewVault, onSyncDrive, onOpenDriveSettings }) {
  const aside = document.createElement('aside');
  aside.className = 'sidebar glass';

  aside.innerHTML = `
    <div class="label">Spaces</div>
    <nav id="spaces" aria-label="Spaces"></nav>
    <div class="side-foot">
      <button class="ghost-btn" id="new-vault">
        ${icon('plus', 15)}<span>New vault</span>
      </button>
      <div class="drive-card" id="drive-card" role="group" aria-label="Google Drive backup">
        <div class="dc-ic">${icon('cloud', 16)}</div>
        <button class="dc-text" id="drive-details" type="button" title="Drive settings">
          <span class="dc-t">Google Drive</span>
          <span class="dc-s" id="drive-status">Backed up 10:02</span>
        </button>
        <button class="btn icon small" id="drive-sync" aria-label="Back up now" title="Back up now (Ctrl B)">
          ${icon('sync', 14)}
        </button>
      </div>
    </div>
  `;

  aside.querySelector('#new-vault').onclick = () => onNewVault?.();
  aside.querySelector('#drive-sync').onclick = (event) => {
    event.stopPropagation();
    onSyncDrive?.();
  };
  aside.querySelector('#drive-details').onclick = () => onOpenDriveSettings?.();

  aside.querySelector('#spaces').addEventListener('click', (e) => {
    if (e.target.closest('.space-cmd')) {
      onSelectSpace?.('cmd');
      return;
    }
    const spaceBtn = e.target.closest('.space');
    if (spaceBtn) {
      const spaceId = spaceBtn.dataset.space;
      onSelectSpace?.(spaceId);
    }
  });

  return aside;
}

/* The CMD entry under the note spaces.  It is not a space of notes, so it
   keeps its own state; while it is open no note space is highlighted. */
let cmdEntry = { active: false, sub: 'Off' };
let lastSpaces = [];
let lastActiveId = null;

export function setCmdEntry(changes) {
  cmdEntry = { ...cmdEntry, ...changes };
  renderSpaces(lastSpaces, lastActiveId);
}

function cmdButton() {
  const button = document.createElement('button');
  button.className = `space space-cmd ${cmdEntry.active ? 'active' : ''}`;
  button.dataset.space = 'cmd';
  button.style.setProperty('--c', 'var(--cmd)');
  button.innerHTML = `
    <span class="ic">${icon('terminal', 17)}</span>
    <span class="txt"><span class="nm">CMD</span><span class="sub"></span></span>
    <span class="badge">${icon('right', 12)}</span>
  `;
  button.querySelector('.sub').textContent = cmdEntry.sub;
  button.setAttribute('aria-label', `CMD, ${cmdEntry.sub}`);
  return button;
}

/**
 * Renders the list of spaces into #spaces, then the CMD entry.
 */
export function renderSpaces(spaces, activeSpaceId) {
  lastSpaces = spaces || [];
  lastActiveId = activeSpaceId;
  const container = document.getElementById('spaces');
  if (!container) return;
  if (cmdEntry.active) activeSpaceId = null;

  container.innerHTML = lastSpaces.map(s => {
    const isPlain = s.kind === 'plain';
    const sub = s.id === 'ai' ? 'For AI helpers'
      : isPlain ? 'Always open' : s.created === false ? 'Not set up' : s.locked ? 'Locked' : 'Unlocked';
    const ic = spaceIcon(s);
    const isActive = s.id === activeSpaceId;
    const isLocked = !isPlain && s.locked;
    const badgeContent = isLocked ? icon('key', 12) : (s.note_count ?? s.notes?.length ?? 0);

    return `
      <button class="space ${isActive ? 'active' : ''} ${isLocked ? 'is-locked' : ''}"
              data-space="${s.id}"
              style="--c: var(${s.colorVar || '--accent'})"
              aria-label="${s.name}, ${sub}">
        <span class="ic">${icon(ic, 17)}</span>
        <span class="txt">
          <span class="nm">${s.name}</span>
          <span class="sub">${sub}</span>
        </span>
        <span class="badge">${badgeContent}</span>
      </button>
    `;
  }).join('') + '<div class="space-sep" role="separator"></div>';
  container.appendChild(cmdButton());
}

export function updateDriveCard(statusText, opts = {}) {
  const el = document.getElementById('drive-status');
  if (el) el.textContent = statusText;
  const card = document.getElementById('drive-card');
  if (!card) return;
  // "Not just colour" (section 6.7): the wording changes too, and the sync
  // icon only spins while the engine is actually sending something.
  card.classList.toggle('is-busy', Boolean(opts.busy));
  card.classList.toggle('is-off', opts.off === true);
}
