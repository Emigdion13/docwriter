"""Find PHI in text: patient names, ids, patient dates and contact details.

Rule based, no NER.  Detection looks at structure that says "this is a person":

* JSON (az CLI, FHIR, App Insights, MCP results): keys such as ``birthDate``,
  ``patientName``, ``memberId``, FHIR ``HumanName`` / ``identifier`` / ``telecom``
  / ``address`` objects and references to ``Patient/...``.
* ``key: value`` / ``key=value`` pairs anywhere (log lines, query strings).
* Tables: sqlcmd and ``-o table`` output (header + dashes line), and CSV / TSV /
  pipe tables, by column header.
* X12 (278 and friends): NM1/N3/N4/REF/PER in subscriber and patient loops,
  DMG birth dates, DTP service/admit/discharge dates.
* HL7 v2: PID, NK1, GT1, IN1, PV1 fields.
* Labelled free text: "MRN 12345", "DOB 01/02/1980", SSN and phone shapes.

Only *patient* dates are reported (birth, death, service, admit, discharge...);
plain timestamps in logs and git output are left alone.

Names written in plain prose with no label ("we called John about...") are not
found here.  Once a name has been seen in structured data, :mod:`redact` finds
its later appearances anywhere through the token store.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

#: A token written by :mod:`vault`, e.g. ``⟦PHI:NAME:3f9a0c1b2d4e⟧``.
TOKEN_RE = re.compile(r"⟦PHI:[A-Z]+:[0-9a-f]{12,64}⟧")

KINDS = ("NAME", "DOB", "DATE", "MRN", "ID", "SSN", "PHONE", "EMAIL", "ADDR", "ZIP")


@dataclass(frozen=True)
class Finding:
    """One PHI value.  ``start``/``end`` are -1 when it was found by structure only."""

    kind: str
    value: str
    start: int = -1
    end: int = -1

    @property
    def positioned(self) -> bool:
        return self.start >= 0


# --------------------------------------------------------------------------- keys

def norm_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


_EXACT_KEYS: dict[str, str] = {}
for _kind, _keys in {
    "NAME": "firstname lastname middlename fullname givenname familyname surname given family "
            "maidenname mothersmaidenname preferredname legalname",
    "DOB": "dob dateofbirth birthdate birthdatetime birthday",
    "DATE": "deceaseddatetime deathdate dateofdeath dateofservice servicedate dos admitdate "
            "admissiondate dischargedate encounterdate appointmentdate visitdate",
    "MRN": "mrn mrns medicalrecordnumber medicalrecordno",
    "SSN": "ssn socialsecuritynumber",
    "ID": "memberid subscriberid insurancememberid policynumber fhirid fhirids epicid emrid "
          "referralid reportid encounterid encounterfhirid csn",
    "PHONE": "phone phonenumber homephone mobilephone cellphone workphone telephone",
    "ADDR": "address address1 address2 addressline addressline1 addressline2 street "
            "streetaddress streetline city",
    "ZIP": "zip zipcode postalcode",
}.items():
    for _k in _keys.split():
        _EXACT_KEYS[_k] = _kind

_PERSON_WORDS = ("patient", "member", "subscriber", "insured", "guarantor", "dependent",
                 "beneficiary", "nextofkin", "relatedperson", "emergencycontact", "mother",
                 "father", "spouse", "guardian", "policyholder")

#: (substring, kind) checked in order on keys that name a person.
_ATTR_CONTAINS = (("birth", "DOB"), ("dob", "DOB"), ("mrn", "MRN"), ("ssn", "SSN"),
                  ("phone", "PHONE"), ("email", "EMAIL"), ("zip", "ZIP"), ("postal", "ZIP"),
                  ("address", "ADDR"), ("street", "ADDR"), ("firstname", "NAME"),
                  ("lastname", "NAME"), ("middlename", "NAME"), ("givenname", "NAME"),
                  ("familyname", "NAME"), ("fullname", "NAME"), ("identifier", "ID"))
_ATTR_ENDS = (("names", "NAME"), ("name", "NAME"), ("ids", "ID"), ("id", "ID"),
              ("number", "ID"), ("num", "ID"), ("no", "ID"))

#: Key endings that always hold an id, whatever comes before them.
_ID_SUFFIXES = (("mrn", "MRN"), ("emrid", "ID"), ("fhirid", "ID"), ("epicid", "ID"),
                ("memberid", "ID"), ("subscriberid", "ID"), ("referralid", "ID"),
                ("reportid", "ID"), ("patientid", "ID"))

#: Keys that describe a person field rather than hold it (``patientIdType``).
_NOT_VALUE_SUFFIXES = ("type", "status", "count", "flag", "enabled", "required", "format",
                       "system", "source", "kind", "length", "mask", "label", "url", "uri",
                       "path", "template", "exists", "missing", "match", "matched")


def classify_key(key: str) -> str | None:
    """The PHI kind a key holds, ``"PERSON"`` for a bare person key, or None."""
    k = norm_key(key)
    if not k or len(k) > 60:
        return None
    if k in _EXACT_KEYS:
        return _EXACT_KEYS[k]
    if k.endswith(_NOT_VALUE_SUFFIXES):
        return None
    for suffix, kind in _ID_SUFFIXES:   # strEmrId, primaryMrn, epicPatientFhirId
        if k.endswith(suffix):
            return kind
    if k in _PERSON_WORDS or k in ("pt", "subject"):
        return "PERSON" if k != "subject" else None
    if not (any(w in k for w in _PERSON_WORDS) or re.match(r"pt(name|dob|id|mrn|first|last)", k)):
        return None
    for attr, kind in _ATTR_CONTAINS:
        if attr in k:
            return kind
    for attr, kind in _ATTR_ENDS:
        if k.endswith(attr):
            return kind
    return None


# ------------------------------------------------------------------------ values

_SKIP_VALUES = {"", "null", "none", "nil", "n/a", "na", "unknown", "true", "false", "redacted",
                "masked", "string", "0", "-", "undefined", "empty", "nan"}
_DATE_SHAPE = re.compile(r"\d")
_NAME_SHAPE = re.compile(r"[A-Za-z]")


def resolve_kind(kind: str, value: str) -> str | None:
    """Check that ``value`` can be a ``kind`` value; returns the final kind or None."""
    v = value.strip()
    if len(v) < 2 or len(v) > 200 or v.lower() in _SKIP_VALUES:
        return None
    if TOKEN_RE.search(v) or set(v) <= set("*xX#-_ ./"):
        return None
    if kind == "PERSON":
        if re.fullmatch(r"[A-Za-z'.\-]+(?:,?\s+[A-Za-z'.\-]+){1,4}", v):
            return "NAME"
        if re.fullmatch(r"[A-Za-z0-9\-.]{2,64}", v):
            return "ID"
        return None
    if kind in ("DOB", "DATE") and not _DATE_SHAPE.search(v):
        return None
    if kind == "NAME" and (not _NAME_SHAPE.search(v) or "://" in v or "\\" in v or len(v) > 100):
        return None
    if kind == "EMAIL" and "@" not in v:
        return None
    return kind


# ---------------------------------------------------------------------------- JSON

_PERSON_RESOURCES = {"Patient", "RelatedPerson", "Person"}
_DISPLAY_PARENTS = {"subject", "patient", "beneficiary", "subscriber", "policyholder",
                    "individual", "member"}


_DECODER = json.JSONDecoder()
_JSON_START = re.compile(r"^[ \t]*[\[{]|\{\"", re.M)


def _json_findings(text: str) -> list[Finding]:
    """Findings from the whole text as JSON, or from JSON embedded in it.

    Embedded JSON: blocks that start a line (az output after a warning line,
    JSON lines) and objects inside log lines (``... payload={"a": 1}``).
    """
    if len(text) > 20_000_000 or ("{" not in text and "[" not in text):
        return []
    out: list[Finding] = []
    s = text.strip()
    if s[:1] in "[{":
        try:
            _walk(json.loads(s), "", None, out, 0)
            return out
        except (ValueError, RecursionError):
            pass
    pos, tries = 0, 0
    while tries < 500:
        m = _JSON_START.search(text, pos)
        if not m:
            break
        tries += 1
        # line-start match ends on its bracket; an inline '{"' match starts on it
        start = m.end() - 1 if m.group(0)[-1] in "[{" else m.start()
        try:
            data, end = _DECODER.raw_decode(text, start)
        except (ValueError, RecursionError):
            pos = m.end()
            continue
        if isinstance(data, (dict, list)):
            _walk(data, "", None, out, 0)
        pos = max(end, m.end())
    return out


def _add(out: list[Finding], kind: str | None, value: object) -> None:
    if not isinstance(value, str) or kind is None:
        return
    final = resolve_kind(kind, value)
    if final:
        out.append(Finding(final, value.strip()))


def _walk(node: object, parent: str, resource: str | None, out: list[Finding], depth: int) -> None:
    if depth > 60:
        return
    if isinstance(node, list):
        for item in node:
            if isinstance(item, str):
                kind = classify_key(parent) if parent else None
                _add(out, kind, item)
            else:
                _walk(item, parent, resource, out, depth + 1)
        return
    if isinstance(node, str):
        if node[:1] in "[{":
            for f in _json_findings(node):
                out.append(f)
        return
    if not isinstance(node, dict):
        return

    rt = node.get("resourceType")
    resource = rt if isinstance(rt, str) else resource
    p = norm_key(parent)
    system = str(node.get("system", "")).lower()
    id_type = json.dumps(node.get("type", "")).lower()

    for key, value in node.items():
        k = norm_key(key)
        kind = classify_key(key)

        # FHIR shapes that only make sense with their parent key.
        if p == "name" and k in ("text", "family", "given"):
            kind = "NAME"
        elif p == "telecom" and k == "value":
            kind = "EMAIL" if system == "email" else "PHONE"
        elif p == "address" and k in ("line", "text", "city", "district"):
            kind = "ADDR"
        elif p == "address" and k == "postalcode":
            kind = "ZIP"
        elif p == "identifier" and k == "value":
            if "ssn" in system or "ssn" in id_type or '"ss"' in id_type:
                kind = "SSN"
            elif "mrn" in system or '"mr"' in id_type or "mrn" in id_type:
                kind = "MRN"
            elif resource in _PERSON_RESOURCES or resource == "Coverage" or "patient" in system \
                    or "member" in system or '"mb"' in id_type:
                kind = "ID"
            else:
                kind = None
        elif k == "id" and resource in _PERSON_RESOURCES and "resourceType" in node:
            kind = "ID"
        elif k == "display" and (p in _DISPLAY_PARENTS
                                 or str(node.get("reference", "")).startswith(("Patient/", "RelatedPerson/"))):
            kind = "NAME"
        elif k == "name" and resource in _PERSON_RESOURCES and isinstance(value, str):
            kind = "NAME"

        if isinstance(value, str):
            if kind:
                _add(out, kind, value)
            elif value[:1] in "[{":
                out.extend(_json_findings(value))
        elif isinstance(value, list) and kind and all(isinstance(v, str) for v in value):
            for v in value:
                _add(out, kind, v)
        elif isinstance(value, (dict, list)):
            _walk(value, key, resource, out, depth + 1)


# ---------------------------------------------------------------- key: value pairs

_KV_RE = re.compile(
    r"""(?<![A-Za-z0-9_])(?P<q>["']?)(?P<key>[A-Za-z_][A-Za-z0-9_.\-]{0,40}(?:[ ][A-Za-z][A-Za-z0-9_]{0,20}){0,3})(?P=q)"""
    r"""[ \t]*[:=][ \t]*"""
    r"""(?:(?P<vq>["'])(?P<v1>(?:\\.|(?!(?P=vq))[^\\\r\n]){0,200})(?P=vq)"""
    r"""|(?P<v2>[^\s"',;{}\[\]()<>&]+(?:[ \t]+[A-Z][A-Za-z'\-.]*(?![A-Za-z'\-.])(?![ \t]*[:=])){0,3}))"""
)


#: An unquoted value that is a code expression, not data: member access,
#: calls, generics, keywords.
_CODE_VALUE = re.compile(
    r"^(?:[=!]|(?:new|null|await|this|default)\b|(?:string|int)\.)"
    r"|^[A-Za-z_]\w*(?:\??\.[A-Za-z_]\w*)+(?:\(|$|\s)"
    r"|[()<>]"
)


def _kv_findings(text: str) -> list[Finding]:
    """PHI in ``key: value`` / ``key=value`` pairs.

    A pair whose key is not PHI must not swallow the text after it, or
    "check: patientName=X" would hide "patientName=X": so after a rejected
    match the search resumes one character later, not at the match end.
    """
    out: list[Finding] = []
    pos = 0
    while True:
        m = _KV_RE.search(text, pos)
        if not m:
            return out
        finding = _kv_finding(text, m)
        if finding:
            out.append(finding)
            pos = m.end()
        else:
            pos = m.start() + 1


def _kv_finding(text: str, m: re.Match[str]) -> Finding | None:
    key = m.group("key")
    kind = classify_key(key)
    if kind is None and " " in key:
        # "the patient name: X" -> try the last words of the key
        words = key.split(" ")
        for i in range(1, len(words)):
            kind = classify_key(" ".join(words[i:]))
            if kind:
                break
    if kind is None:
        return None
    group = "v1" if m.group("v1") is not None else "v2"
    raw = m.group(group)
    value = raw.strip()
    if group == "v2" and (_CODE_VALUE.search(value) or text.endswith("==", 0, m.start(group))):
        return None   # source code: "InsuranceMemberId = ins.Details.InsuranceMemberId"
    final = resolve_kind(kind, value)
    if not final:
        return None
    start = m.start(group) + (len(raw) - len(raw.lstrip()))
    return Finding(final, value, start, start + len(value))


# ------------------------------------------------------------- labelled free text

_DATE = r"(?:\d{4}-\d{2}-\d{2}(?:T[0-9:.]+(?:Z|[+-]\d{2}:?\d{2})?)?|\d{1,2}/\d{1,2}/\d{2,4}|\d{1,2}-\d{1,2}-\d{4}|\d{8})"
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ID", re.compile(r"\b(?:Patient|RelatedPerson|Person)/(?P<v>[A-Za-z0-9\-.]{1,64}[A-Za-z0-9])")),
    ("ID", re.compile(r"(?i)[?&;](?:patient|subject|beneficiary|patient_id|patientid)=(?P<v>[A-Za-z0-9\-.]{2,64})")),
    ("MRN", re.compile(r"(?i)\b(?:MRN|medical\s+record\s+(?:number|no\.?|#))(?:[ \t]*[:#=][ \t]*|[ \t]+)(?P<v>[A-Za-z0-9][A-Za-z0-9\-]{2,31})\b")),
    ("DOB", re.compile(r"(?i)\b(?:DOB|D\.O\.B\.?|date\s+of\s+birth|birth\s*date)[ \t]*[:=]?[ \t]*(?P<v>" + _DATE + r")")),
    ("SSN", re.compile(r"(?<![\d-])(?P<v>\d{3}-\d{2}-\d{4})(?![\d-])")),
    ("SSN", re.compile(r"(?i)\bSSN[ \t]*[:#=]?[ \t]*(?P<v>\d{9})\b")),
    ("PHONE", re.compile(r"(?<![\w.\-])(?P<v>(?:\+1[-. ]?)?(?:\(\d{3}\)[ ]?|\d{3}[-.])\d{3}[-.]\d{4})(?![\w.\-])")),
)


