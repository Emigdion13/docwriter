/* =================================================================
   EFFECTS & MOTION  (frontend/src/ui/effects.js)
   Sci-fi decrypt scramble, seal scramble-out, effects levels
   ================================================================= */

const GLYPHS = '▓▒░<>/\\|{}[]=+*#%&@$0123456789ABCDEF';
const reducedMotionQuery = typeof window !== 'undefined'
  ? window.matchMedia('(prefers-reduced-motion: reduce)')
  : { matches: false };

let currentFx = 'full'; // 'full' | 'lite' | 'off'

export function randomGlyph() {
  return GLYPHS[(Math.random() * GLYPHS.length) | 0];
}

export function glyphs(count) {
  return Array.from({ length: count }, randomGlyph).join('');
}

export function isCalm() {
  return reducedMotionQuery.matches || currentFx === 'off';
}

export function getEffectsLevel() {
  return currentFx;
}

export function setEffectsLevel(level) {
  currentFx = level;
  const root = document.documentElement;
  root.classList.toggle('fx-lite', level === 'lite');
  root.classList.toggle('fx-off', level === 'off');
}

export function setAccentColor(colorVar) {
  if (typeof document !== 'undefined') {
    document.documentElement.style.setProperty('--accent', `var(${colorVar})`);
  }
}

export function replay(el, className) {
  if (!el) return;
  el.classList.remove(className);
  void el.offsetWidth;
  el.classList.add(className);
}

/**
 * Decrypt reveal animation: resolves glyphs from left to right.
 */
export function scramble(el, text, delay = 0, duration = 650) {
  if (!el) return;
  if (isCalm()) {
    el.textContent = text;
    return;
  }
  const start = performance.now() + delay;
  el.textContent = text.replace(/\S/g, randomGlyph);

  function frame(now) {
    const t = Math.max(0, Math.min(1, (now - start) / duration));
    const reveal = Math.floor(t * text.length);
    let s = '';
    for (let i = 0; i < text.length; i++) {
      s += (i < reveal || text[i] === ' ') ? text[i] : randomGlyph();
    }
    el.textContent = s;
    if (t < 1) {
      requestAnimationFrame(frame);
    } else {
      el.textContent = text;
    }
  }

  requestAnimationFrame(frame);
}

/**
 * Lock animation: scrambles text into glyphs from right to left.
 */
export function scrambleOut(el, delay = 0, duration = 380) {
  if (!el) return;
  if (isCalm()) return;
  const text = el.textContent || '';
  const start = performance.now() + delay;

  function frame(now) {
    const t = Math.max(0, Math.min(1, (now - start) / duration));
    const hide = Math.floor(t * text.length);
    let s = '';
    for (let i = 0; i < text.length; i++) {
      s += (i >= text.length - hide && text[i] !== ' ') ? randomGlyph() : text[i];
    }
    el.textContent = s;
    if (t < 1) {
      requestAnimationFrame(frame);
    }
  }

  requestAnimationFrame(frame);
}
