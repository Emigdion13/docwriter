/* =================================================================
   TERMINAL  (frontend/src/ui/terminal.js)
   The CMD space: a real shell (CMD, PowerShell or Git Bash) drawn by
   xterm.js, one shell per tab.  Python runs each shell in its own Windows
   pseudo-console; keys go out through bridge.terminal_write and the screen
   comes back as terminal_output events, both tagged with the tab's session.
   ================================================================= */

import { Terminal } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';
import '@xterm/xterm/css/xterm.css';

import { bridge, events } from '../bridge.js';
import { icon } from '../icons.js';

/* Where each shell's prompt ends, so the command typed after it can be read
   off the screen when Enter is pressed.  A line that does not look like a
   prompt (a program asking a question, a Python REPL) is never recorded. */
const PROMPTS = {
  cmd: /^(?:\([^)]*\) )?[A-Za-z]:\\[^>]*>(.*)$/,
  powershell: /^(?:\([^)]*\) )?PS [^>]*> ?(.*)$/,
  bash: /^(?:\([^)]*\) )?(?:\S+@\S+(?::\S*)?\s?)?[$#] ?(.*)$/
};

/* Keys that empty the prompt before a command from the list is typed in:
   Esc in CMD and PowerShell, End then Ctrl+U in bash. */
const CLEAR_LINE = { cmd: '\x1b', powershell: '\x1b', bash: '\x05\x15' };

// How long after Enter the line is read, so the shell's echo has landed.
const RECORD_DELAY_MS = 150;

// The most tabs open at once (Python's MAX_SESSIONS).
export const MAX_TABS = 8;

// Output that arrives before terminal_start has answered, kept per session.
const EARLY_LIMIT = 65_536;

const DARK_ANSI = {
  black: '#1b1e2e', red: '#fb7185', green: '#34d399', yellow: '#fbbf24',
  blue: '#60a5fa', magenta: '#c084fc', cyan: '#22d3ee', white: '#cbd5e1',
  brightBlack: '#5d6385', brightRed: '#fda4af', brightGreen: '#6ee7b7', brightYellow: '#fde68a',
  brightBlue: '#93c5fd', brightMagenta: '#d8b4fe', brightCyan: '#67e8f9', brightWhite: '#f8fafc'
};

const LIGHT_ANSI = {
  black: '#0f172a', red: '#be123c', green: '#047857', yellow: '#a16207',
  blue: '#1d4ed8', magenta: '#7e22ce', cyan: '#0e7490', white: '#475569',
  brightBlack: '#64748b', brightRed: '#e11d48', brightGreen: '#059669', brightYellow: '#b45309',
  brightBlue: '#2563eb', brightMagenta: '#9333ea', brightCyan: '#0891b2', brightWhite: '#1e293b'
};

function cssVar(name, fallback) {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || fallback;
}

function xtermTheme() {
  const light = document.documentElement.dataset.theme === 'arctic';
  return {
    ...(light ? LIGHT_ANSI : DARK_ANSI),
    background: '#00000000', // the glass panel shows through
    foreground: cssVar('--text', light ? '#0f172a' : '#e8eaf6'),
    cursor: cssVar('--cmd', '#fb923c'),
    cursorAccent: light ? '#ffffff' : '#07080f',
    selectionBackground: light ? 'rgba(15, 23, 42, 0.18)' : 'rgba(255, 255, 255, 0.22)'
  };
}

