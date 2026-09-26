/* =================================================================
   STATUS BAR  (frontend/src/ui/statusBar.js)
   Bottom glass footer: Save state, Auto-lock countdown ring,
   Backup progress, Effects toggle, Theme toggle, Format info.
   ================================================================= */

import { icon } from '../icons.js';
import { replay } from './effects.js';

export function createStatusBar({ onToggleFx, onCycleTheme }) {
  const footer = document.createElement('footer');
  footer.className = 'statusbar glass';
  // main.js replaces the placeholder <footer id="statusbar"> with this element,
  // so the id has to travel with it (styles and debugging both look for it).
  footer.id = 'statusbar';

  footer.innerHTML = `
    <span class="st-item">
      <span class="dot" id="save-dot"></span>
      <span id="save-text">Saved</span>
    </span>
    <span class="st-item" id="st-lock"></span>
    <span class="st-item" id="st-backup"></span>
    <span class="spacer"></span>
    <button class="st-item st-btn" id="st-fx" title="Change effects level">Effects · Full</button>
    <button class="st-item st-btn" id="st-theme" title="Change theme">Nebula</button>
    <span class="st-item">Markdown · UTF-8</span>
  `;

  footer.querySelector('#st-fx').onclick = () => onToggleFx?.();
  footer.querySelector('#st-theme').onclick = () => onCycleTheme?.();

  return footer;
}

export function setSavingState(isSaving) {
  const dot = document.getElementById('save-dot');
  const text = document.getElementById('save-text');
  if (!dot || !text) return;

  dot.classList.toggle('saving', isSaving);
  if (isSaving) dot.classList.remove('error');
  text.textContent = isSaving ? 'Saving…' : 'Saved';
  if (!isSaving) text.removeAttribute('title');

  if (!isSaving) {
    replay(dot, 'flash');
  }
}

/**
 * Shows a failed save (full disk, no permission, damaged encrypted file).
 * The editor keeps the text, so the status bar must not claim "Saved" (M7).
 */
export function setSaveError(message) {
  const dot = document.getElementById('save-dot');
  const text = document.getElementById('save-text');
  if (!dot || !text) return;

  dot.classList.remove('saving');
  dot.classList.add('error');
  text.textContent = 'Not saved';
  text.title = message || 'The note could not be written to disk';
  replay(dot, 'flash');
}

export function updateLockCountdown(openVaults, remainingMs, totalMs = 600000) {
  const el = document.getElementById('st-lock');
  if (!el) return;

  if (!openVaults || openVaults.length === 0) {
    el.innerHTML = `${icon('lock', 13)}<span>All vaults locked</span>`;
    return;
  }

  const left = Math.max(0, remainingMs);
  const minutes = Math.floor(left / 60000);
  const seconds = Math.floor((left / 1000) % 60);
  const formattedSec = String(seconds).padStart(2, '0');

  const circumference = 2 * Math.PI * 6; // r = 6
  const progress = 1 - (left / Math.max(1, totalMs));
  const offset = circumference * progress;

  const who = openVaults.length === 1 ? `${openVaults[0].name} unlocked` : `${openVaults.length} vaults unlocked`;

  el.innerHTML = `
    <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true">
      <circle class="ring-bg" cx="8" cy="8" r="6"/>
      <circle class="ring-fg" cx="8" cy="8" r="6"
              stroke-dasharray="${circumference.toFixed(2)}"
              stroke-dashoffset="${offset.toFixed(2)}"/>
    </svg>
    <span>${who} · locks in ${minutes}:${formattedSec}</span>
  `;
}

export function updateBackupProgress(statusText, progressFraction = null) {
  const el = document.getElementById('st-backup');
  if (!el) return;

  if (progressFraction === null) {
    el.innerHTML = `${icon('cloud', 13)}<span>Backed up ${statusText}</span>`;
  } else {
    const pct = Math.min(100, Math.max(0, Math.round(progressFraction * 100)));
    el.innerHTML = `
      ${icon('sync', 13)}
      <span>${statusText}</span>
      <span class="progress"><i style="width:${pct}%"></i></span>
    `;
  }
}

export function setStatusBarTheme(name) {
  const el = document.getElementById('st-theme');
  if (el) el.textContent = name;
}

export function setStatusBarFx(levelName) {
  const el = document.getElementById('st-fx');
  if (el) el.textContent = `Effects · ${levelName}`;
}
