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
        body: `---\nimportant: true\ntags: [errands, home]\n---\n# Shopping list\n\n- [x] Coffee beans\n- [ ] Oat milk\n- [ ] Batteries for the [[Home lab]] sensors\n- [ ] Birthday card for Ana`
      },
      {
        id: 'Home lab',
        title: 'Home lab',
        modified: 'Yesterday',
        body: `---\ntags: [home, projects/homelab]\n---\n# Home lab\n\nIdeas for the little server shelf.\n\n| Device | Status |\n|---|---|\n| Raspberry Pi 5 | running |\n| NAS | ordering |\n| Air sensor | needs batteries |\n\nParts go on the [[Shopping list]].`
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
        body: `---\ntags: [journal]\n---\n# Diary\n\nQuiet week. The router password lives in the [[Home lab]] note - Plain keeps the shared parts, this vault keeps mine.\n\nRead [[Plain:Shopping list]] for what to buy.`
      }
    ],
    trash: []
  },
  {
    id: 'ai',
    name: 'AI-Notes',
    kind: 'plain',
    colorVar: '--ai',
    locked: false,
    notes: [
      {
        id: 'Home lab check',
        title: 'Home lab check',
        modified: 'Today 09:40',
        body: `---\ntags: [projects/homelab]\n---\n# Home lab check\n\nThe air sensor in [[Plain:Home lab]] still needs batteries; they are already on [[Plain:Shopping list]].\n\n- [x] Read the Plain notes\n- [ ] Order the NAS\n\nMore in [[About AI-Notes]].`
      },
      {
        id: 'About AI-Notes',
        title: 'About AI-Notes',
        modified: 'Today 08:30',
        body: `# About AI-Notes\n\nThis space belongs to AI helpers such as Claude. They write their findings, summaries, drafts and hand-over notes here, so Plain stays yours alone.`
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
    .replace(MOCK_FRONT_MATTER, '')
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

/* Rough stand-ins for vaultnotes.frontmatter, for the browser preview only:
   the app itself always asks Python whether a note is important. */
const MOCK_FRONT_MATTER = /^---\n([\s\S]*?)\n---\n?/;

function mockImportant(body) {
  const block = MOCK_FRONT_MATTER.exec(body || '');
  return !!block && /^important:\s*(true|yes|on)\s*$/im.test(block[1]);
}

// The "tags: [a, b]" line only; vaultnotes.tags reads every form Obsidian writes.
function mockTags(body) {
  const block = MOCK_FRONT_MATTER.exec(body || '');
  const line = block && /^tags:\s*\[?([^\]\n]*)\]?\s*$/im.exec(block[1]);
  return line ? line[1].split(/[,\s]+/).map(t => t.trim()).filter(Boolean) : [];
}

function mockSetTags(body, tags) {
  const block = MOCK_FRONT_MATTER.exec(body || '');
  const line = tags.length ? `tags: [${tags.join(', ')}]` : '';
  if (!block) return line ? `---\n${line}\n---\n${body}` : body;
  const others = block[1].split('\n').filter(l => l.trim() && !/^tags:/i.test(l));
  const inner = [...others, ...(line ? [line] : [])];
  const rest = body.slice(block[0].length);
  return inner.length ? `---\n${inner.join('\n')}\n---\n${rest}` : rest;
}

function mockHasTags(tags, wanted) {
  const keys = tags.map(t => t.toLowerCase());
  return wanted.every(w => keys.some(k => k === w || k.startsWith(`${w}/`)));
}

function mockSetImportant(body, important) {
  if (mockImportant(body) === important) return body;
  if (important) return `---\nimportant: true\n---\n${body}`;
  return body.replace(MOCK_FRONT_MATTER, '');
}

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

/* Pretend shells for the browser preview only: each echoes what is typed and
   answers every command with one line, so the CMD space and its tabs can be
   reviewed without Python.  The desktop app runs real shells instead. */
const mockTerminal = {
  enabled: false,
  shell: 'cmd',
  recent: ['git status', 'npm run build'],
  favorites: ['python run.py --dev'],
  sessions: new Map() // id -> { shell, line }
};
let mockSessionSeq = 0;
const MOCK_SHELLS = [
  { id: 'cmd', name: 'CMD' },
  { id: 'powershell', name: 'PowerShell' },
  { id: 'bash', name: 'Git Bash' }
];
const MOCK_PROMPTS = { cmd: 'C:\\Users\\you>', powershell: 'PS C:\\Users\\you> ', bash: '$ ' };

/* The SQL space in a plain browser: two pretend connections and made-up
   rows, so the layout can be worked on without Python or a database. */
const mockSql = {
  enabled: false,
  connections: [
    {
      id: 'mock-dev', name: 'Dev ingest', engine: 'mssql', engineName: 'SQL Server',
      server: 'dev-sql01', database: 'Ingest', auth: 'windows', username: '',
      encrypt: true, trust_cert: true, file: '', where: 'dev-sql01 / Ingest', hasPassword: false
    },
    {
      id: 'mock-lite', name: 'scratch', engine: 'sqlite', engineName: 'SQLite',
      server: '', database: '', auth: 'windows', username: '', encrypt: true, trust_cert: false,
      file: 'C:\\Users\\you\\scratch.db', where: 'scratch.db', hasPassword: false
    }
  ],
  sessions: new Map(), // id -> { connection, running, results }
  queries: [
    { id: 'mock-q1', connection: 'mock-dev', name: 'Failed batches today', text: "-- the ones to look at first\nSELECT TOP 10 *\nFROM dbo.Batches\nWHERE status = 'Failed'" },
    { id: 'mock-q2', connection: 'mock-dev', name: 'Rows loaded by source', text: 'SELECT source, SUM(rows_loaded) AS rows_loaded\nFROM dbo.Batches\nGROUP BY source' },
    { id: 'mock-q3', connection: 'mock-lite', name: 'Everything', text: 'SELECT * FROM batches' }
  ]
};

function mockSqlQueries() {
  return mockSql.queries
    .map(q => ({
      id: q.id, connection: q.connection, name: q.name, modified: '2026-10-01T09:00:00Z', lastRun: q.lastRun || null,
      preview: q.text.split('\n').map(l => l.trim()).find(l => l && !l.startsWith('--')) || ''
    }))
    .sort((a, b) => a.name.localeCompare(b.name));
}
let mockSqlSeq = 0;

const MOCK_SQL_COLUMNS = [
  { name: 'batch_id', kind: 'number' },
  { name: 'source', kind: 'text' },
  { name: 'status', kind: 'text' },
  { name: 'rows_loaded', kind: 'number' },
  { name: 'started_at', kind: 'date' },
  { name: 'notes', kind: 'text' }
];

// Virtual tables in the browser preview: their rows are made up like the rest.
mockSql.vtables = [
  {
    name: 'failed_batches', rows: 10, columns: MOCK_SQL_COLUMNS.map(c => c.name),
    source: { connection: 'mock-dev', name: 'Dev ingest', where: 'dev-sql01 / Ingest' },
    query: 'SELECT TOP 10 *', created: '2026-10-01T09:00:00Z'
  }
];

function mockSqlRow(i) {
  const statuses = ['Loaded', 'Pending', 'Failed', 'Loaded', 'Loaded'];
  return [
    1000 + i,
    ['hl7-feed', 'csv-drop', 'fhir-pull'][i % 3],
    statuses[i % statuses.length],
    (i * 7919) % 50000,
    `2026-09-${String(1 + (i % 28)).padStart(2, '0')} 0${i % 10}:15:00`,
    i % 4 === 0 ? null : `run ${i}`
  ];
}

function mockSqlPublic() {
  return mockSql.connections.map(c => ({ ...c }));
}

function mockSqlResults(text) {
  const lower = text.toLowerCase();
  if (lower.includes('nope')) return { error: "Invalid object name 'nope'.", results: [], messages: [] };
  if (!lower.includes('select')) return { error: null, results: [], messages: ['(3 rows affected)'] };
  const total = lower.includes('top 10') ? 10 : 12345;
  const results = [{ columns: MOCK_SQL_COLUMNS, total }];
  if (lower.includes('count')) results.push({ columns: [{ name: 'n', kind: 'number' }], total: 1, single: [[total]] });
  return { error: null, results, messages: results.map(r => `(${r.total} rows)`) };
}

function mockSqlPage(result, offset, limit) {
  if (result.single) return result.single.slice(offset, offset + limit);
  const rows = [];
  for (let i = offset; i < Math.min(result.total, offset + limit); i++) rows.push(mockSqlRow(i));
  return rows;
}

function mockTermOut(id, data) {
  if (mockTerminal.sessions.has(id)) setTimeout(() => events.emit('terminal_output', { id, data }), 5);
}

function mockTermStop(id) {
  if (!mockTerminal.sessions.delete(id)) return;
  setTimeout(() => events.emit('terminal_exit', { id, code: null, stopped: true }), 5);
}

function mockTermLists() {
  return { recent: [...mockTerminal.recent], favorites: [...mockTerminal.favorites] };
}

function mockTermType(id, data) {
  const session = mockTerminal.sessions.get(id);
  const prompt = MOCK_PROMPTS[session.shell];
  for (const ch of data) {
    if (ch === '\r') {
      const cmd = session.line.trim();
      session.line = '';
      if (cmd === 'exit') {
        mockTerminal.sessions.delete(id);
        setTimeout(() => events.emit('terminal_exit', { id, code: 0, stopped: false }), 5);
        return;
      }
      mockTermOut(id, cmd ? `\r\n(preview) ${cmd}\r\n${prompt}` : `\r\n${prompt}`);
    } else if (ch === '\x7f') {
      if (session.line) {
        session.line = session.line.slice(0, -1);
        mockTermOut(id, '\b \b');
      }
    } else if (ch === '\x1b' || ch === '\x15') {
      session.line = '';
      mockTermOut(id, `\r\x1b[K${prompt}`);
    } else if (ch >= ' ') {
      session.line += ch;
      mockTermOut(id, ch);
    }
  }
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
    // "#work budget": the #words are tags, the rest is searched for.
    const words = (query || '').toLowerCase().split(/\s+/).filter(Boolean);
    const wanted = words.filter(w => /^#[\p{L}\p{N}_/-]+$/u.test(w)).map(w => w.slice(1));
    const q = words.filter(w => !/^#[\p{L}\p{N}_/-]+$/u.test(w)).join(' ');
    if (q) list = list.filter(n => n.title.toLowerCase().includes(q) || n.body.toLowerCase().includes(q));
    if (wanted.length) list = list.filter(n => mockHasTags(mockTags(n.body), wanted));
    return list.map(n => ({
      id: n.id,
      title: n.title,
      snippet: n.body.replace(MOCK_FRONT_MATTER, '').slice(0, 80),
      modified: n.modified,
      link_count: 0,
      important: mockImportant(n.body),
      tags: mockTags(n.body)
    })).sort((a, b) => Number(b.important) - Number(a.important));
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
      important: mockImportant(n.body),
      tags: mockTags(n.body),
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
    return { modified: n.modified, important: mockImportant(body), tags: mockTags(body) };
  },

  async set_important(space_id, note_id, important) {
    const api = await waitForBridge();
    if (api?.set_important) return await api.set_important(space_id, note_id, important);
    const s = mockSpaces.find(x => x.id === space_id);
    if (!s || s.locked) return { error: 'locked', message: 'Vault is locked' };
    const n = s.notes.find(x => x.id === note_id);
    if (!n) return { error: 'not_found', message: 'Note not found' };
    const body = mockSetImportant(n.body, important);
    if (body !== n.body) {
      n.body = body;
      n.modified = 'just now';
    }
    return this.open_note(space_id, note_id);
  },

  async set_tags(space_id, note_id, tags) {
    const api = await waitForBridge();
    if (api?.set_tags) return await api.set_tags(space_id, note_id, tags);
    const s = mockSpaces.find(x => x.id === space_id);
    if (!s || s.locked) return { error: 'locked', message: 'Vault is locked' };
    const n = s.notes.find(x => x.id === note_id);
    if (!n) return { error: 'not_found', message: 'Note not found' };
    const clean = [...new Map(tags.map(t => String(t).replace(/^#/, '').trim()).filter(Boolean)
      .map(t => [t.toLowerCase(), t])).values()];
    const body = mockSetTags(n.body, clean);
    if (body !== n.body) {
      n.body = body;
      n.modified = 'just now';
    }
    return this.open_note(space_id, note_id);
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

  /* Flip the index-th checklist item of a note's text.  The engine counts items
     the way the preview does; the browser stand-in only skips code fences. */
  async toggle_task(space_id, body, index) {
    const api = await waitForBridge();
    if (api?.toggle_task) return await api.toggle_task(space_id, body, index);
    const lines = String(body ?? '').split('\n');
    let fenced = false;
    let seen = -1;
    for (let i = 0; i < lines.length; i++) {
      if (/^\s*(```|~~~)/.test(lines[i])) { fenced = !fenced; continue; }
      const m = !fenced && /^((?:\s*>)*\s*(?:[-*+]|\d+[.)])\s+\[)([ xX])(\]\s)/.exec(lines[i]);
      if (!m || ++seen !== index) continue;
      lines[i] = lines[i].slice(0, m[1].length) + (m[2] === ' ' ? 'x' : ' ') + lines[i].slice(m[1].length + 1);
      return { ok: true, body: lines.join('\n') };
    }
    return { error: 'not_found', message: 'That checklist item is no longer in the note.' };
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

  async list_tags(space_id) {
    const api = await waitForBridge();
    if (api?.list_tags) return await api.list_tags(space_id);
    const s = mockSpaces.find(x => x.id === space_id);
    if (!s || s.locked) return [];
    const counts = new Map();
    s.notes.forEach(n => mockTags(n.body).forEach(tag => {
      const row = counts.get(tag.toLowerCase()) || { tag, count: 0 };
      row.count += 1;
      counts.set(tag.toLowerCase(), row);
    }));
    return [...counts.values()].sort((a, b) => b.count - a.count || a.tag.localeCompare(b.tag));
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

  async choose_client_secret() {
    const api = await waitForBridge();
    if (api?.choose_client_secret) return await api.choose_client_secret();
    // The desktop app picks the file in a native dialog; the preview pretends.
    mockBackup.client_secret = true;
    return { ok: true, backup: { ...mockBackup } };
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

  /* ---- The CMD space.  Python picks the program for a shell id; the page
     never sends a path or a command line. ---- */
  async terminal_state() {
    const api = await waitForBridge();
    if (api?.terminal_state) return await api.terminal_state();
    return {
      enabled: mockTerminal.enabled,
      shells: MOCK_SHELLS,
      shell: mockTerminal.shell,
      running: [...mockTerminal.sessions].map(([id, s]) => ({ id, shell: s.shell })),
      ...mockTermLists()
    };
  },

  /** Python asks in a native Windows dialog before the shell is allowed. */
  async terminal_enable() {
    const api = await waitForBridge();
    if (api?.terminal_enable) return await api.terminal_enable();
    mockTerminal.enabled = true;
    return { ok: true, enabled: true };
  },

  async terminal_disable() {
    const api = await waitForBridge();
    if (api?.terminal_disable) return await api.terminal_disable();
    mockTerminal.enabled = false;
    for (const id of [...mockTerminal.sessions.keys()]) mockTermStop(id);
    return { ok: true, enabled: false };
  },

  async terminal_start(shell_id, cols = 80, rows = 24) {
    const api = await waitForBridge();
    if (api?.terminal_start) return await api.terminal_start(shell_id, cols, rows);
    if (!mockTerminal.enabled) return { error: 'terminal_off', message: 'The CMD space is off. Turn it on first.' };
    if (mockTerminal.sessions.size >= 8) return { error: 'too_many', message: 'Up to 8 shells can be open at once. Close a tab first.' };
    const shell = MOCK_SHELLS.find(s => s.id === shell_id) || MOCK_SHELLS[0];
    const id = `mock-${++mockSessionSeq}`;
    mockTerminal.shell = shell.id;
    mockTerminal.sessions.set(id, { shell: shell.id, line: '' });
    mockTermOut(id, `Browser preview: a pretend ${shell.name} (shell ${mockSessionSeq}). The app runs the real one.\r\n\r\n${MOCK_PROMPTS[shell.id]}`);
    return { ok: true, id, shell: shell.id, name: shell.name };
  },

  async terminal_write(session_id, data) {
    const api = await waitForBridge();
    if (api?.terminal_write) return await api.terminal_write(session_id, data);
    if (!mockTerminal.sessions.has(session_id)) return { error: 'not_running', message: 'That shell is not running any more.' };
    mockTermType(session_id, data);
    return { ok: true };
  },

  async terminal_resize(session_id, cols, rows) {
    const api = await waitForBridge();
    if (api?.terminal_resize) return await api.terminal_resize(session_id, cols, rows);
    return { ok: true };
  },

  /** Ends one tab's shell, or every shell when no id is given. */
  async terminal_stop(session_id = null) {
    const api = await waitForBridge();
    if (api?.terminal_stop) return await api.terminal_stop(session_id);
    for (const id of session_id == null ? [...mockTerminal.sessions.keys()] : [session_id]) mockTermStop(id);
    return { ok: true };
  },

  async terminal_remember(command) {
    const api = await waitForBridge();
    if (api?.terminal_remember) return await api.terminal_remember(command);
    const text = typeof command === 'string' && !/^\s/.test(command) ? command.trimEnd() : '';
    if (text) mockTerminal.recent = [text, ...mockTerminal.recent.filter(c => c !== text)].slice(0, 50);
    return { ok: true, saved: !!text, ...mockTermLists() };
  },

  async terminal_set_favorite(command, favorite) {
    const api = await waitForBridge();
    if (api?.terminal_set_favorite) return await api.terminal_set_favorite(command, favorite);
    const text = String(command).trimEnd();
    mockTerminal.favorites = favorite
      ? [...mockTerminal.favorites.filter(c => c !== text), text]
      : mockTerminal.favorites.filter(c => c !== text);
    return { ok: true, ...mockTermLists() };
  },

  async terminal_forget(command) {
    const api = await waitForBridge();
    if (api?.terminal_forget) return await api.terminal_forget(command);
    mockTerminal.recent = mockTerminal.recent.filter(c => c !== command);
    return { ok: true, ...mockTermLists() };
  },

  async terminal_clear_recent() {
    const api = await waitForBridge();
    if (api?.terminal_clear_recent) return await api.terminal_clear_recent();
    mockTerminal.recent = [];
    return { ok: true, ...mockTermLists() };
  },
  /* ---- The SQL space.  Python opens the connections and runs the SQL; a
     run answers at once and its result arrives as a sql_done event.  The
     page never sends a file path: SQLite files are picked in a native dialog.
     ---- */
  async sql_state() {
    const api = await waitForBridge();
    if (api?.sql_state) return await api.sql_state();
    return {
      enabled: mockSql.enabled,
      driver: 'ODBC Driver 18 for SQL Server',
      connections: mockSql.enabled ? mockSqlPublic() : [],
      queries: mockSql.enabled ? mockSqlQueries() : [],
      vtables: mockSql.enabled ? mockSql.vtables.map(t => ({ ...t })) : [],
      sessions: []
    };
  },

  /** Python asks in a native Windows dialog before the SQL space is allowed. */
  async sql_enable() {
    const api = await waitForBridge();
    if (api?.sql_enable) return await api.sql_enable();
    mockSql.enabled = true;
    return { ok: true, enabled: true };
  },

  async sql_disable() {
    const api = await waitForBridge();
    if (api?.sql_disable) return await api.sql_disable();
    mockSql.enabled = false;
    mockSql.sessions.clear();
    return { ok: true, enabled: false };
  },

  /** Adds a connection, or changes the one named by connection.id.  An empty password keeps the saved one. */
  async sql_save_connection(connection, password = null) {
    const api = await waitForBridge();
    if (api?.sql_save_connection) return await api.sql_save_connection(connection, password);
    const where = connection.server + (connection.database ? ` / ${connection.database}` : '');
    const old = mockSql.connections.find(c => c.id === connection.id);
    const saved = {
      ...(old || {}), ...connection, id: old?.id || `mock-${++mockSqlSeq}`, engineName: 'SQL Server', where,
      hasPassword: connection.auth === 'sql' && Boolean(password || old?.hasPassword)
    };
    mockSql.connections = [...mockSql.connections.filter(c => c.id !== saved.id), saved];
    return { ok: true, connection: { ...saved }, connections: mockSqlPublic() };
  },

  /** Picks a SQLite file in the native dialog and saves a connection to it. */
  async sql_add_sqlite() {
    const api = await waitForBridge();
    if (api?.sql_add_sqlite) return await api.sql_add_sqlite();
    const saved = {
      id: `mock-${++mockSqlSeq}`, name: 'picked', engine: 'sqlite', engineName: 'SQLite', server: '', database: '',
      auth: 'windows', username: '', encrypt: true, trust_cert: false, file: 'C:\\data\\picked.db', where: 'picked.db', hasPassword: false
    };
    mockSql.connections.push(saved);
    return { ok: true, connection: { ...saved }, connections: mockSqlPublic() };
  },

  async sql_choose_sqlite_file(connection_id) {
    const api = await waitForBridge();
    if (api?.sql_choose_sqlite_file) return await api.sql_choose_sqlite_file(connection_id);
    return { error: 'cancelled', message: 'No file was chosen.' };
  },

  async sql_delete_connection(connection_id) {
    const api = await waitForBridge();
    if (api?.sql_delete_connection) return await api.sql_delete_connection(connection_id);
    mockSql.connections = mockSql.connections.filter(c => c.id !== connection_id);
    mockSql.queries = mockSql.queries.filter(q => q.connection !== connection_id);
    return { ok: true, connections: mockSqlPublic(), queries: mockSqlQueries() };
  },

  /** Saves SQL on a connection: new, or query.id changed (another connection moves it). */
  async sql_save_query(query) {
    const api = await waitForBridge();
    if (api?.sql_save_query) return await api.sql_save_query(query);
    if (!String(query?.name || '').trim()) return { error: 'invalid_input', message: 'Name is required.' };
    if (!String(query?.text || '').trim()) return { error: 'empty', message: 'There is no SQL to save.' };
    const old = mockSql.queries.find(q => q.id === query.id);
    const saved = { ...(old || {}), id: old?.id || `mock-q${++mockSqlSeq}`, connection: query.connection, name: query.name.trim(), text: query.text };
    mockSql.queries = [...mockSql.queries.filter(q => q.id !== saved.id), saved];
    return { ok: true, query: { ...mockSqlQueries().find(q => q.id === saved.id), text: saved.text }, queries: mockSqlQueries() };
  },

  async sql_get_query(query_id) {
    const api = await waitForBridge();
    if (api?.sql_get_query) return await api.sql_get_query(query_id);
    const found = mockSql.queries.find(q => q.id === query_id);
    if (!found) return { error: 'not_found', message: 'That saved query does not exist any more.' };
    return { ok: true, query: { ...mockSqlQueries().find(q => q.id === query_id), text: found.text } };
  },

  async sql_delete_query(query_id) {
    const api = await waitForBridge();
    if (api?.sql_delete_query) return await api.sql_delete_query(query_id);
    mockSql.queries = mockSql.queries.filter(q => q.id !== query_id);
    return { ok: true, queries: mockSqlQueries() };
  },

  async sql_test_connection(connection, password = null) {
    const api = await waitForBridge();
    if (api?.sql_test_connection) return await api.sql_test_connection(connection, password);
    await new Promise(r => setTimeout(r, 400));
    return { ok: true, elapsedMs: 400 };
  },

  /** Connects a new query tab; slow servers make this take a few seconds. */
  async sql_open(connection_id) {
    const api = await waitForBridge();
    if (api?.sql_open) return await api.sql_open(connection_id);
    const connection = connection_id === 'vt'
      ? { id: 'vt', name: 'Virtual tables', engine: 'sqlite', engineName: 'SQLite', where: 'vt.db' }
      : mockSql.connections.find(c => c.id === connection_id);
    if (!connection) return { error: 'not_found', message: 'That connection does not exist any more.' };
    await new Promise(r => setTimeout(r, 250));
    const id = `mock-session-${++mockSqlSeq}`;
    mockSql.sessions.set(id, { connection, running: null, results: [] });
    return { ok: true, id, connection: { ...connection }, name: connection.name, engine: connection.engine, running: false };
  },

  /** query_id: the saved query the tab shows, so its "last run" moves forward. */
  async sql_run(session_id, text, query_id = null) {
    const api = await waitForBridge();
    if (api?.sql_run) return await api.sql_run(session_id, text, query_id);
    const savedQuery = mockSql.queries.find(q => q.id === query_id);
    if (savedQuery) savedQuery.lastRun = new Date().toISOString();

    const session = mockSql.sessions.get(session_id);
    if (!session) return { error: 'not_open', message: 'That query tab is not connected any more.' };
    if (!String(text).trim()) return { error: 'empty', message: 'There is no SQL to run.' };
    session.lastText = String(text);
    const run = `mock-run-${++mockSqlSeq}`;
    session.running = run;
    const outcome = mockSqlResults(String(text));
    setTimeout(() => events.emit('sql_progress', { session: session_id, run, rows: 4000 }), 300);
    session.timer = setTimeout(() => {
      session.running = null;
      session.results = outcome.results;
      events.emit('sql_done', {
        session: session_id, run, ok: !outcome.error, cancelled: false, error: outcome.error,
        messages: outcome.messages, elapsedMs: 640,
        results: outcome.results.map(r => ({ columns: r.columns, total: r.total, rows: mockSqlPage(r, 0, 200) }))
      });
    }, 700);
    return { ok: true, run, batches: 1 };
  },

  async sql_cancel(session_id) {
    const api = await waitForBridge();
    if (api?.sql_cancel) return await api.sql_cancel(session_id);
    const session = mockSql.sessions.get(session_id);
    if (!session?.running) return { ok: true, cancelled: false };
    clearTimeout(session.timer);
    const run = session.running;
    session.running = null;
    session.results = [];
    setTimeout(() => events.emit('sql_done', {
      session: session_id, run, ok: false, cancelled: true, error: null, messages: [], elapsedMs: 120, results: []
    }), 50);
    return { ok: true, cancelled: true };
  },

  async sql_rows(session_id, result_index, offset = 0, limit = 200) {
    const api = await waitForBridge();
    if (api?.sql_rows) return await api.sql_rows(session_id, result_index, offset, limit);
    const result = mockSql.sessions.get(session_id)?.results[result_index];
    if (!result) return { error: 'not_found', message: 'That result is gone. Run the query again.' };
    await new Promise(r => setTimeout(r, 60));
    return { ok: true, offset, total: result.total, rows: mockSqlPage(result, offset, limit) };
  },

  /** A whole result as tab-separated text, header first. */
  async sql_copy(session_id, result_index) {
    const api = await waitForBridge();
    if (api?.sql_copy) return await api.sql_copy(session_id, result_index);
    const result = mockSql.sessions.get(session_id)?.results[result_index];
    if (!result) return { error: 'not_found', message: 'That result is gone. Run the query again.' };
    const lines = [result.columns.map(c => c.name).join('\t')];
    for (const row of mockSqlPage(result, 0, result.total)) lines.push(row.map(v => (v == null ? 'NULL' : v)).join('\t'));
    return { ok: true, text: lines.join('\r\n') };
  },

  /* ---- Virtual tables: results copied into vt.db, queried in SQL - VT ---- */
  async sql_vt_list() {
    const api = await waitForBridge();
    if (api?.sql_vt_list) return await api.sql_vt_list();
    return { ok: true, tables: mockSql.vtables.map(t => ({ ...t })) };
  },

  /** Keeps one result of a tab's last run as a virtual table; replace overwrites one of that name. */
  async sql_save_vt(session_id, result_index, name, replace = false) {
    const api = await waitForBridge();
    if (api?.sql_save_vt) return await api.sql_save_vt(session_id, result_index, name, replace);
    const session = mockSql.sessions.get(session_id);
    const result = session?.results[result_index];
    if (!result) return { error: 'not_found', message: 'That result is gone. Run the query again.' };
    if (!/^[A-Za-z_][A-Za-z0-9_]{0,62}$/.test(name)) return { error: 'invalid_input', message: 'Letters, digits and _ only.' };
    const existing = mockSql.vtables.find(t => t.name.toLowerCase() === name.toLowerCase());
    if (existing && !replace) return { error: 'exists', message: `A virtual table named ${existing.name} already exists.` };
    const table = {
      name, rows: result.total, columns: result.columns.map(c => c.name),
      source: { connection: session.connection.id, name: session.connection.name, where: session.connection.where },
      query: (session.lastText || '').split('\n')[0], created: new Date().toISOString()
    };
    mockSql.vtables = [...mockSql.vtables.filter(t => t !== existing), table].sort((a, b) => a.name.localeCompare(b.name));
    return { ok: true, table: { name, rows: table.rows, columns: table.columns }, tables: mockSql.vtables.map(t => ({ ...t })) };
  },

  async sql_rename_vt(name, new_name) {
    const api = await waitForBridge();
    if (api?.sql_rename_vt) return await api.sql_rename_vt(name, new_name);
    if (mockSql.vtables.some(t => t.name.toLowerCase() === new_name.toLowerCase() && t.name !== name)) {
      return { error: 'exists', message: `A virtual table named ${new_name} already exists.` };
    }
    mockSql.vtables = mockSql.vtables.map(t => (t.name === name ? { ...t, name: new_name } : t));
    return { ok: true, tables: mockSql.vtables.map(t => ({ ...t })) };
  },

  async sql_delete_vt(name) {
    const api = await waitForBridge();
    if (api?.sql_delete_vt) return await api.sql_delete_vt(name);
    mockSql.vtables = mockSql.vtables.filter(t => t.name !== name);
    return { ok: true, tables: mockSql.vtables.map(t => ({ ...t })) };
  },

  /** Closes one tab's connection, or every one when no id is given. */
  async sql_close(session_id = null) {
    const api = await waitForBridge();
    if (api?.sql_close) return await api.sql_close(session_id);
    if (session_id == null) mockSql.sessions.clear();
    else mockSql.sessions.delete(session_id);
    return { ok: true };
  },
};
