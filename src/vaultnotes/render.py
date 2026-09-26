"""Markdown to HTML renderer for VaultNotes preview."""

from __future__ import annotations

from typing import Any

from markdown_it import MarkdownIt
from mdit_py_plugins.tasklists import tasklists_plugin
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import TextLexer, get_lexer_by_name

from vaultnotes.links import render_links_for_preview


def _highlight_code(code: str, lang: str) -> str:
    """Highlight code block with Pygments using .tok-* class prefix."""
    lang_clean = lang.strip().lower() if lang else ""
    try:
        lexer = get_lexer_by_name(lang_clean) if lang_clean else TextLexer()
    except Exception:
        lexer = TextLexer()

    formatter = HtmlFormatter(nowrap=True, classprefix="tok-")
    return highlight(code, lexer, formatter)


def create_markdown_renderer() -> MarkdownIt:
    """Create a configured MarkdownIt instance.

    Raw HTML is disabled (html=False) for security.
    Tables and task lists are supported.
    Fenced code blocks are colored with Pygments.
    """
    md = MarkdownIt("gfm-like", {"html": False})
    md.use(tasklists_plugin)

    def render_fence(tokens: list[Any], idx: int, options: dict[str, Any], env: dict[str, Any]) -> str:
        token = tokens[idx]
        lang = token.info.strip() if token.info else ""
        code = token.content
        highlighted = _highlight_code(code, lang)
        lang_badge = f'<span class="lang">{lang}</span>' if lang else ""
        return f'<pre class="code">{lang_badge}<code>{highlighted}</code></pre>\n'

    md.renderer.rules["fence"] = render_fence
    return md


_renderer: MarkdownIt | None = None


def get_renderer() -> MarkdownIt:
    """Return shared MarkdownIt renderer singleton."""
    global _renderer
    if _renderer is None:
        _renderer = create_markdown_renderer()
    return _renderer


def render_markdown(body: str, titles: list[str] | None = None) -> str:
    """Render markdown body into HTML.

    Rewrites wikilinks if titles list is provided.
    """
    if not body:
        return ""

    if titles is not None:
        processed_body = render_links_for_preview(body, titles)
    else:
        processed_body = body

    renderer = get_renderer()
    return renderer.render(processed_body)


# Alias matching Bridge API name
render_preview = render_markdown
