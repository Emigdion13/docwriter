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
    const tool = e.target.closest('.space-tool');
    if (tool) {
      onSelectSpace?.(tool.dataset.tool);
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

/* The tool entries under the note spaces: CMD and SQL.  They are not spaces
   of notes, so each keeps its own state; while one is open no note space is
   highlighted. */
const tools = {
  cmd: { name: 'CMD', icon: 'terminal', colorVar: '--cmd', active: false, sub: 'Off' },
  sql: { name: 'SQL', icon: 'database', colorVar: '--sql', active: false, sub: 'Off' }
};
let lastSpaces = [];
let lastActiveId = null;

export function setToolEntry(id, changes) {
  tools[id] = { ...tools[id], ...changes };
  renderSpaces(lastSpaces, lastActiveId);
}

function toolButton(id) {
  const tool = tools[id];
  const button = document.createElement('button');
  button.className = `space space-tool ${tool.active ? 'active' : ''}`;
  button.dataset.tool = id;
  button.style.setProperty('--c', `var(${tool.colorVar})`);
  button.innerHTML = `
    <span class="ic">${icon(tool.icon, 17)}</span>
    <span class="txt"><span class="nm"></span><span class="sub"></span></span>
    <span class="badge">${icon('right', 12)}</span>
  `;
  button.querySelector('.nm').textContent = tool.name;
  button.querySelector('.sub').textContent = tool.sub;
  button.setAttribute('aria-label', `${tool.name}, ${tool.sub}`);
  return button;
}

/**
 * Renders the list of spaces into #spaces, then the tool entries.
 */
export function renderSpaces(spaces, activeSpaceId) {
  lastSpaces = spaces || [];
  lastActiveId = activeSpaceId;
  const container = document.getElementById('spaces');
  if (!container) return;
  if (Object.values(tools).some(t => t.active)) activeSpaceId = null;

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
  container.append(...Object.keys(tools).map(toolButton));
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
