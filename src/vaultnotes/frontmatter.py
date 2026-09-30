"""The "important" mark, kept in a note's own front matter.

A note is marked important by an ``important: true`` line in a YAML front
matter block at the very top of its Markdown::

    ---
    important: true
    ---
    # Call the bank

The mark lives in the note itself rather than in an index or a sidecar file:
a Plain note must never need anything next to it (README section 8), a vault
note's mark is encrypted with its body, and the mark travels with the note
when it is moved, exported, imported or restored.  Obsidian reads the same
block as the note's properties.

Only a block whose lines all look like YAML counts as front matter, so a note
that merely opens with a ``---`` rule followed by prose is left alone.

The same block holds the note's tags (``tags: [work, home]``), written by the
tag box under the title; :mod:`vaultnotes.tags` explains what a tag is.
"""

from __future__ import annotations

import re

#: Front matter is looked for in this many lines only, so listing 500 notes
#: never scans a long body.
_MAX_BLOCK_LINES = 60

_FENCE_RE = re.compile(r"---[ \t]*")
_CLOSE_RE = re.compile(r"(?:---|\.\.\.)[ \t]*")
#: ``key: value``, a ``- list item``, an indented continuation or a comment.
_YAML_LINE_RE = re.compile(r"[ \t]*|[ \t].*|#.*|- .*|-|[^\s:#][^:]*:(?:[ \t].*)?")
_IMPORTANT_KEY_RE = re.compile(r"important[ \t]*:.*", re.IGNORECASE)
_IMPORTANT_TRUE_RE = re.compile(
    r"important[ \t]*:[ \t]*(?:true|yes|on)[ \t]*(?:#.*)?", re.IGNORECASE
)

_BOM = "﻿"


def _front_matter(body: str) -> tuple[list[str], int, int] | None:
    """Find the front matter block.

    Returns ``(lines, start, end)``: every line of ``body`` with its line
    ending kept, the index of the opening ``---`` line, and the index of the
    closing one.  ``None`` when the note has no front matter.
    """
    if not isinstance(body, str) or not body:
        return None
    lines = body.splitlines(keepends=True)[: _MAX_BLOCK_LINES + 2]
    if not lines or not _FENCE_RE.fullmatch(lines[0].removeprefix(_BOM).rstrip("\r\n")):
        return None
    for index in range(1, len(lines)):
        text = lines[index].rstrip("\r\n")
        if _CLOSE_RE.fullmatch(text):
            return lines, 0, index
        if not _YAML_LINE_RE.fullmatch(text):
            return None
    return None


def _newline(body: str) -> str:
    """The line ending the note already uses."""
    return "\r\n" if "\r\n" in body else "\n"


def is_important(body: str) -> bool:
    """Whether the note's front matter says ``important: true``."""
    found = _front_matter(body)
    if found is None:
        return False
    lines, start, end = found
    return any(
        _IMPORTANT_TRUE_RE.fullmatch(line.rstrip("\r\n")) for line in lines[start + 1 : end]
    )


_TAGS_KEY_RE = re.compile(r"tags?[ \t]*:(.*)", re.IGNORECASE)
_LIST_ITEM_RE = re.compile(r"[ \t]*-[ \t]+(.*)")


def front_matter_tags(body: str) -> list[str]:
    """The values of a ``tags:`` key, in the forms Obsidian writes.

    ``tags: [work, home]``, ``tags: work, home``, ``tags: work home`` and a
    block list (``tags:`` then ``- work`` lines) all work.  Values come back
    as written, quotes and a leading ``#`` removed; :mod:`vaultnotes.tags`
    decides which of them are real tags.
    """
    found = _front_matter(body)
    if found is None:
        return []
    lines, start, end = found
    inner = [line.rstrip("\r\n") for line in lines[start + 1 : end]]
    values: list[str] = []
    for index, line in enumerate(inner):
        key = _TAGS_KEY_RE.fullmatch(line)
        if key is None:
            continue
        rest = key.group(1).split(" #", 1)[0].strip()
        if rest:
            values.extend(re.split(r"[,\s]+", rest.strip("[]")))
        else:
            for item in inner[index + 1 :]:
                listed = _LIST_ITEM_RE.fullmatch(item)
                if listed is None:
                    break
                values.append(listed.group(1).split(" #", 1)[0])
    cleaned = (value.strip().strip("'\"").strip().lstrip("#") for value in values)
    return [value for value in cleaned if value]


