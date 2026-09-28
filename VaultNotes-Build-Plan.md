# VaultNotes: Build Plan

> This is a specification you can give to any AI coding assistant, including free ones, to build a **beautiful, futuristic** Markdown notes app. The app has two encrypted vaults, one plain notebook, Obsidian-style links, and Google Drive backup.
> The working name is **VaultNotes**. Rename it if you like.
>
> **Two files go together:**
> - **`VaultNotes-Build-Plan.md`** (this file) says what to build.
> - **`VaultNotes-Design.html`** shows how it should look. Open it in a browser and click around.

---

## 0. How to use this plan with a free AI

1. Start a new chat. Attach or paste this whole file. **For M1, also attach `VaultNotes-Design.html`.**
2. Paste the **Starter prompt** from section 14. Ask for **one milestone at a time**. If you ask for the whole app at once, free models run out of room and start skipping code.
3. After each milestone, run the app and go through its "Done when" list. If you get an error, paste the full error text back into the chat.
4. If the chat gets long or the AI gets confused, start a fresh chat. Paste this plan, the files for the part you're working on, and: *"We finished milestones M1–M(N). Now do M(N+1)."*
5. After each working milestone, save a copy of your code folder, or commit it with Git (section 12). Then you can go back if something breaks.
6. **Getting the look right:** take a screenshot of your app next to the design file, paste both into the chat, and ask *"Make mine match the design."* Most free AIs accept images.

---

## 1. What we're building

A Windows desktop app for writing Markdown notes. Notes are organized in four **spaces**:

| Space | Stored on disk as | Unlocked by |
|---|---|---|
| **Plain** | normal `.md` files that any editor can read | nothing, always open |
| **Encrypted** | encrypted `.vnote` files | key file #1 (e.g. `encrypted.vnkey`) |
| **Personal** | encrypted `.vnote` files | key file #2 (e.g. `personal.vnkey`) |
| **AI-Notes** | normal `.md` files, like Plain, in `ai-notes/` | nothing, always open |

AI-Notes (space id `ai`, added after v1) belongs to AI helpers: `notes.py` lets them write there and nowhere else, so their findings never mix with the user's Plain notes. The app treats it exactly like Plain.

Notes can link to each other the way Obsidian does, by writing `[[Note title]]`. Each note also shows a "Linked from" list of the notes that link to it (backlinks).

**It should look stunning.** The design has:
- Dark frosted-glass panels floating over a slowly drifting aurora background.
- One neon accent color per space. The whole app re-tints when you switch spaces.
- Sci-fi animations: vault notes "decrypt" on screen when you unlock and scramble away when you lock.

Section 6 describes the design, and `VaultNotes-Design.html` shows it.

Everything in the notes folder can be backed up to Google Drive. Encrypted notes are uploaded still encrypted, so Google can't read them. **Key files are never uploaded.**

### Assumptions (change these before you start if they're wrong)

- **Platform:** Windows 10/11 desktop first.
  - The app is a real desktop window, but its inside is drawn with web technology (HTML/CSS). Windows provides this through **Microsoft Edge WebView2**, which is built into Windows 11. That's what makes the futuristic look possible.
  - It also runs on macOS and Linux with small changes. There is no mobile app in v1.
- **"Two encryption keys"** means two separate vaults, each with its own key. The design allows more vaults later.
- A key is a key file holding a random 256-bit key. The app can generate one and load it from anywhere, for example a USB stick. Protecting the key file with a passphrase is an optional extra (M10).
- **Backup is one-way** (computer → Drive), plus a manual "Restore". It is not live two-way sync.
- **One user**, using one computer at a time.

### Not in v1

- A mobile app
- Two-way sync
- Sharing or collaboration
- Images or attachments inside encrypted notes

---

## 2. Tech stack (all free)

The app has two halves:
- **The engine (Python):** storage, encryption, links, Markdown rendering, backup. It's tested with `pytest` and never deals with looks.
- **The look (HTML, CSS, plain JavaScript):** everything you see. Free AIs are very good at making web pages beautiful, which is why the look uses web technology.

The two halves talk through a small, fixed list of functions called the **Bridge API** (section 4.8).

| Part | Choice | Why |
|---|---|---|
| Engine language | **Python 3.12** | What free AIs write best, and it's easy to debug |
| Window | **pywebview** (uses Edge WebView2 on Windows) | A native desktop window with a modern web page inside. Small and free |
| Look | **HTML + CSS + plain JavaScript**, built with Vite | Full CSS for glass, glow, gradients and animation. Vite bundles everything into local files |
| Editor | **CodeMirror 6** | Fast, themeable editor with Markdown colors and autocomplete (used for `[[`) |
| Markdown → HTML | **markdown-it-py** + **mdit-py-plugins** | Renders the preview, including tables and task lists |
| Code colors in preview | **Pygments** | Colors code blocks |
| HTML safety | **DOMPurify** (JavaScript) | Cleans the preview HTML before it's shown |
| Fonts | **Space Grotesk, Inter, JetBrains Mono** via `@fontsource` | Free fonts, bundled inside the app, so no internet is needed |
| Encryption | **`cryptography` package (AES-256-GCM, Scrypt)** | Well-tested, standard library. **No hand-written crypto.** |
| Google Drive | `google-api-python-client`, `google-auth-oauthlib` | Google's own libraries. The Drive API costs nothing |
| Token storage | `keyring` (Windows Credential Manager) | Keeps the Google login token out of plain files |
| Single instance | `filelock` | Stops two copies of the app running at once |
| Tests | `pytest` | |
| Packaging | PyInstaller | Builds a `.exe` |

`requirements.txt`:
```
pywebview>=5.0
cryptography>=42
markdown-it-py[linkify]>=3.0
mdit-py-plugins>=0.4
Pygments>=2.17
filelock>=3.13
google-api-python-client>=2.100
google-auth-oauthlib>=1.2
google-auth-httplib2>=0.2
keyring>=25
pytest>=8
pyinstaller>=6
```

Frontend packages (run inside `frontend/`):
```
npm install codemirror @codemirror/lang-markdown @codemirror/autocomplete @codemirror/language @lezer/highlight dompurify @fontsource/inter @fontsource/space-grotesk @fontsource/jetbrains-mono
npm install --save-dev vite
```

---

## 3. Folder layout

### 3.1 Source code

