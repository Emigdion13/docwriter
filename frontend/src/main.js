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

// UI Modules
import { createToolbar, updateToolbarView, updateToolbarTheme } from './ui/toolbar.js';
import { createSidebar, renderSpaces, updateDriveCard } from './ui/sidebar.js';
import { createNoteList, renderNotes, renderTrash } from './ui/noteList.js';
import { confirmAction, createMoveDialog } from './ui/dialogs.js';
import { initEditor, setEditorContent, getEditorContent, focusEditor, setEditorCursorToEnd } from './ui/editor.js';
import { initPreview, renderPreview, scrollPreviewTo } from './ui/preview.js';
import { renderBacklinks } from './ui/backlinks.js';
import { createSealedHero, createEmptyHero, updateSealedDetails } from './ui/sealedVault.js';
import { createUnlockDialog } from './ui/unlockDialog.js';
import { createVaultSetupDialog } from './ui/vaultSetup.js';
import { createCommandPalette } from './ui/commandPalette.js';
import { createSettingsOverlay } from './ui/settings.js';
import { toast, initToasts } from './ui/toasts.js';
import {
  createStatusBar,
  setSavingState,
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
  isBackingUp: false,
  lastBackup: null
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
  const notes = await bridge.list_notes(space.id, document.getElementById('search')?.value || '', state.sort);
  state.currentNotes = notes;
  renderNotes(space, notes, state.currentNoteId, animate, { sort: state.sort });
}

/* =================================================================
   Space & Note Navigation
   ================================================================= */

async function flushSave() {
  if (saveTimer && state.currentNote && !getActiveSpace()?.locked) {
    clearTimeout(saveTimer);
    saveTimer = null;
    const body = getEditorContent();
    setSavingState(true);
    const res = await bridge.save_note(state.currentSpaceId, state.currentNote.id, body);
    setSavingState(false);
    if (!res.error) {
      state.currentNote.modified = res.modified;
    }
  }
}

async function selectSpace(spaceId, targetNoteId = null, animate = true) {
  await flushSave();
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
    renderNotes(space, [], null, animate, { sort: state.sort });
    renderWorkspace();
    return;
  }

  const notes = await bridge.list_notes(space.id, searchInput?.value || '', state.sort);
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
  await flushSave();
  state.trashMode = false;
  state.currentNoteId = noteId;
  const space = getActiveSpace();
  const notes = await bridge.list_notes(space.id, document.getElementById('search')?.value || '', state.sort);
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
    updateSealedDetails(space);
    return;
  }

  if (!hasNote) return;

  const note = state.currentNote;

  // Crumb
  const crumb = document.getElementById('crumb');
  if (crumb) {
    crumb.innerHTML = `<span class="crumb-dot"></span><span>${space.name}</span><span class="sep">/</span><span>${note.title}</span>`;
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
  await flushSave();
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

  // Switch view to split if in preview mode
  if (state.viewMode === 'preview') {
    setViewMode('split');
  }

  await openNote(newNote.id);
  focusEditor();
  setEditorCursorToEnd();
  toast(`Created “${newNote.title}”`, { icon: 'plus' });
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

  const res = await bridge.rename_note(space.id, note.id, trimmed, true);
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

  const notes = await bridge.list_notes(space.id, document.getElementById('search')?.value || '', state.sort);
  state.currentNotes = notes;
  renderNotes(space, notes, note.id, false, { sort: state.sort });
  renderWorkspace();

  const msg = res.links_updated
    ? `Renamed. Updated ${res.links_updated} link${res.links_updated > 1 ? 's' : ''} in other notes.`
    : 'Renamed';
  toast(msg, { icon: 'link' });
}

