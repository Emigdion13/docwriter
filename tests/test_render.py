"""Tests for Markdown rendering, code highlighting, tables, task lists and link rewriting."""

from __future__ import annotations

from vaultnotes.render import render_markdown, render_preview


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
