/* =================================================================
   RESULT GRID  (frontend/src/ui/resultGrid.js)
   One SQL result on screen.  Python keeps every row; the grid only draws
   the rows in view and asks for the rest a page at a time, so a result of
   millions of rows scrolls as smoothly as one of ten.
   ================================================================= */

const ROW_H = 26;
const HEAD_H = 30;
const PAGE = 200;
// Browsers stop growing an element at a few million pixels; past this the
// scrollbar maps onto the rows instead of being one pixel per pixel.
const MAX_SCROLL_PX = 8_000_000;
// Pages kept at once (oldest dropped first).
const MAX_PAGES = 60;
const MIN_COL = 56;
const MAX_COL = 420;
const NUM_COL_MIN = 44;

let measureCtx = null;
function textWidth(text, font) {
  if (!measureCtx) measureCtx = document.createElement('canvas').getContext('2d');
  measureCtx.font = font;
  return measureCtx.measureText(text).width;
}

function cellText(value) {
  if (value === null || value === undefined) return null;
  if (value === true) return 'true';
  if (value === false) return 'false';
  return String(value);
}

/**
 * @param {object} opts
 * @param {(offset: number, limit: number) => Promise<any[][] | null>} opts.fetchRows
 * @param {(message: string) => void} [opts.notify]
 */
