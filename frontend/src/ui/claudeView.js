/* =================================================================
   CLAUDE VIEW  (frontend/src/ui/claudeView.js)
   The Claude space: a chat with Claude Code instead of a terminal.  Python
   runs Claude Code and sends what it does as claude_event events (text,
   tool, tool_result, done); this file only draws them.  Each chat is a
   thread that keeps its own scroll position, so a reply keeps arriving
   while another chat is open.  The view follows the newest text only while
   it is already at the bottom, and a "Latest" button brings you back.
   ================================================================= */

import DOMPurify from 'dompurify';

import { bridge, events } from '../bridge.js';
import { icon } from '../icons.js';

// The same sanitizing as a note's preview.
const SANITIZE = {
  ADD_TAGS: [
    'div', 'span', 'code', 'pre', 'article', 'blockquote', 'table', 'thead',
    'tbody', 'tr', 'th', 'td', 'ul', 'ol', 'li', 'a', 'strong', 'em',
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'hr'
  ],
  ADD_ATTR: ['class', 'href', 'title']
};

// How long text is gathered before the Markdown is drawn again.
const RENDER_MS = 90;
// Within this many pixels of the bottom still counts as "at the bottom".
const STICK_PX = 48;
// Python's message when Allow starts a retry; shown as a note, not a bubble.
const RETRY_PREFIX = 'I have now allowed this';
// Events that arrive before claude_send has told us the new chat's id.
const EARLY_LIMIT = 500;

const STARTERS = [
  { label: 'What is in my AI-Notes?', text: 'What is in my AI-Notes? List the notes with one line each.' },
  { label: 'Summarize my recent Plain notes', text: 'Summarize my five most recently changed Plain notes.' },
  { label: 'Write an AI-Notes note…', text: 'Write a note in AI-Notes about ' }
];

