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
    // M10: this mock key file is passphrase protected, so the unlock dialog
    // shows its passphrase box in the browser preview too.
    key_wrapped: true,
    notes: [
      {
        id: 'Diary',
        title: 'Diary',
        modified: 'Monday',
        body: `# Diary\n\nQuiet week. The router password lives in the [[Home lab]] note - Plain keeps the shared parts, this vault keeps mine.\n\nRead [[Plain:Shopping list]] for what to buy.`
      }
    ],
    trash: []
  }
];

/* Stand-in for the Python renderer, reached ONLY when this page is opened in a
   plain browser with no pywebview bridge (design review, CSS work).  Real
   rendering - Markdown, sanitising, link resolution - always happens in
   vaultnotes.render; this escapes the text and turns [[links]] into the same
   #vn-open/ and #vn-new/ addresses so the chips and their click handling can
   be seen without the desktop app. */
function mockTitles(spaceId) {
  const space = mockSpaces.find(x => x.id === spaceId);
  return space && !space.locked ? space.notes.map(n => n.title) : [];
}

/* One embedded note, in the shape vaultnotes.render produces (M10). */
function mockEmbedCard(title) {
  const space = mockSpaces.find(x => x.id === 'plain');
  const note = (space?.notes || []).find(n => n.title.toLowerCase() === String(title).toLowerCase())
    || (mockSpaces.find(x => x.locked === false)?.notes || []).find(n => n.title.toLowerCase() === String(title).toLowerCase());
  const inner = (note?.body || '').split('\n').map(line => `<p>${line}</p>`).join('');
  return `<div class="vn-embed"><span class="vn-embed-title">${title}</span><div class="vn-embed-body">${inner}</div></div>`;
}

/* Stand-in for the Python renderer, reached ONLY when this page is opened in a
   plain browser with no pywebview bridge (design review, CSS work).  Real
   rendering - Markdown, sanitising, link resolution - always happens in
   vaultnotes.render; this escapes the text and produces the same addresses the
   engine does (#vn-open/, #vn-new/, and M10's #vn-missing/, the "space/" prefix,
   the "#heading" suffix and ![[embeds]]) so the chips and their click handling
   can be seen without the desktop app. */
