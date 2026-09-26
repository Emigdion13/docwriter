/* =================================================================
   GRAPH VIEW  (frontend/src/ui/graphView.js)
   Notes of one space as glowing dots joined by their links (M10).

   The data is the engine's (`get_graph`), so a locked vault simply has no
   nodes and nothing here can reveal a title (security rule 11).  The layout is
   a small force simulation: repulsion between dots, a spring along each link,
   and a gentle pull to the middle.  It runs on a canvas, never in the DOM, so
   a few hundred notes stay smooth even with Effects set to Full.
   ================================================================= */

import { bridge } from '../bridge.js';
import { isCalm } from './effects.js';
import { icon } from '../icons.js';

const MAX_NODES = 400;       // with more notes, the busiest ones are drawn
const REPULSION = 5200;
const SPRING = 0.008;
const REST_LENGTH = 96;
const CENTER_PULL = 0.012;
const DAMPING = 0.86;
const PREWARM = 220;         // steps run before the first frame is drawn

function accentOf(space) {
  const variable = (space && space.colorVar) || '--accent';
  const raw = getComputedStyle(document.documentElement).getPropertyValue(variable).trim();
  return raw || '#7cd7ff';
}

function muted(hex) {
  // Same hue, dimmer: for ghost nodes (links to a note that is not written yet).
  return `${hex}66`;
}

