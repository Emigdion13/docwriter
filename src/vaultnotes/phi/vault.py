"""The PHI token store: value -> ``⟦PHI:KIND:<id>⟧`` and back.

* **Key:** ``%LOCALAPPDATA%\\VaultNotes\\phi\\phi.vnkey`` (VaultNotes key format,
  unwrapped so the hook can run without a prompt).  Created on first use and
  never backed up.  Losing it makes every token unreadable, which is fine:
  tokens hold no PHI by themselves.
* **Store:** ``tokens.db`` (SQLite) next to the key.  Each row holds the token,
  an HMAC of the normalised value (so the same value always maps to the same
  token, and later sightings can be matched without plaintext) and the value
  encrypted with AES-256-GCM.
* ``PHI_HOME`` overrides the folder (tests).

Two sub-keys are derived from the master key with HKDF: one for the HMAC, one
for AES-GCM.  The HMAC is keyed, so a token cannot be brute forced back to a
date of birth or a short MRN without the key.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from vaultnotes.storage.atomic import atomic_write

KEY_NAME = "phi.vnkey"
DB_NAME = "tokens.db"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
    token   TEXT PRIMARY KEY,
    mac     TEXT NOT NULL UNIQUE,
    kind    TEXT NOT NULL,
    nonce   BLOB NOT NULL,
    ct      BLOB NOT NULL,
    created TEXT NOT NULL
);
"""
_TOKEN_ID = re.compile(r"(?:⟦?PHI:)?(?:([A-Z]+):)?([0-9a-f]{12,64})⟧?")


def default_home() -> Path:
    override = os.environ.get("PHI_HOME")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "VaultNotes" / "phi"


def normalise(kind: str, value: str) -> str:
    """The form that is HMACed: names ignore case, commas and spacing."""
    v = " ".join(value.split())
    if kind == "NAME":
        v = " ".join(v.replace(",", " ").split()).casefold()
    return v


def _hkdf(master: bytes, info: bytes) -> bytes:
    prk = hmac.new(b"vaultnotes-phi-v1", master, hashlib.sha256).digest()
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()


class Vault:
    def __init__(self, home: Path | None = None) -> None:
        self.home = Path(home) if home else default_home()
        self.key_path = self.home / KEY_NAME
        self.db_path = self.home / DB_NAME
        self._mac_key: bytes | None = None
        self._enc_key: bytes | None = None
        self._db: sqlite3.Connection | None = None
        self._known: dict[str, tuple[str, str]] | None = None

    # ------------------------------------------------------------------ setup

    def exists(self) -> bool:
        return self.key_path.is_file() and self.db_path.is_file()

    def _load_key(self) -> None:
        if self._mac_key is not None:
            return
        if not self.key_path.is_file():
            self._create_key()
        data = json.loads(self.key_path.read_text(encoding="utf-8"))
        if data.get("format") != "vaultnotes-key" or data.get("protection", "none") != "none":
            raise ValueError(f"{self.key_path} is not an unwrapped VaultNotes key file")
        master = base64.b64decode(data["key_b64"])
        if len(master) != 32:
            raise ValueError(f"{self.key_path} does not hold a 256-bit key")
        self._mac_key = _hkdf(master, b"phi-mac")
        self._enc_key = _hkdf(master, b"phi-enc")

    def _create_key(self) -> None:
        from filelock import FileLock

        self.home.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self.home / ".key.lock"), timeout=30):
            if self.key_path.is_file():   # another hook won the race
                return
            data = {
                "format": "vaultnotes-key",
                "version": 1,
                "vault_id": str(uuid.uuid4()),
                "vault_name": "PHI tokens",
                "protection": "none",
                "key_b64": base64.b64encode(secrets.token_bytes(32)).decode("ascii"),
                "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            atomic_write(self.key_path, json.dumps(data, indent=2) + "\n")

    def _conn(self) -> sqlite3.Connection:
        if self._db is None:
            self.home.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(_SCHEMA)
        return self._db

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    # --------------------------------------------------------------- hashing

    def mac(self, kind: str, value: str) -> str:
        self._load_key()
        assert self._mac_key is not None
        return hmac.new(self._mac_key, normalise(kind, value).encode("utf-8"), hashlib.sha256).hexdigest()

    def known(self) -> dict[str, tuple[str, str]]:
        """Every stored value's HMAC -> (token, kind).  Empty when nothing is stored yet."""
        if self._known is None:
            if not self.db_path.is_file():
                self._known = {}
            else:
                rows = self._conn().execute("SELECT mac, token, kind FROM tokens").fetchall()
                self._known = {m: (t, k) for m, t, k in rows}
        return self._known

    # ------------------------------------------------------------- tokenise

    def token_for(self, kind: str, value: str) -> str:
        value = value.strip()
        mac = self.mac(kind, value)
        known = self.known()
        if mac in known:
            return known[mac][0]
        db = self._conn()
        row = db.execute("SELECT token, kind FROM tokens WHERE mac = ?", (mac,)).fetchone()
        if row:
            known[mac] = (row[0], row[1])
            return row[0]

        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce = secrets.token_bytes(12)
        ct = AESGCM(self._enc_key).encrypt(nonce, value.encode("utf-8"), mac.encode("ascii"))
        created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for length in (12, 16, 24, 32, 64):
            token = f"⟦PHI:{kind}:{mac[:length]}⟧"
            try:
                db.execute("INSERT INTO tokens (token, mac, kind, nonce, ct, created) VALUES (?, ?, ?, ?, ?, ?)",
                           (token, mac, kind, nonce, ct, created))
                known[mac] = (token, kind)
                return token
            except sqlite3.IntegrityError:
                row = db.execute("SELECT token, kind FROM tokens WHERE mac = ?", (mac,)).fetchone()
                if row:   # a parallel hook stored the same value first
                    known[mac] = (row[0], row[1])
                    return row[0]
                # otherwise a different value owns this short token: try a longer one
        raise RuntimeError("could not store a PHI token")

    # --------------------------------------------------------------- reveal

    def reveal(self, token_or_id: str) -> tuple[str, str, str] | None:
        """(token, kind, value) for a token, ``KIND:id`` or bare id; None if unknown."""
        m = _TOKEN_ID.fullmatch(token_or_id.strip())
        if not m or not self.db_path.is_file():
            return None
        token_id = m.group(2)
        row = self._conn().execute(
            "SELECT token, mac, kind, nonce, ct FROM tokens WHERE token LIKE ? ESCAPE '\\'",
            (f"%:{token_id}⟧",),
        ).fetchone()
        if not row:
            return None
        token, mac, kind, nonce, ct = row
        self._load_key()
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        value = AESGCM(self._enc_key).decrypt(nonce, ct, mac.encode("ascii")).decode("utf-8")
        return token, kind, value

    # ---------------------------------------------------------------- admin

    def count(self) -> int:
        if not self.db_path.is_file():
            return 0
        return self._conn().execute("SELECT COUNT(*) FROM tokens").fetchone()[0]

    def purge(self, days: int) -> int:
        if not self.db_path.is_file():
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        cur = self._conn().execute("DELETE FROM tokens WHERE created < ?", (cutoff,))
        self._known = None
        return cur.rowcount
