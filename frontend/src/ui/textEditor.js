/* =================================================================
   TEXT EDITOR  (frontend/src/ui/textEditor.js)
   The Editor space: text files in tabs, like Notepad++.  Each tab is a
   CodeMirror editor with line numbers, folding, bracket matching, multiple
   cursors and find/replace.  Python opens and saves files through native
   dialogs and gives each file an opaque id; this page never sends a path.

   Unsaved text lives only here, in memory: nothing writes a draft to disk,
   so text pasted into a "new 1" tab is gone when the app closes unless it
   was saved.  Closing a changed tab asks first.
   ================================================================= */

import { basicSetup } from 'codemirror';
import { EditorView, keymap, highlightWhitespace, highlightTrailingWhitespace } from '@codemirror/view';
import { Compartment, EditorState, Prec } from '@codemirror/state';
import { HighlightStyle, syntaxHighlighting } from '@codemirror/language';
import { openSearchPanel, gotoLine } from '@codemirror/search';
import { copyLineDown, moveLineDown, moveLineUp, indentWithTab } from '@codemirror/commands';
import { markdown } from '@codemirror/lang-markdown';
import { sql } from '@codemirror/lang-sql';
import { javascript } from '@codemirror/lang-javascript';
import { html } from '@codemirror/lang-html';
import { css } from '@codemirror/lang-css';
import { tags as t } from '@lezer/highlight';

import { bridge } from '../bridge.js';
import { icon } from '../icons.js';
import { confirmAction } from './dialogs.js';

// Python's MAX_OPEN_FILES is 64; this many tabs is already a lot to scan.
const MAX_TABS = 40;
const PREFS_KEY = 'vn-editor-prefs'; // view settings only, never text
const FONT_MIN = 10;
const FONT_MAX = 28;

const LANGS = [
  { id: 'text', label: 'Plain text', ext: [], make: () => [] },
  { id: 'markdown', label: 'Markdown', ext: ['md', 'markdown'], make: () => markdown() },
  { id: 'sql', label: 'SQL', ext: ['sql'], make: () => sql() },
  { id: 'javascript', label: 'JavaScript', ext: ['js', 'mjs', 'cjs', 'jsx'], make: () => javascript({ jsx: true }) },
  { id: 'typescript', label: 'TypeScript', ext: ['ts', 'tsx', 'mts', 'cts'], make: () => javascript({ typescript: true, jsx: true }) },
  { id: 'json', label: 'JSON', ext: ['json', 'jsonc', 'geojson'], make: () => javascript() },
  { id: 'html', label: 'HTML / XML', ext: ['html', 'htm', 'xhtml', 'xml', 'svg', 'xaml', 'xsd', 'xsl', 'config', 'csproj', 'props', 'targets'], make: () => html() },
  { id: 'css', label: 'CSS', ext: ['css', 'scss', 'less'], make: () => css() }
];
const langById = (id) => LANGS.find(l => l.id === id) || LANGS[0];

const EOLS = [
  { id: 'CRLF', label: 'Windows (CRLF)' },
  { id: 'LF', label: 'Unix (LF)' },
  { id: 'CR', label: 'Mac (CR)' }
];
const ENCODINGS = [
  { id: 'utf-8', label: 'UTF-8' },
  { id: 'utf-8-sig', label: 'UTF-8 BOM' },
  { id: 'utf-16', label: 'UTF-16' },
  { id: 'cp1252', label: 'Windows-1252' }
];

function langForName(name) {
  const ext = String(name || '').toLowerCase().split('.').pop();
  return (LANGS.find(l => l.ext.includes(ext)) || LANGS[0]).id;
}