def _pattern_findings(text: str) -> list[Finding]:
    out = []
    for kind, rx in _PATTERNS:
        for m in rx.finditer(text):
            value = m.group("v")
            if resolve_kind(kind, value):
                out.append(Finding(kind, value, m.start("v"), m.end("v")))
    return out


# -------------------------------------------------------------------------- tables

_DASH_LINE = re.compile(r"^[ \t]*-{2,}(?:[ \t]+-{2,})*[ \t]*$")


def _lines_with_offsets(text: str) -> list[tuple[int, str]]:
    out, pos = [], 0
    for line in text.splitlines(keepends=True):
        out.append((pos, line.rstrip("\r\n")))
        pos += len(line)
    return out


def _cell(out: list[Finding], kind: str | None, line_start: int, line: str, a: int, b: int) -> None:
    if kind is None or a >= len(line):
        return
    raw = line[a:b]
    value = raw.strip().strip('"')
    final = resolve_kind(kind, value)
    if final:
        start = line_start + a + raw.find(value)
        out.append(Finding(final, value, start, start + len(value)))


def _fixed_width_findings(lines: list[tuple[int, str]]) -> list[Finding]:
    out: list[Finding] = []
    for i in range(1, len(lines)):
        dash = lines[i][1]
        if not _DASH_LINE.match(dash):
            continue
        header = lines[i - 1][1]
        cols = [(m.start(), m.end()) for m in re.finditer(r"-+", dash)]
        names = [header[a:b + 1].strip() for a, b in cols]
        if not all(_HEADER_CELL.fullmatch(n) for n in names if n):
            continue
        kinds = [classify_key(n) if n else None for n in names]
        if not any(kinds):
            continue
        # last column runs to the end of the line
        spans = [(a, cols[j + 1][0] if j + 1 < len(cols) else 10**9) for j, (a, _) in enumerate(cols)]
        for start, row in lines[i + 1:]:
            if not row.strip() or _DASH_LINE.match(row) or re.match(r"^\(\d+ rows? affected\)", row.strip()):
                break
            for (a, b), kind in zip(spans, kinds):
                _cell(out, kind, start, row, a, b)
    return out