export function createResultGrid({ fetchRows, notify }) {
  const root = document.createElement('div');
  root.className = 'rg';
  root.tabIndex = 0;
  root.setAttribute('role', 'grid');
  root.innerHTML = `
    <div class="rg-head" role="row"></div>
    <div class="rg-canvas"><div class="rg-window"></div></div>
  `;
  const head = root.querySelector('.rg-head');
  const canvas = root.querySelector('.rg-canvas');
  const win = root.querySelector('.rg-window');

  let columns = [];
  let widths = [];
  let total = 0;
  let numWidth = NUM_COL_MIN;
  let pages = new Map();     // page index -> rows
  let loading = new Set();   // page indexes asked for
  let generation = 0;        // bumps on every new result, so late pages are dropped
  let selected = null;       // { row, col }
  let frame = 0;

  function cellFont() {
    const mono = getComputedStyle(document.documentElement).getPropertyValue('--font-mono').trim() || 'monospace';
    return `400 12px ${mono}`;
  }

  function sizeColumns(sample) {
    const font = cellFont();
    const headFont = font.replace('400', '600');
    widths = columns.map((col, i) => {
      const head = textWidth(col.name, headFont) + 30;
      let w = 0;
      for (const row of sample) {
        const text = cellText(row[i]);
        if (text === null) continue;
        w = Math.max(w, textWidth(text.length > 80 ? text.slice(0, 80) : text, font));
        if (w >= MAX_COL) break;
      }
      // Only the first rows are measured, so leave room for longer ones further down.
      return Math.round(Math.max(MIN_COL, Math.min(MAX_COL, Math.max(head, w * 1.25 + 26))));
    });
    numWidth = Math.max(NUM_COL_MIN, Math.round(textWidth(String(total), font) + 22));
  }

  function fullWidth() {
    return numWidth + widths.reduce((a, b) => a + b, 0);
  }

  function template() {
    return `${numWidth}px ${widths.map(w => `${w}px`).join(' ')}`;
  }

  function paintHead() {
    head.style.gridTemplateColumns = template();
    head.style.width = `${fullWidth()}px`;
    const corner = document.createElement('div');
    corner.className = 'rg-hc rg-num';
    head.replaceChildren(corner, ...columns.map((col, i) => {
      const el = document.createElement('div');
      el.className = `rg-hc k-${col.kind}`;
      el.setAttribute('role', 'columnheader');
      el.title = col.name;
      const name = document.createElement('span');
      name.textContent = col.name; // rule 12a: never innerHTML for data
      const grip = document.createElement('span');
      grip.className = 'rg-grip';
      grip.dataset.col = String(i);
      el.append(name, grip);
      return el;
    }));
  }

  function layout() {
    const tall = total * ROW_H;
    canvas.style.height = `${Math.min(tall, MAX_SCROLL_PX)}px`;
    canvas.style.width = `${fullWidth()}px`;
  }

  /* Where the scrollbar stands, in rows: the first row in view and how far it is scrolled off. */
  function position() {
    const view = Math.max(0, root.clientHeight - HEAD_H);
    const tall = total * ROW_H;
    const virtual = Math.min(tall, MAX_SCROLL_PX);
    const top = root.scrollTop;
    let px = top;
    if (tall > virtual && virtual > view) px = top * (tall - view) / (virtual - view);
    const first = Math.floor(px / ROW_H);
    return { first, shift: px - first * ROW_H, view, top };
  }

  function rowAt(index) {
    const page = pages.get(Math.floor(index / PAGE));
    return page ? page[index % PAGE] : undefined;
  }

  function want(pageIndex) {
    if (pages.has(pageIndex) || loading.has(pageIndex) || pageIndex * PAGE >= total) return;
    loading.add(pageIndex);
    const asked = generation;
    fetchRows(pageIndex * PAGE, PAGE).then((rows) => {
      if (asked !== generation) return;
      loading.delete(pageIndex);
      if (!rows) return;
      pages.set(pageIndex, rows);
      while (pages.size > MAX_PAGES) {
        const oldest = pages.keys().next().value;
        if (oldest === pageIndex) break;
        pages.delete(oldest);
      }
      schedule();
    }).catch(() => loading.delete(pageIndex));
  }

  function paint() {
    frame = 0;
    if (!columns.length) {
      win.replaceChildren();
      return;
    }
    const { first, shift, view, top } = position();
    const count = Math.min(total - first, Math.ceil((view + shift) / ROW_H) + 1);
    win.style.top = `${top}px`;
    win.style.height = `${view}px`;
    win.style.width = `${fullWidth()}px`;
    const tpl = template();
    const rows = [];
    for (let k = 0; k < count; k++) {
      const index = first + k;
      const values = rowAt(index);
      if (!values) want(Math.floor(index / PAGE));
      const row = document.createElement('div');
      row.className = 'rg-row';
      row.style.gridTemplateColumns = tpl;
      row.style.transform = `translateY(${k * ROW_H - shift}px)`;
      row.dataset.row = String(index);
      const num = document.createElement('div');
      num.className = 'rg-c rg-num';
      num.textContent = String(index + 1);
      row.appendChild(num);
      for (let i = 0; i < columns.length; i++) {
        const c = document.createElement('div');
        c.className = `rg-c k-${columns[i].kind}`;
        c.dataset.col = String(i);
        if (selected && selected.row === index && selected.col === i) c.classList.add('sel');
        if (!values) {
          c.classList.add('rg-wait');
        } else {
          const text = cellText(values[i]);
          if (text === null) {
            c.classList.add('rg-null');
            c.textContent = 'NULL';
          } else {
            c.textContent = text.length > 300 ? `${text.slice(0, 300)}…` : text;
          }
        }
        row.appendChild(c);
      }
      rows.push(row);
    }
    win.replaceChildren(...rows);
    // Look a page ahead in the direction of travel.
    want(Math.floor((first + count + PAGE / 2) / PAGE));
  }

  function schedule() {
    if (!frame) frame = requestAnimationFrame(paint);
  }

  root.addEventListener('scroll', schedule, { passive: true });
  new ResizeObserver(schedule).observe(root);

  /* Keep the selected cell in view after the keys moved it. */
  function reveal() {
    if (!selected) return;
    const { first, view } = position();
    const visible = Math.max(1, Math.floor(view / ROW_H));
    const tall = total * ROW_H;
    const virtual = Math.min(tall, MAX_SCROLL_PX);
    const scale = tall > virtual && virtual > view ? (virtual - view) / (tall - view) : 1;
    if (selected.row < first) root.scrollTop = selected.row * ROW_H * scale;
    else if (selected.row >= first + visible) root.scrollTop = (selected.row - visible + 1) * ROW_H * scale;
    let left = numWidth;
    for (let i = 0; i < selected.col; i++) left += widths[i];
    const right = left + widths[selected.col];
    if (left - numWidth < root.scrollLeft) root.scrollLeft = left - numWidth;
    else if (right > root.scrollLeft + root.clientWidth) root.scrollLeft = right - root.clientWidth;
    schedule();
  }

  root.addEventListener('mousedown', (e) => {
    const c = e.target.closest('.rg-c');
    const row = c?.parentElement?.dataset.row;
    if (!c || row === undefined || c.classList.contains('rg-num')) return;
    selected = { row: Number(row), col: Number(c.dataset.col) };
    schedule();
  });

  async function copySelected() {
    if (!selected) return;
    const values = rowAt(selected.row);
    if (!values) return;
    const text = cellText(values[selected.col]);
    try {
      await navigator.clipboard.writeText(text === null ? 'NULL' : text);
      notify?.('Copied the cell.');
    } catch {
      notify?.('Copying was blocked.');
    }
  }

  root.addEventListener('keydown', (e) => {
    const mod = e.ctrlKey || e.metaKey;
    if (mod && e.key.toLowerCase() === 'c') {
      e.preventDefault();
      copySelected();
      return;
    }
    if (!selected || !columns.length) return;
    const moves = { ArrowUp: [-1, 0], ArrowDown: [1, 0], ArrowLeft: [0, -1], ArrowRight: [0, 1] };
    let step = moves[e.key];
    const visible = Math.max(1, Math.floor(position().view / ROW_H) - 1);
    if (e.key === 'PageDown') step = [visible, 0];
    if (e.key === 'PageUp') step = [-visible, 0];
    if (e.key === 'Home' && mod) step = [-total, -columns.length];
    if (e.key === 'End' && mod) step = [total, columns.length];
    if (!step) return;
    e.preventDefault();
    selected = {
      row: Math.max(0, Math.min(total - 1, selected.row + step[0])),
      col: Math.max(0, Math.min(columns.length - 1, selected.col + step[1]))
    };
    reveal();
  });

  /* Drag a header's right edge to resize its column; double-click fits it back. */
  head.addEventListener('mousedown', (e) => {
    const grip = e.target.closest('.rg-grip');
    if (!grip) return;
    e.preventDefault();
    const col = Number(grip.dataset.col);
    const startX = e.clientX;
    const startW = widths[col];
    const move = (ev) => {
      widths[col] = Math.max(MIN_COL, Math.min(1200, startW + ev.clientX - startX));
      head.style.gridTemplateColumns = template();
      head.style.width = `${fullWidth()}px`;
      layout();
      schedule();
    };
    const up = () => {
      window.removeEventListener('mousemove', move);
      window.removeEventListener('mouseup', up);
      root.classList.remove('resizing');
    };
    root.classList.add('resizing');
    window.addEventListener('mousemove', move);
    window.addEventListener('mouseup', up);
  });

  return {
    element: root,

    /** Shows a result: { columns, total, rows } where rows is the first page. */
    setResult(result) {
      generation++;
      columns = result?.columns || [];
      total = result?.total || 0;
      pages = new Map();
      loading = new Set();
      selected = null;
      const firstRows = result?.rows || [];
      if (firstRows.length) pages.set(0, firstRows.slice(0, PAGE));
      sizeColumns(firstRows.slice(0, 100));
      paintHead();
      layout();
      root.scrollTop = 0;
      root.scrollLeft = 0;
      schedule();
    },

    clear() {
      this.setResult(null);
      head.replaceChildren();
    },

    refresh: schedule,

    focus() {
      root.focus();
    }
  };
}
