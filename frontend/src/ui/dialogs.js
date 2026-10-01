/* =================================================================
   DIALOGS  (frontend/src/ui/dialogs.js)
   Reusable confirm dialog and the Move-note dialog (M5).
   ================================================================= */

import { icon, spaceIcon } from '../icons.js';

/**
 * Shows a confirm dialog and resolves true when the user confirms.
 * @param {object} opts - { title, message, confirmLabel, cancelLabel, danger, iconName }
 * @returns {Promise<boolean>}
 */
export function confirmAction({ title, message, confirmLabel = 'Confirm', cancelLabel = 'Cancel', danger = false, iconName = 'alert' }) {
  return new Promise((resolve) => {
    const overlay = document.createElement('div');
    overlay.className = 'overlay';
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');

    overlay.innerHTML = `
      <div class="dialog confirm-dialog ${danger ? 'is-danger' : ''}">
        <div class="confirm-mark">${icon(iconName, 26)}</div>
        <h3></h3>
        <p class="dlg-sub"></p>
        <div class="dlg-actions">
          <button class="btn" type="button" data-x="cancel">Cancel</button>
          <button class="btn ${danger ? 'danger' : 'primary'}" type="button" data-x="ok"></button>
        </div>
      </div>
    `;
    overlay.querySelector('h3').textContent = title;
    overlay.querySelector('.dlg-sub').textContent = message;
    overlay.querySelector('[data-x="cancel"]').textContent = cancelLabel;
    const okBtn = overlay.querySelector('[data-x="ok"]');
    okBtn.textContent = confirmLabel;

    const done = (value) => {
      overlay.classList.remove('open');
      setTimeout(() => overlay.remove(), 260);
      resolve(value);
    };

    overlay.querySelector('[data-x="cancel"]').onclick = () => done(false);
    okBtn.onclick = () => done(true);
    overlay.addEventListener('mousedown', (e) => {
      if (e.target === overlay) done(false);
    });
    overlay.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') done(false);
      if (e.key === 'Enter') done(true);
    });

    document.getElementById('overlays-root')?.appendChild(overlay);
    requestAnimationFrame(() => overlay.classList.add('open'));
    setTimeout(() => overlay.querySelector('[data-x="cancel"]')?.focus(), 60);
  });
}

/**
 * Asks for one line of text; resolves the trimmed text, or null when cancelled.
 * @param {object} opts - { title, message, value, placeholder, confirmLabel, iconName, maxLength, colorVar }
 * @returns {Promise<string|null>}
 */
export function promptText({ title, message = '', value = '', placeholder = '', confirmLabel = 'Save', iconName = 'edit', maxLength = 100, colorVar = '' }) {
  return new Promise((resolve) => {
    const overlay = document.createElement('div');
    overlay.className = 'overlay';
    if (colorVar) overlay.style.setProperty('--c', `var(${colorVar})`);
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');

    overlay.innerHTML = `
      <form class="dialog confirm-dialog prompt-dialog" novalidate>
        <div class="confirm-mark">${icon(iconName, 26)}</div>
        <h3></h3>
        <p class="dlg-sub"></p>
        <div class="field-row"><input type="text" autocomplete="off" spellcheck="false"></div>
        <div class="dlg-actions">
          <button class="btn" type="button" data-x="cancel">Cancel</button>
          <button class="btn primary" type="submit" data-x="ok"></button>
        </div>
      </form>
    `;
    overlay.querySelector('h3').textContent = title;
    overlay.querySelector('.dlg-sub').textContent = message;
    overlay.querySelector('[data-x="ok"]').textContent = confirmLabel;
    const input = overlay.querySelector('input');
    input.value = value;
    input.placeholder = placeholder;
    input.maxLength = maxLength;
    input.setAttribute('aria-label', title);

    const done = (result) => {
      overlay.classList.remove('open');
      setTimeout(() => overlay.remove(), 260);
      resolve(result);
    };

    overlay.querySelector('[data-x="cancel"]').onclick = () => done(null);
    overlay.querySelector('form').addEventListener('submit', (e) => {
      e.preventDefault();
      const text = input.value.trim();
      if (text) done(text);
      else input.focus();
    });
    overlay.addEventListener('mousedown', (e) => {
      if (e.target === overlay) done(null);
    });
    overlay.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        done(null);
      }
    });

    document.getElementById('overlays-root')?.appendChild(overlay);
    requestAnimationFrame(() => overlay.classList.add('open'));
    setTimeout(() => {
      input.focus();
      input.select();
    }, 60);
  });
}