const highlight = HighlightStyle.define([
  { tag: [t.keyword, t.modifier, t.controlKeyword, t.operatorKeyword], color: 'var(--editor)', fontWeight: '600' },
  { tag: [t.string, t.special(t.string), t.regexp], color: 'var(--success)' },
  { tag: [t.number, t.bool, t.atom], color: 'var(--warning)' },
  { tag: t.null, color: 'var(--danger)' },
  { tag: [t.lineComment, t.blockComment, t.comment], color: 'var(--faint)', fontStyle: 'italic' },
  { tag: [t.typeName, t.className, t.namespace], color: 'var(--encrypted)' },
  { tag: [t.propertyName, t.attributeName], color: 'var(--plain)' },
  { tag: [t.tagName, t.angleBracket], color: 'var(--personal)' },
  { tag: [t.function(t.variableName), t.function(t.propertyName)], color: 'var(--sql)' },
  { tag: t.definition(t.variableName), color: 'var(--text)', fontWeight: '600' },
  { tag: t.operator, color: 'var(--muted)' },
  { tag: t.heading, color: 'var(--editor)', fontWeight: '700' },
  { tag: t.strong, fontWeight: '700' },
  { tag: t.emphasis, fontStyle: 'italic' },
  { tag: [t.link, t.url], color: 'var(--plain)', textDecoration: 'underline' },
  { tag: t.invalid, color: 'var(--danger)' }
]);

const theme = EditorView.theme({
  '&': { height: '100%', background: 'transparent', color: 'var(--text)' },
  '.cm-scroller': { fontFamily: 'var(--font-mono)', lineHeight: '1.55' },
  '.cm-content': { caretColor: 'var(--editor)', padding: '8px 0' },
  '.cm-cursor': { borderLeftColor: 'var(--editor)', borderLeftWidth: '2px' },
  '.cm-gutters': { background: 'transparent', border: 'none', color: 'var(--faint)' },
  '.cm-lineNumbers .cm-gutterElement': { padding: '0 10px 0 6px' },
  '.cm-activeLine': { background: 'color-mix(in srgb, var(--editor) 6%, transparent)' },
  '.cm-activeLineGutter': { background: 'transparent', color: 'var(--editor)' },
  '&.cm-focused .cm-selectionBackground, .cm-selectionBackground, ::selection': {
    background: 'color-mix(in srgb, var(--editor) 26%, transparent) !important'
  },
  '.cm-selectionMatch': { background: 'color-mix(in srgb, var(--editor) 14%, transparent)' },
  '.cm-matchingBracket': { background: 'color-mix(in srgb, var(--editor) 24%, transparent)', outline: 'none' },
  '.cm-searchMatch': { background: 'color-mix(in srgb, var(--warning) 30%, transparent)' },
  '.cm-searchMatch-selected': { background: 'color-mix(in srgb, var(--warning) 55%, transparent)' },
  '.cm-foldPlaceholder': { background: 'var(--glass-strong)', border: '1px solid var(--glass-border)', color: 'var(--muted)' },
  '.cm-tooltip': { background: 'var(--surface)', border: '1px solid var(--glass-border)', borderRadius: '10px' },
  '.cm-panels': { background: 'var(--surface)', color: 'var(--text)', borderColor: 'var(--glass-border)' },
  '.cm-panel input, .cm-panel button': { fontFamily: 'var(--font-ui)' },
  '.cm-highlightSpace': { backgroundImage: 'radial-gradient(circle at 50% 55%, var(--faint) 12%, transparent 13%)' },
  '.cm-highlightTab': { backgroundImage: 'none', borderBottom: '1px dotted var(--faint)' },
  '.cm-trailingSpace': { backgroundColor: 'color-mix(in srgb, var(--danger) 22%, transparent)' }
});

/* Notepad++ habits on top of CodeMirror's own keys. */
const extraKeys = Prec.high(keymap.of([
  { key: 'Mod-d', run: copyLineDown, preventDefault: true },        // duplicate line
  { key: 'Mod-Shift-ArrowUp', run: moveLineUp, preventDefault: true },
  { key: 'Mod-Shift-ArrowDown', run: moveLineDown, preventDefault: true },
  { key: 'Mod-g', run: gotoLine, preventDefault: true },             // go to line
  { key: 'Mod-h', run: openSearchPanel, preventDefault: true },      // find and replace
  indentWithTab
]));