function mockPreview(space_id, body) {
  const titles = mockTitles(space_id).map(t => t.toLowerCase());
  const plainTitles = mockTitles('plain').map(t => t.toLowerCase());
  const escaped = String(body ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');

  return escaped
    .split('\n')
    .map(line => {
      const inner = line.replace(
        /(!)?\[\[([^\]|#]+)(?:#([^\]|]*))?(?:\|([^\]]*))?\]\]/g,
        (match, bang, target, heading, alias) => {
          let spaceName = '';
          let clean = String(target).trim();
          const colon = clean.indexOf(':');
          if (colon !== -1) {
            const head = clean.slice(0, colon).trim().toLowerCase();
            const rest = clean.slice(colon + 1).trim();
            // Naming a vault is refused by the engine, so the text stays as
            // written.  Only "Plain:" may ever appear.
            if (head !== 'plain' || !rest) return match;
            spaceName = 'plain';
            clean = rest;
          }
          clean = clean.replace(/\.md$/i, '');
          const list = spaceName === 'plain' ? plainTitles : titles;
          const known = list.includes(clean.toLowerCase());
          if (bang === '!' && known && !spaceName) {
            return mockEmbedCard(clean);
          }
          const label = (alias && alias.trim()) || String(target).trim();
          const path = spaceName ? `${spaceName}/${encodeURIComponent(clean)}` : encodeURIComponent(clean);
          const prefix = known ? '#vn-open/' : (spaceName ? '#vn-missing/' : '#vn-new/');
          const suffix = heading ? `#${encodeURIComponent(heading.trim())}` : '';
          const hint = known ? `Open ${clean}` : `${clean} is not written yet`;
          return `<a href="${prefix}${path}${suffix}" title="${hint}">${label}</a>`;
        }
      );
      // An embed that stands alone is a card, not a paragraph.
      return /^\s*<div class="vn-embed">.*<\/div>\s*$/.test(inner) ? inner : `<p>${inner}</p>`;
    })
    .join('');
}

/* Backup state for the standalone browser preview (M1's fake API).  The real
   app answers the same shape from Python (section 4.6 and 8.2). */
const MOCK_BACKUP = {
  connected: false,
  enabled: false,
  interval_minutes: 60,
  last_backup: null,
  has_folder: false,
  folder_name: 'VaultNotes Backup',
  running: false,
  kind: null,
  manifest_entries: 0,
  client_secret: false
};

const mockBackup = { ...MOCK_BACKUP };

/**
 * No Python, no network: walk the status bar through the same event sequence
 * the engine sends ("Backing up 3/12…" then backup_done), so the progress bar
 * and toasts can be checked in a plain browser.
 */
function simulateBackup(kind, stepMs = 420) {
  const total = 12;
  const word = kind === 'restore' ? 'Restoring' : 'Backing up';
  mockBackup.running = true;
  mockBackup.kind = kind;
  for (let done = 1; done <= total; done += 1) {
    setTimeout(() => {
      events.emit('backup_progress', {
        done,
        total,
        label: `${word} ${done}/${total}…`,
        kind
      });
    }, stepMs * done);
  }
  setTimeout(() => {
    mockBackup.running = false;
    mockBackup.kind = null;
    mockBackup.connected = true;
    if (kind !== 'restore') {
      mockBackup.last_backup = new Date().toISOString().slice(0, 19) + 'Z';
      mockBackup.has_folder = true;
      mockBackup.manifest_entries = total;
    }
    events.emit('backup_done', {
      ok: true,
      kind,
      uploaded: kind === 'restore' ? 0 : total,
      updated: 0,
      skipped: 0,
      failed: 0,
      restored: kind === 'restore' ? total : 0,
      last_backup: mockBackup.last_backup,
      message: kind === 'restore'
        ? `Restored ${total} files. Vaults still need their key files to open.`
        : `Uploaded ${total} new and 0 changed files.`,
      errors: []
    });
  }, stepMs * (total + 1));
}

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
        note_count: s.notes.length,
        key_wrapped: !!s.key_wrapped
      })),
      look: { theme: 'nebula', effects: 'full', view_mode: 'split', editor_font_size: 13.5 },
      last_backup: null,
      backup: { ...MOCK_BACKUP },
      notes_root: 'Documents/VaultNotes',
      needs_setup: false
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

  async note_links(space_id, note_id) {
    const api = await waitForBridge();
    if (api?.note_links) return await api.note_links(space_id, note_id);
    return { backlinks: [], outgoing: [], link_count: 0 };
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
    const src = mockSpaces.find(x => x.id === space_id);
    const dst = mockSpaces.find(x => x.id === target_space_id);
    if (!src || !dst) return { error: 'invalid_space', message: 'Space not found' };
    if (space_id === target_space_id) return { error: 'same_space', message: 'The note is already in that space' };
    if (src.locked || dst.locked) return { error: 'locked', message: 'Unlock the vault first' };
    const idx = src.notes.findIndex(x => x.id === note_id);
    if (idx === -1) return { error: 'not_found', message: 'Note not found' };
    const [moved] = src.notes.splice(idx, 1);
    dst.notes.unshift(moved);
    return { new_id: moved.id, title: moved.title, broken_links: 0 };
  },

  async import_notes(space_id) {
    const api = await waitForBridge();
    if (api?.import_notes) return await api.import_notes(space_id);
    return { error: 'import_unavailable', message: 'Import is available in the desktop app.' };
  },

  async export_note(space_id, note_id) {
    const api = await waitForBridge();
    if (api?.export_note) return await api.export_note(space_id, note_id);
    return { error: 'export_unavailable', message: 'Export is available in the desktop app.' };
  },

  async purge_note(space_id, note_id) {
    const api = await waitForBridge();
    if (api?.purge_note) return await api.purge_note(space_id, note_id);
    const s = mockSpaces.find(x => x.id === space_id);
    if (!s) return { error: 'not_found', message: 'Space not found' };
    const idx = s.trash.findIndex(x => x.id === note_id);
    if (idx === -1) return { error: 'not_found', message: 'Note not in trash' };
    const [gone] = s.trash.splice(idx, 1);
    return { ok: true, title: gone.title };
  },

  async empty_trash(space_id) {
    const api = await waitForBridge();
    if (api?.empty_trash) return await api.empty_trash(space_id);
    const s = mockSpaces.find(x => x.id === space_id);
    if (!s) return { error: 'not_found', message: 'Space not found' };
    const purged = s.trash.length;
    s.trash = [];
    return { ok: true, purged };
  },

  async render_preview(space_id, body) {
    const api = await waitForBridge();
    if (api?.render_preview) return await api.render_preview(space_id, body);
    return mockPreview(space_id, body);
  },

  /* Resolve a clicked [[link]] title into a note (M10).  The engine does the
     matching, so case, ".md" and a #heading behave exactly as in the preview. */
  async open_note_by_title(space_id, title, heading = '') {
    const api = await waitForBridge();
    if (api?.open_note_by_title) return await api.open_note_by_title(space_id, title, heading);
    const s = mockSpaces.find(x => x.id === space_id);
    if (!s) return { error: 'invalid_space', message: 'Space not found' };
    if (s.locked) return { error: 'locked', message: `Unlock ${s.name} first.` };
    const wanted = String(title ?? '').trim().toLowerCase().replace(/\.md$/i, '');
    const match = s.notes.find(n => String(n.title).trim().toLowerCase() === wanted);
    if (!match) return { error: 'not_found', message: `No note named “${title}” in ${s.name}.` };
    const found = { ok: true, space_id, note_id: match.id, title: match.title };
    if (heading) found.heading = heading;
    return found;
  },

  /* Nodes and edges for the graph view (M10).  Without the desktop app the
     edges are read straight from the mock notes' bodies. */
  async get_graph(space_id) {
    const api = await waitForBridge();
    if (api?.get_graph) return await api.get_graph(space_id);
    const s = mockSpaces.find(x => x.id === space_id);
    if (!s || s.locked) return { space_id, nodes: [], edges: [], locked: true };
    const nodes = s.notes.map(n => ({ id: n.id, title: n.title, links: 0 }));
    const byTitle = new Map(nodes.map(n => [String(n.title).toLowerCase(), n.id]));
    const edges = [];
    for (const note of s.notes) {
      const pattern = /(!)?\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]/g;
      let match = pattern.exec(String(note.body ?? ''));
      while (match !== null) {
        const clean = String(match[2]).trim().replace(/\.md$/i, '');
        const target = byTitle.get(clean.toLowerCase());
        edges.push({
          from: note.id,
          to: target ?? null,
          title: clean,
          resolved: target != null
        });
        nodes.forEach(n => { if (n.id === note.id) n.links += 1; });
        match = pattern.exec(String(note.body ?? ''));
      }
    }
    return { space_id, nodes, edges, locked: false };
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

  /* A passphrase wraps the new key file (section 4.5, M10); empty keeps the
     plain form where the file itself is the secret. */
  async create_vault(space_id, passphrase = '') {
    const api = await waitForBridge();
    if (api?.create_vault) return await api.create_vault(space_id, null, passphrase || '');
    return { error: 'setup_unavailable', message: 'Vault setup is available in the desktop app.' };
  },

  async choose_notes_folder() {
    const api = await waitForBridge();
    if (api?.choose_notes_folder) return await api.choose_notes_folder();
    return { error: 'setup_unavailable', message: 'Folder selection is available in the desktop app.' };
  },

  async initialize_vaults(passphrases = null) {
    const api = await waitForBridge();
    if (api?.initialize_vaults) return await api.initialize_vaults(null, passphrases || null);
    return { error: 'setup_unavailable', message: 'Vault setup is available in the desktop app.' };
  },

  async unlock_vault(space_id, passphrase = '') {
    const api = await waitForBridge();
    if (api?.unlock_vault) return await api.unlock_vault(space_id, passphrase || '');
    const s = mockSpaces.find(x => x.id === space_id);
    if (!s) return { error: 'invalid_space', message: 'Space not found' };
    // The browser preview keeps the Personal key file wrapped (M10), so the
    // passphrase step can be tried out without the desktop app.
    if (s.key_wrapped && !String(passphrase ?? '').trim()) {
      return { error: 'passphrase_required', message: 'This key file is protected. Enter its passphrase to unlock.' };
    }
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

  /** Tells Python the page has saved everything and the window may close. */
  async ready_to_close() {
    const api = await waitForBridge();
    if (api?.ready_to_close) return await api.ready_to_close();
    return { ok: true };
  },

  async update_settings(changes) {
    const api = await waitForBridge();
    if (api?.update_settings) return await api.update_settings(changes);
    return { ok: true, ...changes };
  },

  async backup_now() {
    const api = await waitForBridge();
    if (api?.backup_now) return await api.backup_now();
    simulateBackup('backup');
    return { ok: true, started: true, folder: mockBackup.folder_name };
  },

  async connect_drive() {
    const api = await waitForBridge();
    if (api?.connect_drive) return await api.connect_drive();
    // The desktop app opens the browser sign-in itself; here the preview just
    // flips to "connected" so the Drive UI can be reviewed.
    mockBackup.connected = true;
    mockBackup.client_secret = true;
    return { ok: true, connected: true, folder: mockBackup.folder_name };
  },

  async disconnect_drive() {
    const api = await waitForBridge();
    if (api?.disconnect_drive) return await api.disconnect_drive();
    mockBackup.connected = false;
    mockBackup.enabled = false;
    return { ok: true, connected: false };
  },

  /* Python picks the folder itself (security rule 12f): no path from here. */
  async restore_from_drive() {
    const api = await waitForBridge();
    if (api?.restore_from_drive) return await api.restore_from_drive();
    simulateBackup('restore');
    return { ok: true, started: true };
  },

  async prune_drive_backup() {
    const api = await waitForBridge();
    if (api?.prune_drive_backup) return await api.prune_drive_backup();
    return { ok: true, removed: 0 };
  },
};
