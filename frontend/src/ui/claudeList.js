/* =================================================================
   CLAUDE LIST  (frontend/src/ui/claudeList.js)
   The Claude space's middle column: the chats, newest first.  Clicking
   one opens it; the ✕ takes it off the list (Claude Code keeps its own
   history file).  A dot shows a chat that is still being answered.
   ================================================================= */

import { icon } from '../icons.js';

function when(stamp) {
  if (!stamp) return '';
  const date = new Date(stamp);
  if (Number.isNaN(date.getTime())) return '';
  const today = new Date();
  const sameDay = date.toDateString() === today.toDateString();
  return sameDay
    ? date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
    : date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

export function createClaudeList({ onOpen, onNew, onForget }) {
  const section = document.createElement('section');
  section.className = 'cmdlist claudelist glass';
  section.setAttribute('aria-label', 'Claude chats');

  section.innerHTML = `
    <div class="nl-head">
      <div>
        <div class="nl-title">Chats</div>
        <div class="nl-count" id="cl-count">0 chats</div>
      </div>
      <div class="nl-actions">
        <button class="btn icon small" id="cl-new" type="button" aria-label="New chat" title="New chat (Ctrl+Shift+T)">
          ${icon('plus', 15)}
        </button>
      </div>
    </div>
    <label class="search">
      ${icon('search', 15)}
      <input id="cl-filter" placeholder="Filter chats" autocomplete="off" spellcheck="false" aria-label="Filter chats">
    </label>
    <div class="cmd-scroll">
      <div class="cmd-rows" id="cl-rows" role="list" aria-label="Chats"></div>
    </div>
    <p class="sql-driver">Claude Code keeps what was said in its own history. Here only the titles are kept.</p>
  `;

  const filter = section.querySelector('#cl-filter');
  let last = { enabled: false, chats: [], activeId: null };

  filter.addEventListener('input', () => render(last));
  filter.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && filter.value) {
      e.stopPropagation();
      filter.value = '';
      render(last);
    }
  });

  section.querySelector('#cl-new').onclick = () => onNew?.();

  section.addEventListener('click', (e) => {
    const row = e.target.closest('.cl-row');
    if (!row) return;
    if (e.target.closest('.cl-x')) onForget?.(row.dataset.id);
    else if (e.target.closest('.cl-open')) onOpen?.(row.dataset.id);
  });

  function forgetButton(title) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'btn icon small cl-x';
    button.setAttribute('aria-label', `Remove “${title}” from the list`);
    button.title = 'Remove from the list';
    button.innerHTML = icon('x', 14); // a fixed icon, never data
    return button;
  }

  function row(chat, active) {
    const el = document.createElement('div');
    el.className = 'cmd-row cl-row';
    el.classList.toggle('on', chat.id === active);
    el.setAttribute('role', 'listitem');
    el.dataset.id = chat.id;

    const open = document.createElement('button');
    open.type = 'button';
    open.className = 'sql-open cl-open';
    open.innerHTML = `<span class="ic">${icon('sparkle', 15)}</span><span class="txt"><span class="nm"></span><span class="sub"></span></span><span class="cl-dot"></span>`;
    // rule 12a: a title is what someone typed, so it is text, never HTML
    open.querySelector('.nm').textContent = chat.title || 'Chat';
    open.querySelector('.sub').textContent = chat.running ? 'Answering…' : when(chat.updated);
    open.classList.toggle('is-running', !!chat.running);
    open.title = chat.title || 'Chat';

    el.append(open, forgetButton(chat.title || 'Chat'));
    return el;
  }

  function empty(message) {
    const el = document.createElement('div');
    el.className = 'cmd-empty';
    el.textContent = message;
    return el;
  }

  function render(state) {
    last = { chats: [], activeId: null, ...state };
    const q = filter.value.trim().toLowerCase();
    const shown = last.chats.filter(c => !q || (c.title || '').toLowerCase().includes(q));
    section.querySelector('#cl-rows').replaceChildren(
      ...(shown.length
        ? shown.map(c => row(c, last.activeId))
        : [empty(q ? 'No chat matches.' : 'No chats yet. Write a message to start one.')])
    );
    const n = last.chats.length;
    section.querySelector('#cl-count').textContent = `${n} chat${n === 1 ? '' : 's'}`;
    section.classList.toggle('is-off', !last.enabled);
    filter.disabled = !last.enabled;
    section.querySelector('#cl-new').disabled = !last.enabled;
  }

  return {
    element: section,
    render,
    focusFilter() {
      filter.focus();
      filter.select();
    }
  };
}