```
vaultnotes/
├─ requirements.txt
├─ README.md
├─ run.py                        # "python run.py" starts the app ("--dev" for live CSS editing)
├─ build.bat                     # builds the frontend, then the .exe (M9)
├─ design/
│  └─ VaultNotes-Design.html     # the visual reference (not shipped)
├─ frontend/                     # THE LOOK
│  ├─ package.json
│  ├─ vite.config.js             # base: './', builds into ../src/vaultnotes/web
│  ├─ index.html                 # includes the strict CSP (section 5, rule 12)
│  ├─ src/
│  │  ├─ main.js                 # waits for pywebview, starts everything
│  │  ├─ bridge.js               # wraps window.pywebview.api + event bus (fake version in M1)
│  │  ├─ icons.js                # inline SVG icons (copy from the design file)
│  │  ├─ styles/
│  │  │  ├─ tokens.css           # colors, fonts, sizes, motion. 3 themes
│  │  │  ├─ base.css             # background, glass, app grid, typography
│  │  │  ├─ components.css       # buttons, lists, dialogs, palette, toasts, status bar
│  │  │  └─ preview.css          # rendered Markdown + code colors
│  │  └─ ui/
│  │     ├─ toolbar.js
│  │     ├─ sidebar.js
│  │     ├─ noteList.js
│  │     ├─ editor.js            # CodeMirror 6 + theme + [[ suggestions
│  │     ├─ preview.js           # inserts cleaned HTML, handles link clicks
│  │     ├─ backlinks.js
│  │     ├─ sealedVault.js       # the "vault is locked" screen
│  │     ├─ unlockDialog.js
│  │     ├─ commandPalette.js
│  │     ├─ settings.js
│  │     ├─ toasts.js
│  │     ├─ statusBar.js
│  │     └─ effects.js            # decrypt scramble, lock scramble, reduced motion
├─ src/vaultnotes/               # THE ENGINE
│  ├─ __init__.py
│  ├─ app.py                     # creates the pywebview window
│  ├─ api.py                     # the Bridge API (section 4.8)
│  ├─ events.py                  # Python → frontend events
│  ├─ config.py                  # load/save settings.json, defaults
│  ├─ models.py                  # Note dataclass, space types
│  ├─ links.py                   # [[link]] parsing, resolving, renaming, backlink index
│  ├─ render.py                  # Markdown → HTML for the preview
│  ├─ autolock.py                # idle timer that locks vaults
│  ├─ storage/
│  │  ├─ atomic.py               # atomic_write(path, data: bytes)
│  │  ├─ plain_store.py          # plain .md notes
│  │  └─ vault_store.py          # encrypted notes
│  ├─ crypto/
│  │  ├─ keyfile.py              # generate / load / save key files
│  │  └─ notecrypt.py            # encrypt / decrypt notes, vault verifier
│  ├─ backup/
│  │  ├─ gdrive_auth.py          # Google sign-in, token kept in keyring
│  │  └─ gdrive_backup.py        # upload changed files, restore
│  └─ web/                       # the built frontend (made by "npm run build", don't edit)
└─ tests/
   ├─ test_notecrypt.py
   ├─ test_keyfile.py
   ├─ test_plain_store.py
   ├─ test_vault_store.py
   ├─ test_links.py
   ├─ test_render.py
   ├─ test_api.py
   └─ test_backup_manifest.py
```

### 3.2 User data (default locations, all changeable in Settings)

```
%USERPROFILE%\Documents\VaultNotes\        ← notes root (this is what gets backed up)
├─ plain\
│  ├─ Shopping list.md
│  └─ .trash\
├─ ai-notes\                                (AI helpers write here; same format as plain\)
│  ├─ About AI-Notes.md
│  └─ .trash\
└─ vaults\
   ├─ encrypted\
   │  ├─ vault.json
   │  ├─ 3f2a9c...e1.vnote
   │  └─ .trash\
   └─ personal\
      ├─ vault.json
      ├─ 8b01d4...7a.vnote
      └─ .trash\

%APPDATA%\VaultNotes\                       ← app settings (NOT backed up)
├─ settings.json
├─ client_secret.json                       (Google OAuth client, added in M8)
├─ backup_manifest.json
└─ app.lock                                 (stops two copies of the app running at once)

Key files: wherever YOU choose, e.g. E:\keys\personal.vnkey on a USB stick.
The app refuses to save or use a key file located inside the notes root.
```

---

## 4. Data formats (the AI must follow these exactly)

### 4.1 Plain note

- A normal UTF-8 `.md` file.
- The note's title is the filename without `.md`.
- Characters that Windows doesn't allow in filenames (`\ / : * ? " < > |`) are replaced with `-`.
- If a filename is already taken, add ` (2)`, ` (3)` and so on.
- Plain notes have no hidden metadata, so they open fine in Obsidian, VS Code, Notepad and other editors.

### 4.2 Key file (`*.vnkey`), UTF-8 JSON

```json
{
  "format": "vaultnotes-key",
  "version": 1,
  "vault_id": "0b6f2c1e-9a4d-4c55-8f0e-2d7c1b9a3e44",
  "vault_name": "Personal",
  "protection": "none",
  "key_b64": "<base64 of 32 random bytes>",
  "created": "2026-09-25T10:00:00Z"
}
```

- The key is created with `secrets.token_bytes(32)`.
- M10 adds `"protection": "scrypt"` (see 4.5).

### 4.3 Vault header (`vault.json`, in the vault folder, gets backed up, contains no secret)

```json
{
  "format": "vaultnotes-vault",
  "version": 1,
  "vault_id": "0b6f2c1e-9a4d-4c55-8f0e-2d7c1b9a3e44",
  "name": "Personal",
  "verifier_b64": "<base64 of: nonce(12 bytes) + AESGCM(key).encrypt(nonce, b'vaultnotes-verify', b'verifier:' + vault_id.bytes)>"
}
```

To unlock a vault, the app runs two checks in this order:
1. The key file's `vault_id` must match the one in `vault.json`. If it doesn't, show *"This key belongs to a different vault."*
2. Decrypting the verifier must succeed. If it fails, show *"Wrong or damaged key."*

### 4.4 Encrypted note (`<note_id>.vnote`, binary)

- `note_id` is a random UUID4 written as 32 hex characters with no dashes. It is the filename, so titles never appear on disk or in Google Drive.

| Bytes | Content |
|---|---|
| 0–3 | magic `b"VNT1"` |
| 4 | format version `0x01` |
| 5–16 | nonce: 12 random bytes (`os.urandom(12)`), new on **every save** |
| 17–end | AES-256-GCM ciphertext + 16-byte tag (exactly what `AESGCM.encrypt` returns) |

- **AAD** (associated data) = `b"VNT1" + b"\x01" + vault_id.bytes + note_id.bytes`, where both IDs are raw 16-byte UUID values. If a file is renamed or copied into another vault, it fails to decrypt, so tampering is detected.
- The **plaintext** is UTF-8 JSON:

```json
{"title": "Bank stuff", "created": "2026-09-25T10:00:00Z", "modified": "2026-09-25T10:05:00Z", "tags": [], "body": "# Bank stuff\n..."}
```

### 4.5 Passphrase-protected key file (M10, optional)

```json
{
  "format": "vaultnotes-key", "version": 1,
  "vault_id": "...", "vault_name": "Personal",
  "protection": "scrypt",
  "kdf": {"salt_b64": "<16 random bytes>", "n": 131072, "r": 8, "p": 1},
  "nonce_b64": "<12 random bytes>",
  "wrapped_key_b64": "<AESGCM(KEK).encrypt(nonce, key, vault_id.bytes)>"
}
```

The key that unlocks the vault key (KEK) is derived as `Scrypt(salt=salt, length=32, n=2**17, r=8, p=1).derive(passphrase.encode("utf-8"))`.

### 4.6 `settings.json`

