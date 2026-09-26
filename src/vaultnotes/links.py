"""Obsidian-style links between notes (Milestone M6).

This module is **pure logic**: it never touches the disk, pywebview or the
Bridge API, so it can be unit tested on its own (``tests/test_links.py``).

What it provides
----------------
``parse_links(body)``
    Every note link in a Markdown body, with target, display text, heading and
    the exact start/end offsets of the syntax in the body.
``resolve(target, titles)``
    Turn a link target into a note id, ignoring case, surrounding whitespace
    and a trailing ``.md``.
``rename_links(body, old_title, new_title)``
    Rewrite every form of a link when a note is renamed, keeping the display
    text and the heading.
``render_links_for_preview(body, titles)``
    Rewrite note links into internal Markdown addresses (``#vn-open/`` for a
    note that exists, ``#vn-new/`` for one that does not) before markdown-it
    renders the preview.
``LinkIndex``
    The in-memory link graph for **one space**: backlinks, outgoing links and
    the title list used by the ``[[`` suggestions.

Rules implemented (section 4.7 of the build plan)
-------------------------------------------------
* Links only ever resolve inside their own space, because every space owns its
  own :class:`LinkIndex`.
* Matching ignores upper/lower case and whitespace at the start or end.
* A trailing ``.md`` is ignored, in both ``[[Note.md]]`` and ``[text](Note.md)``.
* Inside a table the alias separator may be written ``\\|``; both forms parse.
* ``[[...]]`` inside inline code or a fenced code block is **not** a link.
* ``[[#Heading]]`` (no note name) is a heading link inside the current note and
  is therefore not a note link.

Security rule 11: a vault's index exists only in memory while the vault is
unlocked.  It is never written to disk, and :meth:`LinkIndex.clear` is called
on lock.
"""

from __future__ import annotations

import re
import urllib.parse
from bisect import bisect_right
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

if TYPE_CHECKING:  # pragma: no cover - typing only
    from vaultnotes.models import Note

__all__ = [
    "Link",
    "LinkIndex",
    "parse_links",
    "resolve",
    "rename_links",
    "render_links_for_preview",
    "find_links_in_body",
    "count_links",
    "rename_links_in_body",
    "calculate_backlinks",
    "OPEN_PREFIX",
    "NEW_PREFIX",
]

# Destinations starting with any of these are not note links.
_EXTERNAL_PREFIXES = (
    "http://",
    "https://",
    "mailto:",
    "ftp://",
    "ftps://",
    "file:",
    "#",
    "/",
    "\\",
    "?",
    "data:",
    "javascript:",
    "vbscript:",
    "tel:",
    "callto:",
    "ssh:",
    "git:",
    "ws:",
    "wss:",
    "about:",
    "blob:",
)

# ``[[target]]``: no "]" and no newline inside, so a link never spans lines.
_WIKILINK_RE = re.compile(r"\[\[([^\]\n]+)\]\]")

# ``[text](destination)``.  The destination excludes whitespace and unbalanced
# parentheses so that two links on one line stay two separate matches; an
# optional "title" part may follow it.
_MD_LINK_RE = re.compile(
    r"\[(?P<text>[^\[\]\n]*)\]\(\s*"
    r"(?P<url><[^<>\n]*>|[^\s()]*(?:\([^\s()]*\))?[^\s()]*)"
    r"(?:\s+(?:\"[^\"]*\"|'[^']*'))?"
    r"\s*\)"
)

_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_BACKTICK_RUN_RE = re.compile(r"(`+)")

#: Address prefixes the preview click handler routes on (section 4.7).
OPEN_PREFIX = "#vn-open/"
NEW_PREFIX = "#vn-new/"


# ----------------------------------------------------------------------
# Link model
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class Link:
    """One note link found in a Markdown body.

    ``start``/``end`` are offsets into the *original* body and cover the whole
    syntax (``[[...]]`` or ``[text](...)``), so callers can replace or decorate
    that exact range.
    """

    target: str
    """Note title the link points at, without ``.md`` and without a heading."""

    display: str
    """Text shown to the reader: the alias if one was given, else the target."""

    heading: str = ""
    """``#Heading`` part. v1 opens the note; M10 scrolls to the heading."""

    start: int = 0
    end: int = 0

    raw: str = ""
    """The exact source text of the link."""

    kind: str = "wikilink"
    """``"wikilink"`` for ``[[...]]``, ``"markdown"`` for ``[text](Name.md)``."""

    escaped_pipe: bool = False
    """True when the alias separator was written ``\\|`` (inside a table)."""

    explicit_display: bool = False
    """True when the author wrote an alias (``[[Target|alias]]``)."""


