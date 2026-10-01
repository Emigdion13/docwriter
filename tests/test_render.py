"""Tests for Markdown rendering, code highlighting, tables, task lists and link rewriting."""

from __future__ import annotations

from vaultnotes.render import render_markdown, render_preview, toggle_task


def test_render_tables() -> None:
    source = """
| Header 1 | Header 2 |
|---|---|
| Cell 1 | Cell 2 |
| Cell 3 | Cell 4 |
"""
    html = render_markdown(source)
    assert "<table>" in html
    assert "<th>Header 1</th>" in html
    assert "<th>Header 2</th>" in html
    assert "<td>Cell 1</td>" in html
    assert "<td>Cell 2</td>" in html


def test_render_task_lists() -> None:
    source = """
- [x] Completed task
- [ ] Incomplete task
"""
    html = render_markdown(source)
    assert "task-list" in html or "task" in html
    assert "Completed task" in html
    assert "Incomplete task" in html
    assert 'checked="checked"' in html or "checked" in html


def test_render_code_colors() -> None:
    source = """
```python
def calculate(a, b):
    # Sum values
    return a + b
```
"""
    html = render_markdown(source)
    assert '<pre class="code">' in html
    assert '<span class="lang">python</span>' in html
    assert "tok-k" in html  # keyword 'def' or 'return'
    assert "calculate" in html


def test_render_raw_html_escaped() -> None:
    source = """
Normal text with <script>alert(1)</script> and <img src=x onerror=alert(1)> tags.
"""
    html = render_markdown(source)
    # Raw HTML must be escaped into entity references, not active tags
    assert "<script>" not in html
    assert "<img" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html


def test_render_wikilinks_existing_and_missing() -> None:
    source = "Link to [[Home lab]] and [[Missing Page]] and [[Travel 2026|my trip]]."
    titles = ["Home lab", "Travel 2026"]
    html = render_preview(source, titles=titles)

    # Existing note links point to #vn-open/
    assert 'href="#vn-open/Home%20lab"' in html
    assert 'href="#vn-open/Travel%202026"' in html
    assert "my trip" in html

    # Missing note links point to #vn-new/
    assert 'href="#vn-new/Missing%20Page"' in html


def test_render_wikilinks_in_code_ignored() -> None:
    source = "Inline `[[Not A Link]]` and block:\n```\n[[Also Not A Link]]\n```"
    html = render_preview(source, titles=["Not A Link", "Also Not A Link"])

    assert "#vn-open" not in html
    assert "#vn-new" not in html
    assert "[[Not A Link]]" in html
    assert "[[Also Not A Link]]" in html


# ----------------------------------------------------------------------
# M10: embeds, heading addresses and links out to the Plain space
# ----------------------------------------------------------------------
class Library:
    """Notes the renderer may read, keyed by title, like the vault stores are."""

    def __init__(self, **notes: str) -> None:
        self.notes = notes

    def titles(self) -> list[str]:
        return sorted(self.notes)

    def read(self, title: str):
        body = self.notes.get(title)
        return None if body is None else {"title": title, "body": body}


def test_heading_target_is_part_of_the_address() -> None:
    library = Library(**{"Travel 2026": "# Travel 2026\n\n## Hotels\n\nBook it.\n"})
    html = render_markdown("[[Travel 2026#Hotels]]", library.titles())

    assert 'href="#vn-open/Travel%202026#Hotels"' in html


def test_block_embed_inlines_the_other_note() -> None:
    library = Library(**{"Home lab": "# Home lab\n\n- Pi 5\n"})
    html = render_markdown("intro\n\n![[Home lab]]\n\noutro", library.titles(), read_note=library.read)

    assert '<div class="vn-embed"' in html
    assert '<span class="vn-embed-title">Home lab</span>' in html
    assert "<h1>Home lab</h1>" in html
    assert "<li>Pi 5</li>" in html
    # The surrounding text keeps its own paragraphs.
    assert html.index("<p>intro</p>") < html.index('class="vn-embed"') < html.index("<p>outro</p>")


def test_inline_embed_does_not_break_the_sentence() -> None:
    library = Library(**{"Home lab": "See [[Trip]].\n", "Trip": "# Trip\n"})
    html = render_markdown("Notes: ![[Home lab]] end.", library.titles(), read_note=library.read)

    assert "Notes:" in html and "end." in html
    assert 'class="vn-embed"' in html
    # The link inside the embedded note is still clickable.
    assert 'href="#vn-open/Trip"' in html


def test_embed_of_a_missing_note_falls_back_to_a_link() -> None:
    html = render_markdown("![[Gone]]", [], read_note=Library().read)

    assert "\u2063" not in html
    assert 'href="#vn-new/Gone"' in html


