# PyPDF for Mac — Production Readiness Audit (Phase 2 Report)

Date: 6 July 2026 · Version audited: APP_VERSION `5.4-native` (bundle plist says `5.0.0`) · Scope: `pdf_editor_app.py` (4,411 lines), `PyPDF.spec`, `build_native.command`, `setup.py`, `NOTICE.md`, `LICENSE`, `README.md`

**No code has been changed.** This report is for review and approval. Note: the brief referenced CHANGELOG.md; **no CHANGELOG.md exists in this repository** — creating one is recommended as the first approved change so the "check before fixing" rule becomes enforceable.

---

## 1. Executive summary

This is a well-crafted, single-file native macOS PDF editor: a local HTTP server (127.0.0.1) plus a pywebview/WKWebView window, with PyMuPDF doing all PDF work in memory. Code quality is unusually high for a single-author project — thoughtful comments, CSRF/Origin defences, atomic in-place save, undo memory caps, render caches, lazy page loading, progress channels, and a working single-instance/hand-off design.

However, measured against "App Store quality production ready":

1. **The app cannot go to the Mac App Store in its current form**, for licensing (AGPL PyMuPDF), signing (ad-hoc, unnotarized), and sandboxing (local HTTP server architecture) reasons. Realistic target: **notarized Developer ID distribution outside the App Store**, which the AGPL permits.
2. There is **one genuine data-corruption risk** (non-atomic overwrite in the native save bridge), **one data-loss risk** (closing the window discards unsaved edits with no prompt), and **one availability risk** (idle watchdog can kill the app while its window is open).
3. The localhost API is readable by **any process and any user on the machine** (GET endpoints require no token), which contradicts the app's core privacy promise.
4. There are **no tests, no linting, no CI, no CHANGELOG**, and the 4,411-line monolith mixes Python, CSS, and ~1,700 lines of JS in one string.

Nothing here is fatal. The critical items are small, surgical fixes; the strategic items (distribution, licensing) are decisions rather than code.

---

## 2. Critical issues

### C1 — Non-atomic file overwrite in the native save path (data corruption)
- **Where:** `NativeApi.save_pdf()` (line ~4054) and `NativeApi.overwrite()` (line ~4085): `open(path, "wb").write(data)` directly onto the target, including when the user saves over the original file.
- **Problem:** A crash, kill, or full disk mid-write leaves the user's original PDF truncated/corrupted. The HTTP route `/api/save_inplace` already does this correctly (temp file in same dir → fsync → `os.replace`), so the app has two save implementations with different safety levels. In native mode the safe one is never used for the save panel path.
- **Root cause:** The native bridge was added later and did not reuse the atomic-write helper.
- **Fix:** Extract one `atomic_write(path, data)` helper; use it in `save_pdf`, `overwrite`, `save_png`, `save_copy_pages`. Also: `NativeApi.overwrite()` appears to be dead code (no JS caller) — remove or wire it up.
- **Effort:** ~1 hour. **Risk of fix:** low.

### C2 — Unsaved changes silently lost when the native window closes
- **Where:** `main()`: `webview.start(...)` blocks; on window close the process runs `os._exit(0)` (line ~4399). The JS `beforeunload` guard exists but WKWebView does not show `beforeunload` dialogs, and pywebview quits regardless.
- **Problem:** User edits for an hour, hits ⌘Q or the red close button, everything is gone with no warning. For a document editor this is a top-tier defect.
- **Fix:** Hook pywebview's `closing` event; query dirty state (any `State.dirty` across `DOCS`) and show a native confirm dialog ("Save / Don't Save / Cancel") before allowing close.
- **Effort:** 2–4 hours. **Risk:** low; pywebview supports cancellable `closing` since 4.x.