export function createTerminal({ onListsChanged, onStateChanged, notify }) {
  const root = document.createElement('div');
  root.className = 'ws-terminal';
  root.innerHTML = `
    <div class="term-head">
      <div class="crumb"><span class="crumb-dot"></span><span>CMD</span><span class="sep">/</span><span id="term-where">Shell</span></div>
      <div class="term-actions">
        <span class="meta" id="term-status"></span>
        <select class="settings-select term-shell" id="term-shell" aria-label="Shell for this tab" title="Shell for this tab"></select>
        <button class="btn icon" id="term-clear" type="button" aria-label="Clear the screen" title="Clear the screen">
          ${icon('eraser', 16)}
        </button>
        <button class="btn icon" id="term-restart" type="button" aria-label="Restart this tab's shell" title="Restart this tab's shell">
          ${icon('refresh', 16)}
        </button>
      </div>
    </div>
    <div class="term-tabs">
      <div class="term-tablist" id="term-tablist" role="tablist" aria-label="Shells"></div>
      <button class="btn icon small term-new" id="term-new" type="button" aria-label="New tab" title="New tab (Ctrl+Shift+T)">
        ${icon('plus', 15)}
      </button>
    </div>
    <div class="term-body" id="term-body">
      <div class="term-empty">
        <p>No shell is open.</p>
        <button class="btn" id="term-empty-new" type="button">${icon('plus', 15)}<span>New tab</span></button>
      </div>
    </div>
    <div class="term-off">
      <div class="term-off-ic">${icon('terminal', 40)}</div>
      <span class="hud-tag">CMD</span>
      <h2>A command window inside VaultNotes</h2>
      <p>
        Run CMD, PowerShell or Git Bash right here, several at once in tabs.
        Favorite and recent commands sit in the list on the left, one click
        away.
      </p>
      <p class="term-warn">
        ${icon('shield', 15)}
        <span>A shell can run anything on this PC with your permissions, so it
        is off until you allow it. Windows will ask you to confirm.</span>
      </p>
      <button class="btn primary" id="term-enable" type="button">${icon('terminal', 16)}Turn on the CMD space…</button>
    </div>
  `;

  const body = root.querySelector('#term-body');
  const tabList = root.querySelector('#term-tablist');
  const newButton = root.querySelector('#term-new');
  const shellSelect = root.querySelector('#term-shell');
  const statusEl = root.querySelector('#term-status');
  const whereEl = root.querySelector('#term-where');

  let info = { enabled: false, shells: [], shell: 'cmd', recent: [], favorites: [] };
  let visible = false;

  /* Every tab owns its xterm, its shell session and its outgoing keys:
     { key, shell, sessionId, exited, starting, status, unread, term, fit,
       host, outgoing, sending }. */
  const tabs = [];
  let active = null;
  let tabSeq = 0;
  const early = new Map();

  function shellName(id) {
    return info.shells.find(s => s.id === id)?.name || 'Shell';
  }

  function tabBySession(id) {
    return id ? tabs.find(t => t.sessionId === id) : undefined;
  }

  function tabByKey(key) {
    return tabs.find(t => String(t.key) === key);
  }

  /* "CMD", or "CMD 2" once a second CMD tab is open. */
  function tabLabel(tab) {
    const same = tabs.filter(t => t.shell === tab.shell);
    return same.length > 1 ? `${shellName(tab.shell)} ${same.indexOf(tab) + 1}` : shellName(tab.shell);
  }

  function paintTabs() {
    tabList.replaceChildren(...tabs.map((tab) => {
      const label = tabLabel(tab);
      const el = document.createElement('div');
      el.className = 'term-tab';
      el.classList.toggle('on', tab === active);
      el.classList.toggle('ended', tab.exited);
      el.classList.toggle('unread', tab.unread);
      el.dataset.key = String(tab.key);

      const pick = document.createElement('button');
      pick.type = 'button';
      pick.className = 'term-tab-pick';
      pick.setAttribute('role', 'tab');
      pick.setAttribute('aria-selected', String(tab === active));
      pick.tabIndex = tab === active ? 0 : -1;
      pick.innerHTML = `${icon('terminal', 13)}<span class="nm"></span><span class="dot"></span>`; // fixed markup only
      pick.querySelector('.nm').textContent = label;
      pick.title = tab.exited ? `${label} (ended)` : label;

      const close = document.createElement('button');
      close.type = 'button';
      close.className = 'term-tab-x';
      close.setAttribute('aria-label', `Close ${label}`);
      close.title = 'Close tab (Ctrl+Shift+W)';
      close.tabIndex = -1;
      close.innerHTML = icon('x', 12);

      el.append(pick, close);
      return el;
    }));
    newButton.disabled = !info.enabled || tabs.length >= MAX_TABS;
    root.classList.toggle('no-tabs', !tabs.length);
  }

  function paint() {
    root.classList.toggle('is-off', !info.enabled);
    shellSelect.replaceChildren(...info.shells.map(s => {
      const option = document.createElement('option');
      option.value = s.id;
      option.textContent = s.name;
      return option;
    }));
    shellSelect.value = active?.shell || info.shell;
    shellSelect.disabled = !info.enabled || !info.shells.length;
    whereEl.textContent = !info.enabled ? 'Off' : active ? tabLabel(active) : 'No shell';
    statusEl.textContent = active?.status || '';
    paintTabs();
    onStateChanged?.(publicState());
  }

  function publicState() {
    const shell = active?.shell || info.shell;
    return {
      enabled: info.enabled,
      running: tabs.some(t => t.sessionId),
      tabs: tabs.length,
      shell,
      shellName: shellName(shell),
      shells: info.shells,
      recent: info.recent,
      favorites: info.favorites
    };
  }

  function setLists(result) {
    if (Array.isArray(result?.recent)) info.recent = result.recent;
    if (Array.isArray(result?.favorites)) info.favorites = result.favorites;
    onListsChanged?.(publicState());
  }

  function setStatus(tab, text) {
    tab.status = text;
    if (tab === active) statusEl.textContent = text;
  }

  /* Keys are sent one call at a time per tab, in order: pywebview runs every
     call on its own thread, so two calls in flight could arrive swapped. */
  async function pump(tab) {
    if (tab.sending) return;
    tab.sending = true;
    try {
      while (tab.outgoing && tab.sessionId) {
        const data = tab.outgoing;
        tab.outgoing = '';
        const res = await bridge.terminal_write(tab.sessionId, data);
        if (res?.error === 'not_running') break;
      }
    } finally {
      tab.sending = false;
    }
  }

  function send(tab, data) {
    if (!tab?.sessionId || !data) return;
    tab.outgoing += data;
    pump(tab);
  }

  /* The whole logical line at buffer row `row`, wrapped rows joined. */
  function lineAt(term, row) {
    const buf = term.buffer.active;
    let start = row;
    while (start > 0 && buf.getLine(start)?.isWrapped) start--;
    let end = row;
    while (buf.getLine(end + 1)?.isWrapped) end++;
    let text = '';
    for (let i = start; i <= end; i++) {
      text += buf.getLine(i)?.translateToString(i === end) ?? '';
    }
    return text;
  }

  function recordLineLater(tab) {
    const buf = tab.term.buffer.active;
    const row = buf.baseY + buf.cursorY;
    const shell = tab.shell;
    setTimeout(async () => {
      if (!tabs.includes(tab)) return;
      const match = PROMPTS[shell]?.exec(lineAt(tab.term, row));
      const command = match?.[1] ?? '';
      // A leading space keeps a command out of the list on purpose.
      if (!command.trim() || /^\s/.test(command)) return;
      setLists(await bridge.terminal_remember(command.trimEnd()));
    }, RECORD_DELAY_MS);
  }

  async function copySelection(term) {
    const text = term.getSelection();
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      notify?.('Copying was blocked. Select the text again and press Ctrl+Shift+C.');
    }
    term.clearSelection();
  }

  function fitActive() {
    if (!visible || !active || root.offsetParent === null) return;
    try { active.fit.fit(); } catch { /* not laid out yet */ }
  }

  /* The tab keys, read inside the shell since that is where the focus is:
     Ctrl+Shift+T new, Ctrl+Shift+W close, Ctrl+Tab / Ctrl+PageDown next. */
  function tabKey(e) {
    const mod = e.ctrlKey || e.metaKey;
    if (!mod || e.altKey) return null;
    const key = e.key.toLowerCase();
    if (e.shiftKey && key === 't') return () => newTab();
    if (e.shiftKey && key === 'w') return () => closeTab(active);
    if (e.key === 'Tab') return () => cycle(e.shiftKey ? -1 : 1);
    if (!e.shiftKey && (e.key === 'PageDown' || e.key === 'PageUp')) return () => cycle(e.key === 'PageUp' ? -1 : 1);
    return null;
  }

  function buildTab(shell) {
    const tab = {
      key: ++tabSeq, shell, sessionId: null, exited: false, starting: null, status: '', unread: false,
      outgoing: '', sending: false
    };
    tab.host = document.createElement('div');
    tab.host.className = 'term-host';
    body.appendChild(tab.host);

    tab.term = new Terminal({
      allowTransparency: true,
      cursorBlink: true,
      cursorStyle: 'bar',
      fontFamily: cssVar('--font-mono', 'Consolas, monospace'),
      fontSize: 13,
      lineHeight: 1.15,
      scrollback: 5000,
      theme: xtermTheme(),
      // A link a program prints (OSC 8) opens in the browser through the same
      // check as a note's links, never with window.open inside the app.
      linkHandler: {
        activate: (_event, uri) => {
          if (/^(https?:|mailto:)/i.test(uri)) bridge.open_external(uri);
        },
        allowNonHttpProtocols: false
      }
    });
    tab.fit = new FitAddon();
    tab.term.loadAddon(tab.fit);

    // The app's own shortcuts keep working; everything else is the shell's.
    tab.term.attachCustomKeyEventHandler((e) => {
      if (e.type !== 'keydown') return true;
      const mod = e.ctrlKey || e.metaKey;
      const key = e.key.toLowerCase();
      const tabAction = tabKey(e);
      if (tabAction) {
        e.preventDefault();
        tabAction();
        return false;
      }
      if (mod && !e.shiftKey && !e.altKey && (key === 'k' || key === 'l' || e.key === '\\')) return false;
      if (mod && key === 'c' && (e.shiftKey || tab.term.hasSelection())) {
        e.preventDefault();
        copySelection(tab.term);
        return false;
      }
      // Ctrl+V / Ctrl+Shift+V: let the browser paste; xterm turns it into input.
      if (mod && key === 'v') return false;
      return true;
    });

    tab.term.onData((data) => {
      if (tab.exited) {
        if (data.includes('\r')) startIn(tab, tab.shell, { reset: false });
        return;
      }
      if (data.includes('\r')) recordLineLater(tab);
      send(tab, data);
    });

    let resizeTimer = null;
    tab.term.onResize(({ cols, rows }) => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => {
        if (tab.sessionId) bridge.terminal_resize(tab.sessionId, cols, rows);
      }, 80);
    });

    new ResizeObserver(() => {
      if (tab === active) fitActive();
    }).observe(tab.host);

    return tab;
  }

  // Follow the app theme.
  new MutationObserver(() => {
    for (const tab of tabs) tab.term.options.theme = xtermTheme();
  }).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });

  events.on('terminal_output', (data) => {
    if (!data?.id) return;
    const tab = tabBySession(data.id);
    if (!tab) {
      // terminal_start has not answered yet: keep it for the tab it is for.
      const kept = (early.get(data.id) || '') + data.data;
      if (kept.length <= EARLY_LIMIT) early.set(data.id, kept);
      return;
    }
    tab.term.write(data.data);
    if (tab !== active && !tab.unread) {
      tab.unread = true;
      paintTabs();
    }
  });

  events.on('terminal_exit', (data) => {
    early.delete(data?.id);
    const tab = tabBySession(data?.id);
    if (!tab) return;
    tab.sessionId = null;
    if (!data.stopped) {
      tab.exited = true;
      const code = data.code == null ? '' : ` (exit code ${data.code})`;
      tab.term.write(`\r\n\x1b[2m[${shellName(tab.shell)} ended${code}. Press Enter to start it again, or close the tab.]\x1b[0m\r\n`);
      setStatus(tab, 'Ended');
    }
    paint();
  });

  /* Starts (or restarts) the shell in a tab; reset: false keeps the old screen. */
  async function startIn(tab, shellId = tab.shell, { reset = true } = {}) {
    if (!info.enabled) return false;
    if (tab.starting) return tab.starting;
    tab.starting = (async () => {
      if (tab === active) fitActive();
      const old = tab.sessionId;
      tab.sessionId = null;
      tab.exited = false;
      tab.outgoing = '';
      if (old) await bridge.terminal_stop(old);
      if (reset) tab.term.reset();
      setStatus(tab, 'Starting…');
      const res = await bridge.terminal_start(shellId, tab.term.cols, tab.term.rows);
      if (!tabs.includes(tab)) {
        // Closed while it was starting.
        if (res?.id) bridge.terminal_stop(res.id);
        return false;
      }
      if (res?.error) {
        setStatus(tab, '');
        notify?.(res.message || 'The shell could not be started.');
        return false;
      }
      tab.sessionId = res.id;
      tab.shell = res.shell;
      info.shell = res.shell;
      setStatus(tab, '');
      const kept = early.get(res.id);
      early.delete(res.id);
      if (kept) tab.term.write(kept);
      paint();
      if (visible && tab === active) tab.term.focus();
      return true;
    })();
    try {
      return await tab.starting;
    } finally {
      tab.starting = null;
    }
  }

  function activate(tab) {
    if (!tab) return;
    active = tab;
    tab.unread = false;
    for (const t of tabs) t.host.classList.toggle('on', t === tab);
    paint();
    if (!visible) return;
    requestAnimationFrame(() => {
      fitActive();
      if (active === tab) tab.term.focus();
    });
  }

  function cycle(step) {
    if (tabs.length < 2) return;
    const i = tabs.indexOf(active);
    activate(tabs[(i + step + tabs.length) % tabs.length]);
  }

  async function newTab(shellId = active?.shell || info.shell) {
    if (!info.enabled) return false;
    if (tabs.length >= MAX_TABS) {
      notify?.(`Up to ${MAX_TABS} tabs can be open at once. Close one first.`);
      return false;
    }
    const tab = buildTab(shellId);
    tabs.push(tab);
    activate(tab);
    tab.term.open(tab.host); // once it shows, so xterm can measure the font
    fitActive();
    const ok = await startIn(tab, shellId);
    // A tab whose shell never started is not kept.
    if (!ok && tabs.includes(tab) && !tab.sessionId) closeTab(tab);
    return ok;
  }

  function closeTab(tab) {
    const i = tabs.indexOf(tab);
    if (i < 0) return;
    tabs.splice(i, 1);
    const id = tab.sessionId;
    tab.sessionId = null;
    if (id) bridge.terminal_stop(id);
    tab.term.dispose();
    tab.host.remove();
    if (active === tab) {
      active = null;
      activate(tabs[Math.min(i, tabs.length - 1)]);
    }
    paint();
  }

  function closeAll() {
    for (const tab of tabs.splice(0)) {
      tab.sessionId = null;
      tab.term.dispose();
      tab.host.remove();
    }
    active = null;
    early.clear();
  }

  tabList.addEventListener('click', (e) => {
    const tab = tabByKey(e.target.closest('.term-tab')?.dataset.key);
    if (!tab) return;
    if (e.target.closest('.term-tab-x')) closeTab(tab);
    else activate(tab);
  });

  // Middle-click closes a tab, as in a browser.
  tabList.addEventListener('mousedown', (e) => {
    if (e.button === 1) e.preventDefault(); // no auto-scroll cursor
  });
  tabList.addEventListener('auxclick', (e) => {
    if (e.button !== 1) return;
    const tab = tabByKey(e.target.closest('.term-tab')?.dataset.key);
    if (tab) closeTab(tab);
  });

  tabList.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
      e.preventDefault();
      cycle(e.key === 'ArrowRight' ? 1 : -1);
      tabList.querySelector('.term-tab.on .term-tab-pick')?.focus();
    } else if (e.key === 'Delete') {
      e.preventDefault();
      closeTab(active);
    }
  });

  newButton.onclick = () => newTab();
  root.querySelector('#term-empty-new').onclick = () => newTab();

  shellSelect.addEventListener('change', async () => {
    const wanted = shellSelect.value;
    if (!active) {
      await newTab(wanted);
      return;
    }
    if (wanted === active.shell && active.sessionId) return;
    await startIn(active, wanted);
  });

  root.querySelector('#term-restart').onclick = async () => {
    if (active) await startIn(active);
    else await newTab();
  };

  root.querySelector('#term-clear').onclick = () => {
    active?.term.clear();
    active?.term.focus();
  };

  root.querySelector('#term-enable').onclick = () => enable();

  async function refresh() {
    const res = await bridge.terminal_state();
    if (res?.error) return publicState();
    info = { ...info, ...res };
    if (res.running?.length && !tabs.length) {
      // The page was reloaded while shells ran: start fresh ones instead of
      // showing screens we cannot draw.
      await bridge.terminal_stop();
    }
    paint();
    setLists(res);
    return publicState();
  }

  async function enable() {
    const res = await bridge.terminal_enable();
    if (res?.error) {
      if (res.error !== 'cancelled') notify?.(res.message || 'The CMD space could not be turned on.');
      return false;
    }
    info.enabled = true;
    paint();
    if (visible && !tabs.length) await newTab(info.shell);
    return true;
  }

  async function disable() {
    await bridge.terminal_disable();
    info.enabled = false;
    closeAll();
    paint();
  }

  return {
    element: root,
    refresh,
    enable,
    disable,
    getState: publicState,

    async show() {
      visible = true;
      paint();
      if (!info.enabled) return;
      if (!tabs.length) {
        await newTab(info.shell);
        return;
      }
      requestAnimationFrame(() => {
        fitActive();
        active?.term.focus();
      });
    },

    hide() {
      visible = false;
    },

    focus() {
      active?.term.focus();
    },

    /** Restarts the open tab's shell; with shellId, switches it to that shell. */
    async restart(shellId) {
      if (!active) return newTab(shellId);
      return startIn(active, shellId || active.shell);
    },

    /** Opens a new tab (with shellId, that shell). */
    newTab: (shellId) => newTab(shellId),

    /** Closes the open tab and its shell. */
    closeTab: () => closeTab(active),

    /** Types a command at the open tab's prompt; with run, presses Enter too. */
    async insert(command, run = false) {
      if (!info.enabled) return false;
      if (!active && !(await newTab(info.shell))) return false;
      const tab = active;
      if (!tab.sessionId && !(await startIn(tab))) return false;
      send(tab, (CLEAR_LINE[tab.shell] || '') + command + (run ? '\r' : ''));
      if (run) setLists(await bridge.terminal_remember(command));
      tab.term.focus();
      return true;
    },

    async setFavorite(command, favorite) {
      const res = await bridge.terminal_set_favorite(command, favorite);
      if (res?.error) {
        notify?.(res.message || 'That command could not be starred.');
        return false;
      }
      setLists(res);
      return true;
    },

    async forget(command) {
      setLists(await bridge.terminal_forget(command));
    },

    async clearRecent() {
      setLists(await bridge.terminal_clear_recent());
    }
  };
}
