# PyPDF for Mac — macOS App

## What's new (2026-06-12) — v4.1 — Retina-sharp rendering

- **Crisp text on Retina displays.** Pages were rendered at CSS-pixel width
  and then upscaled 2× by the browser on HiDPI screens — the source of the
  soft, fuzzy text compared to Preview/Acrobat. Pages (and the Organize /
  Copy thumbnails) are now rendered at device-pixel resolution (capped at
  3200px wide) and displayed at CSS size, and JPEG quality went from 82 → 90.
  Text edits and signature boxes still align exactly (they're positioned in
  CSS pixels). Verified by a new HiDPI browser test (device_scale_factor=2).

## What's new (2026-06-12) — v4.0 — polish release

- **Working overlay**: long operations (compress, merge, images→PDF, open,
  organize) now show a spinner that blocks stray clicks; it only appears for
  operations that take longer than a blink.
- **✕ Remove pages in Organize Pages**: reorder, rotate AND delete in one
  sheet, applied together as a single undoable step.
- **🖨 Print** button: renders every page at high resolution and opens the
  system print dialog.
- **Merge / Images→PDF are now undoable**: the replaced document (and its
  name) come back with one Undo, instead of clearing history.
- **Self-contained install**: `build_app.command` now builds a private Python
  environment with PyMuPDF *inside* the app bundle (one-time, ~40MB download).
  The app keeps working even if Homebrew or system Python changes. Falls back
  to system Python automatically if the build is offline.
- Tests grew to ~130 checks across four suites — including a stuck-overlay
  bug the new tests caught before it ever shipped.

## What's new (2026-06-12) — v3.5 — full review + scenario coverage

- **Fixed**: opening a password-protected PDF via Open (or double-click) used
  to fail with an error; it now prompts for the password and opens normally.
- **Every scenario now covered by real-browser tests** (`tests/e2e_scenarios.py`,
  29 checks): text editing, signature placement by dragging a box, delete
  pages, compress, page→PNG, merge with order list, keyboard shortcuts
  (⌘S/⌘Z/arrows/±), double-click zoom both directions, damaged files,
  encrypted files, external drag-drop, and two-tab sync.
- Full suite: 30 + 29 browser E2E, 35 endpoint, 21 jsdom UI — all passing.
- Noted by design: Merge replaces the document and clears undo history.

## What's new (2026-06-12) — v3.4 — drag-reorder fix

- **Fixed**: dragging a thumbnail in Organize Pages could open the
  "Create PDF from images" dialog instead of reordering. Cause: page images
  are natively draggable in browsers, so the image drag hijacked the
  thumbnail drag, and on drop the browser presented the image as a dropped
  *file* to the window-level drag-&-drop handler. All app images are now
  `draggable=false` (the thumbnail itself drags properly again), and the
  file-drop handler ignores drags that started inside the app or while a
  sheet is open.
- New E2E regression test: a real Chromium drag of the last thumbnail onto
  the first slot — verifies the reorder applies server-side and no dialog
  appears. E2E suite is now 30 checks; 35 endpoint + 21 UI checks unchanged.

## What's new (2026-06-12) — v3.3 — stale-instance fix + full revalidation

- **Root cause of "broken features" found and fixed.** The single-instance
  token was the same across all builds, so a server still running from an OLD
  build (kept alive by an old browser tab) captured every new launch and kept
  serving the old interface. The token is now versioned — a new build never
  hands off to an older server — and `build_app.command` also stops any
  running editor before installing.
- **App icon fix**: the installer now writes the icon as both `applet.icns`
  and `AppIcon.icns`, points `CFBundleIconFile` at it explicitly, verifies the
  conversion succeeded, and refreshes Finder + Dock so the new icon shows.
  (If it still looks stale, log out/in once — macOS caches icons hard.)
- **Dialog fix**: pressing Escape on the image-quality dialog no longer leaves
  Create-from-Images hung.
- **Every feature revalidated end-to-end in a real Chromium browser** against
  the real server (`tests/e2e_test.py`, 26 checks): welcome, open, organize
  (thumbnails/rotate/apply), copy pages, undo, unsaved-changes guard,
  save-with-rename, create-from-images incl. quality + Escape, zoom, About,
  close. Plus 35 endpoint checks and 21 jsdom UI checks — all passing.

## What's new (2026-06-12) — v3.2

- **New app icon** matching the PyPDF for Mac name (blue document with PDF
  ribbon; old icon kept at `restore_points/appicon_old.png`).
- **"Remove white background" hidden**: signatures are placed as-is; the
  option stays off by default and is no longer shown.
- **Organize Pages hardened**: the first screenful of thumbnails now loads
  immediately (lazy loading covers the rest), and the sheet is made visible
  before rendering. Note: the installed app runs the copy *embedded at build
  time* — changes to this folder only take effect after re-running
  `build_app.command`.
- **Create from Images — Standard / Small file** choice (remembered): Small
  downscales each photo to ~1600px and re-encodes as JPEG for a much lighter
  PDF (and never bloats graphics-heavy images — it keeps whichever is smaller).
  Page sizes stay identical in both modes.
