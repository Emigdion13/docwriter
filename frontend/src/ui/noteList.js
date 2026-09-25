/* =================================================================
   NOTE LIST  (frontend/src/ui/noteList.js)
   Middle panel: Search bar, note cards list, encrypted noise mode
   ================================================================= */

import { icon } from '../icons.js';
import { glyphs, isCalm } from './effects.js';

let noiseInterval = null;

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

export function createNoteList({ onSelectNote, onNewNote, onSearchInput }) {
  const section = document.createElement('section');
  section.className = 'notelist glass';
  section.setAttribute('aria-label', 'Notes');

  section.innerHTML = `
    <div class="nl-head">
      <div>
        <div class="nl-title" id="list-title">Notes</div>
        <div class="nl-count" id="list-count">0 notes</div>
      </div>
      <button class="btn icon primary" id="new-note" aria-label="New note" title="New note (Ctrl N)">
        ${icon('plus', 17)}
      </button>
    </div>
    <label class="search">
      ${icon('search', 15)}
      <input id="search" placeholder="Search this space" autocomplete="off" aria-label="Search this space">
    </label>
    <div class="notes" id="notes"></div>
  `;

  section.querySelector('#new-note').onclick = () => onNewNote?.();

  const searchInput = section.querySelector('#search');
  searchInput.addEventListener('input', () => {
    onSearchInput?.(searchInput.value);
  });

  section.querySelector('#notes').addEventListener('click', (e) => {
    const noteEl = e.target.closest('.note');
    if (noteEl) {
      const noteId = noteEl.dataset.note;
      onSelectNote?.(noteId);
    }
  });

  return section;
}

/**
 * Renders notes or encrypted noise into the list panel.
 */
export function renderNotes(space, notes, activeNoteId, animate = true) {
  clearInterval(noiseInterval);
  const titleEl = document.getElementById('list-title');
  const countEl = document.getElementById('list-count');
  const searchInput = document.getElementById('search');
  const notesContainer = document.getElementById('notes');

  if (!notesContainer) return;

  if (titleEl) titleEl.textContent = space.name;

  if (space.locked) {
    if (searchInput) {
      searchInput.value = '';
      searchInput.disabled = true;
    }
    const count = space.note_count ?? space.notes?.length ?? 0;
    if (countEl) countEl.textContent = `${count} notes · sealed`;

    notesContainer.innerHTML = Array.from({ length: 6 }, (_, i) => `
      <div class="ghost" style="animation-delay:${i * 50}ms">
        <span class="g" data-n="${11 + (i * 7) % 9}"></span>
        <span class="g s" data-n="${26 + (i * 11) % 14}"></span>
      </div>
    `).join('') + `
      <div class="list-lock">
        ${icon('shield', 15)}
        <span>Titles and text are encrypted on disk</span>
      </div>
    `;

    const fillNoise = () => {
      document.querySelectorAll('.ghost .g').forEach(g => {
        g.textContent = glyphs(+g.dataset.n);
      });
    };

    fillNoise();
    if (!isCalm()) {
      noiseInterval = setInterval(fillNoise, 650);
    }
    return;
  }

  if (searchInput) searchInput.disabled = false;
  const count = notes.length;
  if (countEl) countEl.textContent = `${count} note${count === 1 ? '' : 's'}`;

  if (!notes.length) {
    const q = searchInput?.value?.trim();
    notesContainer.innerHTML = q
      ? `<div class="empty-list">No notes match “${escapeHtml(q)}”</div>`
      : `<div class="empty-list">No notes in this space yet</div>`;
    return;
  }

  notesContainer.innerHTML = notes.map((n, i) => {
    const isActive = n.id === activeNoteId;
    const lc = n.link_count || 0;
    const delay = i * 45;

    return `
      <button class="note ${isActive ? 'active' : ''} ${animate ? '' : 'still'}"
              data-note="${n.id}"
              style="animation-delay:${delay}ms">
        <span class="t">${escapeHtml(n.title)}</span>
        <span class="s">${escapeHtml(n.snippet || '')}</span>
        <span class="m">
          <span>${escapeHtml(n.modified || 'today')}</span>
          ${lc ? `<span>${icon('link', 11)}${lc}</span>` : ''}
        </span>
      </button>
    `;
  }).join('');
}
