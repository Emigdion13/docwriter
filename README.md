# VaultNotes

A futuristic Markdown notes app for Windows, with two encrypted vaults whose keys
live on **your** computer (or a USB stick) — never in the cloud, never in the app.

- **Plain** — ordinary `.md` files you can open in any editor.
- **Encrypted** and **Personal** — each note is AES-256-GCM encrypted on disk, each
  with its own key file. The two vaults use different keys, on purpose.
- **Google Drive backup** — your notes folder, uploaded by you, on your schedule.
  The key files are never part of it.

The engine is Python (pywebview + Edge WebView2). The look is HTML/CSS/JS built by
Vite into `src/vaultnotes/web/`.

---

## 1. Install

### Run it from this folder (needs Python and Node)

1. Install **Python 3.12** from <https://www.python.org> — tick *"Add python.exe to PATH"*.
2. Install **Node.js LTS** from <https://nodejs.org>. You only need it to build the look.
3. In this folder:

   ```bat
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   cd frontend
   npm install
   npm run build
   cd ..
   python run.py
   ```

`python run.py` loads the built frontend. During CSS or JS work, start the Vite
dev server (`npm run dev` inside `frontend/`) and run `python run.py --dev` instead,
so the window reloads as you type.

### Run the packaged app (needs neither Python nor Node)

```bat
build.bat
```

That builds the frontend, regenerates the app icon, and runs PyInstaller. The app
ends up at **`dist\VaultNotes\VaultNotes.exe`**; copy the whole `VaultNotes` folder
to move it. On Windows 11 nothing else is needed. On Windows 10, if the window opens
blank, install the free *Microsoft Edge WebView2 Runtime* from Microsoft.

### Tests

```bat
.venv\Scripts\activate
python -m pytest -q
```

Frontend changes are checked by `npm run build` (it fails on a broken import) and by
opening the app; there is no separate JS test step.

---

## 2. First run

1. VaultNotes asks for a **notes folder** (default: `Documents\VaultNotes`). Everything
   you write lives there, in plain files:

   ```
   Documents\VaultNotes\
   ├─ plain\                 Shopping list.md, Home lab.md, …   (+ .trash\)
   └─ vaults\
      ├─ encrypted\          vault.json, <opaque-id>.vnote      (+ .trash\)
      └─ personal\           vault.json, <opaque-id>.vnote      (+ .trash\)
   ```

   Encrypted notes are named after a random id, **not** their title, so a filename
   reveals nothing.

2. Choose **Create vaults** in the setup dialog. VaultNotes generates two key files and
   asks where to save each one, e.g. `E:\keys\personal.vnkey`. Key files are refused
   *inside* the notes folder — that is the whole point of them.
3. Unlock a vault with its key file. Locking (or the auto-lock timer, 10 minutes by
   default) throws away every decrypted note in memory; nothing is cached on disk.
4. Optionally type a passphrase in the setup dialog to wrap each new key file
   (see §4 below).

Settings, the notes-folder location and the auto-lock delay live in
`%APPDATA%\VaultNotes\settings.json`.

---

## 3. Back up your key files — read this part

A key file is the only way into its vault. There is **no recovery, no "email me a reset
link"**: lose the file and the vault's notes are unreadable forever.

- Copy each `.vnkey` to a second place you control (a second USB stick, or a password
  manager that stores files). Keep them **out of the notes folder**, and out of the
  Drive backup.
- Print the base64 blob if you like: `type personal.vnkey` and keep the text somewhere
  safe. `key_b64` is the whole secret for an unprotected key file.
- Losing a key file does not delete your notes; it just makes them unreadable. Replace
  the lost file by creating a fresh vault and copying your notes in, if it ever happens.

`*.vnkey` is listed in `.gitignore` so a key file can never be committed by accident.

---

## 4. Key files protected by a passphrase (optional)

By default a key file holds its key as plain base64: whoever has the *file* has the key.
You can wrap it with a passphrase instead (format in the build plan, §4.5):

