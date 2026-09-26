/* =================================================================
   BRIDGE API  (frontend/src/bridge.js)
   Wraps window.pywebview.api + event bus.
   Connects the frontend look to the Python engine.
   ================================================================= */

// Event bus
const listeners = new Map();

export const events = {
  on(name, handler) {
    if (!listeners.has(name)) listeners.set(name, new Set());
    listeners.get(name).add(handler);
  },
  off(name, handler) {
    if (listeners.has(name)) listeners.get(name).delete(handler);
  },
  emit(name, data) {
    if (listeners.has(name)) {
      listeners.get(name).forEach(fn => {
        try { fn(data); } catch (err) { console.error('Event listener error:', err); }
      });
    }
  }
};

// Expose event bus to window.vn for Python -> Frontend bridge events
if (typeof window !== 'undefined') {
  window.vn = {
    emit: (name, data) => events.emit(name, data)
  };
}

let pywebviewReadyPromise = null;

/**
 * Wait for pywebview to inject window.pywebview.api.
 */
export function waitForBridge(timeoutMs = 1200) {
  if (typeof window === 'undefined') return Promise.resolve(null);
  if (window.pywebview?.api) return Promise.resolve(window.pywebview.api);
  if (pywebviewReadyPromise) return pywebviewReadyPromise;

  pywebviewReadyPromise = new Promise((resolve) => {
    let done = false;
    const timer = setTimeout(() => {
      if (!done) {
        done = true;
        resolve(window.pywebview?.api || null);
      }
    }, timeoutMs);

    window.addEventListener('pywebviewready', () => {
      if (!done) {
        done = true;
        clearTimeout(timer);
        resolve(window.pywebview?.api || null);
      }
    }, { once: true });
  });

  return pywebviewReadyPromise;
}

/* Fallback mock data used ONLY when viewing frontend in a standalone browser without Python */
const mockSpaces = [
  {
    id: 'plain',
    name: 'Plain',
    kind: 'plain',
    colorVar: '--plain',
    locked: false,
    notes: [
      {
        id: 'Shopping list',
        title: 'Shopping list',
        modified: 'Today 09:12',
        body: `# Shopping list\n\n- [x] Coffee beans\n- [ ] Oat milk\n- [ ] Batteries for the [[Home lab]] sensors\n- [ ] Birthday card for Ana`
      },
      {
        id: 'Home lab',
        title: 'Home lab',
        modified: 'Yesterday',
        body: `# Home lab\n\nIdeas for the little server shelf.\n\n| Device | Status |\n|---|---|\n| Raspberry Pi 5 | running |\n| NAS | ordering |\n| Air sensor | needs batteries |\n\nParts go on the [[Shopping list]].`
      }
    ],
    trash: []
  },
  {
    id: 'encrypted',
    name: 'Encrypted',
    kind: 'vault',
    colorVar: '--encrypted',
    locked: true,
    notes: [],
    trash: []
  },
  {
    id: 'personal',
    name: 'Personal',
    kind: 'vault',
    colorVar: '--personal',
    locked: true,
    notes: [],
    trash: []
  }
];