### C3 — Idle watchdog can kill the app while the window is open
- **Where:** `_idle_watchdog()` (line ~4253) exits via `os._exit(0)` if no `/api/ping` for 90 s.
- **Problem:** In native mode, the heartbeat comes from a JS timer inside WKWebView. macOS App Nap/occlusion throttling, a JS exception, or a long GC pause can starve the heartbeat while the window is still open — the app then vanishes (with any unsaved work, compounding C2). The 90 s value is tuned for browser tabs, but native mode shouldn't depend on heartbeats at all.
- **Fix:** In native mode, disable the heartbeat watchdog entirely and tie process lifetime to the window `closed` event (which is already how `webview.start()` returning is handled). Keep the watchdog only in browser-fallback mode.
- **Effort:** ~1 hour. **Risk:** low.

### C4 — Local API is world-readable on the machine (privacy)
- **Where:** `Handler.do_GET` — all document GETs (`/api/export`, `/api/pdf`, `/api/page`, `/api/text`, `/api/tabs`, `/api/state`) require only the Origin check, which passes for **any request with no Origin header** (curl, scripts, other users' processes). `/api/tabs` leaks tab IDs and full file paths, defeating the random-tab-id obscurity. `GET /` serves the page containing `APP_NONCE`, so even the POST token is obtainable by any local process.
- **Problem:** On a shared/multi-user Mac, any other user (or any malware running as any user) can read every open document while the app runs. This directly contradicts the in-app claim "nothing is uploaded / everything stays private". Browser cross-site exfiltration is blocked (CORS prevents reads), so the exposure is local-process/local-user, not remote — but the app's marquee promise is privacy.
- **Fix (layered):** (a) require `X-PyPDF-Token` (or `?token=`) on all `/api/*` GETs — the native window and the page control every URL, so this is fully compatible; (b) serve `/` with the nonce only to the first native-window load or gate it on a boot secret passed via the pywebview URL; (c) use `secrets.compare_digest` for the hand-off secret comparison (line ~1665, currently `!=`).
- **Effort:** 4–6 hours incl. regression testing of `<img src>`, print iframe, downloads. **Risk:** medium (touches every fetch path); needs careful testing.

---

## 3. High priority issues

### H1 — No crash recovery / autosave
All documents and undo stacks live only in RAM. An engine crash (PyMuPDF is C code; malformed PDFs can segfault), a watchdog exit, or a macOS kill loses everything. Recommend a periodic background snapshot of dirty documents to `~/Library/Application Support/PyPDF for Mac/recovery/` and a "Restore unsaved documents?" prompt at launch. Effort: 1–2 days.

### H2 — Window `mouseup` listener leak
`attachSign()` (line ~2905) adds a `window.addEventListener("mouseup", ...)` **per page stage per rebuild** and never removes it. Every zoom, resize, or operation on an N-page document adds N more permanent listeners; a long session on a 200-page file accumulates thousands. Each holds closures over stage DOM (also delaying GC of removed nodes). Fix: one delegated window listener, or add via `AbortController`/named removal on rebuild. Effort: ~1 hour.

### H3 — Unbounded aggregate memory across tabs
Per-tab caps exist (`UNDO_MAX_BYTES` = 120 MB) but are per `State`; 8 tabs of scanned PDFs can hold ~1 GB of undo snapshots plus live docs plus the pristine copy `compress()` keeps (`original` + trials = 3–4× doc size transiently). No global memory budget, no doc-size guard on open. Recommend a global undo budget and a size warning above ~300 MB per document. Effort: half a day.

### H4 — Version and metadata drift
`APP_VERSION = "5.4-native"` (line 110) vs `5.0.0` in `PyPDF.spec` and `setup.py`. The single-instance token is derived from `APP_VERSION`, so this works, but About, Finder Get Info, and crash reports disagree. Single-source the version (spec reads it from the .py). Effort: ~1 hour.

### H5 — No tests, no lint, no CI, no CHANGELOG
Zero automated coverage on a codebase with subtle concurrency (global `STATE` rebinding under `RLock`) and an HTTP API of ~25 endpoints. Minimum viable: pytest suite hitting the handler with an in-process server (open/edit/undo/merge/compress/tabs/auth paths), `ruff` config, a `CHANGELOG.md`, and a pre-build check in `build_native.command`. Effort: 2–3 days for meaningful coverage.

---

## 4. Medium priority issues

- **M1 — Global-STATE rebinding architecture.** Every request mutates module global `STATE` under `STATE_LOCK`. Correct today, but any future handler that forgets the lock corrupts cross-tab state silently. Passing the bound `State` explicitly to operations would make misuse impossible. (Refactor, not a bug.)
- **M2 — Runtime pip auto-install** (`_ensure_pymupdf`, line 63): running from source silently pip-installs/upgrades into the user's environment, with a `--break-system-packages` fallback. Surprising side effect; should prompt or just fail with instructions.
- **M3 — Build script fragility** (`build_native.command`): edits `com.apple.launchservices.secure.plist` directly (unreliable on modern macOS; the supported route is `LSSetDefaultRoleHandlerForContentType` or asking the user), `killall Finder`, `pkill -f pdf_editor_app.py` (matches unrelated processes), `codesign --deep` (deprecated practice), quarantine stripping.
- **M4 — localStorage persistence in WKWebView is not guaranteed** for non-persistent data stores; zoom/theme/compress-level prefs may reset across launches depending on pywebview config. The app already has `settings.json` — persist prefs there via the JS bridge.
- **M5 — `os._exit(0)` everywhere** skips `atexit`, so the hand-off token file in `/tmp` is not cleaned on normal quit (registered at line ~4346 but bypassed at ~4399). Also skips flushing anything. Use a proper shutdown path where feasible.
- **M6 — Content-Disposition header injection edge:** `safe_filename` strips `/\:` and control chars but allows `"` — a crafted filename can break the header in `_download`. Quote/escape or whitelist.
- **M7 — Error strings leak raw engine text**: `friendly_error` falls through to `return raw`, so users can see raw MuPDF/Python messages (and tracebacked paths) in the status bar.
- **M8 — Print UX in native mode** opens the PDF in Preview rather than a print dialog. Functional, but jarring; consider `NSPrintOperation` via pyobjc or WKWebView print of the blob (matching the browser path).
- **M9 — Accessibility gaps**: modals have `aria-modal` but no focus trap and no focus restoration; Organize reorder is mouse-drag only (no keyboard alternative); checkbox "Delete pages" dots have no accessible group label; status bar uses `role=status` (good) but busy overlay cancel button is always hidden.
- **M10 — Stale `setup.py`** (py2app, unused per README) and `__pycache__`, `.build_venv`, `.venv`, `.DS_Store` committed in the working tree; needs `.gitignore` hygiene before the repo is public (AGPL obliges publishing sources).
- **M11 — Apparent dead code / unused imports**: `NativeApi.overwrite()` (no caller), `glob` import, `server` arg in `_idle_watchdog`, legacy `thresh` param in `knockout_white`.

---

## 5. Low priority issues

- `find_free_port` has a TOCTOU race between probe-close and server bind (rare, benign — bind fails loudly).
- `PROGRESS` dict relies on GIL atomicity; fine today, fragile if free-threaded Python ever lands. A tiny lock would be free.
- Console `print()` diagnostics instead of `logging`; nothing is written to a log file, so field issues are undiagnosable (add rotating log in Application Support, no document content in logs).
- `pageInput`/find pill: Enter in find box while matches exist re-steps rather than re-searching if text changed only in case — minor.
- Double-click zoom conflicts with double-click text selection expectation in Edit mode (guarded for spans, but not for span-adjacent whitespace).
- Emoji in `<h1>` title ("📄") is read aloud by VoiceOver.
- `LSMinimumSystemVersion: 10.14` is unrealistic — bundled Python 3.11/3.12 wheels and PyMuPDF require newer; set honestly (12+).

---

## 6. Distribution / "App Store" rejection risks (strategic)

1. **AGPL-3.0 (PyMuPDF) vs Mac App Store:** App Store terms have historically been held incompatible with (A)GPL distribution (the VLC precedent). Shipping this to MAS without an Artifex commercial licence is a legal risk, not just a review risk. **Realistic path: Developer ID + notarization, distributed outside MAS** — fully AGPL-compatible.
2. **Ad-hoc signing + quarantine stripping** works only on the building Mac. Any distribution requires: Developer ID Application cert, Hardened Runtime, `com.apple.security.cs.allow-unsigned-executable-memory` review for PyInstaller, notarization (`notarytool`), stapling. The current `codesign --deep -s -` and `xattr -dr` steps must be replaced.
3. **If MAS were ever pursued:** App Sandbox is mandatory; a localhost HTTP server requires network client+server entitlements and is reviewable; direct default-handler plist manipulation and `killall Finder` in tooling are irrelevant to the bundle but the LaunchServices behaviour (auto-offering to own all PDFs) should use supported APIs.
4. **Missing user-facing legal surfaces:** no privacy statement beyond a hint line, no About-window licence attribution (AGPL requires offering source); add a "Licences" section in About linking NOTICE and the source repo.

---

## 7. UX improvements (ranked)

1. Save/close guard parity with native window (C2) — the single biggest trust fix.
2. **Recent files** (File > Open Recent equivalent + welcome-screen list from `settings.json`).
3. **Session restore**: reopen the tabs that were open at last quit (paths exist in `State.path`).
4. Unify **Delete pages** into Organize (the sidebar checkbox dots duplicate Organize's ✕ with a worse interface); sidebar section can become "Pages" with thumbnails.
5. Native **menu bar**: ⌘W = close tab (currently closes the window), ⌘T/⌘O, Edit menu with working copy/paste, Window menu. pywebview exposes menu APIs.
6. Print without a detour through Preview (M8).
7. Drag-and-drop of a **mixed** PDFs+images drop could offer "merge all" instead of erroring.
8. Persist per-document zoom/scroll across relaunches (currently per-session only).
9. Status-bar messages are the only feedback channel; important results (compress summary) deserve a toast/dialog with a "Show in Finder" button after save.
10. First-run onboarding: one screen stating the privacy model and the read-only-by-default behaviour (the current welcome note covers some of this well).

## 8. Performance improvements

- Fix H2 (listener leak) and H3 (memory budget).
- `display_size_kb()` still serialises the full document once per epoch; on 200 MB scans this is a visible stall after each edit — compute it lazily/async and update the header when ready.
- `content_bbox`/render caches are good; consider capping `_RENDER_CACHE` by bytes rather than entries (64 entries of 3200-px JPEGs ≈ large).
- `compress()` binary search is smart; add a hard wall-clock budget with progress-cancel (the Cancel button in the busy overlay is permanently hidden).
- Thumbnails at `renderWidth(150)` re-render on every `thumbZoom` step; debounce.

## 9. Security improvements

Priority order: C4 (token on GETs, nonce exposure, `compare_digest` for hand-off), M6 (header quoting), M7 (error hygiene), M5 (token file cleanup). Then: bind check that the socket peer is same-UID via `SO_PEERCRED`-equivalent (not available for TCP on macOS — practical alternative is a UNIX socket for the native window, TCP only for browser fallback). Document the threat model in README.

## 10. Accessibility improvements

Focus trap + focus return in modals; keyboard reorder (⌥↑/⌥↓ on thumbnails) in Organize; labelled group for page checkboxes; visible focus rings on toolbar tiles; `prefers-reduced-motion` respect for smooth-scroll and spinner; replace emoji-in-title; test with VoiceOver on WKWebView (largely works out of the box given the existing aria labels — which are notably good).

## 11. Code quality improvements

Split the monolith: `server.py` (HTTP), `operations.py` (PDF engine), `state.py` (State/tabs), `native.py` (pywebview bridge, Apple events), `web/index.html` + `app.js` + `app.css` (packaged via PyInstaller datas). Add type hints (the codebase is hint-free), `ruff` + `ruff format`, docstring style is already excellent. Delete dead code (M11). Single-source version (H4).

## 12. Suggested refactoring (sequenced, low-risk order)

1. Extract `atomic_write()` (fixes C1 with net-negative line count).
2. Extract frontend into real files (mechanical; enables JS linting and diffable UI changes).
3. Replace global `STATE` rebinding with explicit `state = resolve_tab(headers)` passed into operations (kills the whole class of lock-forgetting bugs; ~100 call sites but mechanical).
4. Introduce `AppConfig` dataclass wrapping `settings.json` and move JS localStorage prefs into it (fixes M4).
5. Wrap the server in a class to eliminate remaining module globals (`HEARTBEAT`, `PROGRESS`, caches).

## 13. Future roadmap

- **v5.5 (stability):** C1–C4, H2, H4, CHANGELOG, logging. 1 week.
- **v5.6 (trust):** autosave/crash recovery (H1), session restore, recent files, close-guard polish, tests+CI (H5). 2–3 weeks.
- **v6.0 (distribution):** Developer ID + Hardened Runtime + notarization pipeline in `build_native.command` (or a `Makefile`), frontend extraction, menu bar, Sparkle-style update check (or manual "check for updates"). 3–4 weeks.
- **Later:** annotations (highlight/notes), form filling, OCR integration (tesseract is AGPL-friendly), Apple Silicon/Intel universal2 build.

## 14. Risk assessment

| Area | Risk | Likelihood | Impact |
|---|---|---|---|
| Native overwrite corruption (C1) | Data loss | Low per-save, certain eventually | Severe |
| Close-without-prompt (C2) | Data loss | High (everyday action) | High |
| Watchdog kill in native mode (C3) | Crash-like exit | Medium | High |
| Local API exposure (C4) | Privacy breach on shared Macs | Low–medium | High (breaks core promise) |
| No tests before refactors (H5) | Regressions during any of the above | High | Medium |
| AGPL vs paid/closed distribution | Legal | Only if strategy changes | Severe |
| PyMuPDF segfault on hostile PDF | Crash, lose session | Low–medium | High until H1 lands |

## 15. Step-by-step implementation plan (each step = restore point → approval → implement → validate)

1. **Step 0 — Process:** create `CHANGELOG.md` (backfilled from restore_points naming), `.gitignore`, and a `restore_points/` snapshot convention already in use. No behaviour change.
2. **Step 1 — C1:** `atomic_write()` helper; adopt in all four NativeApi writers; remove dead `overwrite()`. Validate: save over original, pull power scenario simulated with `kill -9` mid-write on a test file.
3. **Step 2 — C3:** native-mode watchdog disable; browser mode unchanged. Validate: occlude window 5 min, confirm alive; browser mode still exits when tab closes.
4. **Step 3 — C2:** pywebview `closing` handler + native confirm when any tab dirty. Validate: dirty doc → close → prompt; clean doc → close instantly.
5. **Step 4 — H2:** delegated mouseup listener. Validate: listener count stable across 20 rebuilds (Safari Web Inspector attached to WKWebView).
6. **Step 5 — C4:** token on GETs (+`?token=` for img/print/download URLs), `compare_digest` for hand-off, nonce delivery hardening. Validate: full manual pass of open/edit/sign/print/export in native and browser modes; `curl` without token gets 403.
7. **Step 6 — H4/M11:** version single-sourcing, dead code removal.
8. **Step 7 — H5:** pytest harness + ruff + minimal CI script; then H1 autosave behind it.
9. **Step 8 — Distribution track** (separate approval): Developer ID signing/notarization changes to `build_native.command`.

Each step is independently shippable; stopping after Step 5 already removes every critical risk.