function loadPrefs() {
  try {
    const p = JSON.parse(localStorage.getItem(PREFS_KEY) || '{}');
    return {
      wrap: p.wrap === true,
      whitespace: p.whitespace === true,
      fontSize: Number.isInteger(p.fontSize) && p.fontSize >= FONT_MIN && p.fontSize <= FONT_MAX ? p.fontSize : 13
    };
  } catch {
    return { wrap: false, whitespace: false, fontSize: 13 };
  }
}

function plural(n, word) {
  return `${n.toLocaleString()} ${word}${n === 1 ? '' : 's'}`;
}

export function createTextEditor({ onStateChanged, notify }) {
  const root = document.createElement('div');
  root.className = 'ws-editor';
  root.innerHTML = `
    <div class="term-head">
      <div class="crumb"><span class="crumb-dot"></span><span>Editor</span><span class="sep">/</span><span id="ed-where">new 1</span></div>
      <div class="term-actions ed-actions">
        <select class="ed-select" id="ed-lang" aria-label="Language" title="Language (syntax colours)"></select>
        <button class="btn icon" id="ed-open" type="button" aria-label="Open file" title="Open file… (Ctrl+O)">${icon('folder', 15)}</button>
        <button class="btn icon" id="ed-save" type="button" aria-label="Save" title="Save (Ctrl+S; Ctrl+Shift+S saves as)">${icon('save', 15)}</button>
        <button class="btn icon" id="ed-find" type="button" aria-label="Find and replace" title="Find and replace (Ctrl+F, Ctrl+H)">${icon('search', 15)}</button>
        <button class="btn icon ed-toggle" id="ed-wrap" type="button" aria-label="Word wrap" aria-pressed="false" title="Word wrap">${icon('columns', 15)}</button>
        <button class="btn icon ed-toggle" id="ed-ws" type="button" aria-label="Show whitespace" aria-pressed="false" title="Show spaces, tabs and trailing whitespace">${icon('eye', 15)}</button>
        <button class="btn icon" id="ed-json" type="button" aria-label="Format JSON" title="Format JSON (the selection, or the whole file)">${icon('sparkle', 15)}</button>
      </div>
    </div>
    <div class="term-tabs">
      <div class="term-tablist" id="ed-tablist" role="tablist" aria-label="Editor tabs"></div>
      <button class="btn icon small term-new" id="ed-new" type="button" aria-label="New tab" title="New tab (Ctrl+N)">${icon('plus', 15)}</button>
    </div>
    <div class="ed-body" id="ed-body"></div>
    <div class="ed-status" id="ed-status" aria-live="polite">
      <span id="ed-pos"></span>
      <span id="ed-sel"></span>
      <span id="ed-size"></span>
      <span class="ed-grow"></span>
      <select class="ed-mini" id="ed-eol" aria-label="Line endings" title="Line endings used when saving"></select>
      <select class="ed-mini" id="ed-enc" aria-label="Encoding" title="Encoding used when saving"></select>
      <span id="ed-zoom" title="Ctrl + / Ctrl - / Ctrl 0"></span>
    </div>
  `;

  const $ = (id) => root.querySelector(`#${id}`);
  const tabList = $('ed-tablist');
  const body = $('ed-body');
  const whereEl = $('ed-where');
  const langSel = $('ed-lang');
  const eolSel = $('ed-eol');
  const encSel = $('ed-enc');
  const posEl = $('ed-pos');
  const selEl = $('ed-sel');
  const sizeEl = $('ed-size');
  const zoomEl = $('ed-zoom');

  const fill = (select, items) => select.replaceChildren(...items.map(i => {
    const opt = document.createElement('option');
    opt.value = i.id;
    opt.textContent = i.label;
    return opt;
  }));
  fill(langSel, LANGS);
  fill(eolSel, EOLS);
  fill(encSel, ENCODINGS);

  const prefs = loadPrefs();
  const savePrefs = () => {
    try { localStorage.setItem(PREFS_KEY, JSON.stringify(prefs)); } catch { /* private mode: fine */ }
  };

  /* Every tab: { key, fileId, name, path, untitled, eol, encoding, lang, view, pane,
     saved (the doc as last saved or opened), dirty, compartments }. */
  const tabs = [];
  let active = null;
  let tabSeq = 0;
  let visible = false;

  // ---------------------------------------------------------- extensions
  const fontTheme = () => EditorView.theme({ '&': { fontSize: `${prefs.fontSize}px` } });
  const wsExt = () => (prefs.whitespace ? [highlightWhitespace(), highlightTrailingWhitespace()] : []);
  const wrapExt = () => (prefs.wrap ? EditorView.lineWrapping : []);

  function nextUntitled() {
    const used = new Set(tabs.filter(x => x.untitled).map(x => x.untitled));
    let n = 1;
    while (used.has(n)) n += 1;
    return n;
  }

  function label(tab) {
    return tab.name || `new ${tab.untitled}`;
  }

  function buildTab({ fileId = null, name = '', path = '', text = '', eol = 'CRLF', encoding = 'utf-8', lang = null } = {}) {
    const tab = {
      key: ++tabSeq, fileId, name, path, untitled: name ? 0 : nextUntitled(), eol, encoding,
      lang: lang || langForName(name), dirty: false,
      compartments: { lang: new Compartment(), wrap: new Compartment(), ws: new Compartment(), font: new Compartment() }
    };
    tab.pane = document.createElement('div');
    tab.pane.className = 'ed-pane';
    body.appendChild(tab.pane);
    const c = tab.compartments;
    tab.view = new EditorView({
      parent: tab.pane,
      state: EditorState.create({
        doc: text,
        extensions: [
          extraKeys,
          basicSetup,
          theme,
          syntaxHighlighting(highlight),
          EditorState.allowMultipleSelections.of(true),
          c.lang.of(langById(tab.lang).make()),
          c.wrap.of(wrapExt()),
          c.ws.of(wsExt()),
          c.font.of(fontTheme()),
          EditorView.updateListener.of((u) => {
            if (u.docChanged) {
              const dirty = !u.state.doc.eq(tab.saved);
              if (dirty !== tab.dirty) {
                tab.dirty = dirty;
                paintTabs();
                paintHead();
              }
            }
            if ((u.docChanged || u.selectionSet) && tab === active) paintStatus();
          })
        ]
      })
    });
    tab.saved = tab.view.state.doc;
    tabs.push(tab);
    return tab;
  }

  function reconfigureAll(which) {
    for (const tab of tabs) {
      const c = tab.compartments;
      const effect = which === 'wrap' ? c.wrap.reconfigure(wrapExt())
        : which === 'ws' ? c.ws.reconfigure(wsExt())
          : c.font.reconfigure(fontTheme());
      tab.view.dispatch({ effects: effect });
    }
  }

  // --------------------------------------------------------------- paint
  function paintTabs() {
    tabList.replaceChildren(...tabs.map((tab) => {
      const name = label(tab);
      const el = document.createElement('div');
      el.className = 'term-tab';
      el.classList.toggle('on', tab === active);
      el.classList.toggle('dirty', tab.dirty);
      el.dataset.key = String(tab.key);

      const pick = document.createElement('button');
      pick.type = 'button';
      pick.className = 'term-tab-pick';
      pick.setAttribute('role', 'tab');
      pick.setAttribute('aria-selected', String(tab === active));
      pick.tabIndex = tab === active ? 0 : -1;
      pick.innerHTML = `${icon(tab.fileId ? 'file' : 'edit', 13)}<span class="nm"></span>`; // fixed markup only
      pick.querySelector('.nm').textContent = name;
      pick.title = [tab.path || name, tab.dirty ? 'not saved' : ''].filter(Boolean).join(' · ');

      const close = document.createElement('button');
      close.type = 'button';
      close.className = 'term-tab-x';
      close.setAttribute('aria-label', `Close ${name}`);
      close.title = 'Close tab (Ctrl+W)';
      close.tabIndex = -1;
      close.innerHTML = icon('x', 12);
      el.append(pick, close);
      return el;
    }));
  }

  function paintHead() {
    if (!active) return;
    whereEl.textContent = `${label(active)}${active.dirty ? ' •' : ''}`;
    whereEl.title = active.path || 'Not saved yet';
    langSel.value = active.lang;
    eolSel.value = active.eol;
    encSel.value = active.encoding;
    $('ed-wrap').setAttribute('aria-pressed', String(prefs.wrap));
    $('ed-ws').setAttribute('aria-pressed', String(prefs.whitespace));
    $('ed-json').hidden = !['json', 'text', 'javascript'].includes(active.lang);
    onStateChanged?.(getState());
  }

  function paintStatus() {
    if (!active) return;
    const st = active.view.state;
    const main = st.selection.main;
    const line = st.doc.lineAt(main.head);
    posEl.textContent = `Ln ${line.number}, Col ${main.head - line.from + 1}`;
    const ranges = st.selection.ranges;
    const chars = ranges.reduce((n, r) => n + (r.to - r.from), 0);
    if (ranges.length > 1) selEl.textContent = `${ranges.length} cursors · Sel ${chars.toLocaleString()}`;
    else if (chars) {
      const lines = st.doc.lineAt(main.to).number - st.doc.lineAt(main.from).number + 1;
      selEl.textContent = `Sel ${chars.toLocaleString()} | ${plural(lines, 'line')}`;
    } else selEl.textContent = '';
    sizeEl.textContent = `${plural(st.doc.lines, 'line')} · ${plural(st.doc.length, 'char')}`;
    zoomEl.textContent = `${Math.round((prefs.fontSize / 13) * 100)}%`;
  }

  function paint() {
    paintTabs();
    paintHead();
    paintStatus();
  }

  // ---------------------------------------------------------------- tabs
  function show_(tab) {
    active = tab;
    for (const x of tabs) x.pane.classList.toggle('on', x === tab);
    paint();
    requestAnimationFrame(() => {
      if (active === tab && visible) tab.view.focus();
    });
  }

  function newTab(opts) {
    if (tabs.length >= MAX_TABS) {
      notify?.(`Up to ${MAX_TABS} tabs can be open. Close one first.`);
      return null;
    }
    const tab = buildTab(opts);
    show_(tab);
    return tab;
  }

  /* A blank, unchanged "new N" tab is replaced when a file opens, like Notepad++. */
  function isBlank(tab) {
    return tab && !tab.fileId && !tab.dirty && tab.view.state.doc.length === 0;
  }

  function dropTab(tab) {
    const index = tabs.indexOf(tab);
    if (index < 0) return;
    tabs.splice(index, 1);
    tab.view.destroy();
    tab.pane.remove();
    if (tab.fileId && !tabs.some(x => x.fileId === tab.fileId)) bridge.editor_close(tab.fileId);
    if (active === tab) {
      active = null;
      if (tabs.length) show_(tabs[Math.min(index, tabs.length - 1)]);
      else newTab();
    } else paint();
  }

  async function closeTab(tab = active) {
    if (!tab) return;
    if (tab.dirty) {
      show_(tab);
      const ok = await confirmAction({
        title: `Close ${label(tab)} without saving?`,
        message: tab.fileId ? 'Your changes since the last save will be lost.' : 'This text was never saved and will be lost.',
        confirmLabel: 'Close without saving',
        danger: true,
        iconName: 'edit'
      });
      if (!ok) return;
    }
    dropTab(tab);
  }

  function cycle(step) {
    if (tabs.length < 2 || !active) return;
    const index = (tabs.indexOf(active) + step + tabs.length) % tabs.length;
    show_(tabs[index]);
  }

  tabList.addEventListener('click', (e) => {
    const el = e.target.closest('.term-tab');
    if (!el) return;
    const tab = tabs.find(x => String(x.key) === el.dataset.key);
    if (!tab) return;
    if (e.target.closest('.term-tab-x')) closeTab(tab);
    else show_(tab);
  });
  // Middle click closes a tab, as in Notepad++.
  tabList.addEventListener('auxclick', (e) => {
    if (e.button !== 1) return;
    const el = e.target.closest('.term-tab');
    const tab = el && tabs.find(x => String(x.key) === el.dataset.key);
    if (tab) {
      e.preventDefault();
      closeTab(tab);
    }
  });
  tabList.addEventListener('dblclick', (e) => {
    if (!e.target.closest('.term-tab')) newTab();
  });

  // --------------------------------------------------------------- files
  async function open() {
    const res = await bridge.editor_open();
    if (res?.error) {
      if (res.error !== 'cancelled') notify?.(res.message || 'That file could not be opened.');
      return;
    }
    for (const f of res.files || []) {
      const already = tabs.find(x => x.fileId === f.id);
      if (already) {
        // Open again: show it.  Reload only when it has no unsaved changes.
        if (!already.dirty && !already.view.state.doc.eq(EditorState.create({ doc: f.text }).doc)) {
          already.view.dispatch({ changes: { from: 0, to: already.view.state.doc.length, insert: f.text } });
          already.saved = already.view.state.doc;
          already.dirty = false;
        }
        show_(already);
        continue;
      }
      const blank = isBlank(active) ? active : null;
      const tab = newTab({ fileId: f.id, name: f.name, path: f.path, text: f.text, eol: f.eol, encoding: f.encoding });
      if (tab && blank) dropTab(blank);
    }
    for (const f of res.failed || []) notify?.(`${f.name}: ${f.message}`);
  }

  function applySaved(tab, file) {
    // Another tab may have shown this file: it is now this tab's.
    for (const other of tabs) {
      if (other !== tab && other.fileId === file.id) {
        other.fileId = null;
        other.path = '';
        other.name = '';
        other.untitled = nextUntitled();
        other.dirty = true;
      }
    }
    tab.fileId = file.id;
    tab.name = file.name;
    tab.path = file.path;
    tab.untitled = 0;
    tab.encoding = file.encoding;
    tab.saved = tab.view.state.doc;
    tab.dirty = false;
    const lang = langForName(file.name);
    if (tab.lang === 'text' && lang !== 'text') setLang(tab, lang);
    paint();
  }

  async function save(tab = active) {
    if (!tab) return false;
    if (!tab.fileId) return saveAs(tab);
    const res = await bridge.editor_save(tab.fileId, tab.view.state.doc.toString(), tab.eol, tab.encoding);
    if (res?.error) {
      if (res.error === 'not_found') return saveAs(tab);
      notify?.(res.message || 'The file could not be saved.');
      return false;
    }
    applySaved(tab, res.file);
    notify?.(`Saved ${res.file.name}`);
    return true;
  }

  async function saveAs(tab = active) {
    if (!tab) return false;
    const suggested = tab.name || `new ${tab.untitled}.txt`;
    const res = await bridge.editor_save_as(tab.fileId, tab.view.state.doc.toString(), tab.eol, tab.encoding, suggested);
    if (res?.error) {
      if (res.error !== 'cancelled') notify?.(res.message || 'The file could not be saved.');
      return false;
    }
    applySaved(tab, res.file);
    notify?.(`Saved ${res.file.name}`);
    return true;
  }

  // ------------------------------------------------------------ settings
  function setLang(tab, id) {
    tab.lang = langById(id).id;
    tab.view.dispatch({ effects: tab.compartments.lang.reconfigure(langById(tab.lang).make()) });
  }

  langSel.addEventListener('change', () => {
    if (!active) return;
    setLang(active, langSel.value);
    paintHead();
    active.view.focus();
  });
  eolSel.addEventListener('change', () => {
    if (!active) return;
    active.eol = eolSel.value;
    active.dirty = true; // the file on disk now differs from what Save writes
    paint();
  });
  encSel.addEventListener('change', () => {
    if (!active) return;
    active.encoding = encSel.value;
    active.dirty = true;
    paint();
  });

  function toggleWrap() {
    prefs.wrap = !prefs.wrap;
    savePrefs();
    reconfigureAll('wrap');
    paintHead();
  }

  function toggleWhitespace() {
    prefs.whitespace = !prefs.whitespace;
    savePrefs();
    reconfigureAll('ws');
    paintHead();
  }

  function zoom(step) {
    const size = step === 0 ? 13 : Math.min(FONT_MAX, Math.max(FONT_MIN, prefs.fontSize + step));
    if (size === prefs.fontSize) return;
    prefs.fontSize = size;
    savePrefs();
    reconfigureAll('font');
    paintStatus();
  }

  function find() {
    if (!active) return;
    active.view.focus();
    openSearchPanel(active.view);
  }

  function formatJson() {
    if (!active) return;
    const view = active.view;
    const sel = view.state.selection.main;
    const from = sel.empty ? 0 : sel.from;
    const to = sel.empty ? view.state.doc.length : sel.to;
    const text = view.state.sliceDoc(from, to);
    let pretty;
    try {
      pretty = JSON.stringify(JSON.parse(text), null, 2);
    } catch (err) {
      notify?.(`Not valid JSON: ${String(err.message || err).slice(0, 160)}`);
      return;
    }
    view.dispatch({ changes: { from, to, insert: pretty } });
    if (active.lang === 'text') {
      setLang(active, 'json');
      paintHead();
    }
    view.focus();
  }

  $('ed-open').onclick = () => open();
  $('ed-save').onclick = () => save();
  $('ed-find').onclick = () => find();
  $('ed-wrap').onclick = () => toggleWrap();
  $('ed-ws').onclick = () => toggleWhitespace();
  $('ed-json').onclick = () => formatJson();
  $('ed-new').onclick = () => newTab();

  /* Keys from main.js's window handler while the Editor space shows.
     Returns true when the key was the editor's (main.js then stops). */
  function handleKey(e) {
    const mod = e.ctrlKey || e.metaKey;
    if (!mod && e.key !== 'F3') return false;
    if (e.defaultPrevented) return true; // CodeMirror already ran it
    const key = e.key.toLowerCase();
    if (key === 's') { if (e.shiftKey) saveAs(); else save(); return true; }
    if (key === 'o') { open(); return true; }
    if (key === 'n' || (key === 't' && e.shiftKey)) { newTab(); return true; }
    if (key === 'w') { closeTab(); return true; }
    if (e.key === 'Tab') { cycle(e.shiftKey ? -1 : 1); return true; }
    if (e.key === 'PageDown') { cycle(1); return true; }
    if (e.key === 'PageUp') { cycle(-1); return true; }
    if (key === 'f' || key === 'h') { find(); return true; }
    if (key === 'g') { if (active) { active.view.focus(); gotoLine(active.view); } return true; }
    if (e.key === '=' || e.key === '+') { zoom(1); return true; }
    if (e.key === '-') { zoom(-1); return true; }
    if (e.key === '0') { zoom(0); return true; }
    if (key === 'd' || key === 'e') return true; // note keys mean nothing here
    return false;
  }

  // Ctrl + wheel zooms the text, as in Notepad++.
  body.addEventListener('wheel', (e) => {
    if (!(e.ctrlKey || e.metaKey)) return;
    e.preventDefault();
    zoom(e.deltaY < 0 ? 1 : -1);
  }, { passive: false });

  // --------------------------------------------------------------- public
  function getState() {
    return {
      tabs: tabs.length,
      unsaved: tabs.filter(x => x.dirty).length,
      activeName: active ? label(active) : null
    };
  }

  return {
    element: root,
    getState,
    handleKey,
    newTab: () => newTab(),
    open,
    save: () => save(),
    saveAs: () => saveAs(),
    closeTab: () => closeTab(),
    hasUnsaved: () => tabs.some(x => x.dirty),

    show() {
      visible = true;
      if (!tabs.length) newTab();
      else show_(active || tabs[0]);
      requestAnimationFrame(() => active?.view.requestMeasure());
    },

    hide() {
      visible = false;
    },

    focus() {
      active?.view.focus();
    }
  };
}