def set_tags(body: str, tags: list[str]) -> str:
    """Return ``body`` with its ``tags:`` line set to ``tags`` (already clean).

    The line is written ``tags: [work, home]`` where the old one stood, a
    block list below the old key goes with it, and every other key stays as
    it is.  No tags removes the line, and the whole block when nothing else
    was in it, so tagging and untagging a note gives back its old text.
    """
    body = body if isinstance(body, str) else ""
    newline = _newline(body)
    line = f"tags: [{', '.join(tags)}]{newline}" if tags else ""
    found = _front_matter(body)
    all_lines = body.splitlines(keepends=True)

    if found is None:
        if not tags:
            return body
        # A byte order mark has to stay the first character of the file.
        prefix = _BOM if body.startswith(_BOM) else ""
        return prefix + f"---{newline}{line}---{newline}" + body.removeprefix(_BOM)

    _, start, end = found
    inner = all_lines[start + 1 : end]
    kept: list[str] = []
    placed = False
    index = 0
    while index < len(inner):
        key = _TAGS_KEY_RE.fullmatch(inner[index].rstrip("\r\n"))
        index += 1
        if key is None:
            kept.append(inner[index - 1])
            continue
        if not key.group(1).split(" #", 1)[0].strip():
            while index < len(inner) and _LIST_ITEM_RE.fullmatch(inner[index].rstrip("\r\n")):
                index += 1
        if line and not placed:
            kept.append(line)
            placed = True
    if line and not placed:
        kept.append(line)
    if any(text.strip() for text in kept):
        return "".join(all_lines[: start + 1] + kept + all_lines[end:])
    # The tags were the block's only content: drop the whole block.
    prefix = _BOM if all_lines[start].startswith(_BOM) else ""
    return prefix + "".join(all_lines[end + 1 :])


def strip_front_matter(body: str) -> str:
    """The note without its front matter block, for the preview and snippets."""
    found = _front_matter(body)
    if found is None:
        return body
    lines, _, end = found
    return "".join(body.splitlines(keepends=True)[end + 1 :])


def set_important(body: str, important: bool) -> str:
    """Return ``body`` with the important mark set or cleared.

    Other front matter keys are kept exactly as they are.  Clearing the mark
    removes the block again when the mark was all it held, so marking and then
    unmarking a note gives back the text it started with.
    """
    body = body if isinstance(body, str) else ""
    if is_important(body) == bool(important):
        return body

    newline = _newline(body)
    found = _front_matter(body)
    all_lines = body.splitlines(keepends=True)

    if important:
        if found is None:
            # A byte order mark has to stay the first character of the file.
            prefix = _BOM if body.startswith(_BOM) else ""
            block = f"---{newline}important: true{newline}---{newline}"
            return prefix + block + body.removeprefix(_BOM)
        _, start, end = found
        inner = all_lines[start + 1 : end]
        kept = [line for line in inner if not _IMPORTANT_KEY_RE.fullmatch(line.rstrip("\r\n"))]
        new_inner = kept + [f"important: true{newline}"]
        return "".join(all_lines[: start + 1] + new_inner + all_lines[end:])

    assert found is not None  # is_important() was True
    _, start, end = found
    inner = all_lines[start + 1 : end]
    kept = [line for line in inner if not _IMPORTANT_KEY_RE.fullmatch(line.rstrip("\r\n"))]
    if any(line.strip() for line in kept):
        return "".join(all_lines[: start + 1] + kept + all_lines[end:])
    # The mark was the block's only content: drop the whole block.
    prefix = _BOM if all_lines[start].startswith(_BOM) else ""
    return prefix + "".join(all_lines[end + 1 :])