- **New UI test harness** `tests/ui_test.mjs` (jsdom, 21 checks: welcome,
  organize incl. rotate+apply payload, copy pages, quality dialog, hidden
  knockout). Run: `node tests/ui_test.mjs pdf_editor_app.py` (needs `npm i jsdom`).
- Endpoint tests grew to 35 checks (`python3 tests/smoke_test.py`).

## What's new (2026-06-12) — v3.1 "fine-tune" release

- **Renamed to "PyPDF for Mac"** everywhere — window, header, About, and the
  app bundle (`PyPDF for Mac.app`; the installer removes the old
  `PyPDF Editor.app` automatically). Re-run `build_app.command` to apply.
- **Faster on big files**: the document size shown in the header is now cached
  per document version — previously the entire PDF was re-serialised on every
  zoom or resize just to display "KB". Window resizes that don't change the
  width no longer re-render anything.
- **Drag & drop**: drop a PDF anywhere on the window to open it, several PDFs
  to merge them, or images to build a new PDF — with a dashed-outline cue and
  the usual unsaved-changes guard.
- **Keyboard shortcuts**: ⌘S save · ⌘O open · ⌘Z undo · ← → / PageUp PageDown
  pages · + − zoom.
- **Light & dark theme**: the UI now follows the macOS appearance setting
  automatically (it was dark-only).
- Tests: 32 checks in `tests/smoke_test.py`, all passing.

## What's new (2026-06-12) — Phase 3 (v3.0), ported from the iPhone PWA

- **Pinch to zoom**: a trackpad pinch scales the pages live under your cursor
  (one CSS transform, no engine work) and re-renders sharp when you let go,
  anchored at the pinch point. Works in Chrome/Edge/Firefox (ctrl+wheel) and
  Safari (native gesture events). **Double-click** toggles 100% ↔ 200% centred
  on the click. Range stays 50–300%; the − / + buttons now keep the view
  anchored too.
- **Long documents stay fast**: Organize Pages and Copy Pages thumbnails are
  fetched once per document version, cached, and loaded lazily as you scroll —
  opening the sheet on a 100-page PDF is instant, and every rotate/drag/select
  redraws with no re-downloads.
- **Lighter on memory**: undo history is capped at 120MB total (as well as
  15 steps), so huge PDFs can't pile up fifteen full copies in RAM.
- **Welcome screen**: two big buttons — Open a PDF / Create a PDF from images —
  plus "Everything stays on your Mac — nothing is uploaded."
- **ⓘ About** (header): version, engine, session start, privacy note, and the
  **last 3 unexpected errors** (which now also show in the status bar instead
  of dying silently).
- `build_app.command` now stamps the app bundle with the version from
  `APP_VERSION` in `pdf_editor_app.py`. Re-run it to rebuild the installed app.
- Tests: 29 checks in `tests/smoke_test.py`, all passing.

## What's new (2026-06-12) — Phase 2, ported from the iPhone PWA

- **Page rotation**: Organize Pages now has a ⟳ button on every thumbnail
  (90° steps). The preview rotates live with a pending-angle badge, and
  rotations apply together with reordering as one undoable step. Editing text
  on a rotated page warns first (edits assume upright pages).
- **⧉ Copy Pages**: pick pages with thumbnails; they are copied into a
  brand-new PDF (`<name>_pages.pdf`) — the open document is untouched.
- Tests now cover rotation, combined reorder+rotate, undo of both, and
  copy-pages — 25 checks in `tests/smoke_test.py`.

## What's new (2026-06-11) — ported from the iPhone PWA

- **Unsaved-changes protection**: any edit, sign, page change, merge or compress
  marks the document as edited ("Edited" shows in the header). Open, Merge,
  Create from Images, Unlock and Close now ask first — Save first / Continue
  without saving / Cancel — and closing the browser tab warns too. Save clears it.
- **Save dialog with rename**: Save opens a small sheet with a name box; the file
  downloads under the chosen name (sanitised server-side).
- **✕ Close button**: closes the document, frees engine memory and undo history.
  Other open tabs notice and clear as well.
- **Security/robustness**: file names are HTML-escaped before being shown in
  dialogs (DOM-XSS fix), and damaged-file errors now read "This file appears
  damaged, or isn't really a PDF" instead of raw engine output.
- **Tests**: `tests/smoke_test.py` (15 endpoint checks) — run with
  `python3 tests/smoke_test.py` from this folder (needs PyMuPDF).
- Pre-change snapshot saved in `restore_points/`.

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
2. It builds **PyPDF for Mac.app**, drops it in `/Applications`, sets it as the
   **default app for all PDFs**, and refreshes Finder.
3. Done. Double-click any PDF — it opens in the editor (in your browser, served
   locally). Right-click → Open With also lists **PyPDF for Mac**.

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
**Change All…**. To remove the app, drag `/Applications/PyPDF for Mac.app` to
the Trash.