async function deleteNote() {
  const space = getActiveSpace();
  const note = state.currentNote;
  if (!note || space.locked) return;

  await flushSave();

  const res = await bridge.delete_note(space.id, note.id);
  if (res.error) {
    toast(res.message, { icon: 'trash' });
    return;
  }

  const deletedNote = res.deleted;
  const notes = await bridge.list_notes(space.id, document.getElementById('search')?.value || '', state.sort);
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

  toast(`Moved “${deletedNote.title}” to trash`, {
    icon: 'trash',
    action: 'Undo',
    onAction: async () => {
      await bridge.restore_note(space.id, deletedNote.id);
      await refreshSpaces();
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
  const trash = await bridge.list_trash(space.id);
  state.currentTrash = Array.isArray(trash) ? trash : [];
  renderTrash(space, state.currentTrash);
}

async function toggleTrashMode() {
  const space = getActiveSpace();
  if (!space || space.locked) return;
  await flushSave();
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
  await flushSave();

  const links = await bridge.count_links_to(space.id, note.id);
  const incoming = links?.count || 0;
  const outgoing = note.link_count || 0;

  const targetId = await moveDialog?.open({
    spaces: state.spaces,
    currentSpaceId: space.id,
    noteTitle: note.title,
    incomingLinks: incoming,
    outgoingLinks: outgoing
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
  await flushSave();
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

  // Auto-save debounce (1s)
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => {
    if (!state.currentNote) return;
    const res = await bridge.save_note(state.currentSpaceId, state.currentNote.id, newBody);
    setSavingState(false);
    if (!res.error) {
      state.currentNote.modified = res.modified;
      const meta = document.getElementById('meta');
      if (meta) {
        meta.textContent = `Edited ${res.modified} · ${wordCount(newBody)} words`;
      }
      const notes = await bridge.list_notes(state.currentSpaceId, document.getElementById('search')?.value || '', state.sort);
      state.currentNotes = notes;
      renderNotes(getActiveSpace(), notes, state.currentNote.id, false, { sort: state.sort });
    }
  }, 1000);
}

/* =================================================================
   Lock / Unlock Flow
   ================================================================= */

function openUnlockDialogForSpace(spaceId) {
  const space = state.spaces.find(s => s.id === spaceId);
  if (!space) return;
  unlockDialog?.open(space);
}

async function handleUnlockComplete(spaceId) {
  const space = state.spaces.find(s => s.id === spaceId);
  if (!space) return { error: 'invalid_space', message: 'Space not found' };

  const result = await bridge.unlock_vault(spaceId);
  if (result?.error || result?.ok === false) {
    return result;
  }

  state.locksAt = result?.locks_at ?? state.locksAt;
  const refreshed = await bridge.get_state();
  state.spaces = refreshed.spaces;
  state.locksAt = refreshed.locks_at ?? state.locksAt;

  await selectSpace(space.id);

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
  return result;
}

async function lockAllVaults(auto = false) {
  const openVaults = state.spaces.filter(s => s.kind === 'vault' && !s.locked);
  if (!openVaults.length) {
    if (!auto) toast('All vaults are already locked', { icon: 'lock' });
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

  toast('All vaults locked. Decrypted notes removed from memory.', { icon: 'lock' });
}

/* =================================================================
   Python-originated lock events
   ================================================================= */

function handleVaultLockedEvent(data = {}) {
  const ids = data.space_ids || (data.space_id ? [data.space_id] : []);
  if (!ids.length) return;
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
   Drive Backup (M8 Hook)
   ================================================================= */

async function runBackup() {
  if (state.isBackingUp) return;
  state.isBackingUp = true;
  updateBackupProgress('Backing up…', 35);
  toast('Backing up notes to Google Drive…', { icon: 'cloud' });

  setTimeout(async () => {
    const res = await bridge.backup_now();
    state.isBackingUp = false;
    state.lastBackup = res.last_backup || 'just now';
    updateBackupProgress(state.lastBackup, null);
    updateDriveCard(`Backed up ${state.lastBackup}`);
    toast('Backup complete. Key files were not uploaded.', { icon: 'check' });
  }, 1400);
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
    { label: 'Switch view: Edit / Split / Preview', hint: 'Ctrl E', icon: 'columns', run: () => cycleViewMode() },
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
    }
  ];

  for (const s of state.spaces) {
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
          await bridge.lock_vault(s.id);
          handleVaultLockedEvent({ space_id: s.id });
          toast(`Locked ${s.name}. Decrypted notes removed from memory.`, { icon: 'lock' });
        }
      });
    }
    const notes = (s.id === state.currentSpaceId && state.currentNotes.length)
      ? state.currentNotes
      : await bridge.list_notes(s.id, '', state.sort);
    (Array.isArray(notes) ? notes : []).forEach(n => {
      commands.push({
        label: n.title,
        sub: s.name,
        icon: s.kind === 'plain' ? 'file' : 'unlock',
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
        clearTimeout(saveTimer);
        saveTimer = null;
        const body = getEditorContent();
        setSavingState(true);
        const res = await bridge.save_note(state.currentSpaceId, state.currentNote.id, body);
        setSavingState(false);
        if (!res.error) {
          state.currentNote.modified = res.modified;
          const meta = document.getElementById('meta');
          if (meta) {
            meta.textContent = `Edited ${res.modified} · ${wordCount(body)} words`;
          }
          const notes = await bridge.list_notes(state.currentSpaceId, document.getElementById('search')?.value || '', state.sort);
          state.currentNotes = notes;
          renderNotes(getActiveSpace(), notes, state.currentNote.id, false, { sort: state.sort });
          toast('Saved', { icon: 'check' });
        }
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

async function init() {
  initToasts();
  events.on('vault_locked', handleVaultLockedEvent);

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
    onSyncDrive: runBackup
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
        const notes = await bridge.list_notes(space.id, q, state.sort);
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
    onUnlock: () => openUnlockDialogForSpace(state.currentSpaceId)
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
    getTitles: () => {
      const space = getActiveSpace();
      if (space.locked) return [];
      return (state.currentNotes || []).map(n => n.title);
    }
  });

  // Initialize Markdown Preview
  const previewContainer = document.getElementById('preview');
  initPreview(previewContainer, {
    onOpenNoteByTitle: async (targetTitle) => {
      const space = getActiveSpace();
      const notes = await bridge.list_notes(space.id, '', state.sort);
      state.currentNotes = notes;
      const match = notes.find(n => n.title.toLowerCase() === targetTitle.toLowerCase());
      if (match) {
        openNote(match.id);
      }
    },
    onCreateNotePrompt: (title) => {
      toast(`“${title}” isn’t written yet`, {
        icon: 'link',
        action: 'Create note',
        onAction: () => createNote(title)
      });
    },
    onExternalLink: (url) => {
      bridge.open_external(url);
      toast(`Opens ${url} in your browser`, { icon: 'link' });
    }
  });

  // Mount Overlays (Unlock dialog, Command Palette, Settings)
  const overlaysRoot = document.getElementById('overlays-root');

  unlockDialog = createUnlockDialog({
    onUnlockComplete: handleUnlockComplete,
    onBrowseKey: (spaceId) => bridge.choose_key_file(spaceId)
  });
  overlaysRoot.appendChild(unlockDialog.element);

  vaultSetupDialog = createVaultSetupDialog({
    onChooseFolder: () => bridge.choose_notes_folder(),
    onSetup: async () => {
      const result = await bridge.initialize_vaults();
      if (!result?.error) {
        const refreshed = await bridge.get_state();
        state.spaces = refreshed.spaces;
        renderSpaces(state.spaces, state.currentSpaceId);
        updateSealedDetails(getActiveSpace());
      }
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
    onChooseFolder: async () => {
      await flushSave();
      const result = await bridge.choose_notes_folder();
      if (result?.error) {
        if (result.error !== 'cancelled') toast(result.message, { icon: 'folder' });
        return result;
      }
      // The notes root changed: vaults are locked and stores rebuilt.
      // Reset the workspace and start over in Plain.
      clearTimeout(saveTimer);
      saveTimer = null;
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
      toast(`Notes folder: ${result.name || state.notesRoot}`, { icon: 'folder' });
      return result;
    }
  });
  overlaysRoot.appendChild(settingsOverlay.element);

  // Set initial theme, effects, view
  setTheme(state.theme, true);
  setFx(state.effects, true);
  setEditorFontSize(state.editorFontSize, true);
  setViewMode(state.viewMode);
  updateBackupProgress(state.lastBackup || 'never', null);

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
