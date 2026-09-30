/* =================================================================
   TERMINAL  (frontend/src/ui/terminal.js)
   The CMD space: a real shell (CMD, PowerShell or Git Bash) drawn by
   xterm.js.  Python runs the shell in a Windows pseudo-console; keys go
   out through bridge.terminal_write and the screen comes back as
   terminal_output events.
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
        <select class="settings-select term-shell" id="term-shell" aria-label="Shell"></select>
        <button class="btn icon" id="term-clear" type="button" aria-label="Clear the screen" title="Clear the screen">
          ${icon('eraser', 16)}
        </button>
        <button class="btn icon" id="term-restart" type="button" aria-label="Restart the shell" title="Restart the shell">
          ${icon('refresh', 16)}
        </button>
      </div>
    </div>
    <div class="term-body">
      <div class="term-host" id="term-host"></div>
    </div>
    <div class="term-off">
      <div class="term-off-ic">${icon('terminal', 40)}</div>
      <span class="hud-tag">CMD</span>
      <h2>A command window inside VaultNotes</h2>
      <p>
        Run CMD, PowerShell or Git Bash right here. Favorite and recent
        commands sit in the list on the left, one click away.
      </p>
      <p class="term-warn">
        ${icon('shield', 15)}
        <span>A shell can run anything on this PC with your permissions, so it
        is off until you allow it. Windows will ask you to confirm.</span>
      </p>
      <button class="btn primary" id="term-enable" type="button">${icon('terminal', 16)}Turn on the CMD space…</button>
    </div>
  `;

  const host = root.querySelector('#term-host');
  const shellSelect = root.querySelector('#term-shell');
  const statusEl = root.querySelector('#term-status');
  const whereEl = root.querySelector('#term-where');

  let info = { enabled: false, shells: [], shell: 'cmd', recent: [], favorites: [] };
  let term = null;
  let fit = null;
  let sessionId = null;
  let sessionShell = null;
  let starting = null;
  let exited = false;
  let visible = false;

  /* Keys are sent one call at a time, in order: pywebview runs every call on
     its own thread, so two calls in flight could arrive swapped. */
  let outgoing = '';
  let sending = false;

  function setStatus(text) {
    statusEl.textContent = text;
  }

  function shellName(id) {
    return info.shells.find(s => s.id === id)?.name || 'Shell';
  }

  function paint() {
    root.classList.toggle('is-off', !info.enabled);
    shellSelect.replaceChildren(...info.shells.map(s => {
      const option = document.createElement('option');
      option.value = s.id;
      option.textContent = s.name;
      return option;
    }));
    shellSelect.value = sessionShell || info.shell;
    shellSelect.disabled = !info.enabled || !info.shells.length;
    whereEl.textContent = info.enabled ? shellName(sessionShell || info.shell) : 'Off';
    onStateChanged?.(publicState());
  }

  function publicState() {
    return {
      enabled: info.enabled,
      running: !!sessionId,
      shell: sessionShell || info.shell,
      shellName: shellName(sessionShell || info.shell),
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

  async function pump() {
    if (sending) return;
    sending = true;
    try {
      while (outgoing && sessionId) {
        const data = outgoing;
        outgoing = '';
        const res = await bridge.terminal_write(sessionId, data);
        if (res?.error === 'not_running') break;
      }
    } finally {
      sending = false;
    }
  }

  function send(data) {
    if (!sessionId || !data) return;
    outgoing += data;
    pump();
  }

  /* The whole logical line at buffer row `row`, wrapped rows joined. */
  function lineAt(row) {
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

  function recordLineLater() {
    const buf = term.buffer.active;
    const row = buf.baseY + buf.cursorY;
    const shell = sessionShell;
    setTimeout(async () => {
      const match = PROMPTS[shell]?.exec(lineAt(row));
      const command = match?.[1] ?? '';
      // A leading space keeps a command out of the list on purpose.
      if (!command.trim() || /^\s/.test(command)) return;
      setLists(await bridge.terminal_remember(command.trimEnd()));
    }, RECORD_DELAY_MS);
  }

  async function copySelection() {
    const text = term.getSelection();
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      notify?.('Copying was blocked. Select the text again and press Ctrl+Shift+C.');
    }
    term.clearSelection();
  }

  function buildTerminal() {
    term = new Terminal({
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
    fit = new FitAddon();
    term.loadAddon(fit);
    term.open(host);

    // The app's own shortcuts keep working; everything else is the shell's.
    term.attachCustomKeyEventHandler((e) => {
      if (e.type !== 'keydown') return true;
      const mod = e.ctrlKey || e.metaKey;
      const key = e.key.toLowerCase();
      if (mod && !e.shiftKey && !e.altKey && (key === 'k' || key === 'l' || e.key === '\\')) return false;
      if (mod && key === 'c' && (e.shiftKey || term.hasSelection())) {
        e.preventDefault();
        copySelection();
        return false;
      }
      // Ctrl+V / Ctrl+Shift+V: let the browser paste; xterm turns it into input.
      if (mod && key === 'v') return false;
      return true;
    });

    term.onData((data) => {
      if (exited) {
        if (data.includes('\r')) start(sessionShell || info.shell);
        return;
      }
      if (data.includes('\r')) recordLineLater();
      send(data);
    });

    let resizeTimer = null;
    term.onResize(({ cols, rows }) => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => {
        if (sessionId) bridge.terminal_resize(sessionId, cols, rows);
      }, 80);
    });

    new ResizeObserver(() => {
      if (visible && root.offsetParent !== null) {
        try { fit.fit(); } catch { /* not laid out yet */ }
      }
    }).observe(host);

    // Follow the app theme.
    new MutationObserver(() => {
      term.options.theme = xtermTheme();
    }).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
  }

  events.on('terminal_output', (data) => {
    if (term && data?.id && data.id === sessionId) term.write(data.data);
  });

  events.on('terminal_exit', (data) => {
    if (!data?.id || data.id !== sessionId) return;
    sessionId = null;
    if (!data.stopped) {
      exited = true;
      const code = data.code == null ? '' : ` (exit code ${data.code})`;
      term?.write(`\r\n\x1b[2m[${shellName(sessionShell)} ended${code}. Press Enter to start it again.]\x1b[0m\r\n`);
      setStatus('Ended');
    }
    paint();
  });

  async function start(shellId = info.shell) {
    if (!info.enabled) return false;
    if (starting) return starting;
    starting = (async () => {
      if (!term) buildTerminal();
      try { fit.fit(); } catch { /* hidden */ }
      exited = false;
      sessionId = null;
      outgoing = '';
      setStatus('Starting…');
      const res = await bridge.terminal_start(shellId, term.cols, term.rows);
      if (res?.error) {
        setStatus('');
        notify?.(res.message || 'The shell could not be started.');
        return false;
      }
      sessionId = res.id;
      sessionShell = res.shell;
      info.shell = res.shell;
      setStatus('');
      paint();
      if (visible) term.focus();
      return true;
    })();
    try {
      return await starting;
    } finally {
      starting = null;
    }
  }

  shellSelect.addEventListener('change', async () => {
    const wanted = shellSelect.value;
    if (wanted === sessionShell && sessionId) return;
    term?.reset();
    await start(wanted);
  });

  root.querySelector('#term-restart').onclick = async () => {
    term?.reset();
    await start(sessionShell || info.shell);
  };

  root.querySelector('#term-clear').onclick = () => {
    term?.clear();
    term?.focus();
  };

  root.querySelector('#term-enable').onclick = () => enable();

  async function refresh() {
    const res = await bridge.terminal_state();
    if (res?.error) return publicState();
    info = { ...info, ...res };
    if (res.running && !sessionId) {
      // The page was reloaded while a shell ran: start a fresh one instead of
      // showing a screen we cannot draw.
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
    if (visible) await start(info.shell);
    return true;
  }

  async function disable() {
    await bridge.terminal_disable();
    info.enabled = false;
    sessionId = null;
    exited = false;
    term?.reset();
    setStatus('');
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
      if (!sessionId && !exited) await start(info.shell);
      requestAnimationFrame(() => {
        try { fit?.fit(); } catch { /* hidden */ }
        term?.focus();
      });
    },

    hide() {
      visible = false;
    },

    focus() {
      term?.focus();
    },

    async restart(shellId) {
      term?.reset();
      return start(shellId || sessionShell || info.shell);
    },

    /** Types a command at the prompt; with run, presses Enter too. */
    async insert(command, run = false) {
      if (!info.enabled) return false;
      if (!sessionId && !(await start(sessionShell || info.shell))) return false;
      send((CLEAR_LINE[sessionShell] || '') + command + (run ? '\r' : ''));
      if (run) setLists(await bridge.terminal_remember(command));
      term?.focus();
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
