/* =================================================================
   MAIN ENTRY POINT  (frontend/src/main.js)
   Initializes pywebview bridge, mounts UI components, manages state
   and global keyboard shortcuts.
   ================================================================= */

// Stylesheets
import './styles/tokens.css';
import './styles/base.css';
import './styles/components.css';
import './styles/preview.css';

// Bridge & Services
import { bridge, events } from './bridge.js';
import { spaceIcon } from './icons.js';

// UI Modules
import { createToolbar, updateToolbarView, updateToolbarTheme } from './ui/toolbar.js';
import { createSidebar, renderSpaces, updateDriveCard } from './ui/sidebar.js';
import { createNoteList, renderNotes, renderTrash } from './ui/noteList.js';
import { confirmAction, createMoveDialog } from './ui/dialogs.js';
import {
  initEditor,
  setEditorContent,
  getEditorContent,
  focusEditor,
  setEditorCursorToEnd,
  refreshWikilinkDecorations
} from './ui/editor.js';
import { initPreview, renderPreview, scrollPreviewTo, scrollPreviewToHeading } from './ui/preview.js';
import { renderBacklinks } from './ui/backlinks.js';
import { createSealedHero, createEmptyHero, updateSealedDetails } from './ui/sealedVault.js';
import { createUnlockDialog } from './ui/unlockDialog.js';
import { createVaultSetupDialog } from './ui/vaultSetup.js';
import { createCommandPalette } from './ui/commandPalette.js';
import { createSettingsOverlay } from './ui/settings.js';
import { createGraphOverlay } from './ui/graphView.js';
import { toast, initToasts } from './ui/toasts.js';
import {
  createStatusBar,
  setSavingState,
  setSaveError,
  updateLockCountdown,
  updateBackupProgress,
  setStatusBarTheme,
  setStatusBarFx
} from './ui/statusBar.js';
import {
  scramble,
  scrambleOut,
  setAccentColor,
  setEffectsLevel,
  replay,
  isCalm
} from './ui/effects.js';

/* =================================================================
   Application State
   ================================================================= */

const state = {
  spaces: [],
  currentSpaceId: 'plain',
  currentNoteId: null,
  currentNote: null,
  currentNotes: [],
  currentTrash: [],
  trashMode: false,
  sort: 'modified', // 'modified' | 'title'
  viewMode: 'split', // 'edit' | 'split' | 'preview'
  theme: 'nebula',
  effects: 'full',
  editorFontSize: 13.5,
  notesRoot: '',
  autolockMinutes: 10,
  lastActivity: Date.now(),
  locksAt: null,
  history: [],
  historyIndex: -1,
  titles: [],           // titles this space can link to, from bridge.list_titles
  plainTitles: [],      // Plain titles, for [[Plain:Title]] links from a vault (M10)
  isBackingUp: false,
  lastBackup: null,
  backup: {
    connected: false, enabled: false, interval_minutes: 60, last_backup: null,
    has_folder: false, folder_name: 'VaultNotes Backup', running: false, kind: null
  }
};

const THEMES = [
  { id: 'nebula', name: 'Nebula' },
  { id: 'synthwave', name: 'Synthwave' },
  { id: 'arctic', name: 'Arctic' }
];

const FX_LEVELS = [
  { id: 'full', label: 'Full' },
  { id: 'lite', label: 'Lite' },
  { id: 'off', label: 'Off' }
];

const VIEWS = ['edit', 'split', 'preview'];

// UI component references
let unlockDialog = null;
let vaultSetupDialog = null;
let commandPalette = null;
let settingsOverlay = null;
let graphOverlay = null;
let moveDialog = null;

// Timers
let saveTimer = null;
let previewDebounceTimer = null;
let lastTouchSent = 0;

/* =================================================================
   State Helpers
   ================================================================= */

function getActiveSpace() {
  return state.spaces.find(s => s.id === state.currentSpaceId) || state.spaces[0];
}

