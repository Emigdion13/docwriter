"""``phi.py``: the PHI token filter from the command line.

    python phi.py hook                    # Claude Code hook (JSON on stdin)
    python phi.py reveal TOKEN [TOKEN..]  # decrypt tokens (verified Vertex session only)
    python phi.py reveal --stdin          # decrypt every token in a text
    python phi.py scan                    # stdin -> tokenised stdout
    python phi.py status                  # where the key and store are, how many tokens
    python phi.py purge --days 30         # forget tokens older than N days

Every reveal is logged (time and tokens, never values) to ``reveal.log``.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from typing import Sequence

from vaultnotes.phi.detect import TOKEN_RE
from vaultnotes.phi.hook import VERTEX_PROJECT, handle, is_vertex
from vaultnotes.phi.vault import Vault


def _utf8_out(out):
    return out if out is not None else open(sys.stdout.fileno(), "w", encoding="utf-8", closefd=False)


def _audit(vault: Vault, tokens: list[str]) -> None:
    vault.home.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(vault.home / "reveal.log", "a", encoding="utf-8") as f:
        f.write(f"{stamp} {' '.join(tokens)}\n")


def _cmd_hook(stdin) -> int:
    raw = stdin.buffer.read() if hasattr(stdin, "buffer") else stdin.read()
    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    result = handle(json.loads(text))
    if result is not None:
        sys.stdout.buffer.write(json.dumps(result).encode("utf-8"))
    return 0


def _cmd_reveal(args, stdin, out) -> int:
    if not is_vertex():
        print(f"Refused: PHI can only be revealed in a verified Vertex session "
              f"(CLAUDE_CODE_USE_VERTEX=1, project {VERTEX_PROJECT}).", file=sys.stderr)
        return 3
    vault = Vault()
    try:
        if args.stdin:
            text = stdin.read()
            found = []

            def sub(m):
                hit = vault.reveal(m.group(0))
                if hit:
                    found.append(hit[0])
                    return hit[2]
                return m.group(0)

            result = TOKEN_RE.sub(sub, text)
            _audit(vault, found)
            out.write(result)
            return 0
        status, found = 0, []
        for item in args.tokens:
            hit = vault.reveal(item)
            if hit:
                found.append(hit[0])
                out.write(f"{hit[0]}\t{hit[2]}\n")
            else:
                out.write(f"{item}\t(unknown token)\n")
                status = 1
        _audit(vault, found)
        return status
    finally:
        vault.close()


def _cmd_scan(stdin, out) -> int:
    from vaultnotes.phi.redact import Redactor

    vault = Vault()
    try:
        redactor = Redactor(vault)
        raw = stdin.buffer.read() if hasattr(stdin, "buffer") else stdin.read()
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        out.write(redactor.redact(text))
        print(f"{redactor.replaced} value(s) replaced", file=sys.stderr)
        return 0
    finally:
        vault.close()


def _cmd_status(out) -> int:
    vault = Vault()
    try:
        out.write(f"key:     {vault.key_path} ({'present' if vault.key_path.is_file() else 'created on first use'})\n")
        out.write(f"store:   {vault.db_path}\n")
        out.write(f"tokens:  {vault.count()}\n")
        out.write(f"vertex:  {'verified' if is_vertex() else 'no (reveal refused)'}\n")
        return 0
    finally:
        vault.close()


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="phi.py", description="Tokenise PHI in text, and reveal tokens.")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("hook", help="run as a Claude Code PreToolUse/PostToolUse hook")
    r = sub.add_parser("reveal", help="decrypt tokens (verified Vertex session only)")
    r.add_argument("tokens", nargs="*", help="tokens, KIND:id or bare ids")
    r.add_argument("--stdin", action="store_true", help="decrypt every token in the text on stdin")
    sub.add_parser("scan", help="tokenise stdin to stdout")
    sub.add_parser("status", help="show the key, the store and the token count")
    g = sub.add_parser("purge", help="forget tokens older than N days")
    g.add_argument("--days", type=int, required=True)
    return p


def run(argv: Sequence[str] | None = None, stdin=None, out=None) -> int:
    args = _parser().parse_args(argv)
    stdin = stdin if stdin is not None else sys.stdin
    if args.command == "hook":
        return _cmd_hook(stdin)
    out = _utf8_out(out)
    if args.command == "reveal":
        if not args.tokens and not args.stdin:
            print("Give tokens or --stdin.", file=sys.stderr)
            return 2
        return _cmd_reveal(args, stdin, out)
    if args.command == "scan":
        return _cmd_scan(stdin, out)
    if args.command == "status":
        return _cmd_status(out)
    if args.command == "purge":
        vault = Vault()
        try:
            out.write(f"{vault.purge(args.days)} token(s) removed\n")
        finally:
            vault.close()
        return 0
    return 2


def main() -> None:
    try:
        sys.stdin.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    sys.exit(run())
