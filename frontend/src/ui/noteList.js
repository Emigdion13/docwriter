/* =================================================================
   NOTE LIST  (frontend/src/ui/noteList.js)
   Middle panel: Search bar, sort/import/trash actions, note cards list,
   trash view with restore, encrypted noise mode.
   ================================================================= */

import { icon } from '../icons.js';
import { glyphs, isCalm } from './effects.js';

let noiseInterval = null;

// Rows that get an entrance animation.  Past this the list is drawn at once,
// so a space with hundreds of notes opens instantly instead of rippling.
const STAGGER_LIMIT = 12;
const MAX_STAGGERED_ROWS = 60;

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

export function createNoteList({
  onSelectNote,
  onNewNote,
  onSearchInput,
  onSortToggle,
  onImport,
  onTrashToggle,
  onRestoreNote,
  onPurgeNote,
  onEmptyTrash
}) {
  const section = document.createElement('section');
  section.className = 'notelist glass';
  section.setAttribute('aria-label', 'Notes');

  section.innerHTML = `
    <div class="nl-head">
      <div>
        <div class="nl-title" id="list-title">Notes</div>
        <div class="nl-count" id="list-count">0 notes</div>
      </div>
      <div class="nl-actions">
        <button class="btn icon small" id="sort-toggle" aria-label="Toggle sort order" title="Sort: Modified">
          ${icon('sort', 15)}
        </button>
        <button class="btn icon small" id="import-btn" aria-label="Import .md files" title="Import .md files">
          ${icon('upload', 15)}
        </button>
        <button class="btn icon small" id="trash-toggle" aria-label="Show trash" title="Show trash">
          ${icon('trash', 15)}
        </button>
        <button class="btn icon primary" id="new-note" aria-label="New note" title="New note (Ctrl N)">
          ${icon('plus', 17)}
        </button>
      </div>
    </div>
    <label class="search">
      ${icon('search', 15)}
      <input id="search" placeholder="Search this space" autocomplete="off" aria-label="Search this space">
    </label>
    <div class="notes" id="notes"></div>
  `;

  section.querySelector('#new-note').onclick = () => onNewNote?.();
  section.querySelector('#sort-toggle').onclick = () => onSortToggle?.();
  section.querySelector('#import-btn').onclick = () => onImport?.();
  section.querySelector('#trash-toggle').onclick = () => onTrashToggle?.();

  const searchInput = section.querySelector('#search');
  searchInput.addEventListener('input', () => {
    onSearchInput?.(searchInput.value);
  });

  section.querySelector('#notes').addEventListener('click', (e) => {
    const actionBtn = e.target.closest('[data-trash-action]');
    if (actionBtn) {
      e.stopPropagation();
      const noteId = actionBtn.closest('[data-note]')?.dataset.note;
      if (!noteId) return;
      if (actionBtn.dataset.trashAction === 'restore') onRestoreNote?.(noteId);
      else if (actionBtn.dataset.trashAction === 'purge') onPurgeNote?.(noteId);
      return;
    }
    if (e.target.closest('#empty-trash-btn')) {
      onEmptyTrash?.();
      return;
    }
    const noteEl = e.target.closest('.note');
    if (noteEl) {
      const noteId = noteEl.dataset.note;
      onSelectNote?.(noteId);
    }
  });

  return section;
}

function setHeaderButtons({ sort = 'modified', trashMode = false, locked = false }) {
  const sortBtn = document.getElementById('sort-toggle');
  const importBtn = document.getElementById('import-btn');
  const trashBtn = document.getElementById('trash-toggle');
  const newBtn = document.getElementById('new-note');
  if (sortBtn) {
    sortBtn.classList.toggle('on', sort === 'title');
    sortBtn.title = sort === 'title' ? 'Sort: Title (A–Z)' : 'Sort: Modified (newest first)';
    sortBtn.setAttribute('aria-label', sortBtn.title);
    sortBtn.disabled = locked || trashMode;
  }
  if (importBtn) importBtn.disabled = locked;
  if (trashBtn) {
    trashBtn.classList.toggle('on', trashMode);
    trashBtn.title = trashMode ? 'Back to notes' : 'Show trash';
    trashBtn.setAttribute('aria-label', trashBtn.title);
    trashBtn.disabled = locked;
  }
  if (newBtn) newBtn.disabled = locked || trashMode;
}

/**
 * Renders notes or encrypted noise into the list panel.
 */
export function renderNotes(space, notes, activeNoteId, animate = true, opts = {}) {
  clearInterval(noiseInterval);
  const titleEl = document.getElementById('list-title');
  const countEl = document.getElementById('list-count');
  const searchInput = document.getElementById('search');
  const notesContainer = document.getElementById('notes');

  if (!notesContainer) return;

  if (titleEl) titleEl.textContent = space.name;
  setHeaderButtons({ sort: opts.sort || 'modified', trashMode: false, locked: space.locked });

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

  // A 500-note list must not animate for half a minute, and rebuilding it on
  // every keystroke has to stay cheap: only the first rows get a stagger, and
  // long lists skip the entrance animation completely (M7 performance).
  const staggered = animate && notes.length <= MAX_STAGGERED_ROWS && !isCalm();
  notesContainer.innerHTML = notes.map((n, i) => {
    const isActive = n.id === activeNoteId;
    const lc = n.link_count || 0;
    const delay = staggered ? Math.min(i, STAGGER_LIMIT) * 45 : 0;

    return `
      <button class="note ${isActive ? 'active' : ''} ${staggered ? '' : 'still'}"
              data-note="${escapeHtml(n.id)}"
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

/**
 * Renders the trash view: trashed notes with Restore / Delete forever.
 */
export function renderTrash(space, trashEntries) {
  clearInterval(noiseInterval);
  const titleEl = document.getElementById('list-title');
  const countEl = document.getElementById('list-count');
  const searchInput = document.getElementById('search');
  const notesContainer = document.getElementById('notes');

  if (!notesContainer) return;

  if (titleEl) titleEl.textContent = `${space.name} · Trash`;
  if (searchInput) searchInput.disabled = true;
  setHeaderButtons({ trashMode: true, locked: false });

  const count = trashEntries.length;
  if (countEl) {
    countEl.textContent = count
      ? `${count} note${count === 1 ? '' : 's'} in trash`
      : 'Trash is empty';
  }

  if (!count) {
    notesContainer.innerHTML = `
      <div class="empty-list">
        <div class="trash-empty-ic">${icon('trash', 26)}</div>
        <p>Nothing in trash.<br>Deleted notes can be restored here.</p>
      </div>`;
    return;
  }

  notesContainer.innerHTML = trashEntries.map((n) => `
    <div class="note trash-note" data-note="${escapeHtml(n.id)}">
      <span class="t">${escapeHtml(n.title)}</span>
      <span class="m"><span>${escapeHtml(n.modified || '')}</span></span>
      <span class="trash-row-actions">
        <button class="btn small" data-trash-action="restore" title="Restore “${escapeHtml(n.title)}”">
          ${icon('undo', 13)}<span>Restore</span>
        </button>
        <button class="btn icon small danger-ghost" data-trash-action="purge" title="Delete “${escapeHtml(n.title)}” forever">
          ${icon('x', 13)}
        </button>
      </span>
    </div>
  `).join('') + `
    <button class="ghost-btn trash-empty-all" id="empty-trash-btn">
      ${icon('trash', 14)}<span>Empty trash</span>
    </button>
  `;
}
