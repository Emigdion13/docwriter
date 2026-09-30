"""Markdown → HTML renderer for the VaultNotes preview.

The pipeline is the one section 4.7 describes:

1. :func:`vaultnotes.links.render_links_for_preview` rewrites ``[[note links]]``
   into internal addresses (``#vn-open/``, ``#vn-new/``, and the M10
   ``#vn-missing/`` and ``![[embed]]`` markers),
2. markdown-it-py renders the Markdown into HTML with **raw HTML turned off**,
   tables, task lists and Pygments code colours on,
3. ``render.py`` swaps the embed markers for the referenced note's rendered
   body, and the frontend cleans the result with DOMPurify before showing it.

Nothing in here imports pywebview, so the renderer is unit-testable alone.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Mapping
from typing import Any

from markdown_it import MarkdownIt
from mdit_py_plugins.tasklists import tasklists_plugin
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import TextLexer, get_lexer_by_name

from vaultnotes.frontmatter import strip_front_matter
from vaultnotes.links import (
    EMBED_BLOCK_OPEN,
    EMBED_CLOSE,
    EMBED_INLINE_OPEN,
    EMBED_MARK,
    render_links_for_preview,
)

#: How many levels of ``![[Note]]`` embeds are followed (a cycle must not spin).
MAX_EMBED_DEPTH = 2

#: ``<separator><T|I><title><separator>``.  Group 1 is the form (``T`` block or
#: ``I`` inline), group 2 the title, so the marker letter never leaks into it.
_EMBED_RE = re.compile(
    f"{re.escape(EMBED_MARK)}([TI])([^\\n]{{0,200}}?){re.escape(EMBED_CLOSE)}"
)

#: The block form while it is still the only content of its paragraph.
_EMBED_PARAGRAPH_RE = re.compile(
    f"<p>{re.escape(EMBED_BLOCK_OPEN)}([^\\n]{{0,200}}?){re.escape(EMBED_CLOSE)}</p>"
)


def _highlight_code(code: str, lang: str) -> str:
    """Colour one code block with Pygments, using the ``.tok-*`` classes."""
    lang_clean = lang.strip().lower() if lang else ""
    try:
        lexer = get_lexer_by_name(lang_clean) if lang_clean else TextLexer()
    except Exception:
        lexer = TextLexer()

    formatter = HtmlFormatter(nowrap=True, classprefix="tok-")
    return highlight(code, lexer, formatter)


def create_markdown_renderer() -> MarkdownIt:
    """Create a configured MarkdownIt instance.

    Raw HTML is disabled (``html: False``) for security, tables and task lists
    are on, and fenced code blocks are coloured by Pygments.
    """
    md = MarkdownIt("gfm-like", {"html": False})
    md.use(tasklists_plugin)

    def render_fence(tokens: list[Any], idx: int, options: dict[str, Any], env: dict[str, Any]) -> str:
        token = tokens[idx]
        lang = token.info.strip() if token.info else ""
        code = token.content
        highlighted = _highlight_code(code, lang)
        lang_badge = f'<span class="lang">{html.escape(lang)}</span>' if lang else ""
        return f'<pre class="code">{lang_badge}<code>{highlighted}</code></pre>\n'

    md.renderer.rules["fence"] = render_fence
    return md


_renderer: MarkdownIt | None = None


def get_renderer() -> MarkdownIt:
    """Return the shared MarkdownIt instance (creating it on first use)."""
    global _renderer
    if _renderer is None:
        _renderer = create_markdown_renderer()
    return _renderer


def _note_body(read_note: Callable[[str], Any] | None, title: str) -> str | None:
    """Ask the caller for one note's Markdown, accepting a few shapes.

    ``read_note`` may return a ``Note``, a mapping with a ``body`` key, the text
    itself, or ``None`` when the note is gone.
    """
    if read_note is None:
        return None
    try:
        found = read_note(title)
    except Exception:
        return None
    if found is None:
        return None
    if isinstance(found, str):
        return found
    if isinstance(found, Mapping):
        body = found.get("body")
    else:
        body = getattr(found, "body", None)
    return body if isinstance(body, str) else None


def _embed_card(title: str, inner_html: str) -> str:
    """The markup around one embed: a labelled card built only by us.

    An embed found in the middle of a sentence becomes a card on its own as
    well: the HTML parser closes the surrounding ``<p>`` at the ``<div>``, which
    is exactly the layout wanted, and a card inside a list item or table cell
    is legal anyway.
    """
    label = html.escape(title, quote=True)
    return (
        f'<div class="vn-embed" data-note="{label}">'
        f'<span class="vn-embed-title">{label}</span>'
        f'<div class="vn-embed-body">{inner_html}</div>'
        f"</div>"
    )


class _RenderContext:
    """Shared state for one preview render: the title lists and the embed depth."""

    def __init__(
        self,
        titles: Any,
        spaces: Mapping[str, Any] | None,
        read_note: Callable[[str], Any] | None,
        depth: int,
        seen: tuple[str, ...],
    ) -> None:
        self.titles = titles
        self.spaces = spaces
        self.read_note = read_note
        self.depth = depth
        self.seen = seen

    @property
    def embeds_enabled(self) -> bool:
        return self.read_note is not None and self.depth < MAX_EMBED_DEPTH

    def child(self, seen: set[str]) -> "_RenderContext":
        """The context for one embedded note: deeper, and embeds stop there."""
        deeper = self.depth + 1 < MAX_EMBED_DEPTH
        return _RenderContext(
            self.titles,
            self.spaces,
            self.read_note if deeper else None,
            self.depth + 1,
            tuple(seen),
        )

    def render(self, body: str) -> str:
        """Rewrite links, render Markdown, then substitute embeds."""
        # Front matter (the important mark, Obsidian properties) is data about
        # the note: markdown-it would draw it as a rule and a heading.
        body = strip_front_matter(body)
        if not body:
            return ""
        source = body
        if self.titles is not None or self.spaces or self.embeds_enabled:
            source = render_links_for_preview(
                body,
                self.titles if self.titles is not None else [],
                spaces=self.spaces,
                embeds=self.embeds_enabled,
            )
        out = get_renderer().render(source)
        if EMBED_MARK in out:
            out = self._substitute_embeds(out)
        return out

    def _substitute_embeds(self, out: str) -> str:
        """Swap embed markers for rendered cards, then remove leftovers."""
        seen: set[str] = set(self.seen)

        def embed(title: str) -> str:
            title = (title or "").strip()
            if not title or title in seen:
                # The same note twice, or a note already on the screen, is
                # embedded once: an embed must never recurse.
                return ""
            body = _note_body(self.read_note, title)
            if body is None:
                return ""
            seen.add(title)
            return _embed_card(title, self.child(seen).render(body))

        # A block embed owns its paragraph, so the <p> goes with it; an inline
        # one is replaced where it stands (see _embed_card).
        out = _EMBED_PARAGRAPH_RE.sub(lambda match: embed(match.group(1)), out)
        out = _EMBED_RE.sub(lambda match: embed(match.group(2)), out)
        out = out.replace("<p></p>", "")
        # Anything left is a marker whose note is gone: never show the glyph.
        return (
            out.replace(EMBED_CLOSE, "")
            .replace(EMBED_BLOCK_OPEN, "")
            .replace(EMBED_INLINE_OPEN, "")
        )


def render_markdown(
    body: str,
    titles: Any = None,
    *,
    read_note: Callable[[str], Any] | None = None,
    spaces: Mapping[str, Any] | None = None,
) -> str:
    """Render one note's Markdown into preview HTML.

    ``titles`` makes ``[[links]]`` clickable (a list, a mapping or Note
    objects); ``read_note`` additionally enables ``![[embeds]]``; ``spaces``
    resolves the M10 cross-space form ``[[Plain:Title]]`` and is only ever given
    the Plain space, because links must not reach into a vault (rule 11).
    """
    context = _RenderContext(titles, spaces, read_note, 0, ())
    return context.render(body or "")


# Alias matching the Bridge API name (section 4.8).
render_preview = render_markdown
