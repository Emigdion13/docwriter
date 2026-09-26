/* =================================================================
   COMMAND PALETTE  (frontend/src/ui/commandPalette.js)
   Quick search and actions overlay (Ctrl + K)
   ================================================================= */

import { icon } from '../icons.js';

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

export function createCommandPalette({ getCommands, onExecuteCommand }) {
  const overlay = document.createElement('div');
  overlay.className = 'overlay palette';
  overlay.id = 'ov-palette';
  overlay.setAttribute('role', 'dialog');
  overlay.setAttribute('aria-modal', 'true');
  overlay.setAttribute('aria-label', 'Command palette');

  overlay.innerHTML = `
    <div class="palette-box">
      <label class="palette-input">
        ${icon('search', 18)}
        <input id="palette-q" placeholder="Type a note title or a command…" autocomplete="off" aria-label="Search notes and commands">
        <span class="kbd">Esc</span>
      </label>
      <div class="palette-list" id="palette-list" role="listbox"></div>
      <div class="palette-foot">
        <span><span class="kbd">↑</span><span class="kbd">↓</span> move</span>
        <span><span class="kbd">Enter</span> open</span>
        <span>Notes in locked vaults never appear here</span>
      </div>
    </div>
  `;

  let items = [];
  let selectedIndex = 0;

  const input = overlay.querySelector('#palette-q');
  const listContainer = overlay.querySelector('#palette-list');

  const closePalette = () => {
    overlay.classList.remove('open');
  };

  const renderList = async () => {
    const query = input.value.trim().toLowerCase();
    const allCommands = getCommands ? await getCommands() : [];

    items = allCommands.filter(c =>
      !query ||
      c.label.toLowerCase().includes(query) ||
      (c.sub && c.sub.toLowerCase().includes(query))
    ).slice(0, 9);

    selectedIndex = Math.min(selectedIndex, Math.max(0, items.length - 1));

    if (!items.length) {
      listContainer.innerHTML = '<div class="pi-empty">Nothing found</div>';
      return;
    }

    listContainer.innerHTML = items.map((c, i) => {
      const isSel = i === selectedIndex;
      const subLabel = c.sub ? `<span class="pi-sub">${escapeHtml(c.sub)}</span>` : (c.hint ? `<span class="pi-sub">${escapeHtml(c.hint)}</span>` : '');
      const colorStyle = c.colorVar ? `--c: var(${c.colorVar})` : '--c: var(--accent)';

      return `
        <button class="pi ${isSel ? 'sel' : ''}" data-index="${i}" role="option" aria-selected="${isSel}" style="${colorStyle}">
          <span class="pi-ic">${icon(c.icon || 'sparkle', 15)}</span>
          <span>${escapeHtml(c.label)}</span>
          ${subLabel}
        </button>
      `;
    }).join('');
  };

  const execute = (idx) => {
    const item = items[idx];
    if (!item) return;
    closePalette();
    setTimeout(() => {
      if (item.run) item.run();
      else onExecuteCommand?.(item);
    }, 120);
  };

  input.addEventListener('input', () => {
    selectedIndex = 0;
    renderList();
  });

  input.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      selectedIndex = (selectedIndex + 1) % Math.max(1, items.length);
      renderList();
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      selectedIndex = (selectedIndex - 1 + items.length) % Math.max(1, items.length);
      renderList();
    } else if (e.key === 'Enter') {
      e.preventDefault();
      execute(selectedIndex);
    } else if (e.key === 'Escape') {
      e.preventDefault();
      closePalette();
    }
  });

  listContainer.addEventListener('click', (e) => {
    const btn = e.target.closest('.pi');
    if (btn) {
      execute(+btn.dataset.index);
    }
  });

  overlay.addEventListener('mousedown', (e) => {
    if (e.target === overlay) closePalette();
  });

  return {
    element: overlay,
    async open() {
      input.value = '';
      selectedIndex = 0;
      await renderList();
      overlay.classList.add('open');
      setTimeout(() => input.focus(), 40);
    },
    close: closePalette
  };
}