_HEADER_CELL = re.compile(r"[A-Za-z_#][A-Za-z0-9_ .#/\-]{0,40}")


def _header_kinds(header: str, delim: str) -> list[str | None]:
    """Column kinds when ``header`` really is a table header, else []."""
    cells = [c.strip().strip('"') for c in header.split(delim)]
    if delim == "|":
        cells = cells[1:-1] if header.strip().startswith("|") else cells
    named = [c for c in cells if c]
    if len(named) < 2 or not all(_HEADER_CELL.fullmatch(c) for c in named):
        return []
    kinds = [classify_key(c) if c else None for c in cells]
    if delim == "|" and header.strip().startswith("|"):
        kinds = [None] + kinds + [None]
    return kinds


def _delimited_findings(lines: list[tuple[int, str]]) -> list[Finding]:
    out: list[Finding] = []
    i = 0
    while i < len(lines):
        _, header = lines[i]
        delim = next((d for d, need in (("\t", 1), ("|", 2), (",", 2)) if header.count(d) >= need), None)
        kinds = _header_kinds(header, delim) if delim else []
        if not any(kinds):
            i += 1
            continue
        n = header.count(delim)
        j = i + 1
        while j < len(lines):
            start, row = lines[j]
            if delim == "|" and re.fullmatch(r"[\s|:\-]+", row):
                j += 1
                continue
            if row.count(delim) != n:
                break
            pos = 0
            for idx, cell in enumerate(row.split(delim)):
                if idx < len(kinds):
                    _cell(out, kinds[idx], start, row, pos, pos + len(cell))
                pos += len(cell) + 1
            j += 1
        i = j if j > i + 1 else i + 1
    return out