function wordCount(text) {
  if (!text) return 0;
  return text.replace(/[#*`>|[\]-]/g, ' ').split(/\s+/).filter(Boolean).length;
}

/**
 * Bridge calls that should return a list can come back as an error object
 * ({error, message}) instead.  Turning that into an empty list plus a toast
 * keeps the UI alive when Python refuses a request (M7).
 */
function asList(result) {
  if (Array.isArray(result)) return result;
  if (result && typeof result === 'object' && result.error) {
    toast(result.message || 'VaultNotes could not read that space', { icon: 'alert' });
  }
  return [];
}

/**
 * Shows the warnings Python reports about files it had to skip (M7).
 */
function showWarnings(warnings) {
  if (!Array.isArray(warnings) || !warnings.length) return;
  warnings.forEach(message => toast(String(message), { icon: 'alert' }));
}

/**
 * A failed save must never look like a saved note: the text is still in the
 * editor, so say so in the status bar and in a toast (M7).
 */
function reportSaveError(result) {
  const message = result?.message || 'The note could not be saved. Your text is still in the editor.';
  setSaveError(message);
  toast(message, { icon: 'alert' });
}

/**
 * Reloads the titles the [[ suggestions and the editor chips may use.
 * They come from list_titles, which returns nothing for a locked vault, so a
 * locked space can never leak a title into the editor (security rule 11).
 */
async function refreshTitles() {
  const space = getActiveSpace();
  if (!space || space.locked) {
    state.titles = [];
  } else {
    try {
      state.titles = asList(await bridge.list_titles(space.id));
    } catch (err) {
      // A missing bridge (or a vault that locked mid-call) must not leave the
      // suggestions showing titles from the previous space.
      console.error('Could not load note titles:', err);
      state.titles = [];
    }
  }
  await refreshPlainTitles(space);
  refreshWikilinkDecorations();
  return state.titles;
}

/**
 * Titles of the Plain space, so a vault note's [[Plain:Title]] chip can tell a
 * real note from a typo (M10).  Plain is always open, and a vault's titles are
 * never consulted from here, so this cannot reveal one (security rule 11).
 */
async function refreshPlainTitles(space) {
  if (!space || space.id === 'plain') {
    state.plainTitles = state.titles;
    return state.plainTitles;
  }
  try {
    state.plainTitles = asList(await bridge.list_titles('plain'));
  } catch (err) {
    state.plainTitles = [];
  }
  return state.plainTitles;
}

/**
 * Follow a [[link]] the user clicked - in the preview, or with Ctrl+click in the
 * editor (M10).  `space` is set for the one cross-space form a vault or
 * AI-Notes note may use, `[[Plain:Title]]`, and then the note is opened by
 * switching spaces.  `heading` asks the preview to scroll to that heading once
 * the note is up.
 */
async function openLinkTarget(title, { space = '', heading = '' } = {}) {
  const current = getActiveSpace();
  if (!current) return;
  // The only cross-space link is [[Plain:Title]]: Plain is always open, and
  // nothing may link into a vault (rule 11).  A hand-written
  // "#vn-open/encrypted/..." in a Plain note goes nowhere.
  if (space && space !== current.id && space !== 'plain') {
    toast('Links only lead to notes in the same space.', { icon: 'alert' });
    return;
  }
  const spaceId = space || current.id;
  const target = state.spaces.find(s => s.id === spaceId);
  if (!target) {
    toast(`“${title}” cannot be opened from here`, { icon: 'alert' });
    return;
  }
  if (target.locked) {
    toast(`Unlock ${target.name} first`, { icon: 'lock' });
    return;
  }

  // The engine resolves the title, so case, ".md" and an alias behave exactly
  // as they do in the preview.
  const result = await bridge.open_note_by_title(spaceId, title, heading || '');
  if (result?.error) {
    // Only a missing note in THIS space may be created from a link.
    if (result.error === 'not_found' && !space) {
      await createNoteFromLink(title);
      return;
    }
    toast(result.message || `“${title}” is not in ${target.name}`, { icon: 'alert' });
    return;
  }

  if (spaceId !== current.id) {
    await selectSpace(spaceId, result.note_id);
  } else {
    await openNote(result.note_id);
  }

  if (result.heading && state.currentNote) {
    // The preview renders as the note loads; render it here too so the scroll
    // has its headings to look at.
    await renderPreview(spaceId, state.currentNote.body);
    scrollPreviewToHeading(result.heading);
  }
}

/**
 * Offer to create the note a [[link]] points at.  Creating is always a separate
 * yes/no step (section 4.7): following a link must never write a file by itself.
 */
async function createNoteFromLink(title) {
  const space = getActiveSpace();
  if (!space) return;
  if (space.locked) {
    toast(`Unlock ${space.name} first`, { icon: 'lock' });
    return;
  }
  const ok = await confirmAction({
    title: `Create note “${title}”?`,
    message: `${space.name} has no note with that name yet.`,
    confirmLabel: 'Create note',
    iconName: 'plus'
  });
  if (ok) await createNote(title);
}

function pushHistory() {
  if (!state.currentNoteId) return;
  const current = state.history[state.historyIndex];
  if (current && current.spaceId === state.currentSpaceId && current.noteId === state.currentNoteId) return;

  state.history.splice(state.historyIndex + 1);
  state.history.push({ spaceId: state.currentSpaceId, noteId: state.currentNoteId });
  state.historyIndex = state.history.length - 1;
  updateNavButtons();
}

function updateNavButtons() {
  const backBtn = document.getElementById('back');
  const fwdBtn = document.getElementById('fwd');
  if (backBtn) backBtn.disabled = state.historyIndex <= 0;
  if (fwdBtn) fwdBtn.disabled = state.historyIndex >= state.history.length - 1;
}

function jumpHistory(entry) {
  const targetSpace = state.spaces.find(s => s.id === entry.spaceId);
  if (!targetSpace) return;
  if (targetSpace.locked) {
    toast(`${targetSpace.name} is locked`, { icon: 'lock' });
    return;
  }
  selectSpace(entry.spaceId, entry.noteId, false);
}

/* =================================================================
   Theme & Effects
   ================================================================= */

function setTheme(themeId, quiet = false) {
  const item = THEMES.find(t => t.id === themeId) || THEMES[0];
  state.theme = item.id;
  document.documentElement.dataset.theme = item.id;

  updateToolbarTheme(item.id);
  setStatusBarTheme(item.name);

  bridge.update_settings({ look: { theme: item.id } });
  if (!quiet) toast(`Theme: ${item.name}`, { icon: 'sparkle' });
}

function cycleTheme() {
  const currentIdx = THEMES.findIndex(t => t.id === state.theme);
  const nextIdx = (currentIdx + 1) % THEMES.length;
  setTheme(THEMES[nextIdx].id);
}

function setFx(levelId, quiet = false) {
  const item = FX_LEVELS.find(f => f.id === levelId) || FX_LEVELS[0];
  state.effects = item.id;
  setEffectsLevel(item.id);
  setStatusBarFx(item.label);

  bridge.update_settings({ look: { effects: item.id } });
  if (!quiet) {
    const desc = item.id === 'full'
      ? 'Effects: Full (blur, glow, living aurora)'
      : item.id === 'lite'
        ? 'Effects: Lite (no blur, still background)'
        : 'Effects: Off (no animations)';
    toast(desc, { icon: 'eye' });
  }
}

function toggleFx() {
  const map = { full: 'lite', lite: 'off', off: 'full' };
  setFx(map[state.effects] || 'full');
}

function setViewMode(mode) {
  state.viewMode = mode;
  const ws = document.getElementById('workspace');
  if (ws) {
    VIEWS.forEach(v => ws.classList.toggle(`view-${v}`, v === mode));
  }
  updateToolbarView(mode);
  bridge.update_settings({ look: { view_mode: mode } });
}

function cycleViewMode() {
  const currentIdx = VIEWS.indexOf(state.viewMode);
  const nextIdx = (currentIdx + 1) % VIEWS.length;
  setViewMode(VIEWS[nextIdx]);
}

function setEditorFontSize(px, quiet = false) {
  const size = Math.min(20, Math.max(10, Number(px) || 13.5));
  state.editorFontSize = size;
  document.documentElement.style.setProperty('--editor-font-size', `${size}px`);
  bridge.update_settings({ look: { editor_font_size: size } });
  if (!quiet) toast(`Editor font size: ${size}px`, { icon: 'edit' });
}

function toggleSort() {
  if (state.trashMode || getActiveSpace()?.locked) return;
  state.sort = state.sort === 'modified' ? 'title' : 'modified';
  refreshNoteList(false);
  toast(state.sort === 'title' ? 'Sort: Title (A–Z)' : 'Sort: Modified (newest first)', { icon: 'sort' });
}

async function refreshSpaces() {
  const stateData = await bridge.get_state();
  state.spaces = stateData.spaces;
  state.locksAt = stateData.locks_at ?? state.locksAt;
  state.notesRoot = stateData.notes_root || state.notesRoot;
  renderSpaces(state.spaces, state.currentSpaceId);
}

async function refreshNoteList(animate = false) {
  const space = getActiveSpace();
  if (!space || space.locked) {
    if (space) renderNotes(space, [], null, false, { sort: state.sort });
    return;
  }
  if (state.trashMode) {
    await refreshTrash();
    return;
  }
  const notes = asList(await bridge.list_notes(space.id, document.getElementById('search')?.value || '', state.sort));
  state.currentNotes = notes;
  renderNotes(space, notes, state.currentNoteId, animate, { sort: state.sort });
}

/* =================================================================
   Space & Note Navigation
   ================================================================= */

/* Autosave.  An edit is remembered together with the note it belongs to, so
   a save that runs after the user switched notes still goes to the right one.
   Saves are sent one at a time, in order, and a failed save stays pending. */
let pendingSave = null;          // { spaceId, noteId, body }
let saveQueue = Promise.resolve(true);

function queueEdit(body) {
  if (!state.currentNote) return;
  pendingSave = { spaceId: state.currentSpaceId, noteId: state.currentNote.id, body };
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => saveNow({ refreshList: true }), 1000);
}

function isShowing(job) {
  return !!state.currentNote && state.currentSpaceId === job.spaceId && state.currentNote.id === job.noteId;
}

/** Sends the pending edit (if any) after the saves already queued. */
function saveNow({ refreshList = false } = {}) {
  clearTimeout(saveTimer);
  saveTimer = null;
  const job = pendingSave;
  if (!job) return saveQueue;
  pendingSave = null;
  saveQueue = saveQueue.then(() => sendSave(job, refreshList));
  return saveQueue;
}

async function sendSave(job, refreshList) {
  setSavingState(true);
  let res;
  try {
    res = await bridge.save_note(job.spaceId, job.noteId, job.body);
  } catch (err) {
    res = { error: 'internal_error', message: 'The note could not be saved. Your text is still in the editor.' };
  }
  if (res?.error) {
    // Keep the edit for the next attempt, unless a newer one replaced it.
    if (!pendingSave) pendingSave = job;
    reportSaveError(res);
    return false;
  }
  if (!pendingSave) setSavingState(false);
  if (isShowing(job)) {
    state.currentNote.modified = res.modified;
    const meta = document.getElementById('meta');
    if (meta) meta.textContent = `Edited ${res.modified} · ${wordCount(job.body)} words`;
    if (refreshList) {
      const notes = asList(await bridge.list_notes(job.spaceId, document.getElementById('search')?.value || '', state.sort));
      if (isShowing(job)) {
        state.currentNotes = notes;
        renderNotes(getActiveSpace(), notes, state.currentNote.id, false, { sort: state.sort });
      }
    }
  }
  return true;
}

/**
 * Saves everything before the editor shows something else (another note, a
 * locked vault, the trash).  Returns false when the text could not be saved:
 * the caller must then stay where it is, so the text is not thrown away.
 */
async function flushSave() {
  const ok = await saveNow();
  return ok && !pendingSave;
}

async function selectSpace(spaceId, targetNoteId = null, animate = true) {
  if (!(await flushSave())) return;
  const prevSpaceId = state.currentSpaceId;
  state.currentSpaceId = spaceId;
  state.trashMode = false;
  const space = getActiveSpace();

  setAccentColor(space.colorVar || '--accent');
  renderSpaces(state.spaces, state.currentSpaceId);

  const searchInput = document.getElementById('search');
  if (searchInput && prevSpaceId !== spaceId) searchInput.value = '';

  if (space.locked) {
    state.currentNoteId = null;
    state.currentNote = null;
    state.currentNotes = [];
    state.titles = [];
    refreshWikilinkDecorations();
    renderNotes(space, [], null, animate, { sort: state.sort });
    renderWorkspace();
    return;
  }

  await refreshTitles();

  const notes = asList(await bridge.list_notes(space.id, searchInput?.value || '', state.sort));
  state.currentNotes = notes;

  if (!targetNoteId && notes.length > 0) {
    targetNoteId = notes[0].id;
  }
  state.currentNoteId = targetNoteId;

  renderNotes(space, notes, state.currentNoteId, animate, { sort: state.sort });
  await loadAndDisplayNote(state.currentNoteId);

  if (animate && prevSpaceId !== spaceId) {
    const panes = document.getElementById('panes');
    if (panes) replay(panes, 'swap');
  }
}

async function openNote(noteId) {
  if (!(await flushSave())) return;
  state.trashMode = false;
  state.currentNoteId = noteId;
  const space = getActiveSpace();
  const notes = asList(await bridge.list_notes(space.id, document.getElementById('search')?.value || '', state.sort));
  state.currentNotes = notes;

  renderNotes(space, notes, state.currentNoteId, false, { sort: state.sort });
  await loadAndDisplayNote(noteId);

  const panes = document.getElementById('panes');
  if (panes) replay(panes, 'swap');
  pushHistory();
}

async function loadAndDisplayNote(noteId) {
  if (!noteId) {
    state.currentNote = null;
    renderWorkspace();
    return;
  }

  const res = await bridge.open_note(state.currentSpaceId, noteId);
  if (res.error) {
    state.currentNote = null;
    renderWorkspace();
    return;
  }

  state.currentNote = res;
  renderWorkspace();
}

function renderWorkspace() {
  const space = getActiveSpace();
  const ws = document.getElementById('workspace');
  if (!ws) return;

  const isLocked = space.locked;
  const hasNote = !isLocked && !!state.currentNote;

  ws.classList.toggle('sealed', isLocked);
  ws.classList.toggle('empty', !isLocked && !hasNote);

  if (isLocked) {
    updateSealedDetails(space, state.notesRoot);
    return;
  }

  if (!hasNote) return;

  const note = state.currentNote;

  // Crumb
  const crumb = document.getElementById('crumb');
  if (crumb) {
    // Built with textContent: titles never go through innerHTML (rule 12a).
    const part = (text, className = '') => {
      const span = document.createElement('span');
      if (className) span.className = className;
      span.textContent = text;
      return span;
    };
    crumb.replaceChildren(part('', 'crumb-dot'), part(space.name), part('/', 'sep'), part(note.title));
  }

  // Title input
  const titleInput = document.getElementById('title');
  if (titleInput && document.activeElement !== titleInput) {
    titleInput.value = note.title;
  }

  // Meta info
  const meta = document.getElementById('meta');
  if (meta) {
    meta.textContent = `Edited ${note.modified || 'just now'} · ${wordCount(note.body)} words`;
  }

  // Editor content
  setEditorContent(note.body);

  // Preview content
  renderPreview(space.id, note.body);

  // Backlinks
  const backlinksContainer = document.getElementById('backlinks');
  renderBacklinks(backlinksContainer, note.backlinks, note.title, (id) => openNote(id));
}

/* =================================================================
   Note Actions
   ================================================================= */

async function createNote(initialTitle = 'Untitled') {
  if (!(await flushSave())) return;
  const space = getActiveSpace();
  if (space.locked) {
    toast(`Unlock ${space.name} first`, { icon: 'lock' });
    return;
  }

  const newNote = await bridge.create_note(space.id, initialTitle);
  if (newNote.error) {
    toast(newNote.message, { icon: 'file' });
    return;
  }

  await refreshSpaces();
  await refreshTitles();

  // Switch view to split if in preview mode
  if (state.viewMode === 'preview') {
    setViewMode('split');
  }

  await openNote(newNote.id);
  focusEditor();
  setEditorCursorToEnd();
  toast(`Created “${newNote.title}”`, { icon: 'plus' });
}

/** "A", "A and B", "A, B and C". */
function listText(items) {
  if (items.length < 2) return items[0] || '';
  return `${items.slice(0, -1).join(', ')} and ${items[items.length - 1]}`;
}

/**
 * The rename prompt's text, from count_links_to(): where the linking notes
 * are (`spaces`, only sent for a Plain note) and which vaults were not looked
 * in because they are locked (`locked`).
 */
function renamePromptMessage(linked, space, oldTitle, newTitle) {
  const count = linked.count;
  const notes = `${count} note${count === 1 ? '' : 's'}`;
  const where = Array.isArray(linked.spaces) && linked.spaces.length
    ? linked.spaces
    : [{ name: space.name, count }];
  const found = where.length === 1
    ? `${notes} in ${where[0].name} link${count === 1 ? 's' : ''} to “${oldTitle}”.`
    : `${notes} link to “${oldTitle}”: ${listText(where.map((item) => `${item.count} in ${item.name}`))}.`;
  const locked = Array.isArray(linked.locked) ? linked.locked.map((vault) => vault.name) : [];
  const unchecked = locked.length
    ? ` ${listText(locked)} ${locked.length === 1 ? 'is' : 'are'} locked, so any links in `
      + `${locked.length === 1 ? 'it' : 'them'} keep the old title.`
    : '';
  return `${found} Rewrite ${count === 1 ? 'it' : 'them'} to “${newTitle}”? `
    + `Aliases and #headings are kept.${unchecked}`;
}

async function renameNote(newTitle) {
  const space = getActiveSpace();
  const note = state.currentNote;
  if (!note || space.locked) return;

  const trimmed = (newTitle || '').trim();
  if (!trimmed) {
    const titleInput = document.getElementById('title');
    if (titleInput) titleInput.value = note.title;
    return;
  }

  if (trimmed === note.title) return;

  // "Update N links in other notes?"  The count comes from the link indexes:
  // this space, plus for a Plain note the [[Plain:Title]] links in AI-Notes
  // and unlocked vaults, which the rename rewrites too.  A locked vault is
  // never looked in, so the prompt says its links keep the old title.
  const linked = await bridge.count_links_to(space.id, note.id);
  const incoming = linked?.count || 0;
  let updateLinks = false;
  if (incoming > 0) {
    updateLinks = await confirmAction({
      title: `Update ${incoming} link${incoming === 1 ? '' : 's'} in other notes?`,
      message: renamePromptMessage(linked, space, note.title, trimmed),
      confirmLabel: 'Update links',
      cancelLabel: 'Rename only',
      iconName: 'link'
    });
  }

  const res = await bridge.rename_note(space.id, note.id, trimmed, updateLinks);
  if (res.error) {
    toast(res.message, { icon: 'file' });
    const titleInput = document.getElementById('title');
    if (titleInput) titleInput.value = note.title;
    return;
  }

  note.title = res.title;
  if (res.id) {
    note.id = res.id;
    state.currentNoteId = res.id;
  }

  const notes = asList(await bridge.list_notes(space.id, document.getElementById('search')?.value || '', state.sort));
  state.currentNotes = notes;
  renderNotes(space, notes, note.id, false, { sort: state.sort });
  renderWorkspace();
  await refreshTitles();

  if (incoming > 0 && !updateLinks) {
    toast(`Renamed. ${incoming} link${incoming === 1 ? '' : 's'} still point at the old title.`, { icon: 'link' });
  } else {
    const msg = res.links_updated
      ? `Renamed. Updated ${res.links_updated} link${res.links_updated > 1 ? 's' : ''} in other notes.`
      : 'Renamed';
    toast(msg, { icon: 'link' });
  }
}

async function deleteNote() {
  const space = getActiveSpace();
  const note = state.currentNote;
  if (!note || space.locked) return;

  if (!(await flushSave())) return;

  const res = await bridge.delete_note(space.id, note.id);
  if (res.error) {
    toast(res.message, { icon: 'trash' });
    return;
  }

  const deletedNote = res.deleted;
  const notes = asList(await bridge.list_notes(space.id, document.getElementById('search')?.value || '', state.sort));
  state.currentNotes = notes;
  const nextNote = notes[0] || null;

  await refreshSpaces();

  if (nextNote) {
    await openNote(nextNote.id);
  } else {
    state.currentNoteId = null;
    state.currentNote = null;
    renderNotes(space, notes, null, false, { sort: state.sort });
    renderWorkspace();
  }

  await refreshTitles();

  toast(`Moved “${deletedNote.title}” to trash`, {
    icon: 'trash',
    action: 'Undo',
    onAction: async () => {
      await bridge.restore_note(space.id, deletedNote.id);
      await refreshSpaces();
      await refreshTitles();
      if (state.currentSpaceId === space.id && !state.trashMode) {
        await openNote(deletedNote.id);
      } else if (state.trashMode) {
        await refreshTrash();
      }
    }
  });
}

/* =================================================================
   Trash View
   ================================================================= */

async function refreshTrash() {
  const space = getActiveSpace();
  if (!space || space.locked) return;
  const trash = asList(await bridge.list_trash(space.id));
  state.currentTrash = Array.isArray(trash) ? trash : [];
  renderTrash(space, state.currentTrash);
}

async function toggleTrashMode() {
  const space = getActiveSpace();
  if (!space || space.locked) return;
  if (!(await flushSave())) return;
  state.trashMode = !state.trashMode;
  if (state.trashMode) {
    await refreshTrash();
    state.currentNoteId = null;
    state.currentNote = null;
    renderWorkspace();
  } else {
    const searchInput = document.getElementById('search');
    if (searchInput) searchInput.value = '';
    await refreshNoteList(false);
    const first = state.currentNotes[0];
    if (first) {
      state.currentNoteId = first.id;
      await loadAndDisplayNote(first.id);
      renderNotes(space, state.currentNotes, first.id, false, { sort: state.sort });
    } else {
      renderWorkspace();
    }
  }
}

async function restoreTrashedNote(noteId) {
  const space = getActiveSpace();
  const res = await bridge.restore_note(space.id, noteId);
  if (res.error) {
    toast(res.message, { icon: 'trash' });
    return;
  }
  await refreshSpaces();
  await refreshTrash();
  // A restored note can be linked to again straight away.
  await refreshTitles();
  toast(`Restored “${res.note?.title || 'note'}”`, { icon: 'undo' });
}

async function purgeTrashedNote(noteId) {
  const space = getActiveSpace();
  const entry = state.currentTrash.find(t => t.id === noteId);
  const ok = await confirmAction({
    title: 'Delete forever?',
    message: `“${entry?.title || 'This note'}” will be permanently deleted. This cannot be undone.`,
    confirmLabel: 'Delete forever',
    danger: true,
    iconName: 'trash'
  });
  if (!ok) return;
  const res = await bridge.purge_note(space.id, noteId);
  if (res.error) {
    toast(res.message, { icon: 'trash' });
    return;
  }
  await refreshTrash();
  toast(`Permanently deleted “${res.title || 'note'}”`, { icon: 'trash' });
}

async function emptyTrash() {
  const space = getActiveSpace();
  if (!state.currentTrash.length) return;
  const ok = await confirmAction({
    title: 'Empty trash?',
    message: `${state.currentTrash.length} note${state.currentTrash.length === 1 ? '' : 's'} in ${space.name} will be permanently deleted. This cannot be undone.`,
    confirmLabel: 'Empty trash',
    danger: true,
    iconName: 'trash'
  });
  if (!ok) return;
  const res = await bridge.empty_trash(space.id);
  if (res.error) {
    toast(res.message, { icon: 'trash' });
    return;
  }
  await refreshTrash();
  toast(`Emptied trash (${res.purged} note${res.purged === 1 ? '' : 's'})`, { icon: 'trash' });
}

/* =================================================================
   Move / Import / Export
   ================================================================= */

async function moveCurrentNote() {
  const space = getActiveSpace();
  const note = state.currentNote;
  if (!note || space.locked || state.trashMode) return;
  if (!(await flushSave())) return;

  // Counted from the link indexes, the same numbers Python reports after the
  // move: the notes linking here (for a Plain note also the [[Plain:Title]]
  // links in AI-Notes and unlocked vaults) and the notes this one links to.
  const linked = await bridge.count_links_to(space.id, note.id);
  const linkInfo = await bridge.note_links(space.id, note.id);
  const incoming = linked?.count || 0;
  const outgoing = Array.isArray(linkInfo?.outgoing)
    ? linkInfo.outgoing.filter(link => link.resolved).length
    : 0;
  const lockedVaults = Array.isArray(linked?.locked) ? linked.locked.map(vault => vault.name) : [];

  const targetId = await moveDialog?.open({
    spaces: state.spaces,
    currentSpaceId: space.id,
    noteTitle: note.title,
    incomingLinks: incoming,
    outgoingLinks: outgoing,
    lockedVaults
  });
  if (!targetId) return;

  const res = await bridge.move_note(space.id, note.id, targetId);
  if (res.error) {
    toast(res.message, { icon: 'move' });
    return;
  }

  const target = state.spaces.find(s => s.id === targetId);
  await refreshSpaces();
  await selectSpace(targetId, res.new_id, false);
  await refreshTitles();
  pushHistory();

  const broken = res.broken_links || 0;
  const brokenMsg = broken ? ` ${broken} link${broken === 1 ? '' : 's'} broke.` : '';
  toast(`Moved “${res.title}” to ${target?.name || 'new space'}.${brokenMsg}`, {
    icon: 'move',
    action: 'Undo',
    onAction: async () => {
      const undo = await bridge.move_note(targetId, res.new_id, space.id);
      await refreshSpaces();
      if (undo.error) {
        toast(undo.message, { icon: 'move' });
        return;
      }
      await selectSpace(space.id, undo.new_id, false);
    }
  });
}

async function importNotes() {
  const space = getActiveSpace();
  if (!space || space.locked) {
    toast(`Unlock ${space?.name || 'the vault'} first`, { icon: 'lock' });
    return;
  }
  const res = await bridge.import_notes(space.id);
  if (res.error === 'cancelled' || res.error === 'key_not_found') return;
  if (res.error) {
    toast(res.message, { icon: 'upload' });
    return;
  }
  const imported = res.imported || [];
  const skipped = res.skipped || [];
  await refreshSpaces();
  await refreshTitles();
  if (state.trashMode) state.trashMode = false;
  const searchInput = document.getElementById('search');
  if (searchInput) searchInput.value = '';
  if (imported.length) {
    await selectSpace(space.id, imported[0].id, false);
  } else {
    await refreshNoteList(false);
  }
  let msg = imported.length
    ? `Imported ${imported.length} note${imported.length === 1 ? '' : 's'}`
    : 'Nothing was imported';
  if (skipped.length) msg += ` (${skipped.length} skipped)`;
  toast(msg, { icon: 'upload' });
}

async function exportCurrentNote() {
  const space = getActiveSpace();
  const note = state.currentNote;
  if (!note || space.locked || state.trashMode) return;
  if (!(await flushSave())) return;
  if (space.kind === 'vault') {
    const ok = await confirmAction({
      title: 'Export unencrypted copy?',
      message: 'This saves an unencrypted copy of the note that anyone can read.',
      confirmLabel: 'Export',
      danger: false,
      iconName: 'download'
    });
    if (!ok) return;
  }
  const res = await bridge.export_note(space.id, note.id);
  if (res.error === 'cancelled') return;
  if (res.error) {
    toast(res.message, { icon: 'download' });
    return;
  }
  toast(`Exported ${res.name}`, { icon: 'download' });
}

function handleEditorChange(newBody) {
  if (!state.currentNote || getActiveSpace().locked) return;

  state.currentNote.body = newBody;
  setSavingState(true);

  // Debounced live preview update (~300 ms after typing stops)
  clearTimeout(previewDebounceTimer);
  previewDebounceTimer = setTimeout(() => {
    if (!state.currentNote) return;
    renderPreview(state.currentSpaceId, newBody);
    const backlinksContainer = document.getElementById('backlinks');
    renderBacklinks(backlinksContainer, state.currentNote.backlinks, state.currentNote.title, (id) => openNote(id));
  }, 300);

  // Auto-save 1 s after typing stops, to the note this text belongs to.
  queueEdit(newBody);
}

/* =================================================================
   Lock / Unlock Flow
   ================================================================= */

/**
 * Show the M10 graph of the space on screen.  The data comes from the engine,
 * so a locked vault has nothing to draw and no title can leak.
 */
function openGraphForCurrentSpace() {
  const space = getActiveSpace();
  if (!space || !graphOverlay) return;
  graphOverlay.open(space.id, space);
}

function openUnlockDialogForSpace(spaceId) {
  const space = state.spaces.find(s => s.id === spaceId);
  if (!space) return;
  unlockDialog?.open(space);
}

async function handleUnlockComplete(spaceId, passphrase = '') {
  const space = state.spaces.find(s => s.id === spaceId);
  if (!space) return { error: 'invalid_space', message: 'Space not found' };

  // A wrapped key file (section 4.5) needs its passphrase; it is passed straight
  // to Python and never stored or echoed by the UI.
  const result = await bridge.unlock_vault(spaceId, passphrase || '');
  if (result?.error || result?.ok === false) {
    return result;
  }

  state.locksAt = result?.locks_at ?? state.locksAt;
  const refreshed = await bridge.get_state();
  state.spaces = refreshed.spaces;
  state.locksAt = refreshed.locks_at ?? state.locksAt;

  await selectSpace(space.id);
  await refreshTitles();

  // Signature decrypt reveal animation
  const noteTitleEls = document.querySelectorAll('#notes .note .t');
  noteTitleEls.forEach((el, i) => {
    scramble(el, el.textContent, i * 70);
  });

  const ws = document.getElementById('workspace');
  if (ws) replay(ws, 'revealing');

  const unlockedSpace = state.spaces.find(s => s.id === spaceId) || space;
  const count = result.count ?? unlockedSpace.note_count ?? 0;
  toast(`${unlockedSpace.name} unlocked. ${count} notes decrypted in memory only.`, { icon: 'unlock' });
  // Damaged files were skipped instead of losing the whole vault (M7).
  showWarnings(result.warnings);
  return result;
}

async function lockAllVaults(auto = false) {
  const openVaults = state.spaces.filter(s => s.kind === 'vault' && !s.locked);
  if (!openVaults.length) {
    if (!auto) toast('All vaults are already locked', { icon: 'lock' });
    return;
  }
  // Save the last keystrokes before the key is dropped.  A manual lock waits
  // for a failed save to be sorted out; the auto-lock locks regardless.
  if (!(await flushSave()) && !auto) {
    toast('Your last change could not be saved, so the vaults stay unlocked for now.', { icon: 'alert' });
    return;
  }

  const active = getActiveSpace();
  if (active.kind === 'vault' && !active.locked && !isCalm()) {
    const noteEls = document.querySelectorAll('#notes .note .t, #notes .note .s');
    noteEls.forEach((el, i) => scrambleOut(el, i * 20));

    const ws = document.getElementById('workspace');
    if (ws) ws.classList.add('sealing');
    await new Promise(r => setTimeout(r, 480));
    if (ws) ws.classList.remove('sealing');
  }

  const lockResult = await bridge.lock_all();
  state.locksAt = null;
  state.spaces.forEach(s => {
    if (s.kind === 'vault') s.locked = true;
  });

  renderSpaces(state.spaces, state.currentSpaceId);
  if (active.kind === 'vault') {
    state.currentNoteId = null;
    state.currentNote = null;
    state.trashMode = false;
    setEditorContent('');
    renderPreview(active.id, '');
    renderBacklinks(document.getElementById('backlinks'), [], '', () => {});
    renderNotes(active, [], null, false, { sort: state.sort });
    renderWorkspace();
  }

  await refreshTitles();
  toast('All vaults locked. Decrypted notes removed from memory.', { icon: 'lock' });
}

/* =================================================================
   Python-originated lock events
   ================================================================= */

function handleVaultLockedEvent(data = {}) {
  const ids = data.space_ids || (data.space_id ? [data.space_id] : []);
  if (!ids.length) return;
  // A locked vault can take no more saves; its editor is about to be cleared.
  if (pendingSave && ids.includes(pendingSave.spaceId)) {
    pendingSave = null;
    clearTimeout(saveTimer);
    saveTimer = null;
  }
  // Titles of a locked vault must disappear from the suggestions at once.
  refreshTitles();
  ids.forEach(id => {
    const space = state.spaces.find(s => s.id === id);
    if (space) space.locked = true;
  });
  state.locksAt = null;
  renderSpaces(state.spaces, state.currentSpaceId);
  const active = getActiveSpace();
  if (active?.kind === 'vault' && active.locked) {
    state.currentNoteId = null;
    state.currentNote = null;
    state.currentNotes = [];
    state.trashMode = false;
    setEditorContent('');
    renderPreview(active.id, '');
    renderBacklinks(document.getElementById('backlinks'), [], '', () => {});
    renderNotes(active, [], null, false, { sort: state.sort });
    renderWorkspace();
  }
}

/* =================================================================
   Drive Backup (M8: one-way backup of the notes folder)
   ================================================================= */

/** "10:02" for today, "3 Sep 10:02" otherwise - the status bar has no room. */
function formatBackupTime(stamp) {
  if (!stamp) return 'never';
  const date = new Date(stamp);
  if (Number.isNaN(date.getTime())) return String(stamp);
  const clock = date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  const today = new Date();
  const sameDay = date.toDateString() === today.toDateString();
  return sameDay ? clock : `${date.toLocaleDateString([], { day: 'numeric', month: 'short' })} ${clock}`;
}

/** The Drive card, the status bar and Settings all read the same snapshot. */
function applyBackupState(backup) {
  if (backup) state.backup = { ...state.backup, ...backup };
  state.lastBackup = state.backup.last_backup ?? null;
  state.isBackingUp = Boolean(state.backup.running);
  paintBackupIdle();
}

/** Idle wording for the sidebar card and the status bar. */
function paintBackupIdle() {
  const { connected, last_backup: last, enabled } = state.backup;
  if (!connected) {
    updateDriveCard('Not connected', { off: true });
    updateBackupProgress('Drive not connected', null);
    return;
  }
  const when = formatBackupTime(last);
  updateDriveCard(`Backed up ${when}`);
  updateBackupProgress(`Backed up ${when}${enabled ? '' : ' · auto off'}`, null);
}

function handleBackupProgress(data = {}) {
  const total = Number(data.total) || 0;
  const done = Number(data.done) || 0;
  state.isBackingUp = true;
  const fraction = total > 0 ? Math.min(1, done / total) : null;
  updateBackupProgress(data.label || (data.kind === 'restore' ? 'Restoring…' : 'Backing up…'), fraction);
  updateDriveCard(data.kind === 'restore' ? 'Restoring…' : 'Backing up…', { busy: true });
}

async function handleBackupDone(data = {}) {
  state.isBackingUp = false;
  const isRestore = data.kind === 'restore';
  const refreshed = await bridge.get_state();
  applyBackupState(refreshed.backup);
  if (data.ok === false) {
    // Section 8.2: a failed run is a toast plus a status line, never a crash.
    toast(data.message || `The ${isRestore ? 'restore' : 'backup'} did not finish.`, { icon: 'alert' });
    updateBackupProgress(isRestore ? 'Restore failed' : 'Backup failed', null);
    updateDriveCard(state.backup.connected ? `Backed up ${formatBackupTime(state.backup.last_backup)}` : 'Not connected');
    settingsOverlay?.paintDrive?.(state.backup);
    return;
  }
  settingsOverlay?.paintDrive?.(state.backup);
  if (isRestore) {
    const count = Number(data.restored) || 0;
    toast(`Restored ${count} file${count === 1 ? '' : 's'}. Unlock a vault with its key file to read those notes.`, { icon: 'cloud' });
    await selectSpace(state.currentSpaceId, null, false);
  } else {
    const skipped = Number(data.skipped) || 0;
    const moved = (Number(data.uploaded) || 0) + (Number(data.updated) || 0);
    toast(moved ? `Drive: ${moved} file${moved === 1 ? '' : 's'} sent, ${skipped} already up to date. Key files stay home.`
                : 'Drive was already up to date.', { icon: 'check' });
  }
}

async function runBackup() {
  if (state.isBackingUp) {
    toast('A backup or restore is already running', { icon: 'cloud' });
    return;
  }
  const res = await bridge.backup_now();
  if (res?.error) {
    if (res.error === 'not_connected') {
      const ok = await confirmAction({
        title: 'Connect Google Drive first',
        message: 'VaultNotes needs a one-time Google sign-in before it can back up your notes folder.',
        confirmLabel: 'Open settings',
        cancelLabel: 'Later',
        iconName: 'cloud'
      });
      if (ok) settingsOverlay?.open();
      return;
    }
    toast(res.message || 'The backup could not start', { icon: 'alert' });
    return;
  }
  // The engine reports progress through backup_progress / backup_done.
  state.isBackingUp = true;
  updateBackupProgress('Starting…', 0);
  updateDriveCard('Starting…');
}

/**
 * Point the app at another notes folder (Settings, the setup dialog and the
 * "not in this notes folder" screen all use this).  The engine locks every
 * vault and rebuilds its stores, so the workspace starts over in Plain.
 * The answer carries `needs_setup` for the new folder.
 */
async function chooseNotesFolder() {
  if (!(await flushSave())) {
    return { error: 'unsaved', message: 'Your last change could not be saved yet, so the folder was not changed.' };
  }
  const result = await bridge.choose_notes_folder();
  if (result?.error) {
    if (result.error !== 'cancelled') toast(result.message, { icon: 'folder' });
    return result;
  }
  showWarnings(result.warnings);
  clearTimeout(saveTimer);
  saveTimer = null;
  pendingSave = null;
  state.currentNoteId = null;
  state.currentNote = null;
  state.currentNotes = [];
  state.trashMode = false;
  state.history = [];
  state.historyIndex = -1;
  state.locksAt = null;
  const refreshed = await bridge.get_state();
  state.spaces = refreshed.spaces;
  state.notesRoot = refreshed.notes_root || '';
  renderSpaces(state.spaces, state.currentSpaceId);
  await selectSpace('plain', null, false);
  pushHistory();

  const folder = result.name || state.notesRoot;
  const vaults = state.spaces.filter(s => s.kind === 'vault');
  const found = vaults.filter(s => s.created !== false).map(s => s.name);
  let message = `Notes folder: ${folder}.`;
  if (found.length === vaults.length) message += ` Found ${found.join(' and ')}; unlock each with its key file.`;
  else if (found.length) message += ` Found ${found.join(' and ')}; the other vault is not set up here.`;
  else message += ' It has no vaults yet.';
  toast(message, { icon: 'folder' });
  return { ...result, needs_setup: Boolean(refreshed.needs_setup) };
}

async function chooseClientSecret() {
  const res = await bridge.choose_client_secret();
  if (res?.error) {
    if (res.error !== 'cancelled') toast(res.message || 'That file was not accepted', { icon: 'alert' });
  } else {
    toast('Google client file saved. Now connect Google Drive.', { icon: 'check' });
    applyBackupState(res.backup);
  }
  return res;
}

async function connectDrive() {
  const res = await bridge.connect_drive();
  if (res?.error) {
    toast(res.message || 'Google sign-in did not complete', { icon: 'alert' });
  } else {
    toast('Google Drive connected. Key files are never uploaded.', { icon: 'check' });
  }
  const refreshed = await bridge.get_state();
  applyBackupState(refreshed.backup);
  return res;
}

async function disconnectDrive() {
  const res = await bridge.disconnect_drive();
  if (res?.error) toast(res.message || 'Could not disconnect', { icon: 'alert' });
  else toast('Disconnected. Your notes stay on this computer.', { icon: 'cloud' });
  const refreshed = await bridge.get_state();
  applyBackupState(refreshed.backup);
  return res;
}

async function restoreFromDrive() {
  const ok = await confirmAction({
    title: 'Restore from Google Drive?',
    message: 'Choose an empty folder. VaultNotes downloads the backup there and points the app at it; vault notes stay encrypted until you load their key files.',
    confirmLabel: 'Choose folder',
    iconName: 'upload'
  });
  if (!ok) return null;
  const res = await bridge.restore_from_drive();
  if (res?.error) {
    if (res.error !== 'cancelled') toast(res.message || 'The restore could not start', { icon: 'alert' });
    return res;
  }
  state.isBackingUp = true;
  updateBackupProgress('Restoring…', 0);
  return res;
}

async function pruneDrive() {
  const ok = await confirmAction({
    title: 'Delete old files from Drive?',
    message: 'VaultNotes will remove the Drive copies of notes you deleted on this computer more than 30 days ago. Files you kept are untouched.',
    confirmLabel: 'Clean up Drive',
    danger: true,
    iconName: 'trash'
  });
  if (!ok) return null;
  const res = await bridge.prune_drive_backup();
  if (res?.error) toast(res.message || 'The cleanup could not run', { icon: 'alert' });
  else toast(res.removed ? `Removed ${res.removed} old file${res.removed === 1 ? '' : 's'} from Drive.` : 'Nothing was old enough to remove.', { icon: 'check' });
  return res;
}

async function saveBackupSettings(changes) {
  const res = await bridge.update_settings({ backup: changes });
  if (res?.error) toast(res.message || 'Those backup settings were not accepted', { icon: 'alert' });
  const refreshed = await bridge.get_state();
  applyBackupState(refreshed.backup);
  return res;
}

/* =================================================================
   Command Palette Provider
   ================================================================= */

async function getPaletteCommands() {
  const space = getActiveSpace();
  const hasOpenNote = !!state.currentNote && !space?.locked && !state.trashMode;
  const commands = [
    { label: 'New note', hint: 'Ctrl N', icon: 'plus', run: () => createNote() },
    ...(hasOpenNote ? [
      { label: `Move “${state.currentNote.title}” to another space…`, sub: space.name, icon: 'move', run: () => moveCurrentNote() },
      { label: `Export “${state.currentNote.title}” to .md`, sub: space.name, icon: 'download', run: () => exportCurrentNote() }
    ] : []),
    ...(!space?.locked ? [
      { label: `Import .md files into ${space?.name || 'this space'}…`, icon: 'upload', run: () => importNotes() },
      {
        label: state.trashMode ? `Back to ${space?.name || ''} notes` : `Show ${space?.name || ''} trash`,
        icon: 'trash',
        run: () => toggleTrashMode()
      },
      {
        label: state.sort === 'title' ? 'Sort: Modified (newest first)' : 'Sort: Title (A–Z)',
        icon: 'sort',
        run: () => toggleSort()
      }
    ] : []),
    { label: 'Lock all vaults', hint: 'Ctrl L', icon: 'lock', run: () => lockAllVaults() },
    { label: 'Back up now', hint: 'Ctrl B', icon: 'cloud', run: () => runBackup() },
    ...(state.backup.connected ? [
      {
        label: state.backup.enabled ? 'Auto-backup: every ' + state.backup.interval_minutes + ' min' : 'Auto-backup: off',
        sub: 'Google Drive',
        icon: 'clock',
        run: () => settingsOverlay?.open()
      },
      { label: 'Restore from Drive…', sub: 'into an empty folder', icon: 'upload', run: () => restoreFromDrive() },
      { label: 'Clean up Drive…', sub: 'delete files you removed over 30 days ago', icon: 'trash', run: () => pruneDrive() },
      { label: 'Disconnect Google Drive', icon: 'cloud', run: () => disconnectDrive() }
    ] : [
      {
        label: 'Choose client_secret.json…',
        sub: 'the Google Cloud file Drive sign-in needs',
        icon: 'file',
        run: () => chooseClientSecret()
      },
      { label: 'Connect Google Drive', sub: 'one-time sign-in', icon: 'cloud', run: () => connectDrive() }
    ]),
    { label: 'Switch view: Edit / Split / Preview', hint: 'Ctrl E', icon: 'columns', run: () => cycleViewMode() },
    {
      label: 'Link graph for this space',
      sub: 'Notes as dots, joined by their links',
      icon: 'graph',
      run: () => openGraphForCurrentSpace()
    },
    ...THEMES.map(t => ({
      label: `Theme: ${t.name}`,
      icon: 'sparkle',
      run: () => setTheme(t.id)
    })),
    ...FX_LEVELS.map(f => ({
      label: `Effects: ${f.label}`,
      icon: 'eye',
      run: () => setFx(f.id)
    })),
    {
      label: 'Settings…',
      icon: 'settings',
      run: () => settingsOverlay?.open()
    },
    {
      label: 'Choose notes folder…',
      sub: state.notesRoot || 'where Plain and the vaults live',
      icon: 'folder',
      run: () => chooseNotesFolder()
    },
    ...(state.spaces.some(s => s.created === false) ? [{
      label: 'Create vaults…',
      sub: 'only if you have none yet: new vaults start empty',
      icon: 'key',
      run: () => vaultSetupDialog?.open()
    }] : [])
  ];

  for (const s of state.spaces) {
    if (s.created === false) {
      // Not in this notes folder: there is nothing to unlock, so open the
      // space, whose screen explains it and offers the folder or setup.
      commands.push({
        label: `${s.name} is not in this notes folder`,
        sub: 'Choose the notes folder, or create vaults',
        icon: 'folder',
        colorVar: s.colorVar,
        run: () => selectSpace(s.id)
      });
      continue;
    }
    if (s.locked) {
      commands.push({
        label: `Unlock ${s.name}`,
        sub: 'Encrypted vault',
        icon: 'key',
        colorVar: s.colorVar,
        run: () => openUnlockDialogForSpace(s.id)
      });
      // Locked vault titles never appear here (security rule 11).
      continue;
    }
    if (s.kind === 'vault') {
      commands.push({
        label: `Lock ${s.name}`,
        sub: 'Remove decrypted notes from memory',
        icon: 'lock',
        colorVar: s.colorVar,
        run: async () => {
          if (!(await flushSave())) {
            toast(`Your last change could not be saved, so ${s.name} stays unlocked for now.`, { icon: 'alert' });
            return;
          }
          await bridge.lock_vault(s.id);
          handleVaultLockedEvent({ space_id: s.id });
          toast(`Locked ${s.name}. Decrypted notes removed from memory.`, { icon: 'lock' });
        }
      });
    }
    const notes = (s.id === state.currentSpaceId && state.currentNotes.length)
      ? state.currentNotes
      : asList(await bridge.list_notes(s.id, '', state.sort));
    (Array.isArray(notes) ? notes : []).forEach(n => {
      commands.push({
        label: n.title,
        sub: s.name,
        icon: spaceIcon(s),
        colorVar: s.colorVar,
        run: () => selectSpace(s.id, n.id)
      });
    });
  }

  return commands;
}

/* =================================================================
   Global Shortcuts & Activity Tracking
   ================================================================= */

function setupShortcuts() {
  const onActivity = () => {
    state.lastActivity = Date.now();
    const hasOpenVault = state.spaces.some(s => s.kind === 'vault' && !s.locked);
    if (hasOpenVault && Date.now() - lastTouchSent >= 15000) {
      lastTouchSent = Date.now();
      bridge.touch().then(result => {
        if (result && Object.prototype.hasOwnProperty.call(result, 'locks_at')) {
          state.locksAt = result.locks_at;
        }
      }).catch(() => {});
    }
  };

  ['keydown', 'mousedown', 'mousemove', 'wheel'].forEach(ev => {
    window.addEventListener(ev, onActivity, { passive: true });
  });

  window.addEventListener('keydown', async (e) => {
    const mod = e.ctrlKey || e.metaKey;
    const key = e.key.toLowerCase();

    if (mod && key === 'k') {
      e.preventDefault();
      commandPalette?.open();
    } else if (mod && key === 'l') {
      e.preventDefault();
      lockAllVaults();
    } else if (mod && key === 'e') {
      e.preventDefault();
      cycleViewMode();
    } else if (mod && key === 'n') {
      e.preventDefault();
      createNote();
    } else if (mod && key === 'b') {
      e.preventDefault();
      runBackup();
    } else if (mod && key === 's') {
      e.preventDefault();
      if (state.currentNote && !getActiveSpace().locked) {
        queueEdit(getEditorContent());
        if (await saveNow({ refreshList: true })) toast('Saved', { icon: 'check' });
      }
    } else if (mod && key === 'f') {
      e.preventDefault();
      const searchInput = document.getElementById('search');
      if (searchInput && !searchInput.disabled) {
        searchInput.focus();
        searchInput.select();
      }
    } else if (e.altKey && e.key === 'ArrowLeft') {
      e.preventDefault();
      if (state.historyIndex > 0) {
        state.historyIndex--;
        jumpHistory(state.history[state.historyIndex]);
      }
    } else if (e.altKey && e.key === 'ArrowRight') {
      e.preventDefault();
      if (state.historyIndex < state.history.length - 1) {
        state.historyIndex++;
        jumpHistory(state.history[state.historyIndex]);
      }
    } else if (e.key === 'Escape') {
      document.querySelectorAll('.overlay.open').forEach(el => el.classList.remove('open'));
    }
  });

  // Title input shortcuts
  const titleInput = document.getElementById('title');
  if (titleInput) {
    titleInput.addEventListener('change', () => renameNote(titleInput.value));
    titleInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        focusEditor();
      } else if (e.key === 'Escape') {
        if (state.currentNote) titleInput.value = state.currentNote.title;
        titleInput.blur();
      }
    });
  }

  // Workspace actions
  document.getElementById('back')?.addEventListener('click', () => {
    if (state.historyIndex > 0) {
      state.historyIndex--;
      jumpHistory(state.history[state.historyIndex]);
    }
  });

  document.getElementById('fwd')?.addEventListener('click', () => {
    if (state.historyIndex < state.history.length - 1) {
      state.historyIndex++;
      jumpHistory(state.history[state.historyIndex]);
    }
  });

  document.getElementById('move')?.addEventListener('click', () => {
    moveCurrentNote();
  });

  document.getElementById('export')?.addEventListener('click', () => {
    exportCurrentNote();
  });

  document.getElementById('trash')?.addEventListener('click', () => {
    deleteNote();
  });
}

