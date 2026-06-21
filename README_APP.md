# PyPDF for Mac — macOS App

## What's new (2026-06-18) — v4.20 — Editing OCR'd text on a scan matches the image

- **Fixed: clicking and editing text on a scanned page looked wrong.** After you
  run OCR, the words become clickable — but that "text" is an invisible OCR layer
  over the image, so editing it the normal way wrote a generic font at the
  OCR-guessed size and the result didn't match the surrounding scan (e.g.
  **Gender: Male → Female** came out thin, small and misaligned). Editing such a
  span now matches the actual image pixels — **size, baseline, left edge, colour
  and bold/regular weight** — the same matching used by the “✎ Edit scanned”
  tool, and it drops the stale OCR word so it can't reappear.
- **Normal (non-scanned) text editing is unchanged** — PDFs with real embedded
  text still keep their original typeface exactly as before. The new behaviour
  only applies to OCR text layers sitting over a scanned image.

## What's new (2026-06-18) — v4.19 — OCR works with the Tesseract you installed

- **Fixed: OCR still said Tesseract wasn't installed.** Even after locating the
  language data, PyMuPDF's built-in OCR could fail on a double-clicked app (it
  doesn't inherit the shell environment, and some PyMuPDF builds can't OCR). OCR
  now falls back to running the **Tesseract binary you installed** directly
  (probing `/opt/homebrew/bin`, `/usr/local/bin`, … so it's found without the
  shell PATH), which produces the same selectable text layer and preserves each
  page's size. If neither path works, the error now says exactly what's missing,
  and **About** shows whether OCR is available and which languages are installed.

## What's new (2026-06-18) — v4.18 — Edit-scanned matches the original text

- **Replacement text now matches the scan.** The first cut of “Edit scanned”
  wrote the new text in a fixed regular font sized to the box you drew, so it
  often came out thinner, smaller and higher than the words it replaced. It now
  measures the original glyphs in the box and matches their **size, baseline,
  left edge and colour**, and auto-detects **bold** (by stroke thickness) so a
  bold field like **Gender: Male** is replaced with bold text of the same size.
- **Bold toggle.** The Edit-text panel shows a **Bold** checkbox while you're
  editing a scanned box, pre-ticked to the detected weight — flip it if the auto
  guess is wrong.
- **Longer replacements aren't shrunk.** “Male → Female” keeps the original
  letter size and simply extends into the blank space to the right, instead of
  being squeezed to fit the box (it only shrinks if it would run off the page).

## What's new (2026-06-18) — v4.17 — Edit text baked into a scan or image