# ----------------------------------------------------------------------------- X12

_X12_PATIENT_ENTITIES = {"IL", "QC", "QD"}   # insured/subscriber, patient, responsible party
#: DTP qualifiers for patient dates: service, admission, discharge, statement, process.
_X12_DATE_QUALIFIERS = {"472", "435", "096", "434", "009"}


def _x12_findings(text: str) -> list[Finding]:
    if "NM1" not in text and "DMG" not in text:
        return []
    isa = text.find("ISA")
    sep, term = "*", "~"
    if isa >= 0 and len(text) > isa + 105:
        sep, term = text[isa + 3], text[isa + 105]
    out: list[Finding] = []
    in_patient = False
    pos = 0
    for raw in re.split(r"(" + re.escape(term) + r"|\r?\n)", text):
        seg_start = pos
        pos += len(raw)
        seg = raw.strip()
        if not seg or seg == term:
            continue
        lead = len(raw) - len(raw.lstrip())
        elems, offsets, p = [], [], seg_start + lead
        for e in seg.split(sep):
            elems.append(e)
            offsets.append(p)
            p += len(e) + 1
        tag = elems[0]

        def add(idx: int, kind: str) -> None:
            if idx < len(elems):
                value = elems[idx].strip()
                final = resolve_kind(kind, value)
                if final:
                    start = offsets[idx] + elems[idx].find(value)
                    out.append(Finding(final, value, start, start + len(value)))

        if tag == "HL":
            in_patient = False
        elif tag == "NM1":
            in_patient = len(elems) > 1 and elems[1] in _X12_PATIENT_ENTITIES
            if in_patient:
                for idx in (3, 4, 5):
                    add(idx, "NAME")
                add(9, "ID")
        elif tag == "DMG" and len(elems) > 2 and elems[1] == "D8":
            add(2, "DOB")
        elif tag == "DTP" and len(elems) > 3 and elems[1] in _X12_DATE_QUALIFIERS:
            add(3, "DATE")
        elif in_patient and tag == "N3":
            add(1, "ADDR")
            add(2, "ADDR")
        elif in_patient and tag == "N4":
            add(1, "ADDR")
            add(3, "ZIP")
        elif in_patient and tag == "REF" and len(elems) > 2:
            add(2, "SSN" if elems[1] == "SY" else "ID")
        elif in_patient and tag == "PER":
            add(2, "NAME")
            for idx in (4, 6, 8):
                if idx < len(elems):
                    add(idx, "EMAIL" if elems[idx - 1] == "EM" else "PHONE")
    return out


