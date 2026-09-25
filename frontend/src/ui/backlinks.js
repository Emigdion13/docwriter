/* =================================================================
   BACKLINKS  (frontend/src/ui/backlinks.js)
   Renders the "Linked from" bar beneath the editor with clickable chips.
   ================================================================= */

import { icon } from '../icons.js';

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

export function renderBacklinks(container, backlinks, noteTitle, onOpenNote) {
  if (!container) return;

  if (!noteTitle) {
    container.innerHTML = '';
    return;
  }

  const label = `<span class="bl-label">${icon('link', 12)}Linked from</span>`;

  if (!backlinks || backlinks.length === 0) {
    container.innerHTML = `${label}<span class="bl-empty">No notes link here yet. Type [[${escapeHtml(noteTitle)}]] in another note.</span>`;
    return;
  }

  const chips = backlinks.map(b => `
    <button class="chip" data-note="${b.id}" aria-label="Open note ${escapeHtml(b.title)}">
      ${escapeHtml(b.title)}
    </button>
  `).join('');

  container.innerHTML = `${label}${chips}`;

  container.onclick = (e) => {
    const chip = e.target.closest('.chip');
    if (chip) {
      const noteId = chip.dataset.note;
      onOpenNote?.(noteId);
    }
  };
}