def test_embed_cycles_are_cut_off_after_the_first_visit() -> None:
    library = Library(
        **{
            "A": "a-body\n\n![[B]]\n",
            "B": "b-body\n\n![[A]] and ![[A]]\n",
        }
    )
    html = render_markdown("![[A]]", library.titles(), read_note=library.read)

    assert html.count("a-body") == 1
    assert html.count("b-body") == 1
    assert "\u2063" not in html
    assert html.count("<script") == 0


def test_embed_depth_is_limited() -> None:
    library = Library(
        **{
            "A": "![[B]]\n",
            "B": "![[C]]\n",
            "C": "![[D]]\n",
            "D": "deep\n",
        }
    )
    html = render_markdown("![[A]]", library.titles(), read_note=library.read)

    # MAX_EMBED_DEPTH is 2, so the deepest level is offered as a link instead
    # of being inlined: the chain is cut, never followed forever.
    assert 'class="vn-embed"' in html
    assert "#vn-open/C" in html
    assert "#vn-open/D" not in html
    assert "deep" not in html


def test_raw_html_in_an_embedded_note_stays_text() -> None:
    library = Library(**{"Home lab": '<script>alert("x")</script>\n'})
    html = render_markdown("![[Home lab]]", library.titles(), read_note=library.read)

    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_plain_link_from_a_vault_note_only_goes_outward() -> None:
    """``[[Plain:Trip]]`` resolves when the caller hands over the Plain space."""
    library = Library(**{"Trip": "# Trip\n"})
    html = render_markdown(
        "[[Plain:Trip]] and [[Trip]]",
        library.titles(),
        read_note=library.read,
        spaces={"plain": library.titles()},
    )

    assert "#vn-open/plain/Trip" in html
    assert "#vn-open/Trip" in html


def test_a_vault_is_never_a_link_target_even_if_offered() -> None:
    html = render_markdown(
        "[[Encrypted:Bank]] and [[Personal:Codes]]",
        [],
        spaces={"plain": ["Bank"]},
    )

    assert "[[Encrypted:Bank]]" in html
    assert "[[Personal:Codes]]" in html
    assert "#vn-" not in html


# ----------------------------------------------------------------------
# Clickable checklists: toggle_task flips the n-th checkbox the preview draws
# ----------------------------------------------------------------------
def test_toggle_task_checks_and_unchecks() -> None:
    body = "- [x] a\n- [ ] b\n"
    assert toggle_task(body, 1) == "- [x] a\n- [x] b\n"
    assert toggle_task(body, 0) == "- [ ] a\n- [ ] b\n"
    assert toggle_task("- [X] a\n", 0) == "- [ ] a\n"


def test_toggle_task_counts_in_preview_order_with_nested_quoted_and_numbered() -> None:
    body = "- [ ] a\n  - [ ] nested\n\n> - [ ] quoted\n\n1. [ ] numbered\n"
    html = render_markdown(body)
    assert html.count("task-list-item-checkbox") == 4
    assert toggle_task(body, 1) == "- [ ] a\n  - [x] nested\n\n> - [ ] quoted\n\n1. [ ] numbered\n"
    assert toggle_task(body, 2) == "- [ ] a\n  - [ ] nested\n\n> - [x] quoted\n\n1. [ ] numbered\n"
    assert toggle_task(body, 3) == "- [ ] a\n  - [ ] nested\n\n> - [ ] quoted\n\n1. [x] numbered\n"


def test_toggle_task_skips_code_blocks_and_plain_brackets() -> None:
    body = "```\n- [ ] in code\n```\n\nnot a task [ ] here\n\n- [ ] real\n"
    assert toggle_task(body, 0) == "```\n- [ ] in code\n```\n\nnot a task [ ] here\n\n- [x] real\n"
    assert toggle_task(body, 1) is None


def test_toggle_task_leaves_front_matter_and_line_endings_alone() -> None:
    body = "---\nimportant: true\ntags: [a]\n---\r\n- [ ] one\r\n- [ ] two\r\n"
    assert toggle_task(body, 1) == "---\nimportant: true\ntags: [a]\n---\r\n- [ ] one\r\n- [x] two\r\n"


def test_toggle_task_rejects_a_missing_item() -> None:
    assert toggle_task("- [ ] only\n", 1) is None
    assert toggle_task("- [ ] only\n", -1) is None
    assert toggle_task("- [ ] only\n", True) is None  # type: ignore[arg-type]
    assert toggle_task("no list at all", 0) is None
    assert toggle_task("", 0) is None