```json
{
  "notes_root": "C:/Users/you/Documents/VaultNotes",
  "vaults": [
    {"name": "Encrypted", "folder": "vaults/encrypted", "key_path": "E:/keys/encrypted.vnkey"},
    {"name": "Personal",  "folder": "vaults/personal", "key_path": "E:/keys/personal.vnkey"}
  ],
  "autolock_minutes": 10,
  "look": {"theme": "nebula", "effects": "full", "view_mode": "split", "editor_font_size": 13.5},
  "backup": {"enabled": false, "interval_minutes": 60, "drive_folder_id": null, "last_backup": null}
}
```

- `key_path` only remembers *where* the key file is. The key itself is never saved here. If `key_path` is empty, the app asks for the file each time.
- `theme` is `nebula`, `synthwave` or `arctic`. `effects` is `full`, `lite` or `off` (section 6.7).

### 4.7 Links between notes (Obsidian-style)

Links are plain text inside a note's Markdown body, so nothing new is stored on disk.

| You write | Meaning |
|---|---|
| `[[Travel 2026]]` | Link to the note titled "Travel 2026" |
| `[[Travel 2026\|my trip]]` | Same link, but the preview shows "my trip" |
| `[[Travel 2026#Hotels]]` | Link to a heading. In v1 this just opens the note; M10 jumps to the heading |
| `[[Travel 2026.md]]` | Same as `[[Travel 2026]]`. A trailing `.md` is ignored |
| `[text](Travel%202026.md)` | A normal Markdown link to a `.md` name with no `http:` etc. is also a note link |
| `[text](https://example.com)` | A web link. Opens in your browser |

Rules:
- **Only the same space.** A link finds notes in the space it's written in. Plain links find Plain notes. Encrypted links find Encrypted notes. Personal links find Personal notes. AI-Notes links find AI-Notes notes; like a vault note, an AI-Notes note may also write `[[Plain:Title]]` (M10), and nothing links into AI-Notes from outside.
- **Matching ignores upper/lower case** and extra spaces at the start or end of the title.
- **Titles must be unique within a space**, ignoring case. Plain notes already are, because titles are filenames. In vaults, creating or renaming a note to a title that already exists adds ` (2)`, ` (3)` and so on.
- **Inside a Markdown table**, write the `|` as `\|` (`[[Travel 2026\|my trip]]`), just like Obsidian.
- **Code is not linked.** `[[...]]` inside inline code (`` `...` ``) or a fenced code block stays plain text.
- **How the preview is made** (`render.py`, in Python):
  1. `links.render_links_for_preview(body, titles)` rewrites each note link into a normal Markdown link with an internal address. Escape `[` and `]` in the shown text.
     - Existing note: `[shown text](#vn-open/<URL-encoded title>)`
     - Missing note: `[shown text](#vn-new/<URL-encoded title>)`
  2. markdown-it-py turns the Markdown into HTML:
     - **Raw HTML is turned off** (`"html": False`).
     - Tables and task lists are on.
     - Code blocks are colored by Pygments.
  3. The frontend cleans the HTML with DOMPurify, then shows it.
  4. CSS styles the links:
     - Existing notes (`a[href^="#vn-open/"]`) look like glowing chips.
     - Missing notes (`a[href^="#vn-new/"]`) look like dashed, dimmer chips.
- **Clicks:** one click handler on the preview. It always calls `preventDefault()`, then:
  - `#vn-open/...` opens the note.
  - `#vn-new/...` asks "Create note 'X'?".
  - `http`, `https` and `mailto` links go to `api.open_external(url)`. Python checks the kind again, then opens your browser.
  - **Every other kind of link is ignored** (`file:`, `javascript:` and so on).

Because Plain note titles are their filenames, you can also open the `plain` folder in Obsidian and the links will work there too.

### 4.8 Bridge API (how the look talks to the engine)

The frontend calls Python with `await window.pywebview.api.<name>(...)`, after the `pywebviewready` event has fired.
- Every function returns plain JSON data.
- Errors come back as `{"error": "<code>", "message": "<text to show the user>"}`. They never crash the app.
- **Keep these names.** In M1, `bridge.js` fakes them with sample data, so M2 can switch to the real ones without changing the UI.
- **How they reach the page:** `app.py` registers exactly the `@bridge_method` functions with `window.expose(...)`. **Never pass the `Api` object as `js_api`.** pywebview would then expose, and let the page call, everything reachable from it (the window, the vault stores, the Drive token store), and walking the native window froze the app at start.
- **Calls run one at a time** (`CallLock`), because pywebview runs each call on its own thread. A call that waits on the user (a file dialog, the Google sign-in) releases the lock while it waits.
- **Paths never come from the page.** Parameters such as `dest_path` or `key_paths` exist for tests only; the exposed functions refuse them.

| Function | Returns |
|---|---|
| `get_state()` | spaces (id, name, kind `plain`/`vault`, locked, note count), look settings, last backup time |
| `list_notes(space_id, query="", sort="modified")` | `[{id, title, snippet, modified, link_count}]` |
| `open_note(space_id, note_id)` | `{id, title, body, modified, backlinks: [{id, title}]}` |
| `create_note(space_id, title)` | the new note |
| `save_note(space_id, note_id, body)` | `{modified}` |
| `rename_note(space_id, note_id, new_title, update_links)` | `{title, links_updated}` |
| `count_links_to(space_id, note_id)` | `{count}`, used by the rename prompt and the move warning |
| `delete_note`, `restore_note`, `list_trash` | trash handling |
| `move_note(space_id, note_id, target_space_id)` | `{new_id, broken_links}` |
| `render_preview(space_id, body)` | HTML string (section 4.7) |
| `list_titles(space_id)` | titles for the `[[` suggestions |
| `unlock_vault(space_id)` | Uses the remembered key path, or Python opens the file picker itself. Returns `{ok, count}`, or an error code: `key_not_found`, `wrong_vault`, `wrong_key` or `damaged` |
| `lock_vault(space_id)`, `lock_all()` | locks and clears memory |
| `touch()` | `{locks_at}`. The frontend calls this on keyboard/mouse activity (at most every 15 s) to reset auto-lock |
| `ready_to_close()` | `{ok}`. When the window is closing, `app.py` asks the page to save (`window.vn.flushBeforeClose()`); the page calls this when done, then the window closes |
| `open_external(url)` | opens http/https/mailto in the browser and ignores anything else |
| `get_settings()`, `update_settings(changes)` | settings |
| `backup_now()`, `connect_drive()`, `disconnect_drive()`, `restore_from_drive()` | backup (M8) |
| `choose_client_secret()` | `{ok, backup}`. Picks the downloaded OAuth client JSON in a native Open dialog, checks it is a Desktop app client, and copies it to `%APPDATA%\VaultNotes\client_secret.json` |

**Events from Python to the frontend** use `window.evaluate_js("window.vn.emit(name, data)")`. Always build that string with `json.dumps(...)`, never by pasting text in. The events are:
- `vault_locked`
- `backup_progress`
- `backup_done`
- `error`

---

## 5. Security rules (required; give these to the AI)