/**
 * Asks to pick one item from a list; a click picks it.  Resolves its id, or null.
 * @param {object} opts - { title, message, items: [{ id, label, sub, icon }], iconName, colorVar, empty }
 * @returns {Promise<string|null>}
 */
export function pickOne({ title, message = '', items = [], colorVar = '', empty = 'Nothing to pick.' }) {
  return new Promise((resolve) => {
    const overlay = document.createElement('div');
    overlay.className = 'overlay';
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');
    if (colorVar) overlay.style.setProperty('--c', `var(${colorVar})`);

    overlay.innerHTML = `
      <div class="dialog move-dialog pick-dialog">
        <h3></h3>
        <p class="dlg-sub"></p>
        <div class="move-targets pick-items" role="listbox"></div>
        <div class="dlg-actions">
          <button class="btn" type="button" data-x="cancel">Cancel</button>
        </div>
      </div>
    `;
    overlay.querySelector('h3').textContent = title;
    overlay.querySelector('.dlg-sub').textContent = message;
    const list = overlay.querySelector('.pick-items');
    if (!items.length) {
      const none = document.createElement('p');
      none.className = 'pick-empty';
      none.textContent = empty;
      list.appendChild(none);
    }
    for (const item of items) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'move-target';
      button.setAttribute('role', 'option');
      button.dataset.id = item.id;
      button.innerHTML = `<span class="ic">${icon(item.icon || 'table', 16)}</span><span class="txt"><span class="nm"></span><span class="sub"></span></span>`;
      button.querySelector('.nm').textContent = item.label; // text, never HTML
      button.querySelector('.sub').textContent = item.sub || '';
      list.appendChild(button);
    }

    const done = (result) => {
      overlay.classList.remove('open');
      setTimeout(() => overlay.remove(), 260);
      resolve(result);
    };

    list.addEventListener('click', (e) => {
      const button = e.target.closest('.move-target');
      if (button) done(button.dataset.id);
    });
    list.addEventListener('keydown', (e) => {
      if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
      e.preventDefault();
      const buttons = [...list.querySelectorAll('.move-target')];
      const i = buttons.indexOf(document.activeElement);
      buttons[(i + (e.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length]?.focus();
    });
    overlay.querySelector('[data-x="cancel"]').onclick = () => done(null);
    overlay.addEventListener('mousedown', (e) => {
      if (e.target === overlay) done(null);
    });
    overlay.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        done(null);
      }
    });

    document.getElementById('overlays-root')?.appendChild(overlay);
    requestAnimationFrame(() => overlay.classList.add('open'));
    setTimeout(() => (list.querySelector('.move-target') || overlay.querySelector('[data-x="cancel"]'))?.focus(), 60);
  });
}

/**
 * Creates the Move-note dialog. open() resolves the chosen target space id
 * (or null when cancelled).
 */
