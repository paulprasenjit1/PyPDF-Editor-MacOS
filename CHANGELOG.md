# Changelog — PyPDF for Mac

All notable changes to this project are recorded here. Check this file before
recommending or implementing any fix, so completed work is not repeated.
Restore points live in `restore_points/` (a snapshot is taken before every
change set).

## [5.5.0-native] — 2026-07-06

Audit fixes (Steps 0–7 of AUDIT_REPORT.md). Restore points:
`restore_points/pdf_editor_app_20260706_111129_pre_v55_fixes.py` (pre Steps 0–5)
and `restore_points/*_pre_v56*` (pre Steps 6–7, incl. PyPDF.spec and setup.py).

### Steps 6–7 (H4, M11, H5)
- **Version single-sourcing.** `BUNDLE_VERSION` in `pdf_editor_app.py` is now
  the only place the version lives; `APP_VERSION` derives from it, and both
  `PyPDF.spec` and `setup.py` parse it at build time. About, Finder Get Info,
  and the versioned hand-off token can no longer drift (they previously
  disagreed: 5.4-native vs 5.0.0).
- **Dead code removed:** unused `glob` import, unused `server` argument on
  `_idle_watchdog`, legacy `thresh` parameter on `knockout_white`, and (from
  Step 1) the unused `NativeApi.overwrite()`.
- **Bug fix found by the new tests:** `safe_filename` truncated long names to
  120 chars AFTER appending `.pdf`, silently dropping the extension; it now
  caps the stem and always keeps `.pdf`.
- **Test harness added:** `tests/test_app.py` — 31 pytest cases covering the
  auth matrix (boot secret, GET/POST tokens, hand-off secret, Origin check),
  helpers (`safe_filename`, `image_format`, `atomic_write`, `friendly_error`),
  State/undo semantics, and end-to-end document operations (open, render,
  spans, edit+undo, delete, reorder, copy, merge, image-open, encrypted
  unlock, tab isolation, mark_saved, export, find).
- **Lint + local CI:** `ruff.toml` (pragmatic ruleset; passes clean) and
  `run_checks.sh` (py_compile → embedded-JS `node --check` → ruff → pytest).

### Fixed
- **C1 — Atomic native saves.** New `atomic_write()` helper (temp file in the
  same directory → fsync → `os.replace`). Adopted by `/api/save_inplace` and by
  the native bridge `save_pdf`, `save_png`, `save_copy_pages`. A crash or full
  disk mid-save can no longer truncate/corrupt the target file. Removed the
  unused (and non-atomic) `NativeApi.overwrite()` dead code.
- **C3 — Idle watchdog no longer runs in native mode.** Process lifetime in the
  native window is tied to the window itself (`webview.start()` returning), so
  a throttled WKWebView heartbeat (App Nap / occlusion) can no longer kill the
  app while its window is open. The heartbeat watchdog still runs in the
  browser-fallback mode, where it remains the only close signal.
- **C2 — Unsaved-changes guard on window close.** WKWebView ignores
  `beforeunload`, so closing the native window silently discarded edits. The
  window's `closing` event now checks every open tab's dirty flag and shows a
  native confirmation dialog before allowing the close.
- **H2 — Window `mouseup` listener leak.** The signature-drag handler added one
  permanent `window` mouseup listener per page stage on every rebuild
  (zoom/resize/operation). Replaced with a single delegated listener plus a
  module-level drag state; listener count is now constant.

### Security
- **C4 — Local API hardened against other local processes/users.**
  - All document GET endpoints (`/api/state`, `/api/tabs`, `/api/page`,
    `/api/text`, `/api/export`, `/api/pdf`, `/api/progress`, …) now require the
    per-process token — as the `X-PyPDF-Token` header, or as `?k=` for direct
    URL loads (`<img>`, downloads, print) which cannot send headers. `tq()` in
    the frontend appends the token to every direct URL. `/api/ping` stays
    tokenless (single-instance discovery needs it; it exposes nothing
    sensitive).
  - Serving `/` (the page that embeds the CSRF token) now requires a
    per-process boot secret carried in the launch URL (`/?boot=…`), so another
    local process can no longer fetch the page and mine the token from it.
  - The single-instance hand-off secret is compared with
    `secrets.compare_digest` instead of `!=`.
- `APP_VERSION` bumped to `5.5-native` so the versioned single-instance token
  prevents hand-off between old and new builds (the API contract changed).

### Added
- `CHANGELOG.md` (this file) and `.gitignore`.
- `AUDIT_REPORT.md` — full production-readiness audit (2026-07-06).

## [5.4-native] — pre-audit

Last version before the audit. Signature background knockout ON by default
(colour-aware, blends like Adobe Sign). Acrobat-style tabs, single-instance
hand-off, read-only-by-default viewer, find, organize/rotate/delete pages,
merge, compress presets, unlock, print-fit PDF, native save bridge.

## [5.1] — 2026-06-23

Restore point `*_20260623_110151_pre_v51*` captures the state before the v5.1
fix set (spec, build script, and app fixes).
