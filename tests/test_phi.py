"""PHI token filter.  Every name, id and date here is made up."""

from __future__ import annotations

import io
import json

import pytest

from vaultnotes.phi import cli, detect
from vaultnotes.phi.detect import TOKEN_RE
from vaultnotes.phi.hook import handle, is_reveal_command
from vaultnotes.phi.redact import Redactor, redact_tree
from vaultnotes.phi.vault import Vault

VERTEX = {"CLAUDE_CODE_USE_VERTEX": "1", "ANTHROPIC_VERTEX_PROJECT_ID": "dev-code-assist-464720"}

FHIR = json.dumps({
    "resourceType": "Patient", "id": "eTestPat0001xyz3",
    "name": [{"family": "Examplar", "given": ["Alicia", "Jo"]}],
    "birthDate": "1980-04-12",
    "identifier": [{"system": "urn:oid:1.2.3.mrn", "value": "MRN0099887"}],
    "telecom": [{"system": "phone", "value": "314-555-0142"}, {"system": "email", "value": "a.ex@example.test"}],
    "address": [{"line": ["12 Testing Ln"], "city": "Faketown", "state": "MO", "postalCode": "63199"}],
})


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setenv("PHI_HOME", str(tmp_path / "phi"))
    for k in VERTEX:
        monkeypatch.delenv(k, raising=False)
    v = Vault()
    yield v
    v.close()


def redact(vault, text):
    r = Redactor(vault)
    return r.redact(text), r.replaced


# ------------------------------------------------------------------ detection

def values(text):
    return {(f.kind, f.value) for f in detect.find(text)}


def test_fhir_patient():
    found = values(FHIR)
    for item in [("ID", "eTestPat0001xyz3"), ("NAME", "Examplar"), ("NAME", "Alicia"), ("NAME", "Jo"),
                 ("DOB", "1980-04-12"), ("MRN", "MRN0099887"), ("PHONE", "314-555-0142"),
                 ("EMAIL", "a.ex@example.test"), ("ADDR", "12 Testing Ln"), ("ADDR", "Faketown"),
                 ("ZIP", "63199")]:
        assert item in found
    assert not any(v == "MO" for _, v in found)


def test_fhir_reference_display_and_query():
    text = '{"subject": {"reference": "Patient/eAbc123", "display": "Alicia Examplar"}}\nGET /fhir/Observation?patient=eAbc123&code=1'
    found = values(text)
    assert ("NAME", "Alicia Examplar") in found
    assert ("ID", "eAbc123") in found


def test_key_value_log_line():
    found = values("INFO PaRequestId 5 patientName=Alicia Examplar MRN=MRN0099887 status=ok")
    assert ("NAME", "Alicia Examplar") in found
    assert ("MRN", "MRN0099887") in found
    assert not any(v in ("5", "ok") for _, v in found)


def test_labelled_free_text():
    found = values("Patient DOB 04/12/1980, MRN 0099887, SSN 123-45-6789, call (314) 555-0142")
    assert {("DOB", "04/12/1980"), ("MRN", "0099887"), ("SSN", "123-45-6789"), ("PHONE", "(314) 555-0142")} <= found


def test_mrn_label_glued_to_value_is_not_split():
    assert ("MRN", "0099887") not in values('"value": "MRN0099887"')


def test_x12_patient_loops_only():
    text = ("NM1*IL*1*EXAMPLAR*ALICIA****MI*W123456789~DMG*D8*19800412*F~"
            "NM1*1P*2*GOOD CLINIC*****XX*1234567893~DTP*472*D8*20261120~")
    found = values(text)
    assert {("NAME", "EXAMPLAR"), ("NAME", "ALICIA"), ("ID", "W123456789"), ("DOB", "19800412"),
            ("DATE", "20261120")} <= found
    assert not any(v in ("GOOD CLINIC", "1234567893") for _, v in found)


def test_hl7_pid():
    text = "PID|1||MRN0099887^^^HOSP^MR||Examplar^Alicia^J||19800412|F|||12 Testing Ln^^Faketown^MO^63199||(314) 555-0142|||||ACCT5551|123-45-6789"
    found = values(text)
    assert {("MRN", "MRN0099887"), ("NAME", "Examplar"), ("NAME", "Alicia"), ("DOB", "19800412"),
            ("ADDR", "12 Testing Ln"), ("ZIP", "63199"), ("SSN", "123-45-6789")} <= found
    assert not any(v in ("HOSP", "MO") for _, v in found)


