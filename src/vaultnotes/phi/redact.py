"""Replace PHI in text with tokens.

Three passes over the text, none of which touches an existing token:

1. **Spans:** values :mod:`detect` found at a position are replaced there.
2. **Repeats:** every value found anywhere in this text (4+ characters) is
   replaced wherever else it appears, as a whole word.  Short structural values
   (a two-letter given name in FHIR JSON) are only replaced as JSON strings.
3. **Known values:** any word or run of capitalised words whose HMAC matches a
   value stored earlier, so a name seen once in FHIR is caught later in a Slack
   message or a log line.  Dates and ZIP codes are left out of this pass: they
   collide with ordinary numbers too often.
"""

from __future__ import annotations

import json
import re
from typing import Iterable

from vaultnotes.phi import detect
from vaultnotes.phi.detect import TOKEN_RE, Finding
from vaultnotes.phi.vault import Vault

MAX_TEXT = 5_000_000
_WORD = re.compile(r"[A-Za-z][A-Za-z'\-]*")
_ID_CANDIDATE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9][A-Za-z0-9\-.]{3,63}(?![A-Za-z0-9])")
_NAME_GAP = re.compile(r"[ \t]{1,3}|,[ \t]{0,2}")
_KNOWN_SKIP = {"DOB", "DATE", "ZIP", "ADDR"}


def _outside_tokens(text: str, fn) -> str:
    """Apply ``fn`` to every part of ``text`` that is not already a token."""
    parts, pos = [], 0
    for m in TOKEN_RE.finditer(text):
        parts.append(fn(text[pos:m.start()]))
        parts.append(m.group(0))
        pos = m.end()
    parts.append(fn(text[pos:]))
    return "".join(parts)


class Redactor:
    def __init__(self, vault: Vault | None = None) -> None:
        self.vault = vault or Vault()
        self.replaced = 0

    def redact(self, text: str) -> str:
        """``text`` with PHI replaced; ``self.replaced`` counts replacements."""
        if not text or len(text) > MAX_TEXT:
            return text
        findings = detect.find(text)
        if findings:
            text = self._replace_spans(text, [f for f in findings if f.positioned])
            text = self._replace_repeats(text, findings)
        if self.vault.exists() and self.vault.known():
            text = _outside_tokens(text, self._replace_known)
        return text

    # ----------------------------------------------------------------- passes

    def _replace_spans(self, text: str, spans: list[Finding]) -> str:
        out, pos = [], 0
        for f in spans:   # sorted and non-overlapping (detect.find)
            out.append(text[pos:f.start])
            out.append(self.vault.token_for(f.kind, f.value))
            pos = f.end
            self.replaced += 1
        out.append(text[pos:])
        return "".join(out)

    def _replace_repeats(self, text: str, findings: Iterable[Finding]) -> str:
        long_values: dict[str, str] = {}
        json_literals: dict[str, str] = {}
        for f in findings:
            token = self.vault.token_for(f.kind, f.value)
            for form in {f.value, json.dumps(f.value)[1:-1]}:
                if len(form) >= 4:
                    long_values[form] = token
                if not f.positioned:
                    json_literals[f'"{form}"'] = f'"{token}"'
                    json_literals[f'\\"{form}\\"'] = f'\\"{token}\\"'
        if not long_values and not json_literals:
            return text

        alts = sorted(long_values, key=len, reverse=True)
        word_rx = re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(map(re.escape, alts)) + r")(?![A-Za-z0-9])") if alts else None
        lit_alts = sorted(json_literals, key=len, reverse=True)
        lit_rx = re.compile("|".join(map(re.escape, lit_alts))) if lit_alts else None

        def sub(part: str) -> str:
            if lit_rx:
                part = lit_rx.sub(self._count(lambda m: json_literals[m.group(0)]), part)
            if word_rx:
                part = word_rx.sub(self._count(lambda m: long_values[m.group(0)]), part)
            return part

        # Literal JSON strings first: they include the quotes, so they cannot
        # clash with the word pass that runs on what is left.
        return _outside_tokens(text, sub)

    def _replace_known(self, part: str) -> str:
        known = self.vault.known()
        spans: list[tuple[int, int, str]] = []

        words = list(_WORD.finditer(part))
        i = 0
        while i < len(words):
            if not words[i].group(0)[0].isupper():
                i += 1
                continue
            hit = None
            for n in range(min(4, len(words) - i), 0, -1):
                group = words[i:i + n]
                if any(not w.group(0)[0].isupper() for w in group):
                    continue
                if any(not _NAME_GAP.fullmatch(part[a.end():b.start()]) for a, b in zip(group, group[1:])):
                    continue
                start, end = group[0].start(), group[-1].end()
                candidate = part[start:end]
                if len(candidate) < 3:
                    continue
                entry = known.get(self.vault.mac("NAME", candidate))
                if entry and entry[1] not in _KNOWN_SKIP:
                    hit = (start, end, entry[0], n)
                    break
            if hit:
                spans.append(hit[:3])
                i += hit[3]
            else:
                i += 1

        taken = [(a, b) for a, b, _ in spans]
        for m in _ID_CANDIDATE.finditer(part):
            value = m.group(0).rstrip(".-")
            start, end = m.start(), m.start() + len(value)
            if len(value) < 4 or any(a < end and start < b for a, b in taken):
                continue
            entry = known.get(self.vault.mac("ID", value))
            # Names are matched by the capitalised-word pass above only: a lower
            # case "mark" hashes like the case-folded name "Mark".
            if entry and entry[1] not in _KNOWN_SKIP and entry[1] != "NAME":
                spans.append((start, end, entry[0]))
                taken.append((start, end))

        if not spans:
            return part
        spans.sort()
        out, pos = [], 0
        for a, b, token in spans:
            out.append(part[pos:a])
            out.append(token)
            pos = b
            self.replaced += 1
        out.append(part[pos:])
        return "".join(out)

    def _count(self, fn):
        def wrapper(m: re.Match[str]) -> str:
            self.replaced += 1
            return fn(m)
        return wrapper


def redact_tree(node: object, redactor: Redactor, key: str | None = None) -> object:
    """Redact every string in a JSON-like tree, keeping its shape (tool outputs)."""
    if isinstance(node, str):
        if key in ("base64", "data") and len(node) > 1000:
            return node
        return redactor.redact(node)
    if isinstance(node, dict):
        return {k: redact_tree(v, redactor, k) for k, v in node.items()}
    if isinstance(node, list):
        return [redact_tree(v, redactor, key) for v in node]
    return node