export const bridge = {
  async get_state() {
    const api = await waitForBridge();
    if (api?.get_state) return await api.get_state();
    return {
      spaces: mockSpaces.map(s => ({
        id: s.id,
        name: s.name,
        kind: s.kind,
        locked: s.locked,
        colorVar: s.colorVar,
        note_count: s.notes.length
      })),
      look: { theme: 'nebula', effects: 'full', view_mode: 'split', editor_font_size: 13.5 },
      last_backup: null
    };
  },

  async list_notes(space_id, query = '', sort = 'modified') {
    const api = await waitForBridge();
    if (api?.list_notes) return await api.list_notes(space_id, query, sort);
    const s = mockSpaces.find(x => x.id === space_id);
    if (!s || s.locked) return [];
    let list = s.notes;
    const q = (query || '').toLowerCase().trim();
    if (q) list = list.filter(n => n.title.toLowerCase().includes(q) || n.body.toLowerCase().includes(q));
    return list.map(n => ({
      id: n.id,
      title: n.title,
      snippet: n.body.slice(0, 80),
      modified: n.modified,
      link_count: 0
    }));
  },

  async open_note(space_id, note_id) {
    const api = await waitForBridge();
    if (api?.open_note) return await api.open_note(space_id, note_id);
    const s = mockSpaces.find(x => x.id === space_id);
    if (!s || s.locked) return { error: 'locked', message: 'Vault is locked' };
    const n = s.notes.find(x => x.id === note_id || x.title === note_id);
    if (!n) return { error: 'not_found', message: 'Note not found' };
    return {
      id: n.id,
      title: n.title,
      body: n.body,
      modified: n.modified,
      backlinks: []
    };
  },

  async create_note(space_id, title = 'Untitled') {
    const api = await waitForBridge();
    if (api?.create_note) return await api.create_note(space_id, title);
    const s = mockSpaces.find(x => x.id === space_id);
    if (!s || s.locked) return { error: 'locked', message: 'Vault is locked' };
    const newNote = {
      id: title,
      title,
      body: `# ${title}\n\n`,
      modified: 'just now',
      snippet: '',
      link_count: 0,
      backlinks: []
    };
    s.notes.unshift(newNote);
    return newNote;
  },

  async save_note(space_id, note_id, body) {
    const api = await waitForBridge();
    if (api?.save_note) return await api.save_note(space_id, note_id, body);
    const s = mockSpaces.find(x => x.id === space_id);
    if (!s || s.locked) return { error: 'locked', message: 'Vault is locked' };
    const n = s.notes.find(x => x.id === note_id);
    if (!n) return { error: 'not_found', message: 'Note not found' };
    n.body = body;
    n.modified = 'just now';
    return { modified: n.modified };
  },

  async rename_note(space_id, note_id, new_title, update_links = true) {
    const api = await waitForBridge();
    if (api?.rename_note) return await api.rename_note(space_id, note_id, new_title, update_links);
    const s = mockSpaces.find(x => x.id === space_id);
    if (!s || s.locked) return { error: 'locked', message: 'Vault is locked' };
    const n = s.notes.find(x => x.id === note_id);
    if (!n) return { error: 'not_found', message: 'Note not found' };
    n.title = new_title;
    n.id = new_title;
    return { title: new_title, id: new_title, links_updated: 0 };
  },

  async count_links_to(space_id, note_id) {
    const api = await waitForBridge();
    if (api?.count_links_to) return await api.count_links_to(space_id, note_id);
    return { count: 0 };
  },

  async delete_note(space_id, note_id) {
    const api = await waitForBridge();
    if (api?.delete_note) return await api.delete_note(space_id, note_id);
    const s = mockSpaces.find(x => x.id === space_id);
    if (!s) return { error: 'not_found', message: 'Space not found' };
    const idx = s.notes.findIndex(x => x.id === note_id);
    if (idx === -1) return { error: 'not_found', message: 'Note not found' };
    const [del] = s.notes.splice(idx, 1);
    s.trash.push(del);
    return { ok: true, deleted: { id: del.id, title: del.title, modified: del.modified } };
  },

  async restore_note(space_id, note_id) {
    const api = await waitForBridge();
    if (api?.restore_note) return await api.restore_note(space_id, note_id);
    const s = mockSpaces.find(x => x.id === space_id);
    if (!s) return { error: 'not_found', message: 'Space not found' };
    const idx = s.trash.findIndex(x => x.id === note_id);
    if (idx === -1) return { error: 'not_found', message: 'Note not in trash' };
    const [res] = s.trash.splice(idx, 1);
    s.notes.unshift(res);
    return { ok: true, note: { id: res.id, title: res.title, modified: res.modified } };
  },

  async list_trash(space_id) {
    const api = await waitForBridge();
    if (api?.list_trash) return await api.list_trash(space_id);
    const s = mockSpaces.find(x => x.id === space_id);
    return s ? s.trash.map(n => ({ id: n.id, title: n.title, modified: n.modified })) : [];
  },

  async move_note(space_id, note_id, target_space_id) {
    const api = await waitForBridge();
    if (api?.move_note) return await api.move_note(space_id, note_id, target_space_id);
    return { error: 'locked', message: 'Target space is locked' };
  },

  async render_preview(space_id, body) {
    const api = await waitForBridge();
    if (api?.render_preview) return await api.render_preview(space_id, body);
    return `<p>${body.replace(/</g, '&lt;').replace(/>/g, '&gt;')}</p>`;
  },

  async list_titles(space_id) {
    const api = await waitForBridge();
    if (api?.list_titles) return await api.list_titles(space_id);
    const s = mockSpaces.find(x => x.id === space_id);
    return s && !s.locked ? s.notes.map(n => n.title) : [];
  },

  async choose_key_file(space_id) {
    const api = await waitForBridge();
    if (api?.choose_key_file) return await api.choose_key_file(space_id);
    return { error: 'key_not_found', message: 'The native file picker is available in the desktop app.' };
  },

  async create_vault(space_id) {
    const api = await waitForBridge();
    if (api?.create_vault) return await api.create_vault(space_id);
    return { error: 'setup_unavailable', message: 'Vault setup is available in the desktop app.' };
  },

  async choose_notes_folder() {
    const api = await waitForBridge();
    if (api?.choose_notes_folder) return await api.choose_notes_folder();
    return { error: 'setup_unavailable', message: 'Folder selection is available in the desktop app.' };
  },

  async initialize_vaults() {
    const api = await waitForBridge();
    if (api?.initialize_vaults) return await api.initialize_vaults();
    return { error: 'setup_unavailable', message: 'Vault setup is available in the desktop app.' };
  },

  async unlock_vault(space_id) {
    const api = await waitForBridge();
    if (api?.unlock_vault) return await api.unlock_vault(space_id);
    const s = mockSpaces.find(x => x.id === space_id);
    if (!s) return { error: 'invalid_space', message: 'Space not found' };
    s.locked = false;
    return { ok: true, count: s.notes.length, locks_at: Date.now() + 600000 };
  },

  async lock_vault(space_id) {
    const api = await waitForBridge();
    if (api?.lock_vault) return await api.lock_vault(space_id);
    events.emit('vault_locked', { space_id });
    return { ok: true };
  },

  async lock_all() {
    const api = await waitForBridge();
    if (api?.lock_all) return await api.lock_all();
    events.emit('vault_locked', { space_ids: ['encrypted', 'personal'] });
    return { ok: true, locked_spaces: ['encrypted', 'personal'] };
  },

  async touch() {
    const api = await waitForBridge();
    if (api?.touch) return await api.touch();
    return { locks_at: Date.now() + 600000 };
  },

  async open_external(url) {
    const api = await waitForBridge();
    if (api?.open_external) return await api.open_external(url);
    if (typeof window !== 'undefined') window.open(url, '_blank');
    return { ok: true };
  },

  async get_settings() {
    const api = await waitForBridge();
    if (api?.get_settings) return await api.get_settings();
    return {
      notes_root: 'Documents/VaultNotes',
      vaults: [],
      autolock_minutes: 10,
      look: { theme: 'nebula', effects: 'full', view_mode: 'split', editor_font_size: 13.5 },
      backup: { enabled: false, interval_minutes: 60, drive_folder_id: null, last_backup: null }
    };
  },

  async update_settings(changes) {
    const api = await waitForBridge();
    if (api?.update_settings) return await api.update_settings(changes);
    return { ok: true, ...changes };
  },

  async backup_now() {
    const api = await waitForBridge();
    if (api?.backup_now) return await api.backup_now();
    return { ok: true, last_backup: 'just now' };
  },

  async connect_drive() {
    const api = await waitForBridge();
    if (api?.connect_drive) return await api.connect_drive();
    return { ok: true, connected: true };
  },

  async disconnect_drive() {
    const api = await waitForBridge();
    if (api?.disconnect_drive) return await api.disconnect_drive();
    return { ok: true, connected: false };
  },

  async restore_from_drive(target_folder) {
    const api = await waitForBridge();
    if (api?.restore_from_drive) return await api.restore_from_drive(target_folder);
    return { ok: true, restored_files: 0 };
  }
};