def test_sqlcmd_table():
    text = ("FirstName  LastName   DOB        Status\n"
            "---------- ---------- ---------- --------\n"
            "Alicia     Examplar   1980-04-12 Approved\n"
            "\n(1 rows affected)\n")
    found = values(text)
    assert {("NAME", "Alicia"), ("NAME", "Examplar"), ("DOB", "1980-04-12")} <= found
    assert not any(v == "Approved" for _, v in found)


def test_csv_table():
    found = values("PaRequestId,PatientName,Status\n5,Alicia Examplar,Approved\n6,Bo Testa,Denied\n")
    assert {("NAME", "Alicia Examplar"), ("NAME", "Bo Testa")} <= found


def test_ordinary_output_is_left_alone():
    text = """commit a1b2c3d4e5f6
Author: Jarow Myers <jarow@example.test>
Date:   Fri Oct 16 08:21:11 2026 -0500

    build 10.0.26200 at 2026-11-13 12:00:00, 42 rows
{"id": 27346, "name": "TokenAuth", "status": "active", "createdBy": {"displayName": "Jarow Myers"}}
                     InsuranceMemberId  = ins.Details.InsuranceMemberId,
                     PaRequestId        = paRequest.PaRequestId,
                     SubmissionId       = 0, // NEW
                  if ( x.InsuranceMemberId == sub.InsuranceMemberId )
"""
    assert detect.find(text) == []


def test_bare_patient_key_resolves_by_value():
    assert ("NAME", "Alicia Examplar") in values('patient: "Alicia Examplar"')
    assert ("ID", "eAbc123") in values('patient=eAbc123')


# ---------------------------------------------------------------- redaction

def test_same_value_same_token_and_no_plaintext(vault):
    out, n = redact(vault, FHIR)
    assert n > 0
    for secret in ("Examplar", "Alicia", '"Jo"', "1980-04-12", "MRN0099887", "314-555-0142", "63199"):
        assert secret not in out
    json.loads(out)   # still valid JSON
    out2, _ = redact(vault, "patientName=Alicia Examplar, family Examplar")
    family_token = vault.token_for("NAME", "Examplar")
    assert family_token in out and family_token in out2


def test_known_values_caught_later_in_free_text(vault):
    redact(vault, FHIR)
    out, n = redact(vault, "Slack: called Examplar back; EXAMPLAR, ALICIA confirmed. ids eTestPat0001xyz3.")
    assert "Examplar" not in out and "EXAMPLAR" not in out and "ALICIA" not in out
    assert "eTestPat0001xyz3" not in out
    assert n == 4


def test_known_names_need_a_capital(vault):
    redact(vault, '{"name": [{"given": ["Mark"]}], "resourceType": "Patient"}')
    out, _ = redact(vault, "mark the item as done. Mark called.")
    assert out.startswith("mark the item") and "Mark called" not in out


def test_tokens_are_not_reprocessed(vault):
    out, _ = redact(vault, FHIR)
    again, n = redact(vault, out)
    assert again == out and n == 0


def test_reveal_round_trip(vault):
    token = vault.token_for("NAME", "Alicia Examplar")
    assert TOKEN_RE.fullmatch(token)
    assert vault.reveal(token) == (token, "NAME", "Alicia Examplar")
    assert vault.reveal(token.split(":")[2].rstrip("⟧"))[2] == "Alicia Examplar"
    assert vault.reveal("⟦PHI:NAME:000000000000⟧") is None


def test_redact_tree_keeps_shape(vault):
    response = {"stdout": "patientName=Alicia Examplar", "stderr": "", "interrupted": False, "isImage": False}
    out = redact_tree(response, Redactor(vault))
    assert set(out) == set(response) and out["interrupted"] is False
    assert "Alicia" not in out["stdout"]


# --------------------------------------------------------------------- hook

