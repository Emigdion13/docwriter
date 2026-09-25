/* =================================================================
   BRIDGE API  (frontend/src/bridge.js)
   Wraps window.pywebview.api + event bus.
   In Milestone M1, this provides a fake in-memory implementation
   with sample data matching section 4.8 of the build plan.
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

/* =================================================================
   M1 Fake in-memory database
   ================================================================= */

const fakeSpaces = [
  {
    id: 'plain',
    name: 'Plain',
    kind: 'plain',
    colorVar: '--plain',
    locked: false,
    keyPath: '',
    notes: [
      {
        id: 'p1',
        title: 'Shopping list',
        modified: 'Today 09:12',
        body: `# Shopping list\n\n- [x] Coffee beans\n- [ ] Oat milk\n- [ ] Batteries for the [[Home lab]] sensors\n- [ ] Birthday card for Ana`
      },
      {
        id: 'p2',
        title: 'Home lab',
        modified: 'Yesterday',
        body: `# Home lab\n\nIdeas for the little server shelf.\n\n| Device | Status |\n|---|---|\n| Raspberry Pi 5 | running |\n| NAS | ordering |\n| Air sensor | needs batteries |\n\nParts go on the [[Shopping list]].`
      },
      {
        id: 'p3',
        title: 'Reading list',
        modified: 'Sep 21',
        body: `# Reading list\n\n> One chapter a day, no excuses.\n\n- *Project Hail Mary*\n- *Snow Crash*\n- [[Book club]] picks for October`
      }
    ],
    trash: []
  },
  {
    id: 'encrypted',
    name: 'Encrypted',
    kind: 'vault',
    colorVar: '--encrypted',
    locked: false,
    keyPath: 'E:\\keys\\encrypted.vnkey',
    notes: [
      {
        id: 'e1',
        title: 'Project ideas',
        modified: '2 min ago',
        body: `# Project ideas\n\nThings to build in **2026**, ranked by excitement.\n\n## Next up\n- [x] Sketch the vault file format\n- [ ] Prototype the [[Unlock animation]]\n- [ ] Rotate the [[API keys]] before launch\n- [ ] Try a [[Graph view]] for links\n\n## Stack\n| Layer | Choice |\n|---|---|\n| Look | pywebview + Vite |\n| Crypto | AES-256-GCM |\n| Backup | Google Drive |\n\n\`\`\`python\ndef unlock(vault, key):\n    if not vault.verify(key):\n        raise WrongKey("This key belongs to a different vault")\n    return vault.decrypt_all(key)\n\`\`\`\n\n> Ship small, ship often.`
      },
      {
        id: 'e2',
        title: 'API keys',
        modified: 'Today 08:40',
        body: `# API keys\n\nRotate every **90 days**. They live only in this vault.\n\n- [x] Maps key rotated\n- [ ] Payments test key\n- [ ] Email service key\n\nBack to [[Project ideas]].`
      },
      {
        id: 'e3',
        title: 'Unlock animation',
        modified: 'Yesterday',
        body: `# Unlock animation\n\nTitles scramble through glyphs, then resolve left to right.\n\n- Duration: \`600ms\`, stagger \`70ms\` per row\n- Respect *reduced motion*\n- Used in [[Project ideas]]`
      },
      {
        id: 'e4',
        title: 'Contracts 2026',
        modified: 'Sep 18',
        body: `# Contracts 2026\n\n| Client | Renewal |\n|---|---|\n| Northwind | March |\n| Contoso | July |\n\n- [ ] Send renewal reminders in February`
      }
    ],
    trash: []
  },
  {
    id: 'personal',
    name: 'Personal',
    kind: 'vault',
    colorVar: '--personal',
    locked: true,
    keyPath: 'E:\\keys\\personal.vnkey',
    notes: [
      {
        id: 'x1',
        title: 'Journal',
        modified: 'Today 07:55',
        body: `# Journal\n\nPlanned the notes app today. It is going to look **amazing**.\n\nMood: 8/10\n\nNext: [[Travel 2026]]`
      },
      {
        id: 'x2',
        title: 'Travel 2026',
        modified: 'Sep 20',
        body: `# Travel 2026\n\n| City | When |\n|---|---|\n| Kyoto | April |\n| Lisbon | June |\n\n- [x] Pick the dates\n- [ ] Book flights\n- [ ] Renew passport\n\nWritten in the [[Journal]].`
      },
      {
        id: 'x3',
        title: 'Health',
        modified: 'Sep 12',
        body: `# Health\n\n- [x] Annual check-up\n- [ ] Start running again\n- [ ] Drink more water`
      }
    ],
    trash: []
  }
];

