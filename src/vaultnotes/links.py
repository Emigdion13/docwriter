"""Obsidian-style wikilinks parsing, rendering, and indexing."""

from __future__ import annotations

import re
import urllib.parse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vaultnotes.models import Note


def _protect_code(text: str) -> tuple[str, list[str]]:
    """Replace fenced code blocks and inline code with placeholders."""
    code_blocks: list[str] = []

    def repl(m: re.Match[str]) -> str:
        code_blocks.append(m.group(0))
        return f"\x00CODE_{len(code_blocks) - 1}\x00"

    # Fenced blocks first
    s = re.sub(r"(```[\s\S]*?```|~~~[\s\S]*?~~~)", repl, text)
    # Inline code
    s = re.sub(r"(`[^`\n]+`)", repl, s)
    return s, code_blocks


def _restore_code(text: str, code_blocks: list[str]) -> str:
    """Restore placeholders with original code blocks."""
    def repl(m: re.Match[str]) -> str:
        idx = int(m.group(1))
        return code_blocks[idx]

    return re.sub(r"\x00CODE_(\d+)\x00", repl, text)


def render_links_for_preview(body: str, titles: list[str]) -> str:
    """Rewrite note links into HTML internal anchors for the preview pane.

    - Existing notes: [shown text](#vn-open/<URL-encoded title>)
    - Missing notes: [shown text](#vn-new/<URL-encoded title>)
    Code blocks and inline code are protected and left untouched.
    """
    if not body:
        return ""

    s, code_blocks = _protect_code(body)

    # Normalize titles map: lowercase -> canonical title
    title_map = {t.strip().lower(): t.strip() for t in titles}

    # 1. Wikilinks: [[target|display]] or [[target#heading|display]] or [[target]]
    def wikilink_repl(m: re.Match[str]) -> str:
        raw = m.group(1).strip()
        # Handle escaped pipe in tables: \|
        parts = re.split(r"(?<!\\)\|", raw, maxsplit=1)
        target_part = parts[0].strip().replace(r"\|", "|")
        display_part = parts[1].strip().replace(r"\|", "|") if len(parts) > 1 else None

        # Separate target note from heading anchor (#heading)
        hparts = target_part.split("#", 1)
        target = hparts[0].strip()
        if target.lower().endswith(".md"):
            target = target[:-3].strip()

        display = display_part if display_part is not None else target_part
        # Escape markdown link characters in display text
        display_escaped = display.replace("[", r"\[").replace("]", r"\]")

        target_norm = target.lower()
        if target_norm in title_map:
            canonical_title = title_map[target_norm]
            enc = urllib.parse.quote(canonical_title)
            return f"[{display_escaped}](#vn-open/{enc})"
        else:
            enc = urllib.parse.quote(target)
            return f"[{display_escaped}](#vn-new/{enc})"

    s = re.sub(r"\[\[([^\]]+)\]\]", wikilink_repl, s)

    # 2. Local markdown links to .md files: [text](Note%20Name.md)
    def md_link_repl(m: re.Match[str]) -> str:
        text = m.group(1)
        url = m.group(2).strip()
        if url.startswith(("http://", "https://", "mailto:", "#")):
            return m.group(0)

        unquoted = urllib.parse.unquote(url)
        if unquoted.lower().endswith(".md"):
            target = unquoted[:-3].strip()
            target_norm = target.lower()
            if target_norm in title_map:
                enc = urllib.parse.quote(title_map[target_norm])
                return f"[{text}](#vn-open/{enc})"
            else:
                enc = urllib.parse.quote(target)
                return f"[{text}](#vn-new/{enc})"
        return m.group(0)

    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", md_link_repl, s)

    return _restore_code(s, code_blocks)


def find_links_in_body(body: str) -> list[str]:
    """Extract list of target note titles referenced via wikilinks in body."""
    if not body:
        return []

    s, _ = _protect_code(body)
    targets: list[str] = []

    for m in re.finditer(r"\[\[([^\]]+)\]\]", s):
        raw = m.group(1).strip()
        parts = re.split(r"(?<!\\)\|", raw, maxsplit=1)
        target_part = parts[0].strip().replace(r"\|", "|")
        hparts = target_part.split("#", 1)
        target = hparts[0].strip()
        if target.lower().endswith(".md"):
            target = target[:-3].strip()
        if target:
            targets.append(target)

    return targets


def count_links(body: str) -> int:
    """Count number of wikilinks in body, ignoring code blocks."""
    return len(find_links_in_body(body))


def rename_links_in_body(body: str, old_title: str, new_title: str) -> tuple[str, int]:
    """Rewrite wikilinks pointing to old_title to new_title.

    Preserves display text and headings. Does not touch code blocks.
    Returns (new_body, count_of_links_updated).
    """
    if not body or not old_title.strip():
        return body, 0

    clean_old = old_title.strip()
    clean_new = new_title.strip()

    s, code_blocks = _protect_code(body)
    count = 0

    pattern = re.compile(
        r"\[\[\s*" + re.escape(clean_old) + r"(?:\.md)?(\s*(?:#[^\]|]*)?(?:(?:\\\||\|)[^\]]*)?)\]\]",
        re.IGNORECASE,
    )

    def repl(m: re.Match[str]) -> str:
        nonlocal count
        count += 1
        suffix = m.group(1)
        return f"[[{clean_new}{suffix}]]"

    s = pattern.sub(repl, s)
    return _restore_code(s, code_blocks), count


def calculate_backlinks(notes: list[Note], target_title: str) -> list[dict[str, str]]:
    """Find notes linking to target_title, returning [{id, title}]."""
    if not target_title:
        return []

    target_norm = target_title.strip().lower()
    backlinks: list[dict[str, str]] = []

    for note in notes:
        if note.title.strip().lower() == target_norm:
            continue
        links = find_links_in_body(note.body)
        if any(lnk.lower() == target_norm for lnk in links):
            backlinks.append({"id": note.id, "title": note.title})

    return backlinks