function startAutoLockTimer() {
  setInterval(() => {
    const openVaults = state.spaces.filter(s => s.kind === 'vault' && !s.locked);
    const totalMs = state.autolockMinutes * 60 * 1000;
    if (!openVaults.length || !state.locksAt) {
      updateLockCountdown([], 0, totalMs);
      return;
    }

    const remaining = Math.max(0, state.locksAt - Date.now());
    if (remaining === 0) {
      // Python is the authority and normally sends vault_locked first.  This
      // is only a UI fallback if a bridge event is delayed.
      lockAllVaults(true);
      return;
    }
    updateLockCountdown(openVaults, remaining, totalMs);
  }, 1000);
}

/* =================================================================
   Initialization
   ================================================================= */

/* Closing the window: app.py holds the close back, calls this, and closes once
   ready_to_close() arrives (or after a few seconds), so the last keystrokes
   are saved instead of lost. */
window.vn.flushBeforeClose = async () => {
  try {
    await flushSave();
  } finally {
    await bridge.ready_to_close();
  }
};

/* A link or file dropped on the window would navigate it away from the app.
   Python refuses Bridge calls from any other page, and nothing is dropped. */
['dragover', 'drop'].forEach(type => {
  window.addEventListener(type, (event) => {
    // Dragged text may still go into the editor; files never go anywhere.
    const files = Array.from(event.dataTransfer?.types || []).includes('Files');
    if (files || !event.target?.closest?.('.cm-editor')) event.preventDefault();
  });
});

