/* =================================================================
   TOASTS  (frontend/src/ui/toasts.js)
   Glass message notifications with optional actions
   ================================================================= */

import { icon } from '../icons.js';

let toastsContainer = null;

export function initToasts() {
  toastsContainer = document.getElementById('toasts');
  if (!toastsContainer) {
    toastsContainer = document.createElement('div');
    toastsContainer.id = 'toasts';
    toastsContainer.className = 'toasts';
    toastsContainer.setAttribute('aria-live', 'polite');
    document.body.appendChild(toastsContainer);
  }
}

/**
 * Shows a toast message.
 * @param {string} msg - Message text
 * @param {object} opts - { icon: string, action: string, onAction: Function, duration: number }
 */
export function toast(msg, opts = {}) {
  if (!toastsContainer) initToasts();

  const toastEl = document.createElement('div');
  toastEl.className = 'toast';

  const iconName = opts.icon || 'sparkle';
  toastEl.innerHTML = `
    <span class="toast-ic">${icon(iconName, 15)}</span>
    <span class="toast-msg"></span>
  `;
  toastEl.querySelector('.toast-msg').textContent = msg;

  const dismiss = () => {
    toastEl.classList.add('out');
    setTimeout(() => {
      if (toastEl.parentNode) toastEl.parentNode.removeChild(toastEl);
    }, 300);
  };

  if (opts.action && typeof opts.onAction === 'function') {
    const btn = document.createElement('button');
    btn.className = 'btn small primary';
    btn.textContent = opts.action;
    btn.onclick = (e) => {
      e.stopPropagation();
      opts.onAction();
      dismiss();
    };
    toastEl.appendChild(btn);
  }

  toastsContainer.appendChild(toastEl);

  const duration = opts.duration || (opts.action ? 6000 : 3400);
  setTimeout(dismiss, duration);
}