1. Use only `AESGCM` and `Scrypt` from the `cryptography` package. Never write your own crypto. Never use ECB or CBC mode. Never use the `random` module for keys or nonces; use `secrets` or `os.urandom`.
2. Use a **new random nonce for every encryption**. Never reuse one.
3. **Decrypted text exists only in memory.** Never write it to disk: no temp files, caches, logs or thumbnails.
4. **Writes are atomic.** Write to `<file>.tmp` in the same folder, call `flush()` and `os.fsync()`, then `os.replace()` it over the real file. For vault notes the temp file holds only ciphertext.
5. Never log note contents, vault note titles, keys, passphrases or Google tokens.
6. Key files are never inside the notes root and are **never uploaded**. The backup code must skip `*.vnkey` even if it finds one.
7. **Auto-lock:**
   - Lock after N minutes with no activity (default 10). The timer runs **in Python** (`autolock.py`), so vaults still lock even if the UI breaks.
   - Also lock on "Lock all" (Ctrl+L).
   - Locking means: save, then remove keys and decrypted notes from memory. This is best effort, because Python can't guarantee memory is wiped. Keep keys in a `bytearray` and overwrite it with zeros when locking.
8. If decryption fails (`InvalidTag`), show a clear error and **never overwrite that file**.
9. Only one copy of the app may run at a time (`filelock` on `%APPDATA%\VaultNotes\app.lock`), so two windows never overwrite each other's changes.
10. Request only the narrowest Google scope: `https://www.googleapis.com/auth/drive.file`. With it, the app can only see files it created itself.
11. **Links never cross spaces.** A Plain note can't open or list notes from a vault. The link and backlink index for a vault exists **only in memory** while the vault is unlocked. It is never written to disk and it's cleared on lock. The command palette and `[[` suggestions never show titles from locked vaults.
12. **Web UI safety:**
    - **a.** Markdown is rendered with raw HTML turned off, and the result is cleaned with DOMPurify before it's shown. Never put unchecked text into `innerHTML`. Use `textContent` for titles, snippets and messages.
    - **b.** `index.html` has a strict Content Security Policy:
      `default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'`
      - This blocks outside scripts. It also blocks images from the internet, so a note can't "phone home" with a tracking image.
      - If the pywebview bridge stops working because of this policy, loosen only the part that's needed and write down why.
      - `--dev` mode may use a looser policy for the Vite dev server. Release builds use the strict one.
    - **c.** Everything the look needs (fonts, scripts, styles, icons) is bundled locally. No CDNs, and no Google Fonts at runtime.
    - **d.** Start pywebview with `private_mode=True`. Never store note text in `localStorage`, `sessionStorage` or IndexedDB. Look settings go to `settings.json` through the Bridge API.
    - **e.** Notes travel only through the Bridge API, in memory. pywebview's built-in local web server serves only the static files in `web/`, never notes.
    - **f.** The Bridge API checks every input:
      - Space IDs must be ones that exist.
      - Vault note IDs must match `^[0-9a-f]{32}$`.
      - Titles are cleaned.
      - **The frontend never sends file paths.** Python opens file pickers itself.
    - **g.** Release builds use `debug=False`, so developer tools are off.

### Limits you should know about

- **If you lose a key file, the notes in that vault are gone forever.** Nobody can recover them. Keep 2 copies of each key file in separate safe places, for example a USB stick plus a password manager attachment. Never store key files in the same Google Drive as the backup.
- Encryption protects the files on disk and in Google Drive. It does **not** protect you from malware on your computer while a vault is unlocked.
- Google can still see how many encrypted files you have, plus their sizes and dates. It can't see titles or contents.
- When you move a plain note into a vault, the plain file is deleted, but SSDs can keep old data around. For anything sensitive, write it in a vault from the start.
- If you type `[[My secret note]]` in a **Plain** note, that text is saved unencrypted. It won't be a working link, but anyone can read the words.

---

## 6. Look & feel (the design)

### 6.1 Direction

**Calm sci-fi.** Think of the control panel of a spaceship in a good movie, not a flashing gaming keyboard.
- **Deep, dark background** with a soft aurora of color that drifts very slowly, plus a faint grid.
- **Frosted glass panels** (blurred, see-through, with a thin light border) floating over it.
- **One neon accent per space:** Plain is cyan, Encrypted is violet, Personal is pink, AI-Notes is lime. When you switch spaces, the whole app smoothly re-tints to that color.
- **Glow only on what matters:** the selected item, focused fields, links, unlocked vaults.
- **Motion that explains something:** notes "decrypt" when a vault unlocks and scramble away when it locks.

**`VaultNotes-Design.html` is the reference.** Open it in Chrome or Edge and click around. It has:
- All three themes.
- The unlock and lock animations.
- The command palette (Ctrl+K).
- Links and backlinks, plus the auto-lock countdown.

**Copy its CSS and HTML structure. Don't copy its JavaScript.** Its CSS is already split into sections named after the files in 3.1 (`tokens.css`, `base.css`, `components.css`, `preview.css`).

### 6.2 Color tokens

- All colors are CSS variables in `tokens.css`. Components use **only** these variables, never raw color codes.
- `--accent` is set from JavaScript to the current space's color. It is registered with `@property` so it can animate between colors.

| Token | Nebula (default, dark) | Synthwave (dark) | Arctic (light) |
|---|---|---|---|
| `--bg` | `#06070d` | `#0a0317` | `#eef1f7` |
| `--text` | `#e8eaf6` | `#fbeaff` | `#0f172a` |
| `--muted` | `#9097b8` | `#b59ccc` | `#51607a` |
| `--faint` (decoration only) | `#5d6385` | `#6e5a85` | `#75839b` |
| `--plain` | `#22d3ee` | `#05d9e8` | `#0891b2` |
| `--encrypted` | `#a78bfa` | `#ff2a6d` | `#7c3aed` |
| `--personal` | `#f472b6` | `#ff9f1c` | `#db2777` |
| `--ai` | `#a3e635` | `#b8f53c` | `#4d7c0f` |
| `--glass` | `rgba(255,255,255,.035)` | `rgba(255,255,255,.04)` | `rgba(255,255,255,.55)` |
| `--glass-border` | `rgba(255,255,255,.085)` | `rgba(255,120,200,.14)` | `rgba(15,23,42,.08)` |
| `--surface` (dialogs) | `rgba(14,16,28,.84)` | `rgba(24,8,44,.86)` | `rgba(255,255,255,.9)` |
| `--success` / `--warning` / `--danger` | `#34d399` / `#fbbf24` / `#fb7185` | `#5dfdcb` / `#ffd166` / `#ff5c8a` | `#059669` / `#d97706` / `#e11d48` |

The full list, including the aurora glow colors, is at the top of the design file. Tinted shades come from `color-mix()`, for example `color-mix(in srgb, var(--accent) 14%, transparent)` for a lightly tinted background.

### 6.3 Type, shape and motion

- **Fonts:** Space Grotesk for titles and labels, Inter for reading text, JetBrains Mono for the editor, code and the status bar.
- **Sizes:**
  - Note title 26px.
  - Preview text 15px with line height 1.7.
  - Editor 13.5px with line height 1.75.
  - UI text 13–14px.
  - Small labels 10–11px, uppercase, with wide letter spacing.