let fakeSettings = {
  notes_root: "C:/Users/you/Documents/VaultNotes",
  vaults: [
    { name: "Encrypted", folder: "vaults/encrypted", key_path: "E:/keys/encrypted.vnkey" },
    { name: "Personal",  folder: "vaults/personal",  key_path: "E:/keys/personal.vnkey" }
  ],
  autolock_minutes: 10,
  look: {
    theme: "nebula",
    effects: "full",
    view_mode: "split",
    editor_font_size: 13.5
  },
  backup: {
    enabled: false,
    interval_minutes: 60,
    drive_folder_id: null,
    last_backup: "10:02"
  }
};

/* =================================================================
   Helpers
   ================================================================= */

function getSpace(id) {
  const s = fakeSpaces.find(x => x.id === id);
  if (!s) throw new Error(`Space not found: ${id}`);
  return s;
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function escapeRegex(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function linkRegex(title, flags = 'gi') {
  return new RegExp('(\\[\\[\\s*)' + escapeRegex(title) + '((?:\\.md)?\\s*)(?=[\\|\\]\\#])', flags);
}

function snippetFromBody(body) {
  return body.split('\n')
    .map(l => l.trim())
    .filter(l => l && !/^(#|```|\|?\s*:?-{3})/.test(l))
    .map(l => l.replace(/^[-*]\s+(\[[ xX]\]\s+)?/, '').replace(/\[\[([^\]|#]+)[^\]]*\]\]/g, '$1').replace(/[*_`>]/g, '').replace(/\|/g, ' '))
    .join(' · ')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 120);
}

function countLinksInBody(body) {
  return (body.match(/\[\[[^\]]+\]\]/g) || []).length;
}

function calculateBacklinks(space, targetTitle) {
  if (space.locked) return [];
  const re = linkRegex(targetTitle, 'i');
  return space.notes
    .filter(n => n.title.toLowerCase() !== targetTitle.toLowerCase() && re.test(n.body))
    .map(n => ({ id: n.id, title: n.title }));
}

function highlightPythonCode(code) {
  return code.replace(/(#[^\n]*)|(&quot;[^\n]*?&quot;|'[^'\n]*')|\b(def|return|import|from|class|if|not|else|elif|for|in|while|with|as|raise|True|False|None)\b|(\w+)(?=\()|\b(\d+)\b/g,
    (m, com, str, kw, fn, num) => {
      if (com) return `<span class="tok-c">${com}</span>`;
      if (str) return `<span class="tok-s">${str}</span>`;
      if (kw) return `<span class="tok-k">${kw}</span>`;
      if (fn) return `<span class="tok-f">${fn}</span>`;
      if (num) return `<span class="tok-n">${num}</span>`;
      return m;
    });
}

function renderInlineMarkdown(text, space) {
  const codes = [];
  let s = text.replace(/`([^`]+)`/g, (m, c) => {
    codes.push(c);
    return `\u0000${codes.length - 1}\u0000`;
  });

  // Wikilinks: [[target|alias]] or [[target]] or [[target#heading]]
  s = s.replace(/\[\[([^\]|#]+)(#[^\]|]*)?(?:\|([^\]]+))?\]\]/g, (m, t, h, a) => {
    const rawTarget = t.trim().replace(/\.md$/i, '');
    const display = a ? a.trim() : rawTarget;
    const exists = space.notes.some(n => n.title.toLowerCase() === rawTarget.toLowerCase());
    const encoded = encodeURIComponent(rawTarget);
    if (exists) {
      return `<a class="wikilink" href="#vn-open/${encoded}">${escapeHtml(display)}</a>`;
    } else {
      return `<a class="wikilink missing" href="#vn-new/${encoded}" title="Not written yet">${escapeHtml(display)}</a>`;
    }
  });

  // External links [text](https://...)
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a class="ext" href="$2" target="_blank" rel="noopener noreferrer">$1</a>');

  // Bold & Italics
  s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/(^|[^*\w])\*([^*\s][^*]*)\*/g, '$1<em>$2</em>');

  // Restore inline code
  return s.replace(/\u0000(\d+)\u0000/g, (m, i) => `<code>${codes[i]}</code>`);
}

function markdownToHtml(source, space) {
  const lines = escapeHtml(source).split('\n');
  let out = '', i = 0;
  const parseRow = s => s.trim().replace(/^\||\|$/g, '').split('|').map(c => c.trim());

  while (i < lines.length) {
    const l = lines[i];

    // Code block
    if (/^```/.test(l)) {
      const lang = l.slice(3).trim();
      const codeLines = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i])) {
        codeLines.push(lines[i++]);
      }
      i++;
      const highlighted = highlightPythonCode(codeLines.join('\n'));
      out += `<pre class="code"><span class="lang">${lang || 'text'}</span><code>${highlighted}</code></pre>`;
      continue;
    }

    // Headings
    const h = l.match(/^(#{1,3})\s+(.*)$/);
    if (h) {
      out += `<h${h[1].length}>${renderInlineMarkdown(h[2], space)}</h${h[1].length}>`;
      i++;
      continue;
    }

    // Blockquote
    if (/^&gt;\s?/.test(l)) {
      const q = [];
      while (i < lines.length && /^&gt;\s?/.test(lines[i])) {
        q.push(lines[i++].replace(/^&gt;\s?/, ''));
      }
      out += `<blockquote>${renderInlineMarkdown(q.join(' '), space)}</blockquote>`;
      continue;
    }

    // Table
    if (/^\|/.test(l) && i + 1 < lines.length && /^\|?\s*:?-{3,}/.test(lines[i + 1])) {
      const head = parseRow(l);
      i += 2;
      const rows = [];
      while (i < lines.length && /^\|/.test(lines[i])) {
        rows.push(parseRow(lines[i++]));
      }
      out += `<table><thead><tr>${head.map(c => `<th>${renderInlineMarkdown(c, space)}</th>`).join('')}</tr></thead><tbody>${rows.map(r => `<tr>${r.map(c => `<td>${renderInlineMarkdown(c, space)}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
      continue;
    }

    // Task list / Bullet list
    if (/^\s*[-*]\s+/.test(l)) {
      const items = [];
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
        items.push(lines[i++].replace(/^\s*[-*]\s+/, ''));
      }
      out += '<ul>' + items.map(it => {
        const t = it.match(/^\[( |x|X)\]\s+(.*)$/);
        if (t) {
          const done = t[1] !== ' ';
          return `<li class="task${done ? ' done' : ''}"><span class="check">${done ? '✓' : ''}</span><span>${renderInlineMarkdown(t[2], space)}</span></li>`;
        }
        return `<li>${renderInlineMarkdown(it, space)}</li>`;
      }).join('') + '</ul>';
      continue;
    }

    if (!l.trim()) {
      i++;
      continue;
    }

    // Paragraph
    const p = [l];
    i++;
    while (i < lines.length && lines[i].trim() && !/^(#{1,3}\s|```|&gt;|\||\s*[-*]\s)/.test(lines[i])) {
      p.push(lines[i++]);
    }
    out += `<p>${renderInlineMarkdown(p.join(' '), space)}</p>`;
  }
  return out;
}

/* =================================================================
   Bridge API Implementation (Section 4.8)
   ================================================================= */

export const bridge = {
  /**
   * Returns current application state (spaces, look settings, backup info).
   */
  async get_state() {
    if (window.pywebview?.api?.get_state) return await window.pywebview.api.get_state();
    return {
      spaces: fakeSpaces.map(s => ({
        id: s.id,
        name: s.name,
        kind: s.kind,
        locked: s.locked,
        colorVar: s.colorVar,
        note_count: s.notes.length,
        key_path: s.keyPath
      })),
      look: { ...fakeSettings.look },
      backup: { ...fakeSettings.backup }
    };
  },

  /**
   * Lists notes in a space, with optional query filter and sorting.
   */
  async list_notes(space_id, query = "", sort = "modified") {
    if (window.pywebview?.api?.list_notes) return await window.pywebview.api.list_notes(space_id, query, sort);
    const s = getSpace(space_id);
    if (s.locked) return [];

    let filtered = s.notes;
    const q = (query || "").trim().toLowerCase();
    if (q) {
      filtered = filtered.filter(n =>
        n.title.toLowerCase().includes(q) || n.body.toLowerCase().includes(q)
      );
    }

    if (sort === "title") {
      filtered = [...filtered].sort((a, b) => a.title.localeCompare(b.title));
    }

    return filtered.map(n => ({
      id: n.id,
      title: n.title,
      snippet: snippetFromBody(n.body),
      modified: n.modified,
      link_count: countLinksInBody(n.body)
    }));
  },

  /**
   * Opens a note by space ID and note ID, returning body and backlinks.
   */
  async open_note(space_id, note_id) {
    if (window.pywebview?.api?.open_note) return await window.pywebview.api.open_note(space_id, note_id);
    const s = getSpace(space_id);
    if (s.locked) return { error: "locked", message: `${s.name} is locked` };
    const n = s.notes.find(x => x.id === note_id);
    if (!n) return { error: "not_found", message: "Note not found" };

    return {
      id: n.id,
      title: n.title,
      body: n.body,
      modified: n.modified,
      backlinks: calculateBacklinks(s, n.title)
    };
  },

  /**
   * Creates a new note in a space.
   */
  async create_note(space_id, title = "Untitled") {
    if (window.pywebview?.api?.create_note) return await window.pywebview.api.create_note(space_id, title);
    const s = getSpace(space_id);
    if (s.locked) return { error: "locked", message: `Unlock ${s.name} first` };

    let finalTitle = title.trim() || "Untitled";
    let counter = 2;
    while (s.notes.some(n => n.title.toLowerCase() === finalTitle.toLowerCase())) {
      finalTitle = `${title} (${counter++})`;
    }

    const newNote = {
      id: 'n_' + Math.random().toString(36).slice(2, 10),
      title: finalTitle,
      body: `# ${finalTitle}\n\n`,
      modified: "just now"
    };

    s.notes.unshift(newNote);
    return {
      ...newNote,
      snippet: "",
      link_count: 0,
      backlinks: []
    };
  },

  /**
   * Saves note body changes.
   */
  async save_note(space_id, note_id, body) {
    if (window.pywebview?.api?.save_note) return await window.pywebview.api.save_note(space_id, note_id, body);
    const s = getSpace(space_id);
    if (s.locked) return { error: "locked", message: "Vault is locked" };
    const n = s.notes.find(x => x.id === note_id);
    if (!n) return { error: "not_found", message: "Note not found" };

    n.body = body;
    n.modified = "just now";
    return { modified: n.modified };
  },

  /**
   * Renames a note and optionally updates links in other notes.
   */
  async rename_note(space_id, note_id, new_title, update_links = true) {
    if (window.pywebview?.api?.rename_note) return await window.pywebview.api.rename_note(space_id, note_id, new_title, update_links);
    const s = getSpace(space_id);
    if (s.locked) return { error: "locked", message: "Vault is locked" };
    const n = s.notes.find(x => x.id === note_id);
    if (!n) return { error: "not_found", message: "Note not found" };

    const targetTitle = new_title.trim();
    if (!targetTitle) return { error: "invalid_title", message: "Title cannot be empty" };

    if (s.notes.some(o => o !== n && o.title.toLowerCase() === targetTitle.toLowerCase())) {
      return { error: "collision", message: "A note with that title already exists" };
    }

    const oldTitle = n.title;
    let linksUpdated = 0;

    if (update_links) {
      s.notes.forEach(o => {
        if (o !== n) {
          o.body = o.body.replace(linkRegex(oldTitle, 'gi'), (m, a, b) => {
            linksUpdated++;
            return a + targetTitle + b;
          });
        }
      });
    }

    // Update note heading if it matches # Old Title
    n.body = n.body.replace(new RegExp('^#\\s+' + escapeRegex(oldTitle) + '\\s*$', 'm'), () => '# ' + targetTitle);
    n.title = targetTitle;
    n.modified = "just now";

    return {
      title: targetTitle,
      links_updated: linksUpdated
    };
  },

  /**
   * Counts how many links point to a note in the space.
   */
  async count_links_to(space_id, note_id) {
    if (window.pywebview?.api?.count_links_to) return await window.pywebview.api.count_links_to(space_id, note_id);
    const s = getSpace(space_id);
    const n = s.notes.find(x => x.id === note_id);
    if (!n) return { count: 0 };
    const count = calculateBacklinks(s, n.title).length;
    return { count };
  },

  /**
   * Moves a note to trash.
   */
  async delete_note(space_id, note_id) {
    if (window.pywebview?.api?.delete_note) return await window.pywebview.api.delete_note(space_id, note_id);
    const s = getSpace(space_id);
    const idx = s.notes.findIndex(x => x.id === note_id);
    if (idx === -1) return { error: "not_found", message: "Note not found" };

    const [deleted] = s.notes.splice(idx, 1);
    s.trash.push(deleted);
    return { ok: true, deleted };
  },

  /**
   * Restores a note from trash.
   */
  async restore_note(space_id, note_id) {
    if (window.pywebview?.api?.restore_note) return await window.pywebview.api.restore_note(space_id, note_id);
    const s = getSpace(space_id);
    const idx = s.trash.findIndex(x => x.id === note_id);
    if (idx === -1) return { error: "not_found", message: "Note not in trash" };

    const [restored] = s.trash.splice(idx, 1);
    s.notes.unshift(restored);
    return { ok: true, note: restored };
  },

  /**
   * Lists trash items.
   */
  async list_trash(space_id) {
    if (window.pywebview?.api?.list_trash) return await window.pywebview.api.list_trash(space_id);
    const s = getSpace(space_id);
    return s.trash.map(n => ({ id: n.id, title: n.title, modified: n.modified }));
  },

  /**
   * Moves a note between spaces.
   */
  async move_note(space_id, note_id, target_space_id) {
    if (window.pywebview?.api?.move_note) return await window.pywebview.api.move_note(space_id, note_id, target_space_id);
    const src = getSpace(space_id);
    const dst = getSpace(target_space_id);
    if (dst.locked) return { error: "locked", message: `Target vault ${dst.name} is locked` };

    const idx = src.notes.findIndex(x => x.id === note_id);
    if (idx === -1) return { error: "not_found", message: "Note not found" };

    const [note] = src.notes.splice(idx, 1);
    const brokenLinks = calculateBacklinks(src, note.title).length;
    dst.notes.unshift(note);
    return { new_id: note.id, broken_links: brokenLinks };
  },

  /**
   * Renders Markdown body to HTML.
   */
  async render_preview(space_id, body) {
    if (window.pywebview?.api?.render_preview) return await window.pywebview.api.render_preview(space_id, body);
    const s = getSpace(space_id);
    return markdownToHtml(body, s);
  },

  /**
   * Returns list of note titles for `[[` autocomplete suggestions.
   */
  async list_titles(space_id) {
    if (window.pywebview?.api?.list_titles) return await window.pywebview.api.list_titles(space_id);
    const s = getSpace(space_id);
    if (s.locked) return [];
    return s.notes.map(n => n.title);
  },

  /**
   * Unlocks a vault space.
   */
  async unlock_vault(space_id) {
    if (window.pywebview?.api?.unlock_vault) return await window.pywebview.api.unlock_vault(space_id);
    const s = getSpace(space_id);
    s.locked = false;
    return { ok: true, count: s.notes.length };
  },

  /**
   * Locks a single vault space.
   */
  async lock_vault(space_id) {
    if (window.pywebview?.api?.lock_vault) return await window.pywebview.api.lock_vault(space_id);
    const s = getSpace(space_id);
    if (s.kind === 'vault') s.locked = true;
    events.emit('vault_locked', { space_id });
    return { ok: true };
  },

  /**
   * Locks all vault spaces.
   */
  async lock_all() {
    if (window.pywebview?.api?.lock_all) return await window.pywebview.api.lock_all();
    const locked = [];
    fakeSpaces.forEach(s => {
      if (s.kind === 'vault') {
        s.locked = true;
        locked.push(s.id);
      }
    });
    events.emit('vault_locked', { space_ids: locked });
    return { ok: true, locked_spaces: locked };
  },

  /**
   * Resets idle auto-lock timer.
   */
  async touch() {
    if (window.pywebview?.api?.touch) return await window.pywebview.api.touch();
    return { locks_at: Date.now() + (fakeSettings.autolock_minutes * 60 * 1000) };
  },

  /**
   * Opens an external URL in default browser.
   */
  async open_external(url) {
    if (window.pywebview?.api?.open_external) return await window.pywebview.api.open_external(url);
    if (typeof window !== 'undefined') {
      console.log('open_external:', url);
    }
    return { ok: true };
  },

  /**
   * Retrieves settings.
   */
  async get_settings() {
    if (window.pywebview?.api?.get_settings) return await window.pywebview.api.get_settings();
    return { ...fakeSettings };
  },

  /**
   * Updates settings.
   */
  async update_settings(changes) {
    if (window.pywebview?.api?.update_settings) return await window.pywebview.api.update_settings(changes);
    fakeSettings = {
      ...fakeSettings,
      ...changes,
      look: { ...fakeSettings.look, ...(changes.look || {}) },
      backup: { ...fakeSettings.backup, ...(changes.backup || {}) }
    };
    return { ...fakeSettings };
  },

  /**
   * Backs up to Google Drive (simulated progression in M1).
   */
  async backup_now() {
    if (window.pywebview?.api?.backup_now) return await window.pywebview.api.backup_now();
    return { ok: true, last_backup: "just now" };
  },

  async connect_drive() {
    if (window.pywebview?.api?.connect_drive) return await window.pywebview.api.connect_drive();
    return { ok: true, connected: true };
  },

  async disconnect_drive() {
    if (window.pywebview?.api?.disconnect_drive) return await window.pywebview.api.disconnect_drive();
    return { ok: true, connected: false };
  },

  async restore_from_drive(target_folder) {
    if (window.pywebview?.api?.restore_from_drive) return await window.pywebview.api.restore_from_drive(target_folder);
    return { ok: true, restored_files: 12 };
  }
};