def post(command, stdout):
    return {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": command},
            "tool_response": {"stdout": stdout, "stderr": "", "interrupted": False, "isImage": False}}


def test_hook_rewrites_bash_output(vault):
    result = handle(post("sqlcmd -Q ...", "patientName=Alicia Examplar"))
    updated = result["hookSpecificOutput"]["updatedToolOutput"]
    assert "Alicia" not in updated["stdout"] and updated["isImage"] is False


def test_hook_silent_when_nothing_found(vault):
    assert handle(post("git status", "On branch main")) is None


def test_hook_fails_closed(vault, monkeypatch):
    monkeypatch.setattr("vaultnotes.phi.redact.Redactor.redact", lambda self, t: 1 / 0)
    result = handle(post("cat x", "patientName=Alicia Examplar"))
    updated = result["hookSpecificOutput"]["updatedToolOutput"]
    assert "Alicia" not in json.dumps(updated) and "withheld" in updated["stdout"]
    assert "Alicia" not in (vault.home / "hook-errors.log").read_text(encoding="utf-8")


def test_reveal_output_passes_through_only_in_vertex(vault, monkeypatch):
    cmd = ".venv/Scripts/python phi.py reveal ⟦PHI:NAME:0123456789ab⟧"
    out = "⟦PHI:NAME:0123456789ab⟧\tpatientName=Alicia Examplar"
    assert handle(post(cmd, out)) is not None
    for k, v in VERTEX.items():
        monkeypatch.setenv(k, v)
    assert handle(post(cmd, out)) is None


@pytest.mark.parametrize("cmd,ok", [
    (".venv/Scripts/python phi.py reveal ⟦PHI:NAME:0123456789ab⟧", True),
    ("cd /c/Users/JuanNunez/repos/vaultnotes && .venv/Scripts/python phi.py reveal NAME:0123456789ab abc123def456", True),
    ("python phi.py reveal x; cat patients.json", False),
    ("python phi.py reveal x | tee out", False),
    ("python phi.py reveal $(cat t)", False),
    ("python phi.py reveal x && cat y", False),
    ("cat patients.json # phi.py reveal x", False),
])
def test_reveal_command_shape(cmd, ok):
    assert is_reveal_command(cmd) is ok


def test_pre_hook_gates_reveal(vault, monkeypatch):
    def pre(cmd):
        out = handle({"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": cmd}})
        return out and out["hookSpecificOutput"]["permissionDecision"]

    assert pre("git status") is None
    assert pre("python phi.py reveal abc123def456") == "deny"
    for k, v in VERTEX.items():
        monkeypatch.setenv(k, v)
    assert pre("python phi.py reveal abc123def456") == "ask"
    assert pre("python phi.py reveal abc123def456; cat x") == "deny"


# ---------------------------------------------------------------------- CLI

def test_cli_reveal_refused_outside_vertex(vault):
    token = vault.token_for("NAME", "Alicia Examplar")
    out = io.StringIO()
    assert cli.run(["reveal", token], out=out) == 3
    assert "Alicia" not in out.getvalue()


def test_cli_reveal_in_vertex_is_audited(vault, monkeypatch):
    for k, v in VERTEX.items():
        monkeypatch.setenv(k, v)
    token = vault.token_for("NAME", "Alicia Examplar")
    vault.close()
    out = io.StringIO()
    assert cli.run(["reveal", token], out=out) == 0
    assert out.getvalue() == f"{token}\tAlicia Examplar\n"
    log = (vault.home / "reveal.log").read_text(encoding="utf-8")
    assert token in log and "Alicia" not in log

    out = io.StringIO()
    cli.run(["reveal", "--stdin"], stdin=io.StringIO(f"Hi {token}!"), out=out)
    assert out.getvalue() == "Hi Alicia Examplar!"


def test_purge(vault):
    vault.token_for("NAME", "Alicia Examplar")
    assert vault.purge(days=1) == 0
    assert vault.purge(days=-1) == 1 and vault.count() == 0


def test_unrelated_pair_does_not_hide_the_next_one():
    found = values("hook check: patientName=Testy Fakename MRN=ZZ0000001")
    assert ("NAME", "Testy Fakename") in found and ("MRN", "ZZ0000001") in found