- **Corners:** panels 18px, cards 13px, buttons 10px, chips 7px.
- **Glass recipe:** `background: var(--glass); border: 1px solid var(--glass-border); backdrop-filter: blur(20px) saturate(140%);` plus a thin inner highlight and a soft shadow.
- **Glow recipe:** a 1px accent-tinted ring plus a soft accent shadow below. Copy it from `.space.active` in the design file.
- **Motion:**
  - Timing: 120–200 ms for hovers, 250–400 ms for most changes, 600–800 ms for the special moments.
  - Easing: `cubic-bezier(.2,.8,.2,1)`.
  - Animate only `transform`, `opacity`, `filter` and the accent color, so everything stays smooth.

### 6.4 Layout

```
┌──────────────────────────────────────────────────────────────────────┐
│ Toolbar: logo · command palette (Ctrl K) · Edit/Split/Preview        │
│          Lock all · Back up · theme switcher                         │
├─────────────┬──────────────────┬─────────────────────────────────────┤
│ SPACES      │ NOTES            │ Encrypted / Project ideas    ← → ⋯  │
│             │ [search…]        │ Project ideas  (big title)          │
│ ◉ Plain     │                  ├──────────────────┬──────────────────┤
│ ◉ Encrypted │ ▌Project ideas   │ MARKDOWN         │ PREVIEW          │
│ ◉ Personal  │  API keys        │ # Project ideas  │ Project ideas    │
│             │  Unlock anim…    │ See [[API keys]] │ See ‹API keys›   │
│ + New vault │                  │                  │                  │
│ ☁ Drive     │                  ├──────────────────┴──────────────────┤
│             │                  │ LINKED FROM: Unlock animation       │
├─────────────┴──────────────────┴─────────────────────────────────────┤
│ ● Saved · Encrypted unlocked, locks in 9:32 · Backed up 10:02        │
└──────────────────────────────────────────────────────────────────────┘
```

When the window is narrower than about 1240px, the sidebar shrinks to icons only.

### 6.5 Components

| Component | What it looks like |
|---|---|
| Toolbar | Glass bar. Hexagon logo with a keyhole, "Vault**Notes**" with a gradient on "Notes". A command-palette button in the middle. On the right: view switcher, Lock all, Back up, and three round theme dots |
| View switcher | Edit / Split / Preview, with a glowing "thumb" that slides between them |
| Space item | A tinted icon tile, the name, a small status line ("Always open", "Unlocked", "Locked"), and a count or key badge. The active one gets a glowing bar on its left edge |
| Note card | Title, a 2-line snippet, then the date and link count. The selected card gets an accent border and a glowing left edge. Cards rise in one after another |
| Search field | Glass input. It gets a soft accent ring when focused |
| Editor (CodeMirror theme) | Heading marks in accent with a faint glow on the heading text. `[[links]]` shown as tinted chips. List markers in accent, task boxes in amber, code in green |
| Preview | H1 with a gradient from the text color into the accent. H2 with a small glowing dash. Round glowing bullets. Task boxes that fill with the accent. Tables with an uppercase header row. Code blocks with a small language label. Quotes with an accent edge |
| Linked from | A bar under the editor with clickable chips |
| Sealed vault screen | Rotating dashed rings around a glowing lock, a "VAULT SEALED" tag, a line explaining the vault is encrypted, an "Unlock with key file" button, and an info line (cipher, key file name, auto-lock time). The note list behind it shows shimmering encrypted "noise" instead of titles |
| Unlock dialog | A progress ring around a key icon that fills, then turns into an open lock. Status text changes from "Checking the key…" to "Decrypting N notes into memory…" |
| Command palette | Ctrl+K. A big search box listing notes and commands, moved with the arrow keys. Notes in locked vaults never appear |
| Toasts | Small glass messages in the bottom right. They can have one button, such as "Undo" or "Create note" |
| Status bar | Save dot (amber pulse while saving, green flash when saved), auto-lock countdown ring, backup progress bar, and buttons for Effects and Theme |

### 6.6 Signature moments

1. **Decrypt reveal (unlock).** Each note title starts as random glyphs (`▓▒░<>/{}#01AF…`) and resolves left to right: about 650 ms per title, starting 70 ms after the one above. At the same time, the editor and preview fade in from blurred to sharp.
2. **Seal (lock).** Titles and snippets scramble back into glyphs from right to left (about 380 ms). The workspace blurs out, then the sealed vault screen appears.
3. **Re-tint.** Switching spaces smoothly changes `--accent` over 0.6 s, so every glow, chip and heading changes color together.
4. **Unlock ring.** The ring in the unlock dialog fills over 1 s, then the key icon swaps to an open lock with a glow.
5. **Save pulse.** The status dot pulses amber while saving, then flashes green.
6. **Living background.**
   - Nebula and Arctic: aurora blobs drift over 40–70 s loops.
   - Synthwave: a neon perspective grid floor moves slowly toward you.

### 6.7 Effects, comfort and accessibility

- **Settings → Effects:**
  - **Full** (default).
  - **Lite:** no blur and a still background, for older PCs.
  - **Off:** no animations at all.
  - If Windows has animations turned off (`prefers-reduced-motion`), skip the animations even on Full.
- **Contrast:** text must have at least 4.5:1 contrast against its background. `--faint` is only for decoration and small labels, never for important text.
- **Keyboard:** everything works from the keyboard, with a visible accent-colored focus ring.
- **Not just color:** locked vaults also show a lock icon and the word "Locked".

### 6.8 Design rules for the AI

- Use only the tokens from `tokens.css`. No raw colors in components.
- Match the design file's spacing, fonts, corners and glow. When in doubt, open the design file.
- **Keep it smooth:**
  - Use blur only on panels that don't move or scroll.
  - Animate only transform, opacity and filter.
  - Test with 500 notes.
- Bundle all fonts and icons locally. Icons are inline SVGs (copy them from the design file) or the free `lucide` icon package.
- Put titles, snippets and messages into the page with `textContent`, never `innerHTML` (security rule 12a).

---

## 7. Features & shortcuts

**Notes (all spaces)**
- Create, rename, and delete notes. Deleting moves the note to that space's `.trash/`, where vault notes stay encrypted. A toast offers "Undo", and notes can also be restored from the trash view.
- Markdown editor:
  - CodeMirror 6 with the VaultNotes theme.
  - Edit, Split and Preview modes.
  - The preview updates about 300 ms after you stop typing.
- **Auto-save** 1 second after you stop typing, and also when you switch notes, close the app or lock. The status bar shows "Saving…" then "Saved".
- Search the current space by title and text. Vault searches run only on the decrypted notes in memory.
- Sort by date modified (newest first) or by title.
- Move a note between spaces. The app encrypts or decrypts it as needed and asks for confirmation first.
- Import `.md` files into any space.
- Export a note to `.md`. When exporting from a vault, warn first: *"This saves an unencrypted copy."*

**Vaults**
- **First-run wizard** (styled like the unlock dialog):
  1. Choose the notes folder.
  2. The app creates the **Encrypted** and **Personal** vaults.
  3. The app generates their two key files.
  4. You choose where to save the key files. The app suggests a USB drive and never allows the notes folder.
  5. The app shows a large warning: "Back up your key files now."
- **Unlock:** click a locked vault to see the sealed vault screen, then "Unlock with key file". This opens the unlock dialog with the remembered key path, a [Browse…] button and an [Unlock] button. Each error gets its own clear message: file not found, key for a different vault, wrong key, damaged file.
- **Lock** one vault, or **Lock all** (Ctrl+L). Plus the auto-lock timer with its countdown ring in the status bar.
- **Open existing vault…** for example after restoring from Drive onto a new PC.
- **New vault…** (advanced) if you ever want more than two.