- **New vaults:** type a passphrase (per vault) in the first-run setup dialog.
- **Existing key file:** wrap it from a terminal —

  ```bat
  .venv\Scripts\python.exe -c "import sys; sys.path.insert(0,'src'); ^
  from pathlib import Path; from vaultnotes.crypto.keyfile import load_key_file, save_key_file; ^
  p = Path(r'E:\keys\personal.vnkey'); k = load_key_file(p); save_key_file(p, k, passphrase='your phrase')"
  ```

- **Unlocking:** a wrapped key file makes the unlock dialog show a *Key file passphrase*
  box. Nothing about the passphrase is stored, logged or sent anywhere, and a wrong
  passphrase simply fails to unlock.
- **Forgotten passphrase:** there is no recovery, same as a lost key file. The wrap uses
  scrypt (`N=2^17`), which is deliberately slow for anyone trying to guess it.

---

## 5. Google Drive backup (one-time setup, about 10 minutes)

1. Go to <https://console.cloud.google.com> and create a project called "VaultNotes".
2. **APIs & Services → Library** → enable **Google Drive API**.
3. Set up the **OAuth consent screen** (it may be listed under "Google Auth Platform"):
   user type **External**, app name VaultNotes, your email, and the scope
   `.../auth/drive.file`. Add your Gmail address as a test user.
4. **Credentials → Create credentials → OAuth client ID** → application type
   **Desktop app**. Download the JSON before you close the popup: Google shows the
   secret only once.
5. In VaultNotes, open **Settings → Choose client_secret.json…** and pick the file you
   downloaded. The app checks that it is a Desktop app client and copies it to:

   ```
   %APPDATA%\VaultNotes\client_secret.json
   ```

   That exact name and folder is the only place the app looks. You can copy it there
   yourself instead, but not from a packaged Windows app (the Claude desktop app, for
   one): its writes to `%APPDATA%` land in its own private copy of the folder.
6. Keep a copy of that file somewhere safe: on a new PC you choose the same file again,
   and your sign-in is already allowed.
7. While the app's status is "Testing", Google asks you to sign in again every 7 days.
   To stop that, set **Publishing status → In production**. With only `drive.file`
   requested, Google normally does not require a review. With a Google Workspace
   account, **Audience → Make internal** does the same without publishing; only
   accounts in your organization can then sign in.

How it works: the app copies your notes folder into a Drive folder named
`VaultNotes Backup`, keeping the same structure, and remembers what it uploaded in
`%APPDATA%\VaultNotes\backup_manifest.json`. Only files whose contents changed are sent.
Key files (`.vnkey`) are never uploaded, and `client_secret.json` stays home too. The
`drive.file` scope means the app can only see files it created itself.

- **Back up now:** Ctrl+B, the Drive card in the sidebar, or Settings → *Back up now*.
- **Auto-backup:** a per-interval timer (default hourly) that runs only while the app
  is open and skips files nothing touched.
- **Prune:** *Clean up Drive…* (Settings, or the palette, once you are connected and
  the Drive folder exists) removes files you deleted locally more than 30 days ago.
  Nothing is deleted from Drive without you pressing it.
- **Restore from Drive…:** pulls the backup into a folder you pick. Restoring into a
  folder that already has notes is refused, so it cannot overwrite your work.

---

## 6. Moving to a new PC

1. Copy the notes folder (`Documents\VaultNotes`), or run *Restore from Drive…* into a
   fresh folder.
2. Put your key files back where they belong (that is why they are backed up
   separately) and set the notes folder in the first-run dialog if it is not at the
   default location.
3. To use Drive again, open **Settings → Choose client_secret.json…**, pick your saved
   copy, and press **Connect Google Drive**.
4. Unlock each vault with its key file. Everything is there: notes, trash, links,
   backlinks — the app keeps no database to rebuild.

---

## 7. Day to day

| Shortcut | What it does |
|---|---|
| Ctrl+K | Command palette: any note, any command |
| Ctrl+N | New note |
| Ctrl+S | Save now (the app also auto-saves about a second after you stop typing) |
| Ctrl+F | Search this space |
| Ctrl+E | Edit / Split / Preview |
| Ctrl+L | Lock all vaults |
| Ctrl+B | Back up now |
| Esc | Close a dialog, the palette or the graph |