export function createMoveDialog() {
  const overlay = document.createElement('div');
  overlay.className = 'overlay';
  overlay.id = 'ov-move';
  overlay.setAttribute('role', 'dialog');
  overlay.setAttribute('aria-modal', 'true');
  overlay.setAttribute('aria-labelledby', 'move-title');

  overlay.innerHTML = `
    <div class="dialog move-dialog">
      <h3 id="move-title">Move note</h3>
      <p class="dlg-sub" id="move-sub"></p>
      <div class="move-targets" id="move-targets" role="radiogroup" aria-label="Target space"></div>
      <div class="move-warnings" id="move-warnings"></div>
      <div class="dlg-actions">
        <button class="btn" id="move-cancel" type="button">Cancel</button>
        <button class="btn primary" id="move-go" type="button">
          ${icon('move', 15)}Move note
        </button>
      </div>
    </div>
  `;

  let resolver = null;
  let selectedTarget = null;

  const close = (value) => {
    overlay.classList.remove('open');
    if (resolver) {
      resolver(value);
      resolver = null;
    }
  };

  overlay.querySelector('#move-cancel').onclick = () => close(null);
  overlay.querySelector('#move-go').onclick = () => close(selectedTarget);
  overlay.addEventListener('mousedown', (e) => {
    if (e.target === overlay) close(null);
  });
  overlay.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') close(null);
    if (e.key === 'Enter' && selectedTarget) close(selectedTarget);
  });

  function renderWarnings(sourceSpace, targetSpace, incomingLinks, outgoingLinks, lockedVaults) {
    const box = overlay.querySelector('#move-warnings');
    const parts = [];
    if (incomingLinks > 0 || outgoingLinks > 0) {
      const bits = [];
      if (incomingLinks > 0) bits.push(`${incomingLinks} incoming link${incomingLinks === 1 ? '' : 's'}`);
      if (outgoingLinks > 0) bits.push(`${outgoingLinks} outgoing link${outgoingLinks === 1 ? '' : 's'}`);
      parts.push(`
        <div class="setup-warning" data-link-warning>
          ${icon('alert', 15)}
          <span><b>${bits.join(' and ')}</b> will break. Links only work within the same space.</span>
        </div>`);
    }
    if (targetSpace.id === 'ai') {
      const unencrypted = sourceSpace.kind === 'vault' ? ', and it will be stored <b>unencrypted</b>' : '';
      parts.push(`
        <div class="setup-warning">
          ${icon('bot', 15)}
          <span>AI helpers can read and change notes in <b>AI-Notes</b>${unencrypted}.</span>
        </div>`);
    } else if (sourceSpace.kind === 'vault' && targetSpace.kind === 'plain') {
      parts.push(`
        <div class="setup-warning">
          ${icon('unlock', 15)}
          <span>The note will be stored <b>unencrypted</b> in ${targetSpace.name}.</span>
        </div>`);
    } else if (sourceSpace.kind === 'plain' && targetSpace.kind === 'vault') {
      parts.push(`
        <div class="move-info">
          ${icon('lock', 15)}
          <span>The note will be <b>encrypted</b> in ${targetSpace.name}.</span>
        </div>`);
    }
    box.innerHTML = parts.join('');
    // A Plain note's [[Plain:…]] links in a locked vault were not counted.
    // Added as text, never as HTML.
    const linkWarning = box.querySelector('[data-link-warning] span');
    if (linkWarning && lockedVaults.length) {
      linkWarning.append(
        ` ${lockedVaults.join(' and ')} ${lockedVaults.length === 1 ? 'is' : 'are'} locked, so links there were not counted.`
      );
    }
  }

  return {
    element: overlay,
    open({ spaces, currentSpaceId, noteTitle, incomingLinks = 0, outgoingLinks = 0, lockedVaults = [] }) {
      const source = spaces.find(s => s.id === currentSpaceId);
      const targets = spaces.filter(s => s.id !== currentSpaceId);
      const firstOpen = targets.find(t => !t.locked) || null;
      selectedTarget = firstOpen?.id || null;

      overlay.querySelector('#move-sub').textContent =
        `Move “${noteTitle}” from ${source?.name || 'this space'} to:`;

      const list = overlay.querySelector('#move-targets');
      list.innerHTML = '';
      targets.forEach((t) => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'move-target' + (t.id === selectedTarget ? ' sel' : '');
        btn.disabled = t.locked;
        btn.setAttribute('role', 'radio');
        btn.setAttribute('aria-checked', String(t.id === selectedTarget));
        btn.style.setProperty('--c', `var(${t.colorVar || '--accent'})`);
        btn.innerHTML = `
          <span class="ic">${icon(spaceIcon(t), 16)}</span>
          <span class="txt"><span class="nm"></span><span class="sub"></span></span>
        `;
        btn.querySelector('.nm').textContent = t.name;
        btn.querySelector('.sub').textContent = t.locked
          ? 'Locked — unlock it first'
          : t.id === 'ai' ? 'Unencrypted, AI helpers can edit' : t.kind === 'plain' ? 'Unencrypted' : 'Encrypted';
        btn.onclick = () => {
          if (t.locked) return;
          selectedTarget = t.id;
          list.querySelectorAll('.move-target').forEach(el => {
            const on = el === btn;
            el.classList.toggle('sel', on);
            el.setAttribute('aria-checked', String(on));
          });
          renderWarnings(source, t, incomingLinks, outgoingLinks, lockedVaults);
        };
        list.appendChild(btn);
      });

      const goBtn = overlay.querySelector('#move-go');
      goBtn.disabled = !selectedTarget;
      if (!targets.some(t => !t.locked)) {
        overlay.querySelector('#move-warnings').innerHTML = `
          <div class="setup-warning">
            ${icon('lock', 15)}
            <span>Every other space is locked. Unlock a vault to move this note.</span>
          </div>`;
      } else {
        renderWarnings(source, targets.find(t => t.id === selectedTarget), incomingLinks, outgoingLinks, lockedVaults);
      }

      overlay.classList.add('open');
      setTimeout(() => (selectedTarget ? goBtn : overlay.querySelector('#move-cancel'))?.focus(), 60);
      return new Promise((resolve) => { resolver = resolve; });
    },
    close: () => close(null)
  };
}