# ----------------------------------------------------------------------------- HL7

#: segment -> field index -> [(component index or 0 for whole field, kind)]
_HL7_FIELDS: dict[str, dict[int, list[tuple[int, str]]]] = {
    "PID": {2: [(1, "ID")], 3: [(1, "MRN")], 4: [(1, "ID")], 5: [(1, "NAME"), (2, "NAME"), (3, "NAME")],
            6: [(1, "NAME")], 7: [(0, "DOB")], 9: [(1, "NAME"), (2, "NAME")],
            11: [(1, "ADDR"), (2, "ADDR"), (3, "ADDR"), (5, "ZIP")],
            13: [(1, "PHONE"), (4, "EMAIL")], 14: [(1, "PHONE")], 18: [(1, "ID")],
            19: [(0, "SSN")], 20: [(1, "ID")], 29: [(0, "DATE")]},
    "NK1": {2: [(1, "NAME"), (2, "NAME")], 4: [(1, "ADDR"), (3, "ADDR"), (5, "ZIP")],
            5: [(1, "PHONE")], 6: [(1, "PHONE")]},
    "GT1": {3: [(1, "NAME"), (2, "NAME")], 5: [(1, "ADDR"), (3, "ADDR"), (5, "ZIP")],
            6: [(1, "PHONE")], 8: [(0, "DOB")], 12: [(0, "SSN")]},
    "IN1": {16: [(1, "NAME"), (2, "NAME")], 18: [(0, "DOB")], 19: [(1, "ADDR"), (3, "ADDR"), (5, "ZIP")],
            36: [(0, "ID")], 49: [(1, "ID")]},
    "IN2": {1: [(1, "ID")], 2: [(0, "SSN")]},
    "PV1": {19: [(1, "ID")], 44: [(0, "DATE")], 45: [(0, "DATE")]},
    "MRG": {1: [(1, "MRN")], 7: [(1, "NAME"), (2, "NAME")]},
}
_HL7_LINE = re.compile(r"^(MSH|PID|NK1|GT1|IN1|IN2|PV1|MRG)(.)", re.M)


