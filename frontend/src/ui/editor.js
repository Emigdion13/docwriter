/* =================================================================
   EDITOR  (frontend/src/ui/editor.js)
   CodeMirror 6 editor with custom VaultNotes dark/neon theme,
   Markdown highlighting, and [[ autocomplete suggestions.
   ================================================================= */

import {
  Annotation,
  Compartment,
  EditorState,
  StateEffect,
  Transaction
} from "@codemirror/state";
import { EditorView, keymap, MatchDecorator, Decoration, ViewPlugin } from "@codemirror/view";
import { defaultKeymap, history, historyKeymap } from "@codemirror/commands";
import { markdown } from "@codemirror/lang-markdown";
import { autocompletion } from "@codemirror/autocomplete";
import { HighlightStyle, syntaxHighlighting } from "@codemirror/language";
import { tags as t } from "@lezer/highlight";

// Lezer highlight style matching the design tokens
const vaultNotesHighlightStyle = HighlightStyle.define([
  { tag: t.heading, color: "var(--text)", textShadow: "0 0 18px color-mix(in srgb, var(--accent) 55%, transparent)", fontWeight: "600" },
  { tag: t.heading1, fontSize: "1.25em" },
  { tag: t.heading2, fontSize: "1.15em" },
  { tag: t.heading3, fontSize: "1.05em" },
  { tag: t.emphasis, fontStyle: "italic", color: "color-mix(in srgb, var(--text) 78%, var(--muted))" },
  { tag: t.strong, fontWeight: "700", color: "var(--text)" },
  { tag: t.monospace, color: "var(--success)" },
  { tag: t.quote, fontStyle: "italic", color: "var(--muted)" },
  { tag: t.list, color: "var(--accent)" },
  { tag: t.link, color: "var(--accent)" },
  { tag: t.meta, color: "var(--faint)" },
  { tag: t.processingInstruction, color: "var(--accent)", opacity: 0.65 },
  { tag: t.keyword, color: "var(--encrypted)" },
  { tag: t.string, color: "var(--success)" },
  { tag: t.comment, color: "var(--faint)", fontStyle: "italic" },
  { tag: t.number, color: "var(--warning)" }
]);

/**
 * The note title a [[link]] points at: no alias, no #heading, no ".md".
 * Mirrors links.py so the editor and the engine agree on what a target is.
 */
function linkTarget(inner) {
  let target = inner.split("|")[0].replace(/\\\|/g, "|").trim();
  const hash = target.indexOf("#");
  if (hash !== -1) target = target.slice(0, hash).trim();
  return target.replace(/\.md$/i, "").trim();
}

// Decorator to style [[wikilinks]] inside CodeMirror.  A link to a note that
// does not exist in this space is drawn as a dashed chip, exactly like the
// preview does, so a typo is visible while typing (M6).
const wikilinkDecorator = new MatchDecorator({
  regexp: /\[\[([^\]\n]+)\]\]/g,
  // CodeMirror calls decorate(add, from, to, match, view): `to` is already the
  // end of the match, so it must not be recomputed from the match here.
  decorate(add, from, to, match) {
    const target = linkTarget(match[1] || "");
    const known = target && knownTitles().some(title => title.toLowerCase() === target.toLowerCase());
    add(
      from,
      to,
      Decoration.mark({ class: known ? "cm-wikilink" : "cm-wikilink is-missing" })
    );
  }
});

// Marks a change that came from the app (loading or clearing a note) rather
// than from the keyboard.  Reporting those as edits would mark a freshly
// opened note dirty and re-save - and re-encrypt - a file nobody touched.
const programmaticEdit = Annotation.define();

// Undo history is kept in a compartment so it can be replaced when another
// note is loaded: with one shared history, Ctrl+Z in note B could pull note
// A's text into B and the auto-save would write it to the wrong file.
const historyCompartment = new Compartment();

// Bumping this effect re-decorates every visible [[link]] without touching
// the document, which is how the chips learn that a note was created, renamed
// or that a vault locked and its titles disappeared.
const refreshLinksEffect = StateEffect.define();

const wikilinkPlugin = ViewPlugin.define(
  view => ({
    decorations: wikilinkDecorator.createDeco(view),
    update(u) {
      const refresh = u.transactions.some(tr => tr.effects.some(e => e.is(refreshLinksEffect)));
      this.decorations = refresh
        ? wikilinkDecorator.createDeco(u.view)
        : wikilinkDecorator.updateDeco(u, this.decorations);
    }
  }),
  { decorations: v => v.decorations }
);

