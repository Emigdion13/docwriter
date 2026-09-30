"""Tags: words that sort notes into groups across a space.

A note's tags live in one place only, the ``tags:`` line of its front matter
(see :mod:`vaultnotes.frontmatter`)::

    ---
    tags: [finance, todo]
    ---
    # Call the bank

The app writes that line from the tag box under the note's title.  A ``#``
anywhere in the text is left alone, so Markdown headings, ``C#`` and
``#123`` never turn into tags by accident.

Like the important mark, tags live in the note itself: nothing is written
next to a Plain note, a vault note's tags are encrypted with its body, and
they travel with the note when it is moved, exported or imported.  Obsidian
reads the same line as the note's tags.

What counts as a tag
--------------------
* Letters, digits, ``_``, ``-`` and ``/``, with at least one character that is
  not a digit: ``2026`` is not a tag, ``q3-2026`` is.  A leading ``#`` is
  dropped, so ``#work`` and ``work`` are the same tag.
* ``project/alpha`` is a nested tag: filtering by ``project`` finds it too.
* Case is ignored when tags are compared (``Work`` and ``work`` are one tag);
  a note keeps the spelling it was given.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from typing import Any

from vaultnotes.frontmatter import front_matter_tags

#: Longer "tags" are almost always something else (a pasted hash, a URL).
MAX_TAG_LENGTH = 64

#: How many tags one note keeps; the front matter line stays readable.
MAX_TAGS_PER_NOTE = 50

# ``\w`` is Unicode-aware for str patterns, so café and 日記 are tags.
_TAG_TEXT_RE = re.compile(r"[\w/-]+")
_HAS_WORD_RE = re.compile(r"[^\W\d]")


def clean_tag(text: Any) -> str:
    """The tag ``text`` names (without ``#``), or ``""`` when it is not one."""
    if not isinstance(text, str):
        return ""
    tag = text.strip().removeprefix("#").rstrip("/-")
    if not tag or len(tag) > MAX_TAG_LENGTH or not _TAG_TEXT_RE.fullmatch(tag):
        return ""
    if not _HAS_WORD_RE.search(tag) or any(not part for part in tag.split("/")):
        return ""
    return tag


def tag_key(tag: str) -> str:
    """What two tags are compared by: ``Work`` and ``work`` are one tag."""
    return tag.casefold()


def unique_tags(tags: Iterable[str]) -> list[str]:
    """The real tags among ``tags``, once each, in their first spelling."""
    found: dict[str, str] = {}
    for raw in tags:
        tag = clean_tag(raw)
        if tag:
            found.setdefault(tag_key(tag), tag)
    return list(found.values())


def extract_tags(body: str) -> list[str]:
    """A note's tags, from its front matter ``tags:`` line."""
    if not isinstance(body, str) or not body:
        return []
    return unique_tags(front_matter_tags(body))


def has_tags(note_tags: Iterable[str], wanted: Iterable[str]) -> bool:
    """Whether a note carries every wanted tag (``a`` also matches ``a/b``)."""
    keys = [tag_key(tag) for tag in note_tags]
    for want in wanted:
        want_key = tag_key(want)
        if not any(key == want_key or key.startswith(want_key + "/") for key in keys):
            return False
    return True


def split_query(query: str) -> tuple[str, list[str]]:
    """Split a search into its text and its ``#tag`` words.

    ``"#work budget"`` searches for "budget" in the notes tagged ``work``.
    A lone ``#`` or ``C#`` stays text.
    """
    words: list[str] = []
    tags: list[str] = []
    for word in (query or "").split():
        tag = clean_tag(word) if word.startswith("#") else ""
        if tag:
            tags.append(tag)
        else:
            words.append(word)
    return " ".join(words), tags


def count_tags(tag_lists: Iterable[Iterable[str]]) -> list[dict[str, Any]]:
    """``[{tag, count}]`` over many notes, the most used tag first.

    Each note counts once per tag.  A tag written in several spellings is shown
    in the one most notes use.
    """
    notes: Counter[str] = Counter()
    spellings: dict[str, Counter[str]] = {}
    for tags in tag_lists:
        for tag in tags:
            key = tag_key(tag)
            notes[key] += 1
            spellings.setdefault(key, Counter())[tag] += 1
    rows = [
        {"tag": spellings[key].most_common(1)[0][0], "count": count}
        for key, count in notes.items()
    ]
    rows.sort(key=lambda row: (-row["count"], tag_key(row["tag"])))
    return rows