def _hl7_findings(text: str) -> list[Finding]:
    if not _HL7_LINE.search(text):
        return []
    out: list[Finding] = []
    pos = 0
    for raw in re.split(r"(\r\n|\r|\n)", text):
        line_start = pos
        pos += len(raw)
        m = _HL7_LINE.match(raw)
        if not m or m.group(1) == "MSH":
            continue
        fs = m.group(2)
        if fs.isalnum() or fs.isspace():
            continue
        spec = _HL7_FIELDS[m.group(1)]
        fields = raw.split(fs)
        offset = line_start
        for idx, field in enumerate(fields):
            if idx in spec and field:
                rep_off = offset
                for rep in field.split("~"):
                    comps = rep.split("^")
                    for comp_idx, kind in spec[idx]:
                        if comp_idx == 0:
                            value, c_off = rep.split("^")[0], rep_off
                        elif comp_idx <= len(comps):
                            value = comps[comp_idx - 1]
                            c_off = rep_off + sum(len(c) + 1 for c in comps[:comp_idx - 1])
                        else:
                            continue
                        v = value.strip()
                        final = resolve_kind(kind, v)
                        if final:
                            start = c_off + value.find(v)
                            out.append(Finding(final, v, start, start + len(v)))
                    rep_off += len(rep) + 1
            offset += len(field) + 1
    return out


# ---------------------------------------------------------------------------- main

def find(text: str) -> list[Finding]:
    """Every PHI value found in ``text``; positioned findings do not overlap."""
    if not text or len(text) < 4:
        return []
    lines = _lines_with_offsets(text)
    found = (_json_findings(text) + _kv_findings(text) + _pattern_findings(text)
             + _fixed_width_findings(lines) + _delimited_findings(lines)
             + _x12_findings(text) + _hl7_findings(text))
    tokens = [(m.start(), m.end()) for m in TOKEN_RE.finditer(text)]
    positioned = sorted((f for f in found if f.positioned), key=lambda f: (f.start, -(f.end - f.start)))
    kept: list[Finding] = []
    last_end = -1
    for f in positioned:
        if any(a < f.end and f.start < b for a, b in tokens):
            continue   # inside an existing token ("MRN:78f1..." in ⟦PHI:MRN:78f1...⟧)
        if f.start >= last_end:
            kept.append(f)
            last_end = f.end
    loose = list(dict.fromkeys(f for f in found if not f.positioned))
    return kept + loose