The **command palette** (Ctrl+K) also runs *Link graph for this space*, *New note*,
*Lock all vaults*, *Back up now*, *Restore from Drive…*, *Clean up Drive…*, the
themes and the effects level, so nothing in the app is only reachable by mouse.

Notes link to each other with `[[Title]]`, `[[Title|shown text]]` and `[[Title#Heading]]`
(jumps to that heading in the preview). `![[Title]]` shows the other note inside the
preview. In a vault note, `[[Plain:Title]]` links to a Plain note — never the other way
round, so a Plain note can never list, open or link into a vault. Renaming a note
rewrites the links that point at it; "Linked from" sits under the preview. Typing
`[[` suggests titles from the open space only.

Ctrl+click (Cmd+click) a `[[link]]` in the **editor** opens it, or offers to create it
if the note does not exist yet.

**Word count** is in the note's meta line. **Themes:** Nebula (default), Synthwave and
Arctic. **Effects:** *Lite* and *Off* drop the blur and glow for older machines
(Settings, the status bar, or the palette).

### Reading notes from a script or an AI helper

`notes.py` (it runs `vaultnotes.notes_cli`) reads Plain and Personal notes without the
app. It never writes anything, and it refuses the **Encrypted** vault outright: it never
lists that vault, never opens its folder, and never uses its key. Use Encrypted for
anything a helper must not see, such as PHI.

```
.venv\Scripts\python notes.py --root "%USERPROFILE%\Documents\VaultNotes" list plain
.venv\Scripts\python notes.py --root ... read plain "Shopping list"
.venv\Scripts\python notes.py --root ... --personal-key E:\keys\personal.vnkey search personal flights
```

`--root` and `--personal-key` can be set once as `VAULTNOTES_ROOT` and
`VAULTNOTES_PERSONAL_KEY`. `read` accepts a note id, its title, or a unique part of the
title. A passphrase-protected Personal key is not accepted; open that vault in the app.

---

## 8. Deliberate limits

Things the spec forbids, so the app does not do them:

- No hidden metadata, index or sidecar files for Plain notes: a `.md` file you edit in
  another program is a note, and VaultNotes must not need anything next to it.
- No cache of decrypted text on disk, ever, including `%TEMP%`.
- No plaintext fallback if the Windows credential store is unavailable: the Drive
  sign-in fails with a clear message instead of writing a token unprotected.
- A `[[Plain:Title]]` link is not rewritten when the *Plain* note is renamed while the
  vault holding the link is locked (a locked vault's index exists only in memory).
  Fix it by unlocking the vault before renaming.
- Backup uploads the whole notes folder on its first run; deleting notes locally does
  not delete them from Drive until you press *Clean up Drive…*.
- No frameless window (the toolbar stays inside a normal title bar), no tag or pin
  system, and no encrypted index cache for fast unlocks. The first is a Windows-only
  cosmetic risk, the second would need hidden metadata next to Plain notes, and the
  third would put a searchable copy of vault titles on disk — all three are optional
  extras the plan allows leaving out, so they are left out rather than half-built.

## 9. Layout of this repository

```
run.py                     start the app ("--dev" for the Vite dev server)
notes.py                   read Plain / Personal notes from a shell (never Encrypted)
build.bat                  the M9 build: frontend, icon, VaultNotes.exe
frontend/                  the look: Vite, plain JS modules, no framework
src/vaultnotes/            the engine: api.py (Bridge API), storage/, crypto/,
                           backup/, links.py, render.py, autolock.py, config.py
src/vaultnotes/web/        built frontend (generated by npm run build, not edited)
packaging/make_icon.py     redraws vaultnotes.png / .ico from the design's logo
design/                    the visual reference (not shipped)
tests/                     pytest: engine, Bridge API, backup, hardening
VaultNotes-Build-Plan.md   the specification this code follows
```