export function createGraphOverlay({ onOpenNote } = {}) {
  const overlay = document.createElement('div');
  overlay.className = 'overlay';
  overlay.id = 'ov-graph';
  overlay.setAttribute('role', 'dialog');
  overlay.setAttribute('aria-modal', 'true');
  overlay.setAttribute('aria-labelledby', 'graph-title');

  overlay.innerHTML = `
    <div class="graph-panel">
      <div class="graph-head">
        <div>
          <h3 id="graph-title">Link graph</h3>
          <p class="graph-sub" id="graph-sub">Reading the link index…</p>
        </div>
        <button class="btn icon" id="graph-close" type="button" aria-label="Close the graph">
          ${icon('x', 16)}
        </button>
      </div>
      <canvas id="graph-canvas" aria-label="Graph of links between the notes in this space"></canvas>
      <div class="graph-foot" id="graph-foot">Drag a dot to move it · click a dot to open that note</div>
    </div>
  `;

  const canvas = overlay.querySelector('#graph-canvas');
  const ctx = canvas.getContext('2d');
  const subEl = overlay.querySelector('#graph-sub');
  const footEl = overlay.querySelector('#graph-foot');

  let raf = 0;
  let alpha = 0;
  let nodes = [];
  let edges = [];
  let color = '#7cd7ff';
  let hover = null;
  let dragging = null;
  let spaceId = null;

  const size = { w: 800, h: 600 };

  function resize() {
    const rect = canvas.getBoundingClientRect();
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    size.w = Math.max(200, Math.round(rect.width));
    size.h = Math.max(160, Math.round(rect.height));
    canvas.width = Math.round(size.w * dpr);
    canvas.height = Math.round(size.h * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    draw();
  }

  function seed() {
    const count = nodes.length || 1;
    const radius = Math.min(size.w, size.h) * 0.34;
    nodes.forEach((node, index) => {
      // A deterministic spiral keeps the layout stable between re-opens.
      const angle = index * 2.399963;           // golden angle
      const r = radius * Math.sqrt((index + 0.5) / count);
      node.x = size.w / 2 + r * Math.cos(angle);
      node.y = size.h / 2 + r * Math.sin(angle);
      node.vx = 0;
      node.vy = 0;
      node.r = 5 + Math.min(11, Math.sqrt(Math.max(0, node.links)) * 3.2);
    });
  }

  function step() {
    const calm = isCalm();
    for (let i = 0; i < nodes.length; i += 1) {
      const a = nodes[i];
      for (let j = i + 1; j < nodes.length; j += 1) {
        const b = nodes[j];
        let dx = b.x - a.x;
        let dy = b.y - a.y;
        let distSq = dx * dx + dy * dy;
        if (distSq < 0.01) {
          dx = (Math.random() - 0.5) * 0.5;
          dy = (Math.random() - 0.5) * 0.5;
          distSq = dx * dx + dy * dy;
        }
        const force = (REPULSION / distSq) * alpha;
        const push = force / Math.sqrt(distSq);
        a.vx -= dx * push;
        a.vy -= dy * push;
        b.vx += dx * push;
        b.vy += dy * push;
      }
    }

    for (const edge of edges) {
      const a = edge.a;
      const b = edge.b;
      if (!a || !b) continue;
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const dist = Math.sqrt(dx * dx + dy * dy) || 1;
      const force = (dist - REST_LENGTH) * SPRING * alpha;
      a.vx += dx * force;
      a.vy += dy * force;
      b.vx -= dx * force;
      b.vy -= dy * force;
    }

    for (const node of nodes) {
      node.vx += (size.w / 2 - node.x) * CENTER_PULL * alpha;
      node.vy += (size.h / 2 - node.y) * CENTER_PULL * alpha;
      if (node === dragging) {
        node.vx = 0;
        node.vy = 0;
        continue;
      }
      node.vx *= DAMPING;
      node.vy *= DAMPING;
      node.x += node.vx * (calm ? 0.6 : 1);
      node.y += node.vy * (calm ? 0.6 : 1);
      const pad = node.r + 6;
      node.x = Math.max(pad, Math.min(size.w - pad, node.x));
      node.y = Math.max(pad, Math.min(size.h - pad, node.y));
    }

    alpha = Math.max(0, alpha - 0.006);
  }

  function draw() {
    ctx.clearRect(0, 0, size.w, size.h);
    if (!nodes.length) return;
    const calm = isCalm();

    ctx.lineWidth = 1;
    ctx.strokeStyle = `${color}55`;
    ctx.beginPath();
    for (const edge of edges) {
      if (!edge.a || !edge.b) continue;
      ctx.moveTo(edge.a.x, edge.a.y);
      ctx.lineTo(edge.b.x, edge.b.y);
    }
    ctx.stroke();

    // Links to a note that is not written yet: dashed, and never clickable.
    ctx.save();
    ctx.setLineDash([3, 4]);
    ctx.strokeStyle = `${color}22`;
    ctx.beginPath();
    for (const edge of edges) {
      if (!edge.a || edge.b) continue;
      const ghost = edge.ghost;
      if (!ghost) continue;
      ctx.moveTo(edge.a.x, edge.a.y);
      ctx.lineTo(ghost.x, ghost.y);
    }
    ctx.stroke();
    ctx.restore();

    for (const node of nodes) {
      const isGhost = node.id === null;
      ctx.beginPath();
      ctx.arc(node.x, node.y, node.r, 0, Math.PI * 2);
      if (!calm) {
        ctx.shadowBlur = node === hover ? 26 : 14;
        ctx.shadowColor = isGhost ? muted(color) : color;
      }
      ctx.fillStyle = isGhost ? `${color}22` : node === hover ? '#ffffff' : color;
      ctx.fill();
      ctx.shadowBlur = 0;
      if (isGhost) {
        ctx.setLineDash([2, 3]);
        ctx.strokeStyle = `${color}88`;
        ctx.stroke();
        ctx.setLineDash([]);
      }
    }

    // Titles only for the dots worth naming, so the picture stays readable.
    ctx.font = '11px var(--font-ui, system-ui), system-ui';
    ctx.textAlign = 'center';
    ctx.fillStyle = 'rgba(232,242,255,.86)';
    const labelled = [...nodes]
      .sort((a, b) => b.links - a.links)
      .slice(0, calm ? 12 : 24);
    for (const node of labelled) {
      if (node === hover || node.links > 0 || labelled.length <= 8) {
        const text = node.title.length > 26 ? `${node.title.slice(0, 25)}…` : node.title;
        ctx.fillText(text, node.x, node.y - node.r - 6);
      }
    }
    if (hover) {
      const text = hover.title.length > 40 ? `${hover.title.slice(0, 39)}…` : hover.title;
      ctx.fillText(text, hover.x, hover.y + hover.r + 14);
    }
  }

  function tick() {
    step();
    draw();
    if (alpha > 0.01 || dragging) {
      raf = window.requestAnimationFrame(tick);
    } else {
      raf = 0;
      draw();
    }
  }

  function start() {
    if (!raf) raf = window.requestAnimationFrame(tick);
  }

  function nodeAt(x, y) {
    let best = null;
    let bestDist = Infinity;
    for (const node of nodes) {
      const dx = node.x - x;
      const dy = node.y - y;
      const dist = Math.sqrt(dx * dx + dy * dy);
      if (dist <= node.r + 7 && dist < bestDist) {
        best = node;
        bestDist = dist;
      }
    }
    return best;
  }

  function pointFromEvent(event) {
    const rect = canvas.getBoundingClientRect();
    return { x: event.clientX - rect.left, y: event.clientY - rect.top };
  }

  canvas.addEventListener('pointermove', (event) => {
    const { x, y } = pointFromEvent(event);
    if (dragging) {
      dragging.x = x;
      dragging.y = y;
      alpha = Math.max(alpha, 0.25);
      start();
      return;
    }
    const found = nodeAt(x, y);
    if (found !== hover) {
      hover = found;
      canvas.classList.toggle('is-pointing', !!found && found.id !== null);
      draw();
    }
  });

  canvas.addEventListener('pointerleave', () => {
    hover = null;
    draw();
  });

  canvas.addEventListener('pointerdown', (event) => {
    const { x, y } = pointFromEvent(event);
    const found = nodeAt(x, y);
    if (!found) return;
    dragging = found;
    canvas.setPointerCapture?.(event.pointerId);
    start();
  });

  canvas.addEventListener('pointerup', (event) => {
    if (!dragging) return;
    const point = pointFromEvent(event);
    const under = nodeAt(point.x, point.y);
    canvas.releasePointerCapture?.(event.pointerId);
    const node = dragging;
    dragging = null;
    alpha = Math.max(alpha, 0.12);
    start();
    // A click (no drag away) on a real note opens it; a ghost does nothing.
    if (under === node && node && node.id !== null) {
      onOpenNote?.(node.id, spaceId);
    }
  });

  overlay.addEventListener('mousedown', (event) => {
    if (event.target === overlay) close();
  });
  overlay.querySelector('#graph-close').addEventListener('click', close);

  function close() {
    overlay.classList.remove('open');
    if (raf) {
      window.cancelAnimationFrame(raf);
      raf = 0;
    }
    nodes = [];
    edges = [];
  }

  async function open(nextSpaceId, space) {
    spaceId = nextSpaceId;
    color = accentOf(space);
    overlay.classList.add('open');
    subEl.textContent = `Reading the links of ${space?.name || 'this space'}…`;
    footEl.textContent = 'Drag a dot to move it · click a dot to open that note';

    const graph = await bridge.get_graph(spaceId);
    const all = graph?.nodes || [];
    // With more notes than dots, draw the ones the most links point at.
    const shown = all.length > MAX_NODES
      ? [...all].sort((a, b) => (b.links || 0) - (a.links || 0)).slice(0, MAX_NODES)
      : all;
    nodes = shown.map(node => ({ ...node, x: 0, y: 0, vx: 0, vy: 0, r: 6 }));

    if (graph?.locked || !nodes.length) {
      edges = [];
      subEl.textContent = graph?.locked
        ? 'This vault is locked, so its notes are not here.'
        : `${space?.name || 'This space'} has no notes to graph yet.`;
      resize();
      draw();
      return;
    }

    // Ghost dots for links to notes that are not written yet: they show where a
    // note is wanted without pretending one exists.
    const byId = new Map(nodes.map(node => [node.id, node]));
    const ghosts = new Map();
    // Only a link to a note that does not exist becomes a ghost. A link to a
    // real note that was left off a crowded graph is simply not drawn.
    const wanted = (graph?.edges || []).filter(edge => !edge.resolved);
    for (const edge of wanted) {
      const title = String(edge.title || '').trim();
      if (!title) continue;
      if (!ghosts.has(title)) {
        const ghost = { id: null, title, links: 0, x: 0, y: 0, vx: 0, vy: 0, r: 4 };
        ghosts.set(title, ghost);
        nodes.push(ghost);
      }
      ghosts.get(title).links += 1;
    }

    edges = (graph?.edges || [])
      .map(edge => ({
        a: byId.get(edge.from) || null,
        b: edge.resolved ? byId.get(edge.to) || null : null,
        ghost: !edge.resolved ? ghosts.get(String(edge.title || '').trim()) || null : null
      }))
      .filter(edge => edge.a);

    const linkCount = edges.length;
    const realCount = all.length;
    subEl.textContent = `${realCount} note${realCount === 1 ? '' : 's'} · ${linkCount} link${linkCount === 1 ? '' : 's'}`
      + (all.length > MAX_NODES ? `, showing the ${MAX_NODES} with the most links` : '')
      + (ghosts.size ? ` · ${ghosts.size} link${ghosts.size === 1 ? '' : 's'} to a note not written yet` : '');

    resize();
    seed();
    alpha = 1;
    for (let i = 0; i < PREWARM; i += 1) step();
    // Ghosts sit just above the notes that want them, so they never crowd
    // the middle of the picture.
    for (const ghost of ghosts.values()) {
      const sources = edges.filter(edge => edge.ghost === ghost).map(edge => edge.a);
      if (!sources.length) continue;
      ghost.x = sources.reduce((sum, node) => sum + node.x, 0) / sources.length;
      ghost.y = sources.reduce((sum, node) => sum + node.y, 0) / sources.length - 70;
    }
    alpha = 0.35;
    start();
    draw();
  }

  // The global Escape handler closes any open overlay itself, so the animation
  // is stopped by watching the class rather than by handling the key here.
  if (typeof MutationObserver === 'function') {
    new MutationObserver(() => {
      if (!overlay.classList.contains('open') && raf) {
        window.cancelAnimationFrame(raf);
        raf = 0;
      }
    }).observe(overlay, { attributes: true, attributeFilter: ['class'] });
  }

  if (typeof ResizeObserver === 'function') {
    new ResizeObserver(() => {
      if (overlay.classList.contains('open')) {
        resize();
        start();
      }
    }).observe(canvas);
  }

  return { element: overlay, open, close };
}
