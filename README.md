# PyPDF for Mac — native build

A standalone macOS PDF editor. It runs in its own native window (via pywebview /
WKWebView) — no browser, no visible `localhost` URL — and opens multiple PDFs as
Acrobat-style tabs. All PDF work happens locally with PyMuPDF; nothing is
uploaded.

This is the native, self-contained successor to the original browser-based build.

## What changed from the browser version

- **Native window** instead of a Safari/Chrome tab. The UI is identical; only the
  wrapper changed. `webbrowser.open()` was replaced with a pywebview window, with
  an automatic fallback to the default browser if pywebview is missing.
- **Multiple documents in tabs.** Each open PDF is its own in-memory document,
  addressed by a tab id sent on every request. Opening a PDF (toolbar **Open**,
  drag-and-drop, **Create from images**, **Unlock**, double-click in Finder)
  creates a new tab with its own close (✕) button. Editing one tab never touches
  another.
- **Self-contained packaging.** `PyInstaller` bundles Python, PyMuPDF and
  pywebview inside the `.app`, so the end user needs nothing installed.

## Run from source (quickest way to try it)

```bash
cd pypdf-native
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 pdf_editor_app.py            # opens the native window
python3 pdf_editor_app.py file.pdf   # open a PDF straight into the first tab
```

If pywebview is not installed it still runs, falling back to your default browser.

## Build the installable .app

```bash
./build_native.command
```

This builds `dist/PyPDF for Mac.app` with PyInstaller (see `PyPDF.spec`),
**ad-hoc signs** it (`codesign -s -`) so it
runs on the Mac that built it without an Apple Developer account, removes the
quarantine flag, registers it as a PDF handler, and installs it to
`/Applications`. It will offer to set the app as the default for all PDFs.

> Ad-hoc signing is for personal use on your own Mac. The app is **not** notarized,
> so distributing it to other Macs would trigger Gatekeeper warnings. For wider
> distribution you would need a Developer ID signature and Apple notarization.

## Licensing (important for the public GitHub repo)

This app uses **PyMuPDF**, which is licensed under **AGPL-3.0** (or a paid
commercial licence from Artifex). Because of AGPL's copyleft:

- Personal use is unrestricted.
- Publishing the source publicly on GitHub is fine **as long as this project is
  also licensed under AGPL-3.0** and the source is available to anyone who uses it.
- You may **not** ship it as a closed-source or paid product without buying a
  commercial PyMuPDF licence.

A `LICENSE` (AGPL-3.0) and `NOTICE` file are included. Keep them in the repo.

## Files

- `pdf_editor_app.py` — the whole app (server + UI + PDF engine).
- `PyPDF.spec` — PyInstaller build configuration (used by the build script).
- `setup.py` — legacy py2app config, kept for reference (not used by the build).
- `build_native.command` — one-click build + ad-hoc sign + install.
- `requirements.txt` — runtime dependencies.
- `appicon.png` — app icon source (converted to `.icns` at build time).
