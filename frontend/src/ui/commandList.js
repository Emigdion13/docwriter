/* =================================================================
   COMMAND LIST  (frontend/src/ui/commandList.js)
   The CMD space's middle column: favorite commands, then recent ones.
   Clicking a command types it at the prompt; ▶ runs it straight away.
   ================================================================= */

import { icon } from '../icons.js';

export function createCommandList({ onInsert, onRun, onFavorite, onForget, onClearRecent }) {
  const section = document.createElement('section');
  section.className = 'cmdlist glass';
  section.setAttribute('aria-label', 'Commands');

  section.innerHTML = `
    <div class="nl-head">
      <div>
        <div class="nl-title">Commands</div>
        <div class="nl-count" id="cmd-count">0 favorites · 0 recent</div>
      </div>
      <div class="nl-actions">
        <button class="btn icon small" id="cmd-clear-recent" type="button" aria-label="Clear recent commands" title="Clear recent commands (favorites stay)">
          ${icon('trash', 15)}
        </button>
      </div>
    </div>
    <label class="search">
      ${icon('search', 15)}
      <input id="cmd-filter" placeholder="Filter commands" autocomplete="off" spellcheck="false" aria-label="Filter commands">
    </label>
    <form class="cmd-add" id="cmd-add" autocomplete="off">
      <input id="cmd-add-input" placeholder="Add a favorite command…" spellcheck="false" aria-label="Add a favorite command">
      <button class="btn icon small" type="submit" aria-label="Add to favorites" title="Add to favorites">${icon('plus', 15)}</button>
    </form>
    <div class="cmd-scroll">
      <div class="cmd-sec">
        <div class="cmd-sec-h">${icon('star', 12)}<span>Favorites</span><span class="c" id="cmd-fav-count">0</span></div>
        <div class="cmd-rows" id="cmd-favs" role="list" aria-label="Favorite commands"></div>
      </div>
      <div class="cmd-sec">
        <div class="cmd-sec-h">${icon('clock', 12)}<span>Recent</span><span class="c" id="cmd-recent-count">0</span></div>
        <div class="cmd-rows" id="cmd-recent" role="list" aria-label="Recent commands"></div>
      </div>
    </div>
  `;

  const filter = section.querySelector('#cmd-filter');
  const addForm = section.querySelector('#cmd-add');
  const addInput = section.querySelector('#cmd-add-input');
  let last = { favorites: [], recent: [], enabled: false };

  filter.addEventListener('input', () => renderCommands(last));
  filter.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && filter.value) {
      e.stopPropagation();
      filter.value = '';
      renderCommands(last);
    }
  });

  addForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const text = addInput.value;
    if (!text.trim()) return;
    if (await onFavorite?.(text.trim(), true)) addInput.value = '';
  });

  section.querySelector('#cmd-clear-recent').onclick = () => onClearRecent?.();

  section.addEventListener('click', (e) => {
    const row = e.target.closest('.cmd-row');
    if (!row) return;
    const command = row.dataset.command;
    if (e.target.closest('.cmd-run')) onRun?.(command);
    else if (e.target.closest('.cmd-star')) onFavorite?.(command, row.dataset.fav !== 'true');
    else if (e.target.closest('.cmd-x')) onForget?.(command);
    else if (e.target.closest('.cmd-text')) onInsert?.(command);
  });

  function iconButton(className, name, label) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `btn icon small ${className}`;
    button.setAttribute('aria-label', label);
    button.title = label;
    button.innerHTML = icon(name, 14); // a fixed icon, never command text
    return button;
  }

  function row(command, { favorite, inRecent }) {
    const el = document.createElement('div');
    el.className = 'cmd-row';
    el.setAttribute('role', 'listitem');
    el.dataset.command = command;
    el.dataset.fav = String(favorite);

    const text = document.createElement('button');
    text.type = 'button';
    text.className = 'cmd-text';
    text.textContent = command; // rule 12a: command text never goes through innerHTML
    text.title = `Put “${command}” at the prompt`;

    const star = iconButton(`cmd-star ${favorite ? 'on' : ''}`, 'star', favorite ? 'Remove from favorites' : 'Add to favorites');
    star.setAttribute('aria-pressed', String(favorite));
    const run = iconButton('cmd-run', 'play', 'Run it now');

    el.append(text, star, run);
    if (inRecent) el.append(iconButton('cmd-x', 'x', 'Remove from recent'));
    return el;
  }

  function empty(message) {
    const el = document.createElement('div');
    el.className = 'cmd-empty';
    el.textContent = message;
    return el;
  }

  function renderCommands(state) {
    last = state;
    const q = filter.value.trim().toLowerCase();
    const matches = (c) => !q || c.toLowerCase().includes(q);
    const favSet = new Set(state.favorites);
    const favs = state.favorites.filter(matches);
    const recent = state.recent.filter(matches);

    section.querySelector('#cmd-favs').replaceChildren(
      ...(favs.length
        ? favs.map(c => row(c, { favorite: true, inRecent: false }))
        : [empty(q ? 'No favorite matches.' : 'Star a command to keep it here, or add one above.')])
    );
    section.querySelector('#cmd-recent').replaceChildren(
      ...(recent.length
        ? recent.map(c => row(c, { favorite: favSet.has(c), inRecent: true }))
        : [empty(q ? 'No recent command matches.' : 'Commands you run show up here. Start one with a space to keep it out.')])
    );
    section.querySelector('#cmd-fav-count').textContent = String(state.favorites.length);
    section.querySelector('#cmd-recent-count').textContent = String(state.recent.length);
    section.querySelector('#cmd-count').textContent =
      `${state.favorites.length} favorite${state.favorites.length === 1 ? '' : 's'} · ${state.recent.length} recent`;
    section.querySelector('#cmd-clear-recent').disabled = !state.recent.length;
    section.classList.toggle('is-off', !state.enabled);
    filter.disabled = !state.enabled;
    addInput.disabled = !state.enabled;
  }

  return {
    element: section,
    render: renderCommands,
    focusFilter() {
      filter.focus();
      filter.select();
    }
  };
}