let editorView = null;
let titlesCallback = () => [];

/**
 * Titles the current space can link to.  They come from the Bridge API's
 * list_titles, so a locked vault never contributes one (security rule 11).
 */
function knownTitles() {
  try {
    const titles = titlesCallback ? titlesCallback() : [];
    return Array.isArray(titles) ? titles : [];
  } catch (err) {
    return [];
  }
}

/**
 * Autocompletion source for [[wikilinks]].
 */
function wikilinkCompletionSource(context) {
  const before = context.matchBefore(/\[\[([^\]]*)/);
  if (!before) return null;
  if (before.from === before.to && !context.explicit) return null;

  const query = before.text.slice(2).toLowerCase();

  // Titles of the current space (bridge.list_titles), so suggestions never
  // come from a search filter or from a locked vault.
  const matches = knownTitles()
    .filter(title => String(title).toLowerCase().includes(query))
    .sort((a, b) => {
      // Exact start first, then alphabetical: useful with hundreds of notes.
      const aStarts = String(a).toLowerCase().startsWith(query) ? 0 : 1;
      const bStarts = String(b).toLowerCase().startsWith(query) ? 0 : 1;
      return aStarts - bStarts || String(a).localeCompare(String(b), undefined, { sensitivity: "base" });
    })
    .slice(0, 50);

  const options = matches.map(title => ({
    label: title,
    apply: `${title}]]`,
    type: "link",
    detail: "Note"
  }));

  return {
    from: before.from + 2,
    options,
    validFor: /^[^\]]*$/
  };
}

export function initEditor(container, { onChange, onScroll, getTitles }) {
  titlesCallback = getTitles || (() => []);

  const updateListener = EditorView.updateListener.of((update) => {
    if (update.docChanged) {
      const fromTheApp = update.transactions.every(tr => tr.annotation(programmaticEdit));
      if (!fromTheApp) onChange?.(update.state.doc.toString());
    }
    if (update.geometryChanged || update.viewportChanged) {
      const scroller = editorView?.scrollDOM;
      if (scroller) onScroll?.(scroller.scrollTop);
    }
  });

  const scrollHandler = EditorView.domEventHandlers({
    scroll(event, view) {
      onScroll?.(view.scrollDOM.scrollTop);
    }
  });

  const state = EditorState.create({
    doc: "",
    extensions: [
      historyCompartment.of(history()),
      keymap.of([...defaultKeymap, ...historyKeymap]),
      markdown(),
      syntaxHighlighting(vaultNotesHighlightStyle),
      wikilinkPlugin,
      autocompletion({
        override: [wikilinkCompletionSource],
        activateOnTyping: true
      }),
      updateListener,
      scrollHandler,
      EditorView.lineWrapping
    ]
  });

  if (editorView) {
    editorView.destroy();
  }

  editorView = new EditorView({
    state,
    parent: container
  });

  return editorView;
}

/**
 * Puts a note's text into the editor.
 *
 * This is the app writing, not the user: the change is annotated so no save is
 * triggered, kept out of the undo stack, and the undo stack is reset so the
 * previous note's edits cannot be undone into this one.
 */
export function setEditorContent(text) {
  if (!editorView) return;
  const next = text || "";
  if (editorView.state.doc.toString() === next) return;

  editorView.dispatch({
    changes: { from: 0, to: editorView.state.doc.length, insert: next },
    annotations: [programmaticEdit.of(true), Transaction.addToHistory.of(false)],
    effects: historyCompartment.reconfigure(history())
  });
  if (editorView.scrollDOM) {
    editorView.scrollDOM.scrollTop = 0;
  }
}

export function getEditorContent() {
  return editorView ? editorView.state.doc.toString() : "";
}

export function focusEditor() {
  if (editorView) editorView.focus();
}

/**
 * Redraws the [[link]] chips after the space's titles changed.
 */
export function refreshWikilinkDecorations() {
  if (!editorView) return;
  editorView.dispatch({ effects: refreshLinksEffect.of(null) });
}

export function setEditorCursorToEnd() {
  if (!editorView) return;
  const docLen = editorView.state.doc.length;
  editorView.dispatch({
    selection: { anchor: docLen, head: docLen }
  });
}