async function init() {
  initToasts();
  events.on('vault_locked', handleVaultLockedEvent);
  events.on('backup_progress', handleBackupProgress);
  events.on('backup_done', handleBackupDone);
  // Python pushes problems it could not answer a call with (skipped files).
  events.on('error', (data) => {
    if (Array.isArray(data?.warnings)) showWarnings(data.warnings);
    else if (data?.message) toast(String(data.message), { icon: 'alert' });
  });

  // Load state from bridge
  const appState = await bridge.get_state();
  state.spaces = appState.spaces;
  state.theme = appState.look?.theme || 'nebula';
  state.effects = appState.look?.effects || 'full';
  state.viewMode = appState.look?.view_mode || 'split';
  state.editorFontSize = appState.look?.editor_font_size || 13.5;
  state.notesRoot = appState.notes_root || '';
  state.autolockMinutes = appState.autolock_minutes || state.autolockMinutes;
  state.locksAt = appState.locks_at ?? null;
  applyBackupState(appState.backup);
  showWarnings(appState.warnings);

  // Mount Toolbar
  const toolbarContainer = document.getElementById('toolbar');
  const toolbarEl = createToolbar({
    onViewChange: setViewMode,
    onLockAll: () => lockAllVaults(),
    onBackup: runBackup,
    onThemeChange: setTheme,
    onOpenPalette: () => commandPalette?.open()
  });
  toolbarContainer.replaceWith(toolbarEl);

  // Mount Sidebar
  const sidebarContainer = document.getElementById('sidebar');
  const sidebarEl = createSidebar({
    onSelectSpace: (id) => selectSpace(id),
    onNewVault: async () => {
      // The setup dialog creates any missing built-in vaults. When both
      // vaults already exist there is nothing to set up in v1.
      const st = await bridge.get_state();
      state.spaces = st.spaces;
      renderSpaces(state.spaces, state.currentSpaceId);
      if (st.needs_setup) vaultSetupDialog?.open();
      else toast('Encrypted and Personal vaults already exist', { icon: 'shield' });
    },
    onSyncDrive: runBackup,
    onOpenDriveSettings: () => settingsOverlay?.open()
  });
  sidebarContainer.replaceWith(sidebarEl);

  // Mount Note List
  const noteListContainer = document.getElementById('notelist');
  const noteListEl = createNoteList({
    onSelectNote: openNote,
    onNewNote: () => createNote(),
    onSearchInput: async (q) => {
      const space = getActiveSpace();
      if (!space.locked && !state.trashMode) {
        const notes = asList(await bridge.list_notes(space.id, q, state.sort));
        state.currentNotes = notes;
        renderNotes(space, notes, state.currentNoteId, false, { sort: state.sort });
      }
    },
    onSortToggle: () => toggleSort(),
    onImport: () => importNotes(),
    onTrashToggle: () => toggleTrashMode(),
    onRestoreNote: (noteId) => restoreTrashedNote(noteId),
    onPurgeNote: (noteId) => purgeTrashedNote(noteId),
    onEmptyTrash: () => emptyTrash()
  });
  noteListContainer.replaceWith(noteListEl);

  // Mount Status Bar
  const statusBarContainer = document.getElementById('statusbar');
  const statusBarEl = createStatusBar({
    onToggleFx: toggleFx,
    onCycleTheme: cycleTheme
  });
  statusBarContainer.replaceWith(statusBarEl);

  // Mount Sealed and Empty heroes into workspace
  const sealedHeroContainer = document.getElementById('sealed-hero-container');
  const sealedHeroEl = createSealedHero({
    onUnlock: () => openUnlockDialogForSpace(state.currentSpaceId),
    onChooseFolder: () => chooseNotesFolder(),
    onSetup: () => vaultSetupDialog?.open()
  });
  sealedHeroContainer.replaceWith(sealedHeroEl);

  const emptyHeroContainer = document.getElementById('empty-hero-container');
  const emptyHeroEl = createEmptyHero({
    onNewNote: () => createNote()
  });
  emptyHeroContainer.replaceWith(emptyHeroEl);

  // Initialize CodeMirror 6 Editor
  const cmContainer = document.getElementById('cm-editor');
  initEditor(cmContainer, {
    onChange: handleEditorChange,
    onScroll: (top) => scrollPreviewTo(top),
    // The full title list for this space (bridge.list_titles), not the notes
    // that happen to match the current search filter.
    getTitles: () => {
      const space = getActiveSpace();
      return space && !space.locked ? state.titles : [];
    },
    // Only ever Plain, for the [[Plain:Title]] form (M10).
    getPlainTitles: () => state.plainTitles,
    // M10: Ctrl+click (Cmd+click) a [[link]] in the editor to open it.
    onLinkClick: (link) => openLinkTarget(link.target, {
      space: link.space || '',
      heading: link.heading || ''
    })
  });

  // Initialize Markdown Preview
  const previewContainer = document.getElementById('preview');
  initPreview(previewContainer, {
    onOpenNoteByTitle: (targetTitle, options = {}) => openLinkTarget(targetTitle, options),
    onCreateNotePrompt: (title) => createNoteFromLink(title),
    // A link to a note that is missing in ANOTHER space: report it, never create
    // it here, because a note belongs to exactly one space (M10).
    onMissingNoteInOtherSpace: (title, options = {}) => {
      const name = state.spaces.find(sp => sp.id === options.space)?.name || 'that space';
      toast(`“${title}” is not in ${name}. Notes are never created across spaces.`, { icon: 'alert' });
    },
    onExternalLink: (url) => {
      bridge.open_external(url);
      toast(`Opens ${url} in your browser`, { icon: 'link' });
    }
  });

  // Mount Overlays (Unlock dialog, Command Palette, Settings, graph)
  const overlaysRoot = document.getElementById('overlays-root');

  graphOverlay = createGraphOverlay({
    onOpenNote: async (noteId, spaceId) => {
      graphOverlay.close();
      if (spaceId && spaceId !== state.currentSpaceId) {
        await selectSpace(spaceId, noteId);
      } else {
        await openNote(noteId);
      }
    }
  });
  overlaysRoot.appendChild(graphOverlay.element);

  unlockDialog = createUnlockDialog({
    onUnlockComplete: handleUnlockComplete,
    onBrowseKey: (spaceId) => bridge.choose_key_file(spaceId)
  });
  overlaysRoot.appendChild(unlockDialog.element);

  vaultSetupDialog = createVaultSetupDialog({
    // The same folder switch as Settings: it resets the workspace, so the
    // notes on screen are the chosen folder's, not the previous one's.
    onChooseFolder: () => chooseNotesFolder(),
    onSetup: async (passphrases) => {
      const result = await bridge.initialize_vaults(passphrases);
      if (result?.error) {
        if (result.error !== 'cancelled') toast(result.message || 'Vault setup could not be completed.', { icon: 'alert' });
        return result;
      }
      const refreshed = await bridge.get_state();
      state.spaces = refreshed.spaces;
      renderSpaces(state.spaces, state.currentSpaceId);
      const active = getActiveSpace();
      if (active?.kind === 'vault') updateSealedDetails(active, state.notesRoot);
      const made = (result.created || []).map(id => state.spaces.find(s => s.id === id)?.name || id);
      toast(
        made.length
          ? `Created ${made.join(' and ')}. Back up the key files, then unlock each vault with its key.`
          : 'Both vaults already exist in this notes folder. Unlock each with its key file.',
        { icon: 'shield' }
      );
      return result;
    }
  });
  overlaysRoot.appendChild(vaultSetupDialog.element);

  commandPalette = createCommandPalette({
    getCommands: getPaletteCommands
  });
  overlaysRoot.appendChild(commandPalette.element);

  moveDialog = createMoveDialog();
  overlaysRoot.appendChild(moveDialog.element);

  settingsOverlay = createSettingsOverlay({
    getSettings: () => ({
      look: { theme: state.theme, effects: state.effects, editor_font_size: state.editorFontSize },
      autolock_minutes: state.autolockMinutes,
      notes_root: state.notesRoot
    }),
    getBackup: () => state.backup,
    onSaveBackup: (changes) => saveBackupSettings(changes),
    onChooseClientSecret: () => chooseClientSecret(),
    onConnectDrive: () => connectDrive(),
    onDisconnectDrive: () => disconnectDrive(),
    onBackupNow: () => runBackup(),
    onRestoreDrive: () => restoreFromDrive(),
    onPruneDrive: () => pruneDrive(),
    onSaveSettings: (changes) => {
      if (changes.look?.theme) setTheme(changes.look.theme, true);
      if (changes.look?.effects) setFx(changes.look.effects, true);
      if (changes.look?.editor_font_size) setEditorFontSize(changes.look.editor_font_size, true);
      if (changes.autolock_minutes) {
        state.autolockMinutes = changes.autolock_minutes;
        bridge.update_settings({ autolock_minutes: changes.autolock_minutes })
          .then(() => bridge.touch())
          .then(result => { state.locksAt = result?.locks_at ?? null; })
          .catch(() => {});
      }
      toast('Settings saved', { icon: 'check' });
    },
    onChooseFolder: () => chooseNotesFolder()
  });
  overlaysRoot.appendChild(settingsOverlay.element);

  // Set initial theme, effects, view
  setTheme(state.theme, true);
  setFx(state.effects, true);
  setEditorFontSize(state.editorFontSize, true);
  setViewMode(state.viewMode);
  paintBackupIdle();

  // Select initial space (Plain space with first note)
  await selectSpace('plain', null, false);
  pushHistory();

  if (appState.needs_setup) {
    vaultSetupDialog?.open();
  }

  // Setup global shortcuts and autolock timer
  setupShortcuts();
  startAutoLockTimer();
}

// Start application when DOM is ready
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