export function createClaudeView({ onStateChanged, notify }) {
  const root = document.createElement('div');
  root.className = 'ws-claude';
  root.innerHTML = `
    <div class="term-head">
      <div class="crumb"><span class="crumb-dot"></span><span>Claude</span><span class="sep">/</span><span id="cl-where">New chat</span></div>
      <div class="term-actions">
        <span class="meta" id="cl-status"></span>
        <button class="btn icon" id="cl-new-chat" type="button" aria-label="New chat" title="New chat (Ctrl+Shift+T)">
          ${icon('plus', 16)}
        </button>
      </div>
    </div>
    <div class="cl-stage" id="cl-stage">
      <div class="cl-start" id="cl-start">
        <div class="term-off-ic">${icon('sparkle', 40)}</div>
        <h2>Ask Claude about your vault</h2>
        <p>Claude reads your notes and writes to AI-Notes. It asks before it changes anything else.</p>
        <div class="cl-starters" id="cl-starters"></div>
      </div>
      <button class="btn cl-jump" id="cl-jump" type="button" hidden>${icon('down', 14)}<span>Latest</span></button>
    </div>
    <form class="cl-composer" id="cl-composer" autocomplete="off">
      <textarea id="cl-input" rows="1" maxlength="100000" spellcheck="true"
        placeholder="Message Claude…  (Enter sends, Shift+Enter adds a line)" aria-label="Message Claude"></textarea>
      <button class="btn primary cl-send" id="cl-send" type="submit" aria-label="Send" title="Send (Enter)">${icon('right', 16)}</button>
      <button class="btn cl-stop" id="cl-stop" type="button" aria-label="Stop" title="Stop (Esc)" hidden>${icon('stop', 16)}</button>
    </form>
    <p class="cl-foot" id="cl-foot"></p>
    <div class="term-off cl-off">
      <div class="term-off-ic">${icon('sparkle', 40)}</div>
      <span class="hud-tag">Claude</span>
      <h2>Claude, in a tab of its own</h2>
      <p>
        Chat with Claude Code about your vault without a terminal: replies are drawn as
        text you can scroll, with what it does folded into one line each.
      </p>
      <p class="term-warn">
        ${icon('shield', 15)}
        <span>It runs Claude Code from your notes folder and can send the notes it reads
        to Anthropic. It asks before it writes or runs anything, and the Encrypted vault is
        always kept out. Windows will ask you to confirm.</span>
      </p>
      <p class="term-warn cl-missing" id="cl-missing" hidden>
        ${icon('alert', 15)}
        <span>Claude Code was not found on this PC. Install it, then reopen this tab.</span>
      </p>
      <button class="btn primary" id="cl-enable" type="button">${icon('sparkle', 16)}Turn on the Claude space…</button>
    </div>
  `;

  const stage = root.querySelector('#cl-stage');
  const startEl = root.querySelector('#cl-start');
  const jump = root.querySelector('#cl-jump');
  const form = root.querySelector('#cl-composer');
  const input = root.querySelector('#cl-input');
  const sendBtn = root.querySelector('#cl-send');
  const stopBtn = root.querySelector('#cl-stop');
  const whereEl = root.querySelector('#cl-where');
  const statusEl = root.querySelector('#cl-status');
  const footEl = root.querySelector('#cl-foot');

  let info = { enabled: false, available: true, folder: '', chats: [] };
  let visible = false;

  /* A thread is one chat: { key, id, title, el, inner, running, loaded, stick, scrollTop,
     draft, assistant, tools, cards, status }.  `id` is null until the first message is sent. */
  const threads = [];
  let active = null;
  let threadSeq = 0;
  const early = new Map();
  let replaying = false; // history is being drawn: repaint once at the end

  const byChat = (id) => (id ? threads.find(t => t.id === id) : undefined);

  // ---------------------------------------------------------------- state
  function publicState() {
    return {
      enabled: info.enabled,
      available: info.available,
      chats: info.chats,
      running: info.chats.filter(c => c.running).length,
      activeId: active?.id || null
    };
  }

  function paint() {
    root.classList.toggle('is-off', !info.enabled);
    root.querySelector('#cl-missing').hidden = info.available !== false;
    root.querySelector('#cl-enable').disabled = info.available === false;
    whereEl.textContent = !info.enabled ? 'Off' : (active?.title || 'New chat');
    const t = active;
    const empty = !!t && !t.inner.childElementCount;
    startEl.hidden = !(info.enabled && empty);
    statusEl.textContent = t?.status || '';
    sendBtn.hidden = !!t?.running;
    stopBtn.hidden = !t?.running;
    input.disabled = !info.enabled;
    footEl.textContent = info.enabled && info.folder ? `Runs in ${info.folder} · the Encrypted vault is kept out` : '';
    updateJump();
    onStateChanged?.(publicState());
  }

  function setStatus(thread, text) {
    thread.status = text;
    if (thread === active) statusEl.textContent = text;
  }

  // ------------------------------------------------------------ scrolling
  function atBottom(thread) {
    return thread.el.scrollHeight - thread.el.scrollTop - thread.el.clientHeight < STICK_PX;
  }

  function follow(thread) {
    if (thread.stick && thread === active && visible) thread.el.scrollTop = thread.el.scrollHeight;
  }

  function updateJump() {
    jump.hidden = !active || active.stick || !active.inner.childElementCount;
  }

  jump.onclick = () => {
    if (!active) return;
    active.stick = true;
    active.el.scrollTo({ top: active.el.scrollHeight, behavior: 'smooth' });
    updateJump();
  };

  // -------------------------------------------------------------- markdown
  async function renderBlock(block) {
    block.timer = null;
    if (block.rendering) {
      block.dirty = true;
      return;
    }
    block.rendering = true;
    const raw = block.raw;
    let html = null;
    try {
      html = await bridge.render_chat(raw);
    } catch {
      html = null;
    }
    block.rendering = false;
    if (!block.el.isConnected) return;
    if (typeof html === 'string') {
      block.el.innerHTML = DOMPurify.sanitize(html, SANITIZE);
      decorate(block.el);
    } else {
      block.el.textContent = raw;
    }
    if (block.dirty || block.raw !== raw) {
      block.dirty = false;
      scheduleRender(block);
    }
    follow(block.thread);
  }

  function scheduleRender(block, now = false) {
    if (block.timer) {
      if (!now) return;
      clearTimeout(block.timer);
    }
    block.timer = setTimeout(() => renderBlock(block), now ? 0 : RENDER_MS);
  }

  /* A Copy button on every code block. */
  function decorate(el) {
    for (const pre of el.querySelectorAll('pre')) {
      if (pre.querySelector('.cl-copy')) continue;
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'cl-copy';
      button.textContent = 'Copy';
      button.setAttribute('aria-label', 'Copy this code');
      pre.appendChild(button);
    }
  }

  async function copyText(text, button) {
    try {
      await navigator.clipboard.writeText(text);
      if (button) {
        const was = button.dataset.label || button.textContent;
        button.dataset.label = was;
        button.textContent = 'Copied';
        setTimeout(() => { button.textContent = was; }, 1200);
      }
    } catch {
      notify?.('Copying was blocked. Select the text and press Ctrl+C.');
    }
  }

  root.addEventListener('click', (e) => {
    const copyCode = e.target.closest('.cl-copy');
    if (copyCode) {
      const pre = copyCode.closest('pre');
      copyText((pre.querySelector('code') || pre).textContent, copyCode);
      return;
    }
    const copyMsg = e.target.closest('.cl-msg-copy');
    if (copyMsg) {
      const msg = copyMsg.closest('.cl-msg');
      copyText([...msg.querySelectorAll('.cl-md')].map(b => b.dataset.raw || b.textContent).join('\n\n'), copyMsg);
      return;
    }
    const link = e.target.closest('.cl-md a[href]');
    if (link) {
      e.preventDefault();
      const href = link.getAttribute('href') || '';
      if (/^(https?:|mailto:)/i.test(href)) bridge.open_external(href);
    }
  });

  // -------------------------------------------------------------- messages
  function buildThread(id, title) {
    const thread = {
      key: ++threadSeq, id, title: title || 'New chat', running: false, loaded: !id, stick: true, scrollTop: 0,
      draft: '', assistant: null, tools: new Map(), cards: [], status: ''
    };
    thread.el = document.createElement('div');
    thread.el.className = 'cl-thread';
    thread.inner = document.createElement('div');
    thread.inner.className = 'cl-inner';
    thread.el.appendChild(thread.inner);
    stage.insertBefore(thread.el, startEl);
    thread.el.addEventListener('scroll', () => {
      thread.stick = atBottom(thread);
      if (thread === active) updateJump();
    }, { passive: true });
    new ResizeObserver(() => {
      follow(thread);
      if (thread === active) updateJump();
    }).observe(thread.inner);
    threads.push(thread);
    return thread;
  }

  /* The assistant message is complete: draw its last text, and add its Copy bar. */
  function closeAssistant(thread) {
    const a = thread.assistant;
    if (!a) return;
    if (a.block) scheduleRender(a.block, true);
    if (a.el.querySelector('.cl-md') && !a.el.querySelector('.cl-actions')) {
      const bar = document.createElement('div');
      bar.className = 'cl-actions';
      bar.innerHTML = `<button class="cl-msg-copy" type="button">${icon('copy', 13)}<span>Copy</span></button>`;
      a.body.appendChild(bar);
    }
    thread.assistant = null;
  }

  function addUser(thread, text) {
    closeAssistant(thread);
    const el = document.createElement('div');
    el.className = 'cl-msg user';
    const bubble = document.createElement('div');
    bubble.className = 'cl-bubble';
    bubble.textContent = text; // what someone typed is text, never HTML
    el.appendChild(bubble);
    thread.inner.appendChild(el);
  }

  function addNote(thread, text, kind = '') {
    closeAssistant(thread);
    const el = document.createElement('div');
    el.className = `cl-note ${kind}`.trim();
    el.textContent = text;
    thread.inner.appendChild(el);
    return el;
  }

  function ensureAssistant(thread) {
    if (thread.assistant) return thread.assistant;
    const el = document.createElement('div');
    el.className = 'cl-msg assistant';
    el.innerHTML = `<div class="cl-avatar">${icon('sparkle', 14)}</div><div class="cl-body"></div>`;
    thread.inner.appendChild(el);
    thread.assistant = { el, body: el.querySelector('.cl-body'), block: null };
    return thread.assistant;
  }

  function appendText(thread, text) {
    const a = ensureAssistant(thread);
    if (!a.block) {
      const el = document.createElement('div');
      el.className = 'cl-md preview';
      a.body.appendChild(el);
      a.block = { el, raw: '', timer: null, rendering: false, dirty: false, thread };
    }
    a.block.raw += text;
    a.block.el.dataset.raw = a.block.raw;
    scheduleRender(a.block);
    setStatus(thread, 'Writing…');
  }

  function addTool(thread, ev) {
    const a = ensureAssistant(thread);
    a.block = null; // the next text is a new paragraph block
    const row = document.createElement('details');
    row.className = 'cl-tool running';
    row.innerHTML = `
      <summary><span class="cl-tool-ic">${icon('terminal', 13)}</span><span class="cl-tool-name"></span><span class="cl-tool-sum"></span><span class="cl-tool-state"></span></summary>
      <pre class="cl-tool-out"></pre>
    `;
    row.querySelector('.cl-tool-name').textContent = ev.name || 'Tool';
    row.querySelector('.cl-tool-sum').textContent = ev.summary || '';
    row.querySelector('.cl-tool-out').textContent = '';
    row.querySelector('summary').title = ev.summary || '';
    a.body.appendChild(row);
    if (ev.id) thread.tools.set(ev.id, row);
    setStatus(thread, `Using ${ev.name || 'a tool'}…`);
  }

  function toolResult(thread, ev) {
    const row = thread.tools.get(ev.id);
    if (!row) return;
    row.classList.remove('running');
    row.classList.add(ev.ok ? 'ok' : 'bad');
    row.querySelector('.cl-tool-state').textContent = ev.ok ? '' : 'refused or failed';
    const out = row.querySelector('.cl-tool-out');
    out.textContent = ev.preview || '';
    out.hidden = !ev.preview;
    setStatus(thread, 'Thinking…');
  }

  function addCard(thread, denials) {
    closeAssistant(thread);
    const card = document.createElement('div');
    card.className = 'cl-deny';
    card.innerHTML = `
      <div class="cl-deny-h">${icon('shield', 15)}<span>Claude needs your OK</span></div>
      <ul class="cl-deny-list"></ul>
      <div class="cl-deny-actions">
        <button class="btn primary" type="button" data-act="exact">Allow and continue</button>
        <button class="btn" type="button" data-act="tool"></button>
        <button class="btn" type="button" data-act="dismiss">Not now</button>
      </div>
    `;
    const list = card.querySelector('.cl-deny-list');
    for (const d of denials) {
      const li = document.createElement('li');
      const tool = document.createElement('b');
      tool.textContent = d.tool;
      const what = document.createElement('code');
      what.textContent = d.summary;
      li.append(tool, ' ', what);
      list.appendChild(li);
    }
    const tools = [...new Set(denials.map(d => d.tool))];
    card.querySelector('[data-act="tool"]').textContent = `Allow every ${tools.join(' and ')} in this chat`;
    const exact = card.querySelector('[data-act="exact"]');
    exact.disabled = !denials.every(d => d.exact);
    if (exact.disabled) exact.title = 'This can only be allowed for the whole tool.';
    card.dataset.ids = JSON.stringify(denials.map(d => d.id));
    card.dataset.exact = String(!exact.disabled);
    thread.inner.appendChild(card);
    thread.cards.push(card);
  }

  /* A card whose chance has passed: Python forgets a refusal when the next message goes out. */
  function expireCards(thread) {
    for (const card of thread.cards.splice(0)) card.remove();
  }

  stage.addEventListener('click', async (e) => {
    const button = e.target.closest('.cl-deny [data-act]');
    if (!button) return;
    const card = button.closest('.cl-deny');
    const thread = threads.find(t => t.cards.includes(card));
    if (!thread) return;
    const act = button.dataset.act;
    if (act === 'dismiss') {
      card.remove();
      thread.cards = thread.cards.filter(c => c !== card);
      return;
    }
    const buttons = [...card.querySelectorAll('button')];
    buttons.forEach(b => { b.disabled = true; });
    thread.running = true;
    thread.stick = true;
    setStatus(thread, 'Allowed. Claude is retrying…');
    paint();
    const res = await bridge.claude_allow(thread.id, JSON.parse(card.dataset.ids), act);
    if (res?.error) {
      thread.running = false;
      setStatus(thread, '');
      buttons.forEach(b => { b.disabled = false; });
      card.querySelector('[data-act="exact"]').disabled = card.dataset.exact !== 'true';
      notify?.(res.message || 'That could not be allowed.');
      paint();
      return;
    }
    card.classList.add('done');
    card.querySelector('.cl-deny-h span').textContent = 'Allowed';
    card.querySelector('.cl-deny-actions').remove();
    thread.cards = thread.cards.filter(c => c !== card);
    refreshChats();
  });

  function finish(thread, ev) {
    thread.running = false;
    setStatus(thread, '');
    closeAssistant(thread);
    if (ev.stopped) addNote(thread, 'Stopped.');
    else if (ev.error) addNote(thread, ev.error, 'error');
    if (Array.isArray(ev.denials) && ev.denials.length) addCard(thread, ev.denials);
    if (ev.gone) thread.id = null; // it never got going: the next message starts the chat fresh
  }

  /* One event from Python, or one replayed from history (replay: true). */
  function handle(thread, ev) {
    switch (ev.kind) {
      case 'user':
        if (String(ev.text).startsWith(RETRY_PREFIX)) addNote(thread, 'Allowed. Claude retried.');
        else addUser(thread, ev.text);
        break;
      case 'text': appendText(thread, ev.text); break;
      case 'tool': addTool(thread, ev); break;
      case 'tool_result': toolResult(thread, ev); break;
      case 'done': finish(thread, ev); refreshChats(); break;
      default: break;
    }
    follow(thread);
    if (thread === active && !replaying) paint();
  }

  events.on('claude_event', (ev) => {
    if (!ev?.chat) return;
    const thread = byChat(ev.chat);
    if (!thread) {
      // claude_send has not answered yet: keep it for the chat it is for.
      const kept = early.get(ev.chat) || [];
      if (kept.length < EARLY_LIMIT) kept.push(ev);
      early.set(ev.chat, kept);
      return;
    }
    handle(thread, ev);
  });

  // ---------------------------------------------------------------- chats
  async function refreshChats() {
    const res = await bridge.claude_state();
    if (res?.error) return;
    info = { ...info, ...res };
    paint();
  }

  function show_(thread) {
    if (active && active !== thread) {
      active.draft = input.value;
      active.scrollTop = active.el.scrollTop;
    }
    active = thread;
    for (const t of threads) t.el.classList.toggle('on', t === thread);
    input.value = thread.draft || '';
    autosize();
    paint();
    requestAnimationFrame(() => {
      if (active !== thread) return;
      if (thread.stick) thread.el.scrollTop = thread.el.scrollHeight;
      else thread.el.scrollTop = thread.scrollTop;
      updateJump();
      if (visible) input.focus();
    });
  }

  function newChat() {
    if (!info.enabled) return null;
    // An empty draft is reused instead of piling up.
    const blank = threads.find(t => !t.id && !t.inner.childElementCount && !t.running);
    const thread = blank || buildThread(null, 'New chat');
    show_(thread);
    return thread;
  }

  async function openChat(id) {
    if (!info.enabled) return;
    let thread = byChat(id);
    if (!thread) {
      const row = info.chats.find(c => c.id === id);
      if (!row) return;
      thread = buildThread(id, row.title);
      thread.loaded = false;
    }
    show_(thread);
    if (!thread.loaded) {
      thread.loaded = true;
      const res = await bridge.claude_history(id);
      if (res?.error) {
        notify?.(res.message || 'The earlier messages could not be read.');
      } else if (res.events?.length) {
        thread.stick = true;
        replaying = true;
        for (const ev of res.events) handle(thread, ev);
        replaying = false;
        closeAssistant(thread);
        for (const row of thread.inner.querySelectorAll('.cl-tool.running')) row.classList.remove('running');
        setStatus(thread, '');
      } else {
        addNote(thread, 'The earlier messages are not shown here, but Claude still remembers them. Carry on below.');
      }
      paint();
    }
  }

  async function forget(id) {
    const res = await bridge.claude_forget(id);
    if (res?.error) {
      notify?.(res.message || 'That chat could not be removed.');
      return;
    }
    const thread = byChat(id);
    if (thread) {
      threads.splice(threads.indexOf(thread), 1);
      thread.el.remove();
      if (active === thread) {
        active = null;
        newChat();
      }
    }
    info.chats = res.chats || info.chats.filter(c => c.id !== id);
    paint();
  }

  // ------------------------------------------------------------- composer
  function autosize() {
    input.style.height = 'auto';
    input.style.height = `${Math.min(input.scrollHeight, 220)}px`;
  }

  async function send(text) {
    const thread = active;
    text = String(text ?? '').trim();
    if (!thread || !text || thread.running || !info.enabled) return false;
    expireCards(thread);
    addUser(thread, text);
    thread.running = true;
    thread.stick = true;
    setStatus(thread, 'Thinking…');
    input.value = '';
    thread.draft = '';
    autosize();
    paint();
    const res = await bridge.claude_send(thread.id, text);
    if (res?.error) {
      thread.running = false;
      setStatus(thread, '');
      addNote(thread, res.message || 'The message could not be sent.', 'error');
      if (res.error === 'not_found') thread.id = null;
      paint();
      return false;
    }
    thread.title = res.chat.title || thread.title;
    if (!thread.id) {
      thread.id = res.chat.id;
      for (const ev of early.get(thread.id) || []) handle(thread, ev);
      early.delete(thread.id);
    }
    refreshChats();
    return true;
  }

  async function stop() {
    if (active?.running && active.id) await bridge.claude_stop(active.id);
  }

  form.addEventListener('submit', (e) => {
    e.preventDefault();
    send(input.value);
  });
  stopBtn.onclick = () => stop();
  input.addEventListener('input', autosize);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      send(input.value);
    } else if (e.key === 'Escape' && active?.running) {
      e.preventDefault();
      stop();
    }
  });

  root.querySelector('#cl-new-chat').onclick = () => { newChat(); };
  root.querySelector('#cl-enable').onclick = () => enable();

  const starters = root.querySelector('#cl-starters');
  for (const s of STARTERS) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'btn cl-starter';
    button.textContent = s.label;
    button.onclick = () => {
      input.value = s.text;
      autosize();
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
    };
    starters.appendChild(button);
  }

  // --------------------------------------------------------------- public
  async function refresh() {
    const res = await bridge.claude_state();
    if (res?.error) return publicState();
    info = { ...info, ...res };
    paint();
    return publicState();
  }

  async function enable() {
    const res = await bridge.claude_enable();
    if (res?.error) {
      if (res.error !== 'cancelled') notify?.(res.message || 'The Claude space could not be turned on.');
      return false;
    }
    info.enabled = true;
    await refreshChats();
    if (visible && !active) newChat();
    paint();
    return true;
  }

  async function disable() {
    await bridge.claude_disable();
    info.enabled = false;
    info.chats = [];
    for (const t of threads.splice(0)) t.el.remove();
    active = null;
    early.clear();
    paint();
  }

  return {
    element: root,
    refresh,
    enable,
    disable,
    getState: publicState,
    newChat,
    openChat,
    forget,
    stop,

    async show() {
      visible = true;
      await refreshChats();
      if (info.enabled && !active) newChat();
      paint();
      requestAnimationFrame(() => {
        if (active) follow(active);
        if (info.enabled) input.focus();
      });
    },

    hide() {
      visible = false;
    },

    focus() {
      input.focus();
    },

    /** Puts a message in a new chat and sends it (used by the palette). */
    async ask(text) {
      if (!info.enabled) return false;
      newChat();
      return send(text);
    }
  };
}