# ----------------------------------------------------------------------
# Code spans (links inside code are never links)
# ----------------------------------------------------------------------
def _fenced_spans(text: str) -> list[tuple[int, int]]:
    """Return the ranges of fenced code blocks, CommonMark style.

    A fence is 3+ backticks or tildes; it closes on a line using the same
    character, at least as long, with nothing else on the line.  An unclosed
    fence runs to the end of the document.
    """
    spans: list[tuple[int, int]] = []
    position = 0
    open_fence: tuple[str, int, int] | None = None

    for line in text.splitlines(keepends=True):
        start = position
        position += len(line)
        match = _FENCE_RE.match(line)
        if match is None:
            continue
        marker = match.group(1)
        if open_fence is None:
            open_fence = (marker[0], len(marker), start)
            continue
        char, length, fence_start = open_fence
        rest = line[match.end() :].strip()
        if marker[0] == char and len(marker) >= length and not rest:
            spans.append((fence_start, position))
            open_fence = None

    if open_fence is not None:
        spans.append((open_fence[2], len(text)))
    return spans


def _inline_code_spans(text: str, fenced: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    """Return the ranges of inline code spans outside ``fenced``.

    A run of N backticks is closed by the next run of *exactly* N backticks; an
    unclosed run is literal text, not code.
    """
    spans: list[tuple[int, int]] = []
    search_from = 0
    length = len(text)

    while search_from < length:
        opener = _BACKTICK_RUN_RE.search(text, search_from)
        if opener is None:
            break
        if _inside_spans(fenced, opener.start()):
            search_from = opener.end()
            continue
        ticks = len(opener.group(1))
        closer = _BACKTICK_RUN_RE.search(text, opener.end())
        closed = False
        while closer is not None:
            if len(closer.group(1)) == ticks:
                spans.append((opener.start(), closer.end()))
                search_from = closer.end()
                closed = True
                break
            closer = _BACKTICK_RUN_RE.search(text, closer.end())
        if not closed:
            search_from = opener.end()

    return spans


def _code_spans(text: str) -> list[tuple[int, int]]:
    """All code ranges (fenced blocks plus inline code), sorted by offset."""
    fenced = _fenced_spans(text)
    return sorted(fenced + _inline_code_spans(text, fenced))


def _inside_spans(spans: Sequence[tuple[int, int]], offset: int) -> bool:
    """Whether ``offset`` falls inside one of the sorted, disjoint ``spans``."""
    if not spans:
        return False
    idx = bisect_right(spans, (offset, float("inf")))  # type: ignore[arg-type]
    if idx == 0:
        return False
    start, end = spans[idx - 1]
    return start <= offset < end


# ----------------------------------------------------------------------
# Small text helpers
# ----------------------------------------------------------------------
def _strip_md_suffix(value: str) -> str:
    """Drop a trailing ``.md`` (any case) from a link target."""
    return value[:-3] if value.lower().endswith(".md") else value


def _fold(title: str) -> str:
    """Comparison key for a title: trimmed ends, case-insensitive."""
    return str(title).strip().casefold()


# ----------------------------------------------------------------------
# Parsing
# ----------------------------------------------------------------------
def _parse_wikilink(match: re.Match[str]) -> Link | None:
    """Build a :class:`Link` from a ``[[...]]`` match, or ``None`` to skip it."""
    inner = match.group(1)
    if not inner.strip():
        return None

    pipe = inner.find("|")
    if pipe == -1:
        target_part, alias, escaped_pipe = inner, None, False
    else:
        target_part, alias = inner[:pipe], inner[pipe + 1 :]
        # Inside a Markdown table the separator is written "\|" (section 4.7).
        escaped_pipe = target_part.endswith("\\")
        if escaped_pipe:
            target_part = target_part[:-1]

    target_part = target_part.replace("\\|", "|").strip()
    if alias is not None:
        alias = alias.replace("\\|", "|").strip()

    name_part, heading = target_part, ""
    if "#" in target_part:
        name_part, heading = target_part.split("#", 1)
        name_part, heading = name_part.strip(), heading.strip()

    target = _strip_md_suffix(name_part).strip()
    if not target:
        # ``[[#Heading]]`` points inside the current note: not a note link.
        return None

    display = alias if alias is not None else _strip_md_suffix(target_part).strip()
    return Link(
        target=target,
        display=display or target,
        heading=heading,
        start=match.start(),
        end=match.end(),
        raw=match.group(0),
        kind="wikilink",
        escaped_pipe=escaped_pipe,
        explicit_display=alias is not None,
    )


def _parse_md_link(match: re.Match[str]) -> Link | None:
    """Build a :class:`Link` from a ``[text](Name.md)`` match, or ``None``."""
    text = match.group("text")
    url = match.group("url").strip()
    if not url:
        return None
    if url.startswith("<") and url.endswith(">") and len(url) > 1:
        url = url[1:-1].strip()
    if not url or url.lower().startswith(_EXTERNAL_PREFIXES):
        return None
    # Any remaining "scheme:" prefix means it is not a local note name.
    if ":" in url.split("/")[0]:
        return None

    unquoted = urllib.parse.unquote(url)
    heading = ""
    if "#" in unquoted:
        unquoted, heading = unquoted.split("#", 1)
        heading = heading.strip()
    if not unquoted.lower().endswith(".md"):
        return None

    target = _strip_md_suffix(unquoted).strip()
    if not target:
        return None
    return Link(
        target=target,
        display=text.strip() or target,
        heading=heading,
        start=match.start(),
        end=match.end(),
        raw=match.group(0),
        kind="markdown",
    )


def parse_links(body: str) -> list[Link]:
    """Return every note link in ``body``, in document order.

    Links inside inline code or fenced code blocks are skipped, and so are
    images (``![alt](file.md)``) and destinations carrying a URL scheme.
    """
    if not body or not isinstance(body, str):
        return []

    code = _code_spans(body)
    links: list[Link] = []

    for match in _WIKILINK_RE.finditer(body):
        if _inside_spans(code, match.start()):
            continue
        link = _parse_wikilink(match)
        if link is not None:
            links.append(link)

    for match in _MD_LINK_RE.finditer(body):
        if _inside_spans(code, match.start()):
            continue
        # An image (![alt](x.md)) embeds a file; it is not a note link.
        if match.start() > 0 and body[match.start() - 1] == "!":
            continue
        link = _parse_md_link(match)
        if link is None:
            continue
        # A wikilink can sit inside a Markdown link's text; the wikilink
        # already owns those offsets, so the outer match is skipped.
        if any(match.start() < other.end and other.start < match.end() for other in links):
            continue
        links.append(link)

    links.sort(key=lambda link: link.start)
    return links


# ----------------------------------------------------------------------
# Resolution
# ----------------------------------------------------------------------
def _title_entries(titles: Any) -> list[tuple[str, str]]:
    """Normalize the shapes ``titles`` may take into ``(title, id)`` pairs.

    Accepted: a list of titles (id == title, as in Plain), a mapping of
    title -> note id, or an iterable of ``Note``-like objects or dicts.
    """
    if titles is None:
        return []
    if isinstance(titles, Mapping):
        return [(str(key), str(value)) for key, value in titles.items()]
    if isinstance(titles, (str, bytes)):
        return []

    entries: list[tuple[str, str]] = []
    for item in titles:
        if isinstance(item, str):
            entries.append((item, item))
        elif isinstance(item, Mapping):
            title = item.get("title")
            if isinstance(title, str):
                entries.append((title, str(item.get("id", title))))
        else:
            title = getattr(item, "title", None)
            if isinstance(title, str):
                entries.append((title, str(getattr(item, "id", title))))
    return entries


def _resolver(titles: Any) -> dict[str, tuple[str, str]]:
    """Build ``folded title -> (canonical title, note id)`` from ``titles``.

    The first note claiming a folded title wins; titles are unique inside a
    space, so duplicates only appear when a caller passes an inconsistent list.
    """
    lookup: dict[str, tuple[str, str]] = {}
    for title, note_id in _title_entries(titles):
        folded = _fold(title)
        if folded and folded not in lookup:
            lookup[folded] = (title.strip(), note_id)
    return lookup


def resolve(target: str, titles: Any) -> str | None:
    """Return the note id a link ``target`` points at, or ``None``.

    Ignores case, whitespace at the start or end, a trailing ``.md`` and an
    optional ``#heading``.  When ``titles`` is a plain list of titles the id is
    the canonical title itself, because Plain notes are named by their file.
    """
    if not isinstance(target, str):
        return None
    cleaned = target.strip()
    if not cleaned:
        return None
    if "#" in cleaned:
        cleaned = cleaned.split("#", 1)[0].strip()
    cleaned = _strip_md_suffix(cleaned).strip()
    if not cleaned:
        return None
    entry = _resolver(titles).get(_fold(cleaned))
    return entry[1] if entry else None


# ----------------------------------------------------------------------
# Rewriting
# ----------------------------------------------------------------------
def _escape_link_text(text: str) -> str:
    """Escape Markdown link-text characters so an alias survives rendering.

    ``|`` is escaped too: the preview source is built *before* markdown-it
    parses tables, so a raw pipe in an alias would split a table cell.
    """
    return (
        text.replace("\\", "\\\\")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("|", "\\|")
    )


def _encode_title(title: str) -> str:
    """Percent-encode a title for use inside a ``#vn-*`` address."""
    return urllib.parse.quote(title, safe="")


def _replacement_for_preview(link: Link, lookup: Mapping[str, tuple[str, str]]) -> str:
    """Markdown text that replaces one link in the preview source."""
    if link.kind == "markdown":
        # Keep the author's own link text (it may hold inline formatting);
        # the text part of a Markdown link can never contain "]" itself.
        text = link.raw[1 : link.raw.index("]")] if "]" in link.raw else _escape_link_text(link.display)
    else:
        text = _escape_link_text(link.display)

    entry = lookup.get(_fold(link.target))
    if entry is not None:
        canonical, _note_id = entry
        return f"[{text}]({OPEN_PREFIX}{_encode_title(canonical)})"
    return f"[{text}]({NEW_PREFIX}{_encode_title(link.target)})"


def render_links_for_preview(body: str, titles: Any) -> str:
    """Rewrite note links into internal addresses for the preview pane.

    * a note that exists becomes ``[shown text](#vn-open/<title>)``
    * a note that does not exist becomes ``[shown text](#vn-new/<title>)``

    Code blocks, inline code, images and external links are left untouched.
    """
    if not isinstance(body, str) or not body:
        return body if isinstance(body, str) else ""

    links = parse_links(body)
    if not links:
        return body

    lookup = _resolver(titles)
    pieces: list[str] = []
    cursor = 0
    for link in links:
        pieces.append(body[cursor : link.start])
        pieces.append(_replacement_for_preview(link, lookup))
        cursor = link.end
    pieces.append(body[cursor:])
    return "".join(pieces)


def _replacement_for_rename(link: Link, new_title: str) -> str:
    """Rebuild one link so it points at ``new_title``.

    Display text and heading survive, and the escaped-pipe form used inside
    tables is preserved.  A trailing ``.md`` in the source is dropped because
    the canonical form does not need it.
    """
    heading = f"#{link.heading}" if link.heading else ""
    if link.kind == "markdown":
        text = link.raw[1 : link.raw.index("]")] if "]" in link.raw else link.display
        destination = _encode_title(f"{new_title}.md")
        return f"[{text}]({destination}{heading})"

    if not link.explicit_display:
        return f"[[{new_title}{heading}]]"
    separator = "\\|" if link.escaped_pipe else "|"
    alias = link.display.replace("|", "\\|") if link.escaped_pipe else link.display
    return f"[[{new_title}{heading}{separator}{alias}]]"


def rename_links(body: str, old_title: str, new_title: str) -> tuple[str, int]:
    """Rewrite every link to ``old_title`` so it points at ``new_title``.

    Returns ``(new_body, links_updated)``.  Matching ignores case, whitespace
    at the ends and a trailing ``.md``; display text and headings are kept and
    code is never touched.
    """
    if not isinstance(body, str) or not body:
        return (body if isinstance(body, str) else ""), 0
    if not isinstance(old_title, str) or not isinstance(new_title, str):
        return body, 0

    old_folded = _fold(_strip_md_suffix(old_title))
    clean_new = new_title.strip()
    if not old_folded or not clean_new:
        return body, 0

    links = [link for link in parse_links(body) if _fold(link.target) == old_folded]
    if not links:
        return body, 0

    pieces: list[str] = []
    cursor = 0
    for link in links:
        pieces.append(body[cursor : link.start])
        pieces.append(_replacement_for_rename(link, clean_new))
        cursor = link.end
    pieces.append(body[cursor:])
    return "".join(pieces), len(links)


# ----------------------------------------------------------------------
# Backwards-compatible helpers
# ----------------------------------------------------------------------
def find_links_in_body(body: str) -> list[str]:
    """Every ``[[wikilink]]`` target in ``body``, in order, code excluded."""
    return [link.target for link in parse_links(body) if link.kind == "wikilink"]


def count_links(body: str) -> int:
    """Number of ``[[wikilinks]]`` in ``body``, ignoring code."""
    return len(find_links_in_body(body))


#: Older name kept so existing call sites keep working.
rename_links_in_body = rename_links


def calculate_backlinks(notes: Iterable[Any], target_title: str) -> list[dict[str, str]]:
    """``[{id, title}]`` for the notes in ``notes`` that link to ``target_title``.

    A one-shot helper for callers without an index; :class:`LinkIndex` is the
    fast path used by the Bridge API.
    """
    folded_target = _fold(_strip_md_suffix(target_title or ""))
    if not folded_target:
        return []

    backlinks: list[dict[str, str]] = []
    for note in notes:
        note_id, title, body = _note_fields(note)
        if _fold(title) == folded_target:
            continue  # a note is not its own backlink
        if any(_fold(link.target) == folded_target for link in parse_links(body)):
            backlinks.append({"id": note_id, "title": title})
    backlinks.sort(key=lambda item: (item["title"].casefold(), item["id"]))
    return backlinks


# ----------------------------------------------------------------------
# The in-memory link graph for one space
# ----------------------------------------------------------------------
def _note_fields(note: Any) -> tuple[str, str, str]:
    """Extract ``(id, title, body)`` from a ``Note``, a dict, or anything alike."""
    if isinstance(note, Mapping):
        return (
            str(note.get("id", "")),
            str(note.get("title", "")),
            str(note.get("body", "") or ""),
        )
    return (
        str(getattr(note, "id", "")),
        str(getattr(note, "title", "")),
        str(getattr(note, "body", "") or ""),
    )


class LinkIndex:
    """The link graph of **one space**, held in memory only.

    Build it from the space's notes (at startup for Plain, on unlock for a
    vault), keep it current with :meth:`update` and :meth:`remove` after every
    save, and :meth:`clear` it the moment the space locks.  Nothing is ever
    written to disk (security rule 11).

    Resolution is incremental: a link to a note that does not exist yet is
    remembered as *pending* and starts working as soon as that note is created,
    so build order does not matter and single-note updates stay cheap.
    """

    def __init__(self, space_id: str = "") -> None:
        self.space_id = space_id
        self._titles: dict[str, str] = {}
        self._id_by_folded: dict[str, str] = {}
        self._links: dict[str, list[Link]] = {}
        self._resolved: dict[str, dict[str, None]] = {}
        self._incoming: dict[str, dict[str, None]] = {}
        self._unresolved: dict[str, dict[str, str]] = {}
        self._pending: dict[str, dict[str, None]] = {}

    # -- lifecycle -----------------------------------------------------
    def clear(self) -> None:
        """Forget every note, title and link (called when a space locks)."""
        self._titles.clear()
        self._id_by_folded.clear()
        self._links.clear()
        self._resolved.clear()
        self._incoming.clear()
        self._unresolved.clear()
        self._pending.clear()

    def build(self, notes: Iterable[Any]) -> None:
        """(Re)index a whole space.  Titles are registered before links."""
        self.clear()
        items = [_note_fields(note) for note in notes]
        for note_id, title, _body in items:
            self._set_title(note_id, title)
        for note_id, _title, body in items:
            self._index_links(note_id, body)

    def update(self, note: Any) -> None:
        """Index one new, changed or renamed note."""
        note_id, title, body = _note_fields(note)
        if not note_id:
            return
        self.remove(note_id)
        self._set_title(note_id, title)
        self._promote_pending(note_id)
        self._index_links(note_id, body)

    def remove(self, note_id: str) -> None:
        """Drop one note: its links, its title, and the links pointing at it."""
        if not note_id:
            return
        for target_id in self._resolved.pop(note_id, {}):
            self._incoming.get(target_id, {}).pop(note_id, None)
        for folded in self._unresolved.pop(note_id, {}):
            self._pending.get(folded, {}).pop(note_id, None)
        self._links.pop(note_id, None)

        title = self._titles.pop(note_id, None)
        sources = self._incoming.pop(note_id, {})
        if title is None:
            return
        folded = _fold(title)
        if self._id_by_folded.get(folded) == note_id:
            del self._id_by_folded[folded]
        # Notes that linked here now point at a title that is missing.
        pending = self._pending.setdefault(folded, {})
        for source in sources:
            self._resolved.get(source, {}).pop(note_id, None)
            if source in self._links:
                pending.setdefault(source, None)
                self._unresolved.setdefault(source, {})[folded] = title

    # -- queries -------------------------------------------------------
    def __len__(self) -> int:
        return len(self._titles)

    def __contains__(self, note_id: object) -> bool:
        return str(note_id) in self._titles

    def has(self, note_id: str) -> bool:
        """Whether ``note_id`` is indexed."""
        return note_id in self._titles

    def titles(self) -> list[str]:
        """Every title in this space, sorted for the ``[[`` suggestions."""
        return sorted(self._titles.values(), key=lambda title: (title.casefold(), title))

    def title_of(self, note_id: str) -> str | None:
        """The indexed title of one note, or ``None``."""
        return self._titles.get(note_id)

    def note_id_for(self, title: str) -> str | None:
        """The note id behind a title, ignoring case and a trailing ``.md``."""
        folded = _fold(_strip_md_suffix(title if isinstance(title, str) else ""))
        return self._id_by_folded.get(folded) if folded else None

    def resolve(self, target: str) -> str | None:
        """Resolve a link target inside this space (never across spaces)."""
        return self.note_id_for(target)

    def links_of(self, note_id: str) -> list[Link]:
        """The parsed links stored for one note."""
        return list(self._links.get(note_id, ()))

    def link_count(self, note_id: str) -> int:
        """How many ``[[wikilinks]]`` one note contains."""
        return sum(1 for link in self._links.get(note_id, ()) if link.kind == "wikilink")

    def backlinks(self, note_id: str) -> list[dict[str, str]]:
        """``[{id, title}]`` of the notes in this space that link to ``note_id``."""
        result = [
            {"id": source, "title": self._titles.get(source, "")}
            for source in self._incoming.get(note_id, {})
            if source != note_id and source in self._titles
        ]
        result.sort(key=lambda item: (item["title"].casefold(), item["id"]))
        return result

    def outgoing(self, note_id: str) -> list[dict[str, Any]]:
        """Distinct links *from* one note.

        Each entry is ``{id, title, resolved}``; an unresolved link has
        ``id=None`` and keeps the text the author wrote.  Self-links are
        excluded because they survive renames and moves.
        """
        result: list[dict[str, Any]] = [
            {"id": target, "title": self._titles.get(target, ""), "resolved": True}
            for target in self._resolved.get(note_id, {})
            if target != note_id
        ]
        result.extend(
            {"id": None, "title": written, "resolved": False}
            for written in self._unresolved.get(note_id, {}).values()
        )
        result.sort(key=lambda item: (not item["resolved"], str(item["title"]).casefold()))
        return result

    def missing_targets(self, note_id: str) -> list[str]:
        """Link targets in one note that do not resolve in this space.

        The text the author wrote is returned (not the folded lookup key), so
        the UI can offer to create a note with exactly that title.
        """
        return list(self._unresolved.get(note_id, {}).values())

    def sources_linking_to(self, note_id: str) -> list[str]:
        """Note ids that link to ``note_id`` (same data as :meth:`backlinks`)."""
        return [item["id"] for item in self.backlinks(note_id)]

    # -- internals -----------------------------------------------------
    def _set_title(self, note_id: str, title: str) -> None:
        if not note_id:
            return
        self._titles[note_id] = title
        folded = _fold(title)
        if not folded:
            return
        owner = self._id_by_folded.get(folded)
        # Titles are unique per space; if a caller passes duplicates the first
        # note keeps the name so resolution stays predictable.
        if owner is None or owner == note_id:
            self._id_by_folded[folded] = note_id

    def _promote_pending(self, note_id: str) -> None:
        """Turn "missing" links into real ones now that a title exists."""
        title = self._titles.get(note_id)
        if not title:
            return
        sources = self._pending.pop(_fold(title), None)
        if not sources:
            return
        folded = _fold(title)
        for source in sources:
            self._unresolved.get(source, {}).pop(folded, None)
            if source in self._links:
                self._resolved.setdefault(source, {})[note_id] = None
                self._incoming.setdefault(note_id, {})[source] = None

    def _index_links(self, note_id: str, body: str) -> None:
        links = parse_links(body)
        self._links[note_id] = links
        if not links:
            return
        resolved = self._resolved.setdefault(note_id, {})
        unresolved = self._unresolved.setdefault(note_id, {})
        for link in links:
            folded = _fold(link.target)
            target_id = self._id_by_folded.get(folded)
            if target_id is None:
                unresolved.setdefault(folded, link.target)
                self._pending.setdefault(folded, {})[note_id] = None
            elif target_id != note_id:
                resolved[target_id] = None
                self._incoming.setdefault(target_id, {})[note_id] = None
