/* =================================================================
   EDITOR  (frontend/src/ui/editor.js)
   CodeMirror 6 editor with custom VaultNotes dark/neon theme,
   Markdown highlighting, and [[ autocomplete suggestions.
   ================================================================= */

import { EditorState } from "@codemirror/state";
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

// Decorator to style [[wikilinks]] inside CodeMirror
const wikilinkDecorator = new MatchDecorator({
  regexp: /\[\[([^\]]+)\]\]/g,
  decoration: () => Decoration.mark({ class: "cm-wikilink" })
});

const wikilinkPlugin = ViewPlugin.define(
  view => ({
    decorations: wikilinkDecorator.createDeco(view),
    update(u) {
      this.decorations = wikilinkDecorator.updateDeco(u, this.decorations);
    }
  }),
  { decorations: v => v.decorations }
);

let editorView = null;
let titlesCallback = () => [];

/**
 * Autocompletion source for [[wikilinks]].
 */
function wikilinkCompletionSource(context) {
  const before = context.matchBefore(/\[\[([^\]]*)/);
  if (!before) return null;
  if (before.from === before.to && !context.explicit) return null;

  const query = before.text.slice(2).toLowerCase();
  const availableTitles = titlesCallback ? titlesCallback() : [];

  const options = availableTitles
    .filter(title => title.toLowerCase().includes(query))
    .map(title => ({
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
      onChange?.(update.state.doc.toString());
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
      history(),
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

export function setEditorContent(text) {
  if (!editorView) return;
  const currentDoc = editorView.state.doc.toString();
  if (currentDoc === text) return;

  editorView.dispatch({
    changes: { from: 0, to: editorView.state.doc.length, insert: text || "" }
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

export function setEditorCursorToEnd() {
  if (!editorView) return;
  const docLen = editorView.state.doc.length;
  editorView.dispatch({
    selection: { anchor: docLen, head: docLen }
  });
}