- **New: “✎ Edit scanned”.** The normal click-to-edit only works on real,
  selectable text. Scanned or photographed pages store their words as pixels
  inside an image, so there was nothing to click. The new toolbar mode fixes
  that: click **✎ Edit scanned**, drag a box over the words you want to change,
  and the app reads what's in the box (OCR, pre-filling the editor), clears that
  patch with the surrounding background colour, and writes your replacement text
  sized to fit the box. This is the same cover-and-overlay technique you'd do by
  hand, built into the app — handy for fixing a wrong field on a scanned form
  (e.g. a prescription's **Gender: Male → Female**).
- **Details.** The replacement colour is sampled from the original glyphs
  (dark-on-light scans look right by default), the patch colour is sampled from
  the area around the box (so it blends on tinted forms, not just white), and the
  whole change is **one Undo step**. OCR pre-fill is best-effort — if Tesseract
  isn't installed the box is simply left blank for you to type into. If a page
  already has real selectable text, prefer clicking the span; this mode is the
  fallback for images.

## What's new (2026-06-16) — v4.16 — OCR finds Tesseract even when double-clicked

- **Fixed: "OCR needs Tesseract" even after installing it.** A double-clicked app
  doesn't inherit your shell's `TESSDATA_PREFIX` or Homebrew `PATH`, so PyMuPDF
  couldn't locate Tesseract's language data. The app now probes the standard
  install locations (`/opt/homebrew/share/tessdata`, `/usr/local/share/tessdata`,
  etc.) and passes the folder to the OCR engine directly. Installed languages are
  read from that folder, and **About** now shows OCR status and the detected
  languages so you can confirm at a glance. The error message is also more
  specific (e.g. it points to `tesseract-lang` when a chosen language pack is
  missing).

## What's new (2026-06-16) — v4.15 — OCR progress + edit polish (Review #3, Phases 2-3)

- **OCR shows progress and can be cancelled.** Long OCR now displays "page X of
  Y" and a **Cancel** button instead of an endless spinner; cancelling leaves the
  document unchanged.
- **OCR language choice.** If more than one Tesseract language pack is installed,
  OCR asks which to use (combine with `+`, e.g. `eng+fra`); otherwise it just
  uses English.
- **Text edits blend with coloured backgrounds.** The patch left when you edit
  text now samples the surrounding colour, so edits on shaded headers / coloured
  cells no longer leave a white box (white pages are unaffected).
- **Hygiene:** undo releases the replaced document's memory immediately; the
  new-document poll pauses while the tab is hidden; and the single-instance
  hand-off is now authorised with a per-session secret (defence-in-depth on the
  one endpoint that loads a file by path).

## What's new (2026-06-16) — v4.14 — Save to original + better text edits (Review #3, Phase 1)

- **Save back to the original file.** For PDFs opened from disk (double-click /
  Open), the Save dialog now offers **Save (overwrite original)** as well as
  **Save a copy** to Downloads — no more piling up `file (1).pdf`, `file (2).pdf`.
  Uploaded / merged / image-built documents (which have no file on disk) keep the
  Save-a-copy download.
- **Edited text keeps the original font.** When you edit a text span, the app now
  reuses the page's own embedded typeface if it covers the new characters, so the
  edit blends in instead of switching to a generic substitute. It falls back to
  the closest standard family only when the embedded font can't be reused safely
  (e.g. a subset that lacks the new letters).
- **Rotated pages: edit warning removed.** Verified that text edits land at the
  correct position on rotated pages, so the old "may land in the wrong place"
  prompt was unnecessary and is gone.

## What's new (2026-06-14) — v4.13 — fix "load failed" after switching away

- **The helper no longer shuts down while you glance away.** Actions (e.g. Undo
  right after creating a PDF from an image) sometimes failed with "load failed".
  Cause: the local helper quit after just 12 seconds without a heartbeat, but
  browsers throttle background-tab timers to about once a minute — so briefly
  switching away from the tab let the heartbeat lapse and the helper exited; the
  next click then hit a server that was gone. The idle timeout is now 90 seconds
  (comfortably above the throttle rate), the tab sends a heartbeat the instant it
  regains focus, and if the helper ever is unreachable the app now says so
  clearly ("Reload this page to restart it") instead of "load failed".

## What's new (2026-06-14) — v4.12 — Undo correctness fix

- **A failed edit or signature no longer breaks Undo.** Edit-text and Place-
  Signature recorded their undo checkpoint *before* validating the input, so an
  edit on a stale text span (or a sign on a bad page) left a bogus entry on the
  undo stack — lighting up Undo for a no-op and, worse, making the next Undo
  restore the wrong state. The checkpoint is now taken only after validation, so
  Undo always steps back exactly one real change. (Compress was hardened the same
  way.) Found by the full functional test pass.

## What's new (2026-06-14) — v4.11 — Open works on images too

- **Opening an image no longer errors.** Click **Open** (or double-click / drag)
  a JPEG, PNG, HEIC, etc. and the app now builds a one-page PDF from it instead
  of failing with "this file appears damaged, or isn't really a PDF." The Open
  picker now lists image files as well as PDFs.
- **Clearer import errors.** When an image truly can't be read, the message now
  names the detected format, e.g. "Couldn't read 'receipt.heic' (detected HEIC)
  …", so it's obvious what to convert.

## What's new (2026-06-14) — v4.10 — HEIC / WEBP image import

- **Create-from-Images now accepts HEIC (iPhone photos) and WEBP.** The PDF
  engine can't decode those formats, so import failed on them. As a final step
  the app now converts unsupported formats to PNG using macOS `sips` (built into
  every Mac) and embeds that. PNG/JPEG keep their fast, lossless path; the clear
  "convert to JPEG" message remains for anything even sips can't read.

## What's new (2026-06-14) — v4.9 — safer OCR + image import (Review #2, Phase A)

- **OCR no longer degrades text PDFs.** Previously OCR rasterised every page, so
  running it on a PDF that already had text replaced crisp vector text with an
  image (and bloated the file ~50×). OCR now skips pages that already contain
  text and only processes image-only pages; it reports how many it handled and
  how many it left untouched. Running OCR on an all-text PDF now does nothing
  instead of damaging it.
- **A PDF dropped into Create-from-Images is rejected** with a clear message
  ("…is a PDF, not an image. Use Open or Merge PDFs") instead of being silently
  embedded as a picture.

## What's new (2026-06-14) — v4.8 — robust image import

- **Create-from-Images no longer fails with a cryptic error.** Images the engine
  couldn't decode raised a raw message like `code=7: not a png image`. Import now
  (1) tries a Pixmap fallback decoder for odd encodings the main opener rejects,
  and (2) when an image genuinely can't be read, names the file and explains why:
  e.g. HEIC (iPhone photos) and some camera/RAW formats aren't supported — convert
  to JPEG first. A half-built document is no longer left behind on failure.
- The image picker now accepts all image types (it was limited to PNG/JPEG), so
  valid formats aren't hidden — unsupported ones get the clear message above.

## What's new (2026-06-14) — v4.7 — OCR + polish (Phase 3)

- **OCR — make scanned / image PDFs selectable.** A new **OCR** button runs the
  pages through Tesseract and adds a real text layer, so text can be selected,
  searched and copied (and the Edit-text tool can target it). It's one Undo away
  if you don't like the result. Needs the Tesseract engine: `build_app.command`
  now installs it automatically when Homebrew is present, otherwise the app
  shows `brew install tesseract` when you click OCR. (An image is still a
  picture; OCR recovers the text from it as well as the scan quality allows.)
- **Editing text no longer wipes table lines.** When you edit a text span, only
  the old text is removed now — images and vector rules (e.g. table borders)
  that merely cross the text box are preserved, instead of vanishing.
- **Safari prints correctly.** Safari can't reliably script-print a hidden PDF
  frame, so it now opens the print-ready PDF in a tab to print from (⌘P).
  Chrome/Edge/Firefox keep the seamless one-click print.
- **Upload guard.** Requests larger than ~300 MB are refused up front instead of
  risking an out-of-memory crash.
- **Faster signatures by default.** The build now installs numpy, enabling the
  vectorised white-removal path from v4.6 on a fresh install.

## What's new (2026-06-14) — v4.6 — performance (Phase 2)

Addresses the performance findings from the app review. No behaviour changes —
same outputs, much less waiting on large documents.

- **Snappier after every action.** The header's file-size figure was recomputed
  with a full optimising re-serialise of the whole document after every edit
  (~385 ms on a 100-page PDF). It now uses an ~8x cheaper pass with the same
  byte count (~46 ms), so editing large files no longer stalls between actions.
- **Faster printing.** Fit-to-page found each page's content box by rasterising
  it and scanning pixels in Python. It now reads the content extent directly
  from the page geometry (no rasterisation) and caches it per page — about 6x
  faster on content-heavy pages, and a re-print is instant.
- **Much faster compression on big scans.** Image compression tried each quality
  step in turn; it now binary-searches for the gentlest step that meets the size
  target. On a 5 MB scanned PDF this cut a "Low" compress from ~9.7 s to ~0.45 s
  (identical result) by skipping the slow high-resolution passes.
- **Faster signature placement.** White-background removal on a signature image
  used a per-pixel Python loop; it's now vectorised (~6x faster) when numpy is
  available, with the original loop as a fallback (no new required dependency).

## What's new (2026-06-14) — v4.5 — reliability & security hardening (Phase 1)

Addresses the high-priority findings from the full app review (`APP_REVIEW_v4.4.md`).

- **Thread-safety.** The server is multi-threaded but PyMuPDF is not thread-safe.
  Document access is now serialised behind a single lock, so an operation
  (compress / organize / close) running while pages are still streaming in can
  no longer corrupt state or fail renders. Under a stress test that previously
  produced a 27% error rate, requests now succeed. The heartbeat stays outside
  the lock so the UI never appears frozen.
- **Cross-site request protection.** The local API now rejects browser requests
  from other websites (Origin check) and requires a per-process random token on
  every state-changing request. A web page you visit while the app is running
  can no longer drive the API or read local files via it. The single-instance
  hand-off (which isn't a browser) still works.
- **No more phantom "changed" state.** A failed request used to still bump the
  document version, thrashing the page-render cache and signalling other tabs
  that nothing-actually-happened. The version now changes only on success.
- **Robustness.** Out-of-range page numbers are rejected cleanly (a negative
  index no longer silently returns the last page); cancelled page fetches no
  longer print disconnect tracebacks to the console.

## What's new (2026-06-14) — v4.4 — sharp PNG export + image-PDF quality

- **Page → PNG is sharp again.** It was exporting at ~144 dpi, which softened
  text. It now exports at ~288 dpi (4× zoom), so saved page images are
  print-grade and readable. Feeding a Page→PNG into Create-from-Images no longer
  starts from a blurry source.
- **Create from Images keeps quality.** *Standard* mode embeds each image at
  full resolution with no re-encoding (pixels preserved exactly). *Small file*
  mode is far gentler now — it caps the long edge at 2400px (only shrinking
  images bigger than that) and uses high-quality JPEG (q88) instead of the old
  1600px / q72 that turned invoice text to mush.
  - Note: an image is raster, so a PDF built from it is a picture, not editable
    text. Making it editable would need OCR, which doesn't reproduce invoice
    tables reliably — so the focus here is preserving the source quality. If the
    original is a real text PDF, edit it directly instead of going via an image.
- **Printing fills the page without touching Scaling.** Fit-to-page now uses a
  smaller margin (~0.33"), so content fills the sheet at the dialog's default
  100% — no need to set Scaling to 280%. (An app can't pre-set the macOS print
  dialog's Scaling field; fit-to-page is the equivalent and avoids the cropping
  that an over-large fixed scale would cause.)

## What's new (2026-06-14) — v4.3 — fit-to-page printing + repeat-print fix

- **Print now fits the content to the page.** Documents whose content sat in a
  corner of a larger sheet (e.g. a small invoice on an A4 page) printed tiny.
  Print now scales each page's actual content to fill the printable area,
  centred and with aspect ratio preserved — no more wasted whitespace. It stays
  fully vector (the source page is embedded, not rasterised), so text, barcodes
  and table rules remain razor-sharp. The content extent is detected from what
  actually renders, so it works even when the text layer understates the layout.
- **Fixed: Print only worked once.** After printing (or cancelling) once,
  clicking Print again did nothing. The hidden print frame was being reused;
  it's now rebuilt fresh each time, with a guard against double-triggering and a
  timeout fallback for PDF viewers that don't fire a load event. Print works on
  every click now.

## What's new (2026-06-14) — v4.2 — direct-PDF printing + page jump

- **🖨 Print now prints the real PDF, not an image.** Previously every page was
  rasterised to PNG and those images were sent to the printer, which softened
  text and lines and ballooned memory on long documents. Print now hands the
  actual PDF (with all your current edits) to the browser, which renders it
  natively at the printer's own resolution — vector-sharp text, crisp barcodes
  and table rules, and far lighter on memory.
- **No stray browser tab when printing.** The document is loaded into a hidden
  in-page frame instead of `window.open(...)`, so the system print dialog
  appears without a separate PDF tab opening (and pop-up blockers no longer
  interfere).
- **Page-jump navigation.** The ◀ Prev / `1 / N` / Next ▶ controls are replaced
  by a compact page box: type a page number and press Enter (or click **Go**) to
  jump straight there — handy for 100+ page PDFs. The box stays in sync with the
  scroll position and clamps out-of-range values. Arrow keys / PageUp-Down still
  page through the document.
- **Restore-point hygiene.** Before each version bump a fresh snapshot of
  `pdf_editor_app.py` + `build_app.command` is written to `restore_points/`, and
  older snapshots are pruned so only the latest pre-upgrade copy is kept.

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
