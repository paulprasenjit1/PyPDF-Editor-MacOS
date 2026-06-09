# PyPDF Editor — macOS App

Turns your `pdf_editor_final.py` editor into a real macOS app that lives in
**Applications**, opens PDFs on **double-click**, and shows up under
**right-click → Open With**.

## Files

| File | Purpose |
|------|---------|
| `pdf_editor_app.py` | The editor (same as `pdf_editor_final.py`) plus the ability to auto-open a PDF passed when launched. Do not rename. |
| `appicon.png` | App icon source (converted to `.icns` during build). |
| `build_app.command` | One-click installer. Run it once on your Mac. |

Keep these three files together in the same folder.

## Install (one time)

1. Double-click **`build_app.command`**.
   - If macOS blocks it: right-click → **Open** → **Open**, or run in Terminal:
     `bash "build_app.command"`
2. It builds **PyPDF Editor.app**, drops it in `/Applications`, sets it as the
   **default app for all PDFs**, and refreshes Finder.
3. Done. Double-click any PDF — it opens in the editor (in your browser, served
   locally). Right-click → Open With also lists **PyPDF Editor**.

> If a PDF still opens in Preview right after install, log out and back in once
> (or restart) so macOS commits the default-handler change.

## How it works

The app is a small AppleScript launcher bundle. On open it receives the PDF
path from macOS and runs `pdf_editor_app.py <file>`, which loads that PDF and
opens the editor UI in your browser. Launching the app with no file just opens
an empty editor.

### Single instance (one tab)

Opening several PDFs no longer spawns a new server and tab each time. A launch
first checks whether the editor is already running; if so it hands the new PDF
to that instance, and the existing browser tab switches to it automatically
(within ~1.5s). Only the first open starts a server / opens a tab.

### Faster opening

- Pages render lazily — only pages near the viewport are fetched on open, so the
  first page appears almost immediately even for large PDFs.
- Page images are sent as JPEG (faster to encode and transfer than PNG).
- A small server-side cache avoids re-rendering pages you scroll back to.
- The browser launches as soon as the server is ready (no fixed delay).

Document edits (text, signatures, export) are unaffected and stay full quality.

## Requirements

- A `python3` with **PyMuPDF** (`pip install pymupdf`). The build script finds
  one automatically (Homebrew or system Python). If none has PyMuPDF, the app
  tries to install it on first run.

## Updating

Edit `pdf_editor_app.py`, then re-run `build_app.command` to rebuild.

## Revert default back to Preview

Right-click any PDF → **Get Info** → **Open with:** → choose **Preview** →
**Change All…**. To remove the app, drag `/Applications/PyPDF Editor.app` to
the Trash.