**Links (M6, Obsidian-style; syntax in section 4.7)**
- **Clickable links:** clicking a `[[link]]` in the preview opens that note. Clicking a link to a note that doesn't exist yet asks "Create note 'X'?", as Obsidian does.
- **Suggestions while typing:** typing `[[` opens a list of note titles in the current space, filtered as you type. Pressing Enter inserts `Title]]`.
- **"Linked from" bar** under the editor: lists every note in the same space that links to the open note. Click one to open it.
- **Back / Forward** buttons (Alt+← / Alt+→) to return along the links you followed, like a web browser.
- **Renaming updates links:** when you rename a note, the app asks "Update N links in other notes?" If you say yes, it rewrites `[[Old]]`, `[[Old|text]]` and `[[Old#heading]]` to the new title and keeps the display text and heading.
- **Moving a note warns you:** before moving a note to another space, the app tells you how many links will break, and asks you to confirm.

**Look (M1, M5)**
- Three themes: Nebula, Synthwave and Arctic.
- Three effects levels: Full, Lite and Off.
- **Command palette** (Ctrl+K): open any note in an unlocked space, or run any command.
- A settings screen for theme, effects, editor font size, auto-lock minutes and notes folder.

**Backup (M8)**
- In Settings: "Connect Google Drive" (sign in through the browser) and "Disconnect".
- A "Back up now" button in the toolbar and on the Drive card in the sidebar.
- Optional auto-backup every N minutes while the app is open, and when it closes.
- "Restore from Drive…" downloads the backup into an empty folder you pick.

**Keyboard shortcuts**

| Shortcut | Action |
|---|---|
| Ctrl+K | Command palette |
| Ctrl+N | New note |
| Ctrl+S | Save now |
| Ctrl+F | Search this space |
| Ctrl+E | Switch view mode |
| Ctrl+L | Lock all |
| Ctrl+B | Back up now |
| Alt+← / Alt+→ | Back / Forward through followed links |
| Esc | Close dialog or palette |

---

## 8. Google Drive backup design

### 8.1 One-time setup (you do this; it's free and takes about 10 minutes)

1. Go to https://console.cloud.google.com and create a project called "VaultNotes".
2. Go to **APIs & Services → Library** and enable the **Google Drive API**.
3. Set up the **OAuth consent screen** (it may be listed under "Google Auth Platform"):
   - User type: **External**. App name: VaultNotes. Enter your email.
   - Scopes: add `.../auth/drive.file`.
   - Test users: add your Gmail address.
4. Go to **Credentials → Create credentials → OAuth client ID**. Choose application type **Desktop app**. Download the JSON file (Google shows the secret only once, so download it before you close the popup). In VaultNotes, open **Settings → Choose client_secret.json…** and pick that file: the app checks it and copies it to `%APPDATA%\VaultNotes\client_secret.json`. Copying it there by hand works too, except from a packaged Windows app, whose writes to `%APPDATA%` land in its own private copy of the folder.
5. **Keep a copy of `client_secret.json`** somewhere safe, just as you do with your key files. On a new PC, use the same file. With the `drive.file` scope, the app can only see files created through the same Google Cloud project.
6. While the app's status is "Testing", Google makes you sign in again every 7 days. To stop this, set **Publishing status → In production**. Because the app only asks for `drive.file`, Google normally doesn't require a review. If you see a warning screen when you sign in, it's safe to continue because it's your own app. With a Google Workspace account, **Audience → Make internal** does the same without publishing: only accounts in your organization can sign in, and nothing expires after 7 days.

Notes:

- Work or school Google accounts often block custom apps. A personal Gmail account is easiest.
- Google renames menus in Cloud Console now and then. If a step doesn't match what you see, ask the AI where that option is now.

### 8.2 How backup works

- **Sign-in:** call `InstalledAppFlow.from_client_secrets_file(path, SCOPES).run_local_server(port=0)`. Save only the **refresh token** in `keyring` (service `"VaultNotes"`, user `"gdrive"`). On later runs, rebuild the credentials from the refresh token and `client_secret.json`. If Google rejects the token (`invalid_grant`), ask the user to connect again.
- On the first backup, create a Drive folder called **`VaultNotes Backup`** (`mimeType: application/vnd.google-apps.folder`) and save its ID in settings. Inside it, recreate the same subfolders as on disk (`plain/`, `vaults/encrypted/`, `vaults/personal/`, and their `.trash/` folders).
- The local `backup_manifest.json` records what has been uploaded:
  `{ "plain/Shopping list.md": {"drive_id": "...", "sha256": "...", "uploaded": "2026-09-25T10:02:00Z"} }`
- **Each backup run:**
  1. Go through every file in the notes root. **Include** only `*.md`, `*.vnote` and `vault.json`. **Skip** `*.vnkey`, `*.tmp` and everything else.
  2. For each included file, compute its SHA-256 and compare with the manifest:
     - Not in the manifest: upload it with `files().create(...)` and `MediaFileUpload`.
     - Hash changed: replace the Drive copy with `files().update(fileId=..., media_body=...)`.
     - Hash unchanged: skip it.
  3. If the Drive file is gone (HTTP 404), create it again.
  4. If the manifest is missing, look up files by name and parent folder in Drive before creating any, so you don't get duplicates.
  5. In v1, **files deleted on your computer are not deleted from Drive.** Backup only adds and updates, which is safer. Drive also keeps older versions of updated files for a while.

- Backups run in a background thread (`threading.Thread`), one at a time.
  - Progress goes to the status bar through `backup_progress` events ("Backing up 3/12…" with a gradient progress bar).
  - The UI never freezes.
  - Errors such as no internet or an expired login show a toast and a status-bar message, and the app tries again at the next interval. Backup errors must never crash the app.

- **Restore:**
  1. List the Drive folder, including all subfolders.
  2. Download everything into an empty folder you choose.
  3. Point `notes_root` at that folder.
  4. Vaults still need their key files to open.

### 8.3 The no-code alternative (works today)

Install **Google Drive for desktop** and put the notes folder inside your synced Drive folder. Then everything is backed up automatically, with no API work. You can use this until M8 is done, or instead of M8. **Your key files still go somewhere else!**

---

## 9. Milestones

Build each milestone in its own AI chat. Don't start the next one until the current one's "Done when" list passes.

### M1: The look (app shell with fake data)

- **Python:**
  - `run.py` and `app.py` open a pywebview window: 1280×800, minimum 1000×640, background color `#06070d` so it doesn't flash white on start.
  - Normally the window loads `src/vaultnotes/web/index.html`.
  - With `--dev`, it loads the Vite dev server at `http://localhost:5173` with `debug=True`.
- **Frontend:**
  - Create a Vite project in `frontend/`. In `vite.config.js`, set `base: './'`, `build.outDir: '../src/vaultnotes/web'` and `emptyOutDir: true`.
  - **Port `VaultNotes-Design.html`:** tokens and the three themes, the background, glass panels, every layout area and component, and the signature animations from section 6.6. Split the CSS into the files in section 3.1.
  - Replace the design file's textarea trick with **CodeMirror 6** using a VaultNotes theme with the same colors.
  - `bridge.js` provides a **fake API** with the same function names as section 4.8 and sample data. No real notes yet.
  - Fonts come from `@fontsource` (bundled, not Google Fonts).
- **Done when:**
  - Running `npm run build` and then `python run.py` opens a window that looks like the design file.
  - Switching spaces re-tints the app.
  - The 3 themes and 3 effects levels work.
  - Ctrl+K opens the palette.
  - The fake unlock shows the decrypt animation.
  - With `npm run dev` and `python run.py --dev` running, CSS changes show up live without restarting.
  - With Wi-Fi turned off, the fonts still look right.

### M2: Plain notes (real data)

- `config.py` (settings with defaults, creates the folders it needs), `atomic.py` and `plain_store.py`.
- `render.py`: markdown-it-py with raw HTML off, tables, task lists and Pygments code colors.
- `api.py`: the plain-note part of section 4.8.
- Switch `bridge.js` from the fake API to the real one.
- Auto-save, rename, delete-to-trash with Undo, and search.
- The preview uses `render_preview`, then DOMPurify.
- Add the CSP to `index.html` and the single-instance lock.
- Tests:
  - `test_plain_store.py`: create, read, update, rename, delete, restore, filename collisions.
  - `test_render.py`: tables, task lists, code colors, and raw HTML such as `<script>` shown as text.
- **Done when:**
  - A note with a table and a task list shows correctly in the preview.
  - After closing and reopening the app, the note is still there.
  - The `.md` file opens in Notepad.
  - Typing `<img src=x onerror=alert(1)>` in a note shows it as plain text, with no popup.
  - `pytest` passes.

### M3: Crypto core (no UI)

- `keyfile.py`:
  - `generate_key_file(path, vault_id, vault_name)`
  - `load_key_file(path) -> VaultKey`, which checks the file and gives clear errors.
- `notecrypt.py`:
  - `encrypt_note(key, vault_id, note_id, note: dict) -> bytes`
  - `decrypt_note(key, vault_id, note_id, data: bytes) -> dict`
  - `make_verifier(key, vault_id)` and `check_verifier(key, vault_id, verifier)`
  - All of these use exactly the formats in section 4.
- Tests:
  - An encrypt → decrypt round trip gives back the same note.
  - The wrong key fails.
  - Changing one byte of the file makes it fail.
  - A different `note_id` (a renamed file) fails.
  - Encrypting the same note twice gives different bytes (the nonce is unique).
  - Bad magic bytes or version give a clear error.
  - A key file can be saved and loaded back.
- **Done when:** all tests pass. The UI doesn't change in this milestone.

### M4: Encrypted vaults in the app

- `vault_store.py`:
  - Create a vault (its folder plus `vault.json`).
  - `unlock(key)`: verify the key, then decrypt all notes into memory.
  - List, search, create, save, rename and delete-to-trash notes.
  - `lock()`.
- `autolock.py`: the Python-side idle timer, reset by `touch()`. It sends the `vault_locked` event.
- The vault parts of the Bridge API.
- UI:
  - The sealed vault screen with encrypted "noise" in the note list.
  - The real unlock dialog, and the decrypt and seal animations (section 6.6).
  - The auto-lock countdown ring in the status bar.
  - The first-run wizard (section 7).
  - Refuse any key path inside the notes root.
- **Done when:**
  - You can create both vaults and generate the two key files outside the notes folder.
  - You can write notes in each vault, lock them, and unlock them again.
  - `.vnote` files look like gibberish in Notepad.
  - The Personal key **cannot** unlock Encrypted, and the app shows a clear error.
  - Auto-lock works. Test it with a 1-minute setting.
  - The unlock and lock animations match the design file.
  - `pytest` passes.

### M5: Quality of life

- Move notes between spaces.
- Import and export `.md` files.
- Trash view with restore.
- Sort options.
- The command palette with real data. It never lists notes from locked vaults.
- The settings screen (section 7 "Look"), saved to `settings.json`.
- Toasts with Undo, all keyboard shortcuts, and the narrow-window layout.
- **Done when:** everything under "Notes", "Vaults" and "Look" in section 7 works.

### M6: Links between notes (Obsidian-style; syntax in section 4.7)

- `links.py` (pure logic, **no pywebview**):
  - `parse_links(body) -> list[Link]`. A `Link` has target, display text, heading, and start/end positions. Follow every rule in section 4.7, including skipping code and handling `\|` in tables.
  - `resolve(target, titles) -> note_id | None`. Ignores case, extra spaces at the start or end, and a trailing `.md`.
  - `rename_links(body, old_title, new_title) -> (new_body, count)`. Keeps the display text and heading.
  - `render_links_for_preview(body, titles) -> str`. Rewrites links to the `#vn-open/` and `#vn-new/` addresses from section 4.7.
  - A `LinkIndex` class with `build(notes)`, `update(note)`, `remove(note_id)`, `backlinks(note_id)` and `outgoing(note_id)`.
- Wire it up:
  - Build the Plain index at startup. Build a vault's index on unlock and clear it on lock. Update the index on every save.
  - Enforce unique titles in vaults.
- UI:
  - Link chips in the preview, clicks, and the "Create note?" prompt for missing notes.
  - The `[[` suggestions, using CodeMirror's autocomplete with titles from `list_titles`.
  - `[[links]]` shown as chips in the editor too (CodeMirror `MatchDecorator`).
  - The "Linked from" bar.
  - Back/Forward history.
  - The rename prompt ("Update N links?") and the warning when moving a note breaks links.
- Tests (`test_links.py`):
  - `[[A]]`, `[[A|text]]`, `[[A#h]]`, `[[A.md]]`, `[[A\|text]]` in a table, and the Markdown `.md` link form.
  - Upper/lower case mixed.
  - Links inside `` `code` `` and fenced code blocks are ignored.
  - Links to missing notes.
  - Renaming rewrites every form and keeps the display text.
  - Backlinks are correct after adding, editing and deleting notes.
  - A Plain link never resolves to a vault note.
- **Done when:**
  - Typing `[[Tra` suggests "Travel 2026", and Enter completes it.
  - Clicking the link in the preview opens the note, and Alt+← comes back.
  - Clicking `[[Not written yet]]` offers to create that note.
  - Renaming "Travel 2026" to "Trip 2026" updates the links in other notes.
  - "Linked from" shows the right notes.
  - `[[x]]` inside a code block is not a link.
  - In a Plain note, `[[<title of a vault note>]]` shows as a missing link.
  - After locking a vault, none of its titles appear in suggestions, the palette or "Linked from".
  - `pytest` passes.

### M7: Hardening

- Handle these situations without crashing:
  - Missing or damaged files: skip them and show a warning.
  - A full disk or permission error when saving: keep the text in the editor and show an error.
  - The USB key removed after unlocking: keep working until the vault locks.
  - A 1 MB note.
  - 500+ notes in one vault.
- After saving an encrypted note, decrypt the written file once to confirm it's good before showing "Saved".
- **Performance:** with Effects set to Full and 500 notes, scrolling and typing stay smooth. With Effects set to Lite, the app runs well on an older PC.
- `test_api.py`: bad inputs to the Bridge API (unknown space, bad note ID, path-like titles) return errors and never crash.
- Review the code against section 5 and add any missing tests.
- **Done when:** you can't make the app lose a note by killing it while typing, pulling out the USB key, or giving it a broken file.

### M8: Google Drive backup

- Everything in section 8: the sign-in module, the backup module, the manifest, the background thread and the events.
- Settings UI: Connect/Disconnect, auto-backup interval, "Back up now", "Restore from Drive…".
- The Drive card in the sidebar and the progress bar in the status bar.
- Tests (`test_backup_manifest.py`): test the upload/skip decisions with a **fake** Drive client, with no real network calls. Include a test showing that `.vnkey` files are never picked for upload.
- **Done when:**
  - "Back up now" creates `VaultNotes Backup` in your Drive with the same folder structure.
  - Editing one note uploads only that note.
  - Encrypted notes in Drive can't be read.
  - Restoring into a new folder and then loading the key files shows all your notes.

### M9: Package it

- `build.bat` does two things:
  1. Runs `npm ci` and `npm run build` inside `frontend/`.
  2. Runs PyInstaller with `--windowed --onedir --add-data "src/vaultnotes/web;vaultnotes/web"` and an app icon (the hexagon logo).
- A `README.md` covering: install, first run, backing up key files, Google setup, and restoring on a new PC.
- **WebView2:** it's built into Windows 11. On Windows 10, if the window is blank, install the free "Microsoft Edge WebView2 Runtime" from Microsoft.
- **Done when:** the `.exe` runs on a Windows PC that has neither Python nor Node.js installed.

### M10: Optional extras (pick any)

- Passphrase-protected key files (section 4.5), so someone who steals the USB stick still can't open your vaults.
- More link features:
  - `[[Note#Heading]]` scrolls to that heading.
  - Ctrl+click a `[[link]]` in the editor to open it.
  - `![[Note]]` shows another note's content inside the preview (embeds).
  - **A graph view:** notes as glowing dots in the space's accent color, joined by their links, drawn on a canvas (for example with the free `force-graph` library). This fits the futuristic look perfectly.
- Links from a vault note to a Plain note, e.g. `[[Plain:Title]]`. Never the other way around.
- A frameless window with the toolbar as the title bar (pywebview `frameless=True` plus the `pywebview-drag-region` CSS class).
- More themes. Tags with filtering, pinned notes, word count.
- An encrypted index file, so vaults with thousands of notes unlock quickly.
- Prune: delete from Drive the files you deleted on your computer more than 30 days ago.

---

## 10. Final manual test checklist

- [ ] The app looks like `VaultNotes-Design.html` in all three themes. Effects set to Lite runs smoothly on an older PC.
- [ ] A plain note survives a restart and opens in another editor.
- [ ] `.vnote` files show no readable text in Notepad, and their filenames reveal no titles.
- [ ] A wrong key file gives a clear error, and no file gets overwritten.
- [ ] The Encrypted key can't open Personal, and the Personal key can't open Encrypted.
- [ ] Auto-lock hides vault notes and asks for the key again.
- [ ] `[[links]]` open the right note, suggestions and "Linked from" work, and renaming a note updates the links to it.
- [ ] A Plain note can't open or list anything from a locked or unlocked vault.
- [ ] Typing HTML or `<script>` in a note shows it as text and never runs it.
- [ ] Killing the app in Task Manager while typing loses at most about 1 second of text, and no file is damaged.
- [ ] Your Google Drive contains no `.vnkey` file.
- [ ] No decrypted text is on disk anywhere. To check: write a made-up word in a vault note, lock the vault, then search the notes folder and `%TEMP%` for that word. It must not be found.
- [ ] Restoring from Drive into a fresh folder works once you load the key files.

---

## 11. Coding rules for the AI

- **Python:** 3.12 with type hints, small functions, and docstrings on public functions.
- **Frontend:** plain JavaScript modules, with **no framework** (no React or Vue), so there are fewer moving parts.
- **Keep the engine separate from the look.** Engine modules (`storage/`, `crypto/`, `backup/`, `links.py`, `render.py`) must **not** import pywebview, so they can be unit-tested on their own.
- Always output **complete files** with their paths. Never write "... rest unchanged" unless I ask for a diff.
- **Don't change the file formats in section 4 or the Bridge API names in section 4.8.** If a change is truly needed, bump the version number and explain why.
- Don't add dependencies without explaining why.
- At the end of each milestone, list the files you changed, how to run the app, and how to run the tests.

---

## 12. Before you start (setup for you)

1. Install **Python 3.12** from https://www.python.org. During install, check "Add python.exe to PATH".
2. Install **Node.js LTS** from https://nodejs.org (free). You only need it to build the look; the finished app doesn't need it.
3. Install **VS Code** (free) and its Python extension.
4. Open a terminal in the folder where you want the project and run:
   ```
   mkdir vaultnotes
   cd vaultnotes
   python -m venv .venv
   .venv\Scripts\activate
   ```
   Once M1 gives you `requirements.txt`, run `pip install -r requirements.txt`. Then, inside `frontend/`, run `npm install`.
5. Recommended: install **Git** and save a snapshot after each milestone:
   `git init` (once), then `git add .` and `git commit -m "M1 done"`.
   Add these to a `.gitignore` file: `.venv/`, `node_modules/`, `src/vaultnotes/web/`, `build/`, `dist/` and `*.vnkey`.

---

## 13. Free AI options

Free plans change often, but any of these can work:
- **Chat assistants** (Claude.ai free plan, ChatGPT free, Google Gemini): paste this plan, and copy the code they give you into files.
- **Assistants inside VS Code** (e.g. GitHub Copilot's free tier): these can create and edit the files in your project directly.

Whichever you use: one milestone per chat, and paste errors in full. For design work, paste screenshots as well.

---

## 14. Starter prompt (copy and paste)

**First chat (M1):**

```
You are a senior developer helping me build "VaultNotes", a beautiful, futuristic
Windows desktop Markdown notes app. The engine is Python; the look is HTML/CSS/JS
inside a pywebview window. The full specification is the PLAN below. The visual
design is in the attached file VaultNotes-Design.html. Read both completely before
writing any code.

Rules:
- We build ONE milestone at a time. Today: Milestone M1 only (the look, with fake data).
- Follow the folder layout (section 3), Bridge API names (section 4.8), security
  rules (section 5) and design (section 6) exactly.
- Match the design file's look closely. Copy its CSS and structure, not its demo JavaScript.
- Output every file completely, with its path as a heading. Never write "..." or
  "rest unchanged".
- I am not an expert. After the code, give me exact step-by-step commands to run
  the app, and tell me what I should see.
- If something in the plan is unclear or seems wrong, ask me before guessing.

PLAN:
[paste this whole document here]
```

**Later chats:**

```
We are building VaultNotes using the PLAN below. Milestones M1 to M{N} are done and
working. Here are my current files for the parts this milestone touches (for UI work
I'm also including frontend/src/styles/tokens.css):
[paste files]

Now do Milestone M{N+1} only. Same rules: complete files with paths, exact commands to
run and test, keep the design consistent, ask if unclear.

PLAN:
[paste this whole document here]
```
