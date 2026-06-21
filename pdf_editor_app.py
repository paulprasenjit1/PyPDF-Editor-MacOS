#!/usr/bin/env python3
"""
PyPDF for Mac - Local Web App
============================

A browser-based PDF editor. No tkinter, no PyQt, no native GUI toolkits.
It starts a small local HTTP server, opens your default browser, and does
all PDF work with PyMuPDF (fitz) on the Python side.

Run:
    python3 pdf_editor.py

Then your browser opens at http://localhost:8080 automatically.

Features:
  1. View PDF            - continuous mouse-scroll; every page fits the same width
  2. Edit text           - click a text span, edit it, apply (font/size matched)
  3. Sign PDF            - upload a signature, draw a box, drop it in (white bg removed),
                           Undo to remove the last signature/edit
  4. Merge PDFs          - combine several PDFs into one (PDF binder)
  5. Compress PDF        - shows before/after size in KB
  6. Create from images  - build a PDF from JPG/PNG files
  7. Export              - download the PDF, or a single page as PNG
  8. Save                - download the current working PDF
  9. Delete pages        - pick pages, delete them

Requires: pymupdf  (pip install pymupdf)
"""

import base64
import json
import os
import socket
import sys
import threading
import secrets
import shutil
import tempfile
import subprocess
import glob

try:
    import numpy as _np            # optional: only used to speed up white-knockout
except Exception:
    _np = None
import time
import urllib.parse
import urllib.request
import webbrowser
from collections import OrderedDict, Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------------------
# Dependency check / auto-install
# ---------------------------------------------------------------------------
# Older PyMuPDF builds mis-render some embedded/subsetted CID fonts (text comes
# out as garbled glyphs). We require a reasonably recent version and upgrade if
# the installed one is too old. The check uses package metadata so we never
# import the old C extension before upgrading it.
MIN_PYMUPDF = (1, 24, 0)


def _ensure_pymupdf():
    import subprocess
    import importlib.metadata as md

    def installed_version():
        for name in ("pymupdf", "PyMuPDF", "fitz"):
            try:
                return tuple(int(x) for x in md.version(name).split(".")[:3])
            except Exception:
                continue
        return None

    def pip(*pkgargs):
        for extra in ([], ["--break-system-packages"]):
            try:
                subprocess.check_call([sys.executable, "-m", "pip", "install", *pkgargs, *extra])
                return True
            except subprocess.CalledProcessError:
                continue
        return False

    ver = installed_version()
    if ver is None:
        print("PyMuPDF not found. Installing it ...")
        pip("-U", "pymupdf")
    elif ver < MIN_PYMUPDF:
        print(f"PyMuPDF {'.'.join(map(str, ver))} is outdated; upgrading "
              "for correct font rendering ...")
        pip("-U", "pymupdf")


_ensure_pymupdf()
import fitz  # noqa

PORT = 8080
HOST = "127.0.0.1"
APP_VERSION = "4.30"         # Find-in-text highlights ALL matches in yellow (current in orange); selectable text div
SERVER_STARTED = time.strftime("%Y-%m-%d %H:%M")

UNDO_LIMIT = 15
# Undo snapshots are full document copies; cap their TOTAL size too, so a huge
# PDF can't pile up 15 multi-hundred-MB copies in memory.
UNDO_MAX_BYTES = 120 * 1024 * 1024
# Hard ceiling on a single request body (base64 inflates ~33%, so this admits
# PDFs up to ~220 MB). Guards against a malformed/huge upload exhausting memory.
MAX_REQUEST_BYTES = 300 * 1024 * 1024

# Single-instance support: a launch first looks for an already-running editor on
# one of these ports (identified by APP_TOKEN) and hands the new PDF to it
# instead of starting a second server / browser tab.
CANDIDATE_PORTS = [8080, 8081, 8082, 8090]
# The token is VERSIONED: a new build must never hand off to a server left
# running by an older build (its browser tab would keep showing the old UI and
# "broken" features). A mismatched old server is simply ignored — this launch
# starts fresh on another port, and the old one shuts itself down once its
# last tab closes.
APP_TOKEN = "pypdf-editor-v" + APP_VERSION

# Per-process random token (CSRF defence). The served page embeds it and echoes
# it back on every mutating request via the X-PyPDF-Token header. A malicious
# web page the user visits while the app runs cannot read this value, so it
# cannot forge state-changing requests to the local API. Combined with an
# Origin check, this blocks cross-site requests to 127.0.0.1.
APP_NONCE = secrets.token_urlsafe(24)

# Hand-off secret: written to a 0600 file the launcher reads, then required on
# /api/open_path. Defence-in-depth beyond the Origin check (which the headless
# launcher passes by sending no Origin).
HANDOFF_SECRET = secrets.token_urlsafe(18)

# Idle auto-shutdown: the browser tab sends a heartbeat every few seconds while
# it is open. When every tab has been closed for IDLE_SHUTDOWN_SEC, the server
# quits so it stops using memory/CPU. Opening a PDF again starts a fresh server.
# Must comfortably exceed the rate at which browsers throttle background-tab
# timers (~once per MINUTE). At the old value of 12s, simply switching away from
# the tab for a moment let the heartbeat lapse and the server shut down — so the
# next action failed with "load failed". 90s keeps a backgrounded tab (which
# still pings ~1/min) alive, while a truly-closed tab (no pings) still exits.
IDLE_SHUTDOWN_SEC = 90
STARTUP_GRACE_SEC = 90      # if the browser never connects, exit after this long
HEARTBEAT = {"last": 0.0, "seen": False}

# ---------------------------------------------------------------------------
# In-memory document state (single working document) + undo stack
# ---------------------------------------------------------------------------
class State:
    def __init__(self):
        self.doc = None
        self.filename = "document.pdf"
        self.path = ""   # full path on disk when known (argv / Open With)
        self.undo = []   # list of (label, pdf_bytes)
        self.epoch = 0   # bumped on every document change (cache key + tab sync)
        self.locked = False        # True when a password-protected PDF awaits a password
        self.locked_data = None    # raw encrypted bytes held until authenticated
        self.dirty = False         # True when the document has changes not yet Saved

    def open_bytes(self, data, filename="document.pdf", path=""):
        doc = fitz.open(stream=data, filetype="pdf")   # may raise -> old doc untouched
        # Free the previously-open document, but only once the new one has been
        # parsed successfully (so a damaged upload never closes the doc the user
        # still has open). PyMuPDF docs hold C-level memory; relying on GC alone
        # let repeated opens retain old documents until finalisation.
        old = self.doc
        # Password-protected PDFs open but cannot be rendered/edited until
        # authenticated. Try an empty owner/user password first; if that fails,
        # hold the bytes in a "locked" state so the UI can prompt for a password
        # (rather than crashing or silently failing).
        if doc.is_encrypted and not doc.authenticate(""):
            doc.close()
            if old is not None:
                old.close()
            self.doc = None
            self.locked = True
            self.locked_data = data
            self.filename = filename or "document.pdf"
            self.path = path or ""
            self.undo = []
            self.dirty = False
            self.epoch += 1
            return
        if old is not None:
            old.close()
        self.doc = doc
        self.locked = False
        self.locked_data = None
        self.filename = filename or "document.pdf"
        self.path = path or ""
        self.undo = []
        self.dirty = False
        self.epoch += 1

    def authenticate(self, password):
        """Unlock a previously-loaded password-protected PDF with `password`.
        Returns True on success, False if the password is wrong. The unlocked
        document is decrypted in memory so it can be edited and saved freely."""
        if not self.locked or self.locked_data is None:
            return True
        doc = fitz.open(stream=self.locked_data, filetype="pdf")
        if doc.is_encrypted and not doc.authenticate(password or ""):
            doc.close()
            return False
        out = doc.tobytes(garbage=3, deflate=True, encryption=fitz.PDF_ENCRYPT_NONE)
        doc.close()
        if self.doc is not None:
            self.doc.close()
        self.doc = fitz.open(stream=out, filetype="pdf")
        self.locked = False
        self.locked_data = None
        self.undo = []
        # The decrypted, password-free copy only exists in memory until Saved.
        self.dirty = True
        self.epoch += 1
        return True

    def close(self):
        """Close the working document and release all memory it holds."""
        if self.doc is not None:
            self.doc.close()
        self.doc = None
        self.filename = "document.pdf"
        self.path = ""
        self.undo = []
        self.locked = False
        self.locked_data = None
        self.dirty = False
        self.epoch += 1

    def require(self):
        if self.doc is None:
            raise RuntimeError("No PDF is open.")
        return self.doc

    def snapshot(self, label):
        """Save the current document so the next change can be undone."""
        if self.doc is None:
            return
        self.undo.append((label, self.doc.tobytes(), self.filename))
        if len(self.undo) > UNDO_LIMIT:
            self.undo.pop(0)
        # Cap by total memory as well as steps (always keep the newest snapshot).
        total = sum(len(t[1]) for t in self.undo)
        while len(self.undo) > 1 and total > UNDO_MAX_BYTES:
            dropped = self.undo.pop(0)
            total -= len(dropped[1])

    def pop_undo(self):
        if not self.undo:
            raise RuntimeError("Nothing to undo.")
        label, data, name = self.undo.pop()
        if self.doc is not None:
            self.doc.close()            # release the replaced doc's memory
        self.doc = fitz.open(stream=data, filetype="pdf")
        # undoing a merge / images→PDF also restores the previous file name
        self.filename = name
        return label

    def to_bytes(self, compress=False):
        if compress:
            return self.doc.tobytes(
                garbage=4, deflate=True, clean=True, deflate_images=True
            )
        return self.doc.tobytes(garbage=3, deflate=True)

    def display_size_kb(self):
        """Approximate saved size for the header. Uses garbage=1 instead of the
        garbage=3 of to_bytes(): ~8x faster and the byte count is the same in
        practice. This runs after every operation, so cheap matters more than
        the last few bytes of accuracy."""
        return round(len(self.doc.tobytes(garbage=1, deflate=True)) / 1024, 1)


STATE = State()

# PyMuPDF documents are NOT thread-safe and the server is threaded. Every
# handler that reads or mutates STATE.doc holds this lock for the duration of
# its document work, serialising all engine access. For a single user the cost
# is nil; it removes the races that otherwise surface as failed page renders
# (or worse) when an operation runs while pages are still streaming in.
STATE_LOCK = threading.RLock()


# /api/state is called after every operation AND on every zoom/resize rebuild;
# serialising the whole document each time just to show its size is the single
# hottest wasted cost on large PDFs. Cache it per document version instead.
_SIZE_CACHE = {"epoch": -1, "kb": 0.0}


def doc_size_kb():
    if _SIZE_CACHE["epoch"] != STATE.epoch:
        _SIZE_CACHE["kb"] = STATE.display_size_kb()
        _SIZE_CACHE["epoch"] = STATE.epoch
    return _SIZE_CACHE["kb"]


def safe_filename(name, fallback="document.pdf"):
    """Sanitise a user-chosen download name: strip path separators and control
    characters, refuse hidden/empty names, and ensure a .pdf extension."""
    name = "".join(c for c in str(name or "") if c >= " " and c not in '/\\:')
    name = name.strip().lstrip(".")
    if not name:
        return fallback
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name[:120]


def friendly_error(e):
    """Turn raw engine errors into plain language; keep already-human text."""
    raw = str(e)
    low = raw.lower()
    if ("cannot open" in low or "format error" in low or "no objects found" in low
            or "not a pdf" in low or "syntax error" in low
            or "failed to open stream" in low or "cannot recognize" in low):
        return "This file appears damaged, or isn't really a PDF."
    if "password" in low and "incorrect" not in low and "unlock" not in low:
        return "This PDF is password-protected. Use Unlock PDF to open it."
    if "memory" in low:
        return "This document is too large to process — try compressing it first."
    return raw

# ---------------------------------------------------------------------------
# Font matching helpers (for text edits)
# ---------------------------------------------------------------------------
# PyMuPDF span "flags" bits: 1=superscript, 2=italic, 4=serif, 8=mono, 16=bold
def pick_font(flags):
    bold = int(bool(flags & 16))
    italic = int(bool(flags & 2))
    if flags & 8:
        return {(0, 0): "cour", (1, 0): "cobo", (0, 1): "coit", (1, 1): "cobi"}[(bold, italic)]
    if flags & 4:
        return {(0, 0): "tiro", (1, 0): "tibo", (0, 1): "tiit", (1, 1): "tibi"}[(bold, italic)]
    return {(0, 0): "helv", (1, 0): "hebo", (0, 1): "heit", (1, 1): "hebi"}[(bold, italic)]


def int_to_rgb(color):
    if color is None:
        return (0, 0, 0)
    return (((color >> 16) & 255) / 255.0, ((color >> 8) & 255) / 255.0, (color & 255) / 255.0)


# ---------------------------------------------------------------------------
# Image helper: knock out a (near) white background -> transparent
# ---------------------------------------------------------------------------
def knockout_white(img_bytes, thresh=238):
    try:
        src = fitz.Pixmap(img_bytes)
        if src.alpha:
            src = fitz.Pixmap(src, 0)               # drop existing alpha
        if src.colorspace is None or src.colorspace.n != 3:
            src = fitz.Pixmap(fitz.csRGB, src)      # normalise to RGB
        w, h = src.width, src.height
        if _np is not None:
            # Vectorised: ~6x faster than the per-pixel loop on a 1.4 MP image.
            rgb = _np.frombuffer(src.samples, dtype=_np.uint8).reshape(h * w, 3)
            alpha = _np.where((rgb >= thresh).all(axis=1), 0, 255).astype(_np.uint8)
            rgba = _np.empty((h * w, 4), dtype=_np.uint8)
            rgba[:, :3] = rgb
            rgba[:, 3] = alpha
            return fitz.Pixmap(fitz.csRGB, w, h, rgba.tobytes(), 1).tobytes("png")
        rgb = src.samples
        out = bytearray(w * h * 4)
        for i in range(w * h):
            r, g, b = rgb[i * 3], rgb[i * 3 + 1], rgb[i * 3 + 2]
            out[i * 4] = r
            out[i * 4 + 1] = g
            out[i * 4 + 2] = b
            out[i * 4 + 3] = 0 if (r >= thresh and g >= thresh and b >= thresh) else 255
        return fitz.Pixmap(fitz.csRGB, w, h, bytes(out), 1).tobytes("png")
    except Exception:
        return img_bytes  # fall back to original if anything goes wrong


# ---------------------------------------------------------------------------
# PDF operations
# ---------------------------------------------------------------------------
def render_page(page_num, zoom=None, target_w=None, fmt="png"):
    doc = STATE.require()
    page_num = int(page_num)
    if page_num < 0 or page_num >= doc.page_count:   # no silent negative-index wrap
        raise RuntimeError("Page %d is out of range." % (page_num + 1))
    page = doc[page_num]
    if target_w:
        z = max(0.1, min(8.0, float(target_w) / page.rect.width))
    else:
        z = float(zoom or 1.5)
    pix = page.get_pixmap(matrix=fitz.Matrix(z, z), alpha=False)
    if fmt == "jpeg":
        # JPEG encodes far faster than PNG and transfers smaller -> quicker open.
        # Quality 90: visibly crisper text than the old 82, still compact.
        return pix.tobytes("jpeg", jpg_quality=90), pix.width, pix.height, "image/jpeg"
    return pix.tobytes("png"), pix.width, pix.height, "image/png"


# Cache content bboxes per (document epoch, page number): build_print_pdf calls
# content_bbox once per page on every print, so a re-print is free.
_BBOX_CACHE = OrderedDict()
_BBOX_CACHE_MAX = 512


def content_bbox(page, pad=6.0):
    """Tight bounding box of the page's content (text, vector drawings, images),
    derived directly from the page's object geometry — no rasterisation. Returns
    a fitz.Rect in page-point coordinates; falls back to the full page if empty.

    This replaced a pure-Python per-pixel scan (~6x faster on content-heavy
    pages, and no pixmap allocation). The object extent can be a hair larger than
    the visual ink extent, which only ever makes fit-to-page slightly more
    conservative (never clips)."""
    key = (STATE.epoch, page.number)
    hit = _BBOX_CACHE.get(key)
    if hit is not None:
        _BBOX_CACHE.move_to_end(key)
        return hit
    box = None

    def add(r):
        nonlocal box
        r = fitz.Rect(r)
        if r.is_empty or r.is_infinite:
            return
        box = r if box is None else (box | r)

    for b in page.get_text("blocks"):
        add(b[:4])
    try:
        for dr in page.get_drawings():
            add(dr["rect"])
    except Exception:
        pass
    try:
        for im in page.get_image_info():
            add(im["bbox"])
    except Exception:
        pass
    try:                                              # annotations (stamps, widgets, notes)
        for an in (page.annots() or []):
            add(an.rect)
    except Exception:
        pass

    if box is None:                                   # blank page
        result = page.rect
    else:
        result = fitz.Rect(box.x0 - pad, box.y0 - pad, box.x1 + pad, box.y1 + pad) & page.rect
        # If content already spans almost the whole sheet, snap to the full page
        # rather than risk clipping a hair off an edge the geometry scan
        # under-measured (the fill-to-page print would only be marginally less
        # aggressive, never wrong).
        pr = page.rect
        if result.width >= 0.94 * pr.width and result.height >= 0.94 * pr.height:
            result = pr

    _BBOX_CACHE[key] = result
    if len(_BBOX_CACHE) > _BBOX_CACHE_MAX:
        _BBOX_CACHE.popitem(last=False)
    return result


def build_print_pdf(margin=24.0):
    """A print-optimised copy of the current document: every page's content is
    scaled to FILL the printable area (page minus `margin` pts ≈ 0.33"), centred,
    with aspect ratio preserved. This removes the need to bump the print dialog's
    Scaling — content already fills the sheet at 100%. Stays fully vector
    (show_pdf_page embeds the source page as a form XObject) so text/lines stay
    sharp. The small margin keeps content clear of printers' non-printable edge."""
    src = STATE.require()
    out = fitz.open()
    try:
        for pno in range(src.page_count):
            sp = src[pno]
            pr = sp.rect
            cb = content_bbox(sp)
            if cb.is_empty or cb.width <= 1 or cb.height <= 1:
                cb = pr
            npg = out.new_page(width=pr.width, height=pr.height)
            printable = fitz.Rect(margin, margin, pr.width - margin, pr.height - margin)
            scale = min(printable.width / cb.width, printable.height / cb.height)
            tw, th = cb.width * scale, cb.height * scale
            tx = printable.x0 + (printable.width - tw) / 2
            ty = printable.y0 + (printable.height - th) / 2
            npg.show_pdf_page(fitz.Rect(tx, ty, tx + tw, ty + th), src, pno, clip=cb)
        return out.tobytes()
    finally:
        out.close()


# Small LRU cache of rendered viewer pages. Keyed by (epoch, page, width) so any
# document change (epoch bump) transparently invalidates stale renders.
_RENDER_CACHE = OrderedDict()
_RENDER_CACHE_MAX = 64


def render_page_cached(page_num, target_w):
    key = (STATE.epoch, int(page_num), int(target_w))
    hit = _RENDER_CACHE.get(key)
    if hit is not None:
        _RENDER_CACHE.move_to_end(key)
        return hit
    val = render_page(page_num, target_w=target_w, fmt="jpeg")
    _RENDER_CACHE[key] = val
    if len(_RENDER_CACHE) > _RENDER_CACHE_MAX:
        _RENDER_CACHE.popitem(last=False)
    return val


def page_sizes():
    doc = STATE.require()
    return [[round(p.rect.width, 2), round(p.rect.height, 2)] for p in doc]


def page_spans(page_num):
    doc = STATE.require()
    page_num = int(page_num)
    if page_num < 0 or page_num >= doc.page_count:
        raise RuntimeError("Page %d is out of range." % (page_num + 1))
    page = doc[page_num]
    spans, idx = [], 0
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                spans.append({
                    "index": idx,
                    "text": span.get("text", ""),
                    "bbox": list(span.get("bbox", [0, 0, 0, 0])),
                    "origin": list(span.get("origin", [0, 0])),
                    "font": span.get("font", ""),
                    "size": span.get("size", 11),
                    "flags": span.get("flags", 0),
                    "color": span.get("color", 0),
                })
                idx += 1
    return spans


def find_text(query, max_hits=2000):
    """Find every occurrence of `query` in the document. Returns a flat list of
    {page, rect:[x0,y0,x1,y1]} in page-point coordinates (the viewer scales them
    to CSS pixels the same way it positions spans). Case-insensitive via
    PyMuPDF's search. Capped so a 1-letter query on a huge book can't return a
    million hits."""
    doc = STATE.require()
    q = (query or "").strip()
    if not q:
        return []
    out = []
    for i in range(doc.page_count):
        try:
            rects = doc[i].search_for(q)
        except Exception:
            rects = []
        for r in rects:
            out.append({"page": i, "rect": [round(r.x0, 2), round(r.y0, 2),
                                            round(r.x1, 2), round(r.y1, 2)]})
            if len(out) >= max_hits:
                return out
    return out


def extract_text(page_num=None):
    """Plain selectable text of one page (page_num) or the whole document
    (page_num is None). Used by the 'Text' panel so users can read/copy the
    document's text even though the viewer shows rendered page images."""
    doc = STATE.require()
    if page_num is None:
        return "\n\n".join(doc[i].get_text("text") for i in range(doc.page_count))
    page_num = int(page_num)
    if page_num < 0 or page_num >= doc.page_count:
        raise RuntimeError("Page %d is out of range." % (page_num + 1))
    return doc[page_num].get_text("text")


def _norm_font(name):
    """Normalise a font name for matching: drop a 'ABCDEF+' subset tag, spaces
    and case. get_text reports a font by its resource or PostScript name, which
    may differ from get_fonts' basefont ('DejaVuSans' vs 'DejaVu Sans Book')."""
    s = name or ""
    if len(s) > 7 and s[6] == "+" and s[:6].isalpha():
        s = s[7:]
    return s.replace(" ", "").replace("-", "").lower()


def _reuse_embedded_font(page, span_font_name, new_text):
    """If the span's font is embedded in the page AND covers every character in
    new_text, register it on the page and return its fontname so the edit keeps
    the original typeface. Returns None when it can't be reused safely (Type1 /
    base-14 fonts have no buffer; subsetted fonts often lack the new glyphs), in
    which case the caller falls back to the closest base-14 family."""
    try:
        doc = page.parent
        want = _norm_font(span_font_name)
        if len(want) < 3:
            return None
        # The name get_text reports (PostScript name) can differ from get_fonts'
        # basefont ('DejaVuSans' vs 'DejaVu Sans Book') and from the resource
        # name. Rank candidates: exact normalised match first, then substring
        # (same family). Skip non-embeddable (Type1 / base-14) fonts.
        cands = []
        for f in page.get_fonts(full=True):
            xref, ext, basefont, refname = f[0], f[1], f[3], f[4]
            if ext not in ("ttf", "otf", "cff", "ttc"):
                continue
            nb, nr = _norm_font(basefont), _norm_font(refname)
            exact = want in (nb, nr)
            fuzzy = (want in nb or nb in want or want in nr or nr in want)
            if exact or fuzzy:
                cands.append((0 if exact else 1, xref))
        cands.sort()
        for _, xref in cands:
            info = doc.extract_font(xref)
            buf = info[3] if info and len(info) > 3 else None
            if not buf:
                continue
            try:
                probe = fitz.Font(fontbuffer=buf)
            except Exception:
                continue
            if any(ch.strip() and not probe.has_glyph(ord(ch)) for ch in new_text):
                continue                 # subset is missing a needed glyph
            fontname = "ed%d" % xref
            page.insert_font(fontname=fontname, fontbuffer=buf)
            return fontname
    except Exception:
        pass
    return None


def _bg_fill(page, bbox):
    """Background colour just around a text span, so the cover patch left by an
    edit blends with coloured cells/headers instead of showing a white box.
    Samples a few points around the span and uses a consistent LIGHT colour;
    falls back to white when uncertain (a wrong dark fill looks worse)."""
    try:
        r = fitz.Rect(bbox)
        pr = page.rect
        pad = 3.0
        pts = []
        for x, y in (
            (r.x0 - pad, (r.y0 + r.y1) / 2), (r.x1 + pad, (r.y0 + r.y1) / 2),
            ((r.x0 + r.x1) / 2, r.y0 - pad), ((r.x0 + r.x1) / 2, r.y1 + pad),
        ):
            if pr.x0 <= x <= pr.x1 and pr.y0 <= y <= pr.y1:
                pix = page.get_pixmap(clip=fitz.Rect(x - 0.5, y - 0.5, x + 0.5, y + 0.5),
                                      colorspace=fitz.csRGB, alpha=False)
                if pix.width and pix.height:
                    s = pix.samples
                    pts.append((s[0], s[1], s[2]))
        light = [c for c in pts if sum(c) >= 360]    # avg ≥ ~0.47 (not text/border)
        if len(light) >= 2:
            (rr, gg, bb), _ = Counter(light).most_common(1)[0]
            return (rr / 255.0, gg / 255.0, bb / 255.0)
    except Exception:
        pass
    return (1, 1, 1)


# Names that mean "a standard font" — when the original span uses one of these,
# the base-14 fallback is visually equivalent, so no fidelity warning is needed.
_STANDARD_FONT_HINTS = ("helvetica", "arial", "times", "timesnewroman", "courier",
                        "couriernew", "symbol", "zapfdingbats", "liberation", "nimbus")


def _font_is_standardish(name):
    n = _norm_font(name)
    return (not n) or any(h in n for h in _STANDARD_FONT_HINTS)


def _is_ocr_glyphless(font_name):
    """True when a span belongs to an OCR text layer (Tesseract's invisible
    "GlyphLessFont") laid over a scanned image. Such a span has NO visible glyphs
    of its own — the words you see are pixels in the image underneath — so editing
    it the normal way (insert a generic font at the OCR-estimated size) looks
    nothing like the scan. These spans are matched to the image pixels instead."""
    return "glyphless" in (font_name or "").replace(" ", "").lower()


def edit_span(page_num, span_index, new_text):
    doc = STATE.require()
    spans = page_spans(page_num)                 # validates page bounds
    if not (0 <= span_index < len(spans)):
        raise RuntimeError("Span no longer exists - re-open the page.")
    sp = spans[span_index]
    # Snapshot ONLY after validation, so a failed edit never pushes a bogus
    # undo entry (which would otherwise corrupt the next Undo).
    STATE.snapshot("text edit")
    page = doc[page_num]
    # Scanned page that was made searchable with OCR: the clicked "text" is an
    # invisible OCR layer over the image. Match the image pixels (size, baseline,
    # weight, colour) like the Edit-scanned tool, and drop the stale OCR word so
    # it can't reappear. Normal embedded-text editing (below) is unchanged.
    if _is_ocr_glyphless(sp.get("font", "")):
        bbox = fitz.Rect(sp["bbox"])
        ink = _analyze_ink(page, bbox & page.rect, _bg_fill(page, list(bbox)))
        try:                                     # remove only the invisible OCR text
            page.add_redact_annot(bbox, fill=None)
            try:
                page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                                      graphics=fitz.PDF_REDACT_LINE_ART_NONE)
            except TypeError:
                page.apply_redactions()
        except Exception:
            pass                                 # if removal fails, cover still hides it
        _cover_and_write(page, bbox, new_text, bold=None, ink=ink)
        return {"font_preserved": True, "font": sp.get("font", "")}
    page.add_redact_annot(fitz.Rect(sp["bbox"]), fill=_bg_fill(page, sp["bbox"]))
    # Only remove the old TEXT. Without these flags, redaction also deletes any
    # image or vector line-art (e.g. table rules) that merely touches the span's
    # box — so editing one cell could wipe out a whole table border or logo. The
    # white fill still covers the small box itself (fine for the usual black-text
    # on-white case). Guarded for older PyMuPDF that lacks the keyword args.
    try:
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                              graphics=fitz.PDF_REDACT_LINE_ART_NONE)
    except TypeError:
        page.apply_redactions()
    # Keep the original embedded typeface when we safely can; otherwise fall back
    # to the nearest base-14 family by style flags.
    reused = _reuse_embedded_font(page, sp.get("font", ""), new_text)
    fontname = reused or pick_font(sp["flags"])
    page.insert_text(
        fitz.Point(sp["origin"][0], sp["origin"][1]),
        new_text,
        fontname=fontname,
        fontsize=float(sp["size"]) or 11.0,
        color=int_to_rgb(sp["color"]),
    )
    # font_preserved is False only when we fell back AND the original was a custom
    # (non-standard) embedded font — i.e. the edited word may not match its
    # neighbours. Standard fonts (Helvetica/Arial/Times/Courier) re-render exactly.
    return {"font_preserved": bool(reused) or _font_is_standardish(sp.get("font", "")),
            "font": sp.get("font", "")}


# ---------------------------------------------------------------------------
# Edit text that is baked into a scanned IMAGE (no selectable text layer)
# ---------------------------------------------------------------------------
# The normal "click a span" editor only works on real, selectable text. On a
# scanned / photographed page the words are pixels inside an image, so there are
# no spans to click. These two helpers let the user drag a box over such text,
# read what's there (OCR), then cover the old pixels with the page's background
# colour and lay down the replacement text — the same approach used by hand on
# the prescription PDF, generalised into the app.

def _sample_text_color(page, rect, bg):
    """Best-guess colour of the glyphs inside `rect`: render the region and take
    the average of the pixels that are clearly darker than the background. Falls
    back to near-black, which is right for the usual dark-on-light scan."""
    try:
        pix = page.get_pixmap(clip=rect, colorspace=fitz.csRGB, alpha=False, dpi=200)
        s, n = pix.samples, pix.width * pix.height
        if n:
            bg_lum = (bg[0] + bg[1] + bg[2]) * 255 / 3.0
            rs = gs = bs = cnt = 0
            step = pix.n                     # bytes per pixel (3 for RGB)
            for i in range(0, n * step, step):
                r8, g8, b8 = s[i], s[i + 1], s[i + 2]
                if (r8 + g8 + b8) / 3.0 < bg_lum - 55:   # clearly darker than bg
                    rs += r8; gs += g8; bs += b8; cnt += 1
            if cnt >= 8:
                return (rs / cnt / 255.0, gs / cnt / 255.0, bs / cnt / 255.0)
    except Exception:
        pass
    return (0.12, 0.12, 0.13)


def _analyze_ink(page, r, bg, dpi=200):
    """Measure the original glyphs inside `r`: their left edge, baseline, glyph
    height, colour, and whether they're BOLD. This lets the replacement match the
    scanned text's size, position and weight instead of using a fixed guess.
    Bold is judged by median stroke thickness relative to glyph height (calibrated
    on real scans: bold text ~0.21-0.31, regular ~0.12-0.17, so 0.19 splits them).
    Returns None when the box has no clear ink (e.g. adding text to a blank area)."""
    try:
        import statistics
        pix = page.get_pixmap(clip=r, colorspace=fitz.csRGB, alpha=False, dpi=dpi)
        W, H, n = pix.width, pix.height, pix.n
        if W < 3 or H < 3:
            return None
        s = pix.samples
        bg_lum = (bg[0] + bg[1] + bg[2]) * 255 / 3.0
        thr = max(40.0, bg_lum - 80.0)            # a pixel is "ink" below this
        col = [0] * W
        rmin, rmax, cnt = H, -1, 0
        runs = []                                  # horizontal ink run-lengths
        ink_px = []                                # (lum, R, G, B) for ink pixels
        for y in range(H):
            base = y * W * n
            run = 0
            had = False
            for x in range(W):
                p = base + x * n
                R, G, B = s[p], s[p + 1], s[p + 2]
                lum = (R + G + B) / 3.0
                if lum < thr:
                    cnt += 1; col[x] += 1; run += 1; had = True
                    ink_px.append((lum, R, G, B))
                elif run:
                    runs.append(run); run = 0
            if run:
                runs.append(run)
            if had:
                rmin = min(rmin, y); rmax = max(rmax, y)
        if cnt < 6 or rmax < rmin:
            return None
        cmin = next((x for x in range(W) if col[x] >= 2), 0)
        scale = 72.0 / dpi
        gh_px = max(1, rmax - rmin)
        thick = statistics.median(runs) if runs else 0
        # Colour from the DARKEST ~30% of ink pixels (the stroke core). Averaging
        # all ink pixels includes the light anti-aliased edges, which washes a
        # black word out to grey; the darkest fraction tracks the true ink colour.
        ink_px.sort(key=lambda t: t[0])
        k = max(4, int(len(ink_px) * 0.30))
        core = ink_px[:k]
        color = (sum(t[1] for t in core) / len(core) / 255.0,
                 sum(t[2] for t in core) / len(core) / 255.0,
                 sum(t[3] for t in core) / len(core) / 255.0)
        return {
            "x0": r.x0 + cmin * scale,
            "baseline": r.y0 + (rmax + 1) * scale,
            "cap_h": max(1.0, (rmax - rmin + 1) * scale),
            "bold": (thick / gh_px) >= 0.19,
            "color": color,
        }
    except Exception:
        return None


def _cover_and_write(page, region, new_text, bold=None, ink=None):
    """Core of the scanned-text edit (NO snapshot — the caller owns undo): cover
    the pixels in `region` with the background colour, then draw `new_text`
    matched to the ORIGINAL text's size, baseline, left edge, colour and weight so
    it blends in. Pass a pre-computed `ink` (from _analyze_ink) when the caller
    measured the glyphs before they were covered/redacted."""
    r = (fitz.Rect(region).normalize() & page.rect)
    bg = _bg_fill(page, list(r))             # background colour to paint over with
    if ink is None:
        ink = _analyze_ink(page, r, bg)      # measure the text we're replacing
    # Cover the old pixels. A vector rectangle sits on top of the image, hiding
    # the baked-in glyphs (redaction can't remove image pixels, so we paint).
    # Extend the patch a hair beyond the box so antialiased edges of the old
    # glyphs (which can spill just outside it) are swallowed too.
    pad = min(1.5, r.height * 0.18)
    cover = (fitz.Rect(r.x0 - pad, r.y0 - pad, r.x1 + pad, r.y1 + pad)) & page.rect
    page.draw_rect(cover, color=bg, fill=bg, width=0)
    new_text = (new_text or "").replace("\r", " ").replace("\n", " ").strip()
    if not new_text:
        return                               # box cleared, nothing to write back
    if ink:
        # Match the original: glyph height -> font size (Helvetica cap height is
        # ~0.72*size), and reuse its left edge + baseline so it lands in place.
        is_bold = ink["bold"] if bold is None else bool(bold)
        color = ink["color"]
        size = max(4.0, ink["cap_h"] / 0.72)
        x_left = ink["x0"]
        baseline = ink["baseline"]
    else:
        # No ink found (blank area): fall back to fitting the box.
        is_bold = bool(bold)
        color = _sample_text_color(page, r, bg)
        size = max(4.0, r.height * 0.74)
        x_left = r.x0 + 1.0
        f0 = fitz.Font("helv")
        baseline = r.y0 + (r.height - (f0.ascender - f0.descender) * size) / 2.0 + f0.ascender * size
    fontname = "hebo" if is_bold else "helv"
    font = fitz.Font(fontname)
    # Don't squeeze the text into the box — a longer replacement (Male -> Female)
    # needs room to the right, which on a form field is blank space. Only shrink
    # if it would run off the page edge.
    avail = max(10.0, page.rect.x1 - x_left - 2.0)
    tw = font.text_length(new_text, fontsize=size)
    if tw > avail:
        size = max(4.0, size * avail / tw)
    page.insert_text(fitz.Point(x_left, baseline), new_text,
                     fontname=fontname, fontsize=size, color=color)


def sign_page(page_num, rect, img_bytes, remove_white=True, stretch=False):
    doc = STATE.require()
    page_num = int(page_num)
    if page_num < 0 or page_num >= doc.page_count:   # validate before snapshot
        raise RuntimeError("Page %d is out of range." % (page_num + 1))
    page = doc[page_num]
    if remove_white:
        img_bytes = knockout_white(img_bytes)
    # Snapshot only once we're about to actually place the image, so a failed
    # sign (bad page/image) never leaves a bogus undo entry.
    STATE.snapshot("signature")
    page.insert_image(
        fitz.Rect(rect),
        stream=img_bytes,
        keep_proportion=not stretch,
        overlay=True,
    )


def merge_pdfs(files, include_current=False):
    new = fitz.open()
    if include_current and STATE.doc is not None:
        new.insert_pdf(STATE.doc)
    for _name, raw in files:
        src = fitz.open(stream=raw, filetype="pdf")
        new.insert_pdf(src)
        src.close()
    if new.page_count == 0:
        raise RuntimeError("No pages to merge.")
    # the merge replaces the open document — snapshot it first so Undo can
    # bring it back, then close it so its memory is freed (the snapshot holds
    # the bytes Undo needs; the live doc object is no longer referenced).
    if STATE.doc is not None:
        STATE.snapshot("merge")
        STATE.doc.close()
    STATE.doc = new
    STATE.filename = "merged.pdf"
    STATE.path = ""        # a new combined doc — not tied to any file on disk


def unlock_pdf(data, filename="document.pdf", password=""):
    """Open a (possibly encrypted) PDF, authenticate with the given password,
    and load a fully decrypted copy into STATE so it can be viewed and edited.
    Returns True if the source was encrypted, False if it was already open.
    Raises RuntimeError when the password is wrong."""
    doc = fitz.open(stream=data, filetype="pdf")
    was_encrypted = bool(doc.is_encrypted)
    if was_encrypted:
        # authenticate() accepts the user OR owner password; returns 0 on fail.
        if not doc.authenticate(password or ""):
            doc.close()
            raise RuntimeError("Incorrect password — could not unlock this PDF.")
    # Write out with encryption explicitly stripped so the saved/edited file
    # no longer requires a password.
    out = doc.tobytes(garbage=3, deflate=True, encryption=fitz.PDF_ENCRYPT_NONE)
    doc.close()
    name = filename or "document.pdf"
    base, ext = os.path.splitext(name)
    STATE.open_bytes(out, f"{base}_unlocked{ext or '.pdf'}")
    return was_encrypted


# Three compression levels. Each has a target size ceiling and a list of
# image steps from gentlest to most aggressive: (dpi_threshold, dpi_target,
# jpeg_quality). We first try a lossless structural compress; if that already
# meets the target we keep full quality, otherwise we apply the gentlest image
# step that gets under the target (falling back to the most aggressive step).
COMPRESS_PRESETS = {
    # level     target_kb   steps (gentle -> aggressive)
    "high":   (1024, [(220, 170, 88), (180, 140, 80), (150, 120, 72)]),
    "medium": (700,  [(170, 130, 72), (150, 110, 62), (120, 96, 52)]),
    # low: aim to land just under 200 KB (≈180-195) while staying readable.
    # Fine, gentle ramp so the first step under the ceiling keeps the most
    # quality possible; the aggressive tail lets even dense scans reach <200.
    "low":    (200,  [(220, 160, 78), (190, 140, 70), (165, 120, 62),
                      (140, 100, 54), (110, 82, 44), (96, 72, 34),
                      (84, 62, 28), (75, 56, 24), (72, 54, 20)]),
}


def _save_optimized(doc):
    return doc.tobytes(
        garbage=4, deflate=True, deflate_images=True,
        deflate_fonts=True, clean=True,
    )


def compress(level="medium"):
    doc = STATE.require()
    original = doc.tobytes()                       # pristine copy for each attempt
    before = len(STATE.to_bytes(compress=False))

    target_kb, steps = COMPRESS_PRESETS.get(level, COMPRESS_PRESETS["medium"])
    target = target_kb * 1024

    # 1) lossless structural pass — if it already fits the target, keep full quality
    plain_doc = fitz.open(stream=original, filetype="pdf")
    try:
        plain_doc.subset_fonts()
    except Exception:
        pass
    best = plain_doc.tobytes(garbage=4, deflate=True, deflate_fonts=True, clean=True)
    plain_doc.close()

    # 2) otherwise recompress images. Steps run gentle -> aggressive and produce
    #    monotonically smaller output, so we BINARY-SEARCH for the gentlest step
    #    that meets the target instead of trying each in turn — far fewer trials
    #    (≈log2 N), and it avoids the slow high-DPI gentle passes when the target
    #    needs an aggressive step anyway. Same result as the old linear scan:
    #    the gentlest step under the ceiling (or the smallest output if none fit).
    def _try_step(i):
        thr, tgt, q = steps[i]
        trial = fitz.open(stream=original, filetype="pdf")
        try:
            trial.rewrite_images(
                dpi_threshold=thr, dpi_target=tgt, quality=q,
                lossy=True, lossless=True,
            )
        except Exception:
            pass
        try:
            trial.subset_fonts()
        except Exception:
            pass
        data = _save_optimized(trial)
        trial.close()
        return data

    if len(best) > target:
        lo, hi = 0, len(steps) - 1
        chosen = None                              # gentlest step meeting target
        while lo <= hi:
            mid = (lo + hi) // 2
            data = _try_step(mid)
            if len(data) < len(best):
                best = data                        # track smallest as a fallback
            if len(data) <= target:
                chosen = data
                hi = mid - 1                       # a gentler step might also fit
            else:
                lo = mid + 1                       # need a more aggressive step
        if chosen is not None:
            best = chosen                          # prefer gentlest-that-fits over smallest

    # Snapshot only now, right before swapping the document in — if any of the
    # work above had failed, Undo wouldn't be left with a bogus entry.
    STATE.snapshot("compress")
    STATE.doc.close()                          # free the pre-compress doc's memory
    STATE.doc = fitz.open(stream=best, filetype="pdf")
    return before, len(best), target_kb


def image_format(raw):
    """Best-effort image-format name from the file's magic bytes, or None if the
    bytes aren't a recognised image. Used to route 'Open' on an image to the
    image importer and to name the format in error messages."""
    if not raw:
        return None
    if raw[:3] == b"\xff\xd8\xff":
        return "JPEG"
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "PNG"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "GIF"
    if raw[:2] == b"BM":
        return "BMP"
    if raw[:4] in (b"II*\x00", b"MM\x00*"):
        return "TIFF"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "WEBP"
    if len(raw) >= 12 and raw[4:8] == b"ftyp":
        brand = raw[8:12]
        if brand in (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1"):
            return "HEIC"
        if brand == b"avif":
            return "AVIF"
    return None


def _open_image(name, raw):
    """Open one image as a 1-page document, robustly. Tries the document opener
    first (keeps the original encoding for a lossless embed); if that fails,
    falls back to decoding via Pixmap and re-encoding as PNG. Raises a clear,
    image-named error if the bytes simply aren't a readable image. Returns
    (img_doc, rect, raw_bytes)."""
    if raw[:5] == b"%PDF-":
        raise RuntimeError(
            "“%s” is a PDF, not an image. Use “Open” (or “Merge PDFs”) for PDF "
            "files." % (name or "file"))
    try:
        img = fitz.open(stream=raw, filetype=None)
        if img.page_count >= 1 and not img[0].rect.is_empty:
            return img, img[0].rect, raw
        img.close()
    except Exception:
        pass
    # Fallback: let the pixmap decoder try (covers some formats / odd encodings
    # the document opener rejects), then embed the normalised PNG.
    try:
        pix = fitz.Pixmap(raw)
        if pix.alpha:
            pix = fitz.Pixmap(pix, 0)
        if pix.colorspace is None or pix.colorspace.n not in (1, 3):
            pix = fitz.Pixmap(fitz.csRGB, pix)
        png = pix.tobytes("png")
        img = fitz.open(stream=png, filetype=None)
        return img, img[0].rect, png
    except Exception:
        pass
    # Last resort on macOS: `sips` (built into every Mac) decodes formats the
    # PDF engine can't — notably HEIC (iPhone photos) and WEBP — so convert to
    # PNG and embed that. Silently skipped if sips is unavailable (e.g. Linux).
    png = _sips_to_png(raw)
    if png is not None:
        try:
            img = fitz.open(stream=png, filetype=None)
            return img, img[0].rect, png
        except Exception:
            pass
    fmt = image_format(raw)
    detail = (" (detected %s)" % fmt) if fmt else ""
    raise RuntimeError(
        "Couldn’t read “%s”%s as an image. Please use a PNG or JPEG — some "
        "camera/RAW formats aren’t supported; convert it to JPEG first."
        % (name or "image", detail))


def _sips_to_png(raw):
    """Convert arbitrary image bytes to PNG via macOS `sips`. Returns PNG bytes
    or None if sips is missing or the conversion fails (e.g. truly corrupt data
    or a non-macOS host)."""
    if not shutil.which("sips"):
        return None
    try:
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "in.img")
            dst = os.path.join(td, "out.png")
            with open(src, "wb") as fh:
                fh.write(raw)
            r = subprocess.run(
                ["sips", "-s", "format", "png", src, "--out", dst],
                capture_output=True, timeout=60)
            if r.returncode == 0 and os.path.exists(dst):
                with open(dst, "rb") as fh:
                    return fh.read()
    except Exception:
        pass
    return None


def create_from_images(images, quality="normal"):
    """Build a new PDF from images. quality="normal" embeds each image at FULL
    resolution, losslessly (no re-encode) — the source pixels are preserved
    exactly. quality="small" trades some sharpness for a lighter file: it caps
    the long edge at ~2400px (only downscaling images larger than that) and
    re-encodes as high-quality JPEG, but only keeps it when it's actually
    smaller than the original."""
    new = fitz.open()
    try:
        for name, raw in images:
            img, rect, raw = _open_image(name, raw)
            try:
                if quality == "small":
                    # 2400px long edge keeps invoice text readable; q88 avoids the
                    # blocky JPEG mush the old 1600px/q72 produced.
                    scale = min(1.0, 2400.0 / max(rect.width, rect.height))
                    pm = img[0].get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                    jpg = pm.tobytes("jpeg", jpg_quality=88)
                    # only keep the JPEG when it actually IS smaller (graphics-heavy
                    # PNGs can re-encode larger; photos shrink a lot)
                    if len(jpg) < len(raw):
                        img.close()
                        img = fitz.open(stream=jpg, filetype=None)
                pdfbytes = img.convert_to_pdf()
            finally:
                img.close()
            imgpdf = fitz.open("pdf", pdfbytes)
            page = new.new_page(width=rect.width, height=rect.height)
            page.show_pdf_page(page.rect, imgpdf, 0)
            imgpdf.close()
        if new.page_count == 0:
            raise RuntimeError("No valid images provided.")
    except Exception:
        new.close()                                 # don't leak a half-built doc
        raise
    # replaces the open document — keep it one Undo away, then close the live
    # doc so its memory is freed (Undo restores from the snapshot bytes).
    if STATE.doc is not None:
        STATE.snapshot("create from images")
        STATE.doc.close()
    STATE.doc = new
    STATE.filename = "from_images.pdf"
    STATE.path = ""        # a new doc built from images — no file on disk yet


def delete_pages(pages):
    doc = STATE.require()
    pages = sorted(set(int(p) for p in pages))
    if len(pages) >= doc.page_count:
        raise RuntimeError("Cannot delete every page.")
    STATE.snapshot("delete pages")
    doc.delete_pages(pages)


def reorder_pages(order, rotations=None):
    """Apply a new page order, optional per-position rotations (degrees,
    multiples of 90, aligned with `order`), and deletions — pages NOT listed
    in `order` are removed. All one undoable step."""
    doc = STATE.require()
    order = [int(x) for x in order]
    if not order:
        raise RuntimeError("Keep at least one page.")
    if len(set(order)) != len(order) or any(x < 0 or x >= doc.page_count for x in order):
        raise RuntimeError("Page order is invalid.")
    rotations = [int(r) % 360 for r in (rotations or [])]
    if rotations and len(rotations) != len(order):
        raise RuntimeError("Rotations must give one angle per page.")
    if any(r % 90 for r in rotations):
        raise RuntimeError("Rotations must be multiples of 90 degrees.")
    STATE.snapshot("organize pages")
    doc.select(order)
    for pos, deg in enumerate(rotations):
        if deg:
            page = doc[pos]
            page.set_rotation((page.rotation + deg) % 360)


def copy_pages(pages):
    """Copy the chosen pages into a brand-new PDF and return its bytes.
    The open working document is not modified."""
    doc = STATE.require()
    pages = sorted({int(x) for x in pages})
    if not pages or pages[0] < 0 or pages[-1] >= doc.page_count:
        raise RuntimeError("Pick at least one valid page to copy.")
    out = fitz.open()
    for p in pages:
        out.insert_pdf(doc, from_page=p, to_page=p)
    data = out.tobytes(garbage=3, deflate=True)
    out.close()
    return data


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _write(self, data):
        # The browser cancels in-flight page fetches all the time (scrolling,
        # zoom, rebuild). Swallow the resulting disconnects instead of spewing
        # BrokenPipe/ConnectionReset tracebacks to the console.
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _origin_ok(self):
        """Block browser cross-site requests. Same-origin requests and non-browser
        clients (the single-instance hand-off via urllib) send no foreign Origin
        and are allowed; a page on another site always sends its Origin and is
        rejected."""
        for h in ("Origin", "Referer"):
            v = self.headers.get(h)
            if v:
                try:
                    host = urllib.parse.urlparse(v).hostname
                except Exception:
                    return False
                if host not in (None, "127.0.0.1", "localhost"):
                    return False
        return True

    def _token_ok(self):
        return secrets.compare_digest(self.headers.get("X-PyPDF-Token", ""), APP_NONCE)

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        # Never let the browser cache the app HTML/JSON, otherwise a rebuilt
        # app keeps showing the previous version's UI from disk cache.
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self._write(body)
        return True

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        if length > MAX_REQUEST_BYTES:
            raise RuntimeError(
                "Upload too large (limit %d MB)." % (MAX_REQUEST_BYTES // (1024 * 1024)))
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode() or "{}")

    def _err(self, msg, code=400):
        self._send(code, {"ok": False, "error": str(msg)})

    def _download(self, data, filename, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self._write(data)
        return True

    def do_GET(self):
        try:
            path = self.path.split("?")[0]
            query = {}
            if "?" in self.path:
                for kv in self.path.split("?", 1)[1].split("&"):
                    if "=" in kv:
                        k, v = kv.split("=", 1)
                        query[k] = v

            if path in ("/", "/index.html"):
                html = INDEX_HTML.replace("__APP_NONCE__", APP_NONCE)
                return self._send(200, html, "text/html; charset=utf-8")

            if not self._origin_ok():
                return self._err("Cross-origin request blocked.", 403)

            if path == "/api/ping":
                # Every ping is also a heartbeat: it tells the idle watchdog a
                # browser tab is still open. Kept outside the document lock so the
                # heartbeat keeps flowing even during a long operation.
                HEARTBEAT["last"] = time.time()
                HEARTBEAT["seen"] = True
                return self._send(200, {"ok": True, "app": APP_TOKEN, "epoch": STATE.epoch})

            # Everything below touches the document — serialise engine access.
            with STATE_LOCK:
                return self._get_doc(path, query)
        except Exception as e:  # noqa
            return self._err(friendly_error(e), 500)

    def _get_doc(self, path, query):
            if path == "/api/state":
                if STATE.locked:
                    return self._send(200, {
                        "ok": True, "open": False, "locked": True,
                        "filename": STATE.filename, "path": STATE.path,
                        "epoch": STATE.epoch,
                    })
                if STATE.doc is None:
                    return self._send(200, {"ok": True, "open": False, "epoch": STATE.epoch})
                resp = {
                    "ok": True, "open": True,
                    "epoch": STATE.epoch,
                    "pages": STATE.doc.page_count,
                    "filename": STATE.filename,
                    "path": STATE.path,
                    "size_kb": doc_size_kb(),
                    "can_undo": len(STATE.undo) > 0,
                    "dirty": STATE.dirty,
                }
                # Per-page sizes/rotations only change when the document does.
                # The client passes ?se=<epoch it already has them for>; when that
                # matches we omit the arrays (≈18 KB on a 1000-page doc) and the
                # client reuses its cached copy — e.g. on every zoom rebuild.
                have = query.get("se")
                if not (have is not None and have.lstrip("-").isdigit() and int(have) == STATE.epoch):
                    resp["sizes"] = page_sizes()
                    resp["rotations"] = [p.rotation for p in STATE.doc]
                return self._send(200, resp)

            if path == "/api/page":
                n = int(query.get("n", 0))
                w = query.get("w")
                if w and not query.get("zoom"):
                    img, pw, ph, mime = render_page_cached(n, w)
                else:
                    img, pw, ph, mime = render_page(n, zoom=query.get("zoom"), target_w=w,
                                                    fmt=query.get("fmt", "jpeg"))
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("X-Page-Width", str(pw))
                self.send_header("X-Page-Height", str(ph))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(img)))
                self.end_headers()
                self._write(img)
                return True

            if path == "/api/spans":
                return self._send(200, {"ok": True, "spans": page_spans(int(query.get("n", 0)))})

            if path == "/api/find":
                q = urllib.parse.unquote_plus(query.get("q", ""))
                return self._send(200, {"ok": True, "matches": find_text(q)})

            if path == "/api/text":
                txt = extract_text(None) if query.get("all") else extract_text(int(query.get("n", 0)))
                return self._send(200, {"ok": True, "text": txt})

            if path in ("/api/export", "/api/save"):
                # Pure download — this GET never mutates STATE. GETs are reachable
                # without the CSRF token (a cross-site page with no Referer passes
                # the Origin check), so the rename + dirty-clear that "Save a copy"
                # implies now happen via the tokened POST /api/mark_saved the page
                # calls right after. The `name` here only sets the download filename.
                name = query.get("name")
                name = safe_filename(urllib.parse.unquote_plus(name), STATE.filename) if name else STATE.filename
                return self._download(STATE.to_bytes(), name, "application/pdf")

            if path == "/api/pdf":
                # The live document served INLINE (not as a download) so the
                # browser can render it natively for vector-quality printing.
                # fit=1 -> content scaled to fill the page (still vector).
                data = build_print_pdf() if query.get("fit") else STATE.to_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Disposition", 'inline; filename="document.pdf"')
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self._write(data)
                return True

            if path == "/api/about":
                return self._send(200, {
                    "ok": True,
                    "version": APP_VERSION,
                    "engine": "PyMuPDF " + str(getattr(fitz, "VersionBind", "")).strip() or "PyMuPDF",
                    "python": sys.version.split()[0],
                    "started": SERVER_STARTED,
                })

            if path == "/api/copy_pages":
                sel = [int(x) for x in query.get("pages", "").split(",") if x != ""]
                data = copy_pages(sel)
                name = os.path.splitext(STATE.filename)[0] + "_pages.pdf"
                return self._download(data, safe_filename(name), "application/pdf")

            if path == "/api/export_png":
                n = int(query.get("n", 0))
                # zoom 4.0 ≈ 288 dpi — sharp, print-grade PNG (was 2.0 ≈ 144 dpi,
                # which softened text). Capped to keep huge pages sane.
                z = min(6.0, max(1.0, float(query.get("zoom", 4.0))))
                png, _w, _h, _m = render_page(n, zoom=z, fmt="png")
                name = os.path.splitext(STATE.filename)[0] + f"_p{n+1}.png"
                return self._download(png, name, "image/png")

            return self._err("Unknown endpoint: " + path, 404)

    def do_POST(self):
        path = self.path.split("?")[0]
        # CSRF: reject browser cross-site requests, and require the per-process
        # token on mutating endpoints. The single-instance hand-off to
        # /api/open_path comes from urllib (no Origin, no token) and is allowed
        # by the Origin check alone.
        if not self._origin_ok():
            return self._err("Cross-origin request blocked.", 403)
        if path == "/api/open_path":
            # Hand-off from the launcher: authorise with the per-session secret
            # (read from a 0600 file) instead of the page token, which it can't know.
            if self.headers.get("X-PyPDF-Handoff", "") != HANDOFF_SECRET:
                return self._err("Unauthorised hand-off.", 403)
        elif not self._token_ok():
            return self._err("Missing or invalid request token.", 403)
        # Some POSTs don't change rendered content (just the saved-state flag), so
        # they must NOT bump the epoch — otherwise saving a copy would thrash the
        # render cache, drop search highlights, and make other tabs reload.
        bump = path not in ("/api/mark_saved",)
        try:
            with STATE_LOCK:
                # Optimistically bump the change counter; roll it back if the
                # operation fails, so a failed request never thrashes the render
                # cache or signals a phantom change to other tabs.
                if bump:
                    STATE.epoch += 1
                resp = self._post_doc(path)
                if resp is None:                       # unknown endpoint
                    if bump:
                        STATE.epoch -= 1
                    return self._err("Unknown endpoint: " + path, 404)
                return resp
        except Exception as e:  # noqa
            if bump:
                STATE.epoch -= 1
            return self._err(friendly_error(e), 500)

    def _post_doc(self, path):
            if path == "/api/open_path":
                # Used by single-instance hand-off: load a PDF already on disk.
                p = self._read_json()
                with open(p["path"], "rb") as fh:
                    raw = fh.read()
                fname = os.path.basename(p["path"])
                if image_format(raw):                 # an image — build a PDF from it
                    create_from_images([(fname, raw)])
                    STATE.dirty = True
                    return self._send(200, {"ok": True, "pages": STATE.doc.page_count, "epoch": STATE.epoch})
                STATE.open_bytes(raw, fname, path=os.path.abspath(p["path"]))
                if STATE.locked:   # password-protected: the UI prompts for it
                    return self._send(200, {"ok": True, "locked": True, "pages": 0, "epoch": STATE.epoch})
                return self._send(200, {"ok": True, "pages": STATE.doc.page_count, "epoch": STATE.epoch})

            if path == "/api/open":
                p = self._read_json()
                raw = base64.b64decode(p["data_b64"])
                fname = p.get("filename", "document.pdf")
                # If the user opens an IMAGE (by mistake or on purpose), don't fail
                # with "damaged PDF" — build a one-page PDF from it instead.
                if image_format(raw):
                    create_from_images([(fname, raw)])
                    STATE.dirty = True
                    return self._send(200, {"ok": True, "pages": STATE.doc.page_count})
                STATE.open_bytes(raw, fname)
                if STATE.locked:   # password-protected: the UI prompts for it
                    return self._send(200, {"ok": True, "locked": True, "pages": 0})
                return self._send(200, {"ok": True, "pages": STATE.doc.page_count})

            if path == "/api/save_inplace":
                # Overwrite the original file on disk (only for documents opened
                # from a real path; uploads / merged / image-built docs have none).
                if not STATE.path:
                    raise RuntimeError("This document isn't tied to a file on disk — use “Save a copy”.")
                data = STATE.to_bytes()
                try:
                    with open(STATE.path, "wb") as fh:
                        fh.write(data)
                except OSError as e:
                    raise RuntimeError("Couldn't save to “%s” (%s)."
                                       % (os.path.basename(STATE.path), e.strerror or "write error"))
                STATE.dirty = False
                return self._send(200, {"ok": True, "path": STATE.path,
                                        "name": os.path.basename(STATE.path)})

            if path == "/api/mark_saved":
                # Called by the page after a "Save a copy" download begins: record
                # the chosen name and clear the unsaved-changes flag. Token-guarded
                # (it's a POST), so a cross-site GET can no longer flip these. Does
                # not bump the epoch (see do_POST) — nothing rendered changed.
                p = self._read_json()
                nm = p.get("name")
                if nm:
                    STATE.filename = safe_filename(nm, STATE.filename)
                STATE.dirty = False
                return self._send(200, {"ok": True, "filename": STATE.filename})

            if path == "/api/edit_text":
                p = self._read_json()
                info = edit_span(int(p["page"]), int(p["span_index"]), p["new_text"]) or {}
                STATE.dirty = True
                return self._send(200, {"ok": True,
                                        "font_preserved": info.get("font_preserved", True),
                                        "font": info.get("font", "")})

            if path == "/api/sign":
                p = self._read_json()
                sign_page(
                    int(p["page"]), p["rect"], base64.b64decode(p["data_b64"]),
                    remove_white=bool(p.get("remove_white", True)),
                    stretch=bool(p.get("stretch", False)),
                )
                STATE.dirty = True
                return self._send(200, {"ok": True})

            if path == "/api/undo":
                label = STATE.pop_undo()
                STATE.dirty = True
                return self._send(200, {"ok": True, "undone": label, "pages": STATE.doc.page_count})

            if path == "/api/close":
                STATE.close()
                _RENDER_CACHE.clear()
                return self._send(200, {"ok": True, "epoch": STATE.epoch})

            if path == "/api/authenticate":
                # Provide the password for a PDF that was opened in a locked
                # state (double-clicked / Open With). On success the document
                # becomes a normal editable doc; on failure the UI re-prompts.
                p = self._read_json()
                ok = STATE.authenticate(p.get("password", ""))
                if not ok:
                    return self._send(200, {"ok": True, "authenticated": False})
                return self._send(200, {
                    "ok": True, "authenticated": True,
                    "pages": STATE.doc.page_count, "filename": STATE.filename,
                })

            if path == "/api/unlock":
                p = self._read_json()
                was_enc = unlock_pdf(
                    base64.b64decode(p["data_b64"]),
                    p.get("filename", "document.pdf"),
                    p.get("password", ""),
                )
                # The password-free copy only exists in memory until Saved.
                STATE.dirty = was_enc
                return self._send(200, {
                    "ok": True,
                    "pages": STATE.doc.page_count,
                    "was_encrypted": was_enc,
                })

            if path == "/api/merge":
                p = self._read_json()
                files = [(f["filename"], base64.b64decode(f["data_b64"])) for f in p["files"]]
                merge_pdfs(files, bool(p.get("include_current", False)))
                STATE.dirty = True
                return self._send(200, {"ok": True, "pages": STATE.doc.page_count})

            if path == "/api/compress":
                try:
                    level = (self._read_json() or {}).get("level", "medium")
                except Exception:
                    level = "medium"
                if level not in COMPRESS_PRESETS:
                    level = "medium"
                before, after, target_kb = compress(level)
                STATE.dirty = True
                pct = round(100 * (1 - after / before)) if before else 0
                return self._send(200, {
                    "ok": True,
                    "level": level,
                    "target_kb": target_kb,
                    "met_target": after <= target_kb * 1024,
                    "before_kb": round(before / 1024, 1),
                    "after_kb": round(after / 1024, 1),
                    "saved_pct": pct,
                })

            if path == "/api/create_from_images":
                p = self._read_json()
                imgs = [(f["filename"], base64.b64decode(f["data_b64"])) for f in p["files"]]
                create_from_images(imgs, p.get("quality", "normal"))
                # A brand-new document that exists only in memory until Saved.
                STATE.dirty = True
                return self._send(200, {"ok": True, "pages": STATE.doc.page_count})

            if path == "/api/delete_pages":
                p = self._read_json()
                delete_pages(p["pages"])
                STATE.dirty = True
                return self._send(200, {"ok": True, "pages": STATE.doc.page_count})

            if path == "/api/reorder":
                p = self._read_json()
                reorder_pages(p["order"], p.get("rotations"))
                STATE.dirty = True
                return self._send(200, {"ok": True, "pages": STATE.doc.page_count})

            return None    # unknown endpoint -> do_POST sends 404 and rolls back epoch


# ---------------------------------------------------------------------------
# Frontend (single page)
# ---------------------------------------------------------------------------
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PyPDF for Mac</title>
<style>
  :root { --bg:#0f1115; --panel:#171a21; --line:#262b36; --txt:#e6e9ef; --muted:#8b93a3;
          --accent:#4f8cff; --accent2:#1f6feb; --ok:#3fb950; --warn:#d29922;
          --err:#f85149; --scrolltrack:#0b0d12; --scrollthumb:#3d6fd6; }
  /* follow the macOS appearance setting */
  @media (prefers-color-scheme: light){
    :root { --bg:#f2f3f6; --panel:#ffffff; --line:#d8dce4; --txt:#1d2533; --muted:#5c6575;
            --accent:#2f6fe4; --accent2:#2f6fe4; --ok:#1a7f37; --warn:#9a6700;
            --err:#c93c37; --scrolltrack:#e4e7ee; --scrollthumb:#9db7e8; }
    .stage { box-shadow:0 4px 18px rgba(30,40,60,.18); }
  }
  * { box-sizing:border-box; }
  body { margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
         background:var(--bg); color:var(--txt); height:100vh; display:flex; flex-direction:column; }
  header { display:flex; align-items:center; gap:12px; padding:4px 14px; background:var(--panel);
           border-bottom:1px solid var(--line); }
  header h1 { font-size:13px; margin:0; font-weight:600; letter-spacing:.3px; }
  header .meta { color:var(--muted); font-size:11px; margin-left:auto; }
  .toolbar { display:flex; flex-wrap:nowrap; gap:2px; padding:3px 6px; background:var(--panel);
             border-bottom:1px solid var(--line); align-items:center; overflow-x:auto; }
  .toolbar > * { flex:0 0 auto; white-space:nowrap; }
  .toolbar::-webkit-scrollbar { height:8px; }
  .toolbar::-webkit-scrollbar-thumb { background:var(--line); border-radius:4px; }
  /* compact icon buttons: each toolbar button is a square holding one SVG glyph */
  .toolbar button { padding:5px; line-height:0; display:inline-flex; align-items:center; justify-content:center; }
  .toolbar .ic { width:18px; height:18px; fill:none; stroke:currentColor; stroke-width:2;
                 stroke-linecap:round; stroke-linejoin:round; display:block; }
  /* labelled tiles (icon above a short label) for the main actions — always
     readable without hovering */
  .toolbar button.tile { flex-direction:column; gap:2px; padding:3px 7px; line-height:1; min-width:46px; }
  .toolbar button.tile .ic { width:17px; height:17px; }
  .toolbar button.tile .lbl { font-size:10px; letter-spacing:.2px; font-weight:500; }
  .sep { width:1px; height:28px; background:var(--line); margin:0 4px; }
  /* level popover (Compress) */
  .popover { position:fixed; background:var(--panel); border:1px solid var(--line); border-radius:10px;
             padding:6px; box-shadow:0 8px 28px rgba(0,0,0,.45); z-index:120; min-width:228px; }
  .popover .cap { font-size:10.5px; color:var(--muted); letter-spacing:.4px; padding:4px 10px 6px; }
  .popover .opt { display:flex; justify-content:space-between; gap:14px; padding:8px 10px;
                  border-radius:6px; cursor:pointer; font-size:12px; color:var(--txt); }
  .popover .opt:hover { background:var(--bg); }
  .popover .opt.cur { background:var(--accent2); color:#fff; }
  .popover .opt .sub { color:var(--muted); }
  .popover .opt.cur .sub { color:#cfe0ff; }
  /* unified Find pill: magnifier · field · count · up/down · clear */
  .findpill { display:inline-flex; align-items:center; gap:3px; background:var(--bg);
              border:1px solid var(--line); border-radius:8px; padding:2px 4px 2px 8px; }
  .findpill.focus { border-color:var(--accent); }
  .findpill.dis { opacity:.4; }
  .findpill .micon { width:14px; height:14px; fill:none; stroke:var(--muted); stroke-width:2; stroke-linecap:round; flex:0 0 auto; }
  .findpill input { background:transparent; border:0; outline:0; color:var(--txt); font-size:11px; width:92px; padding:4px 0; }
  .findpill .findcount { min-width:30px; }
  .findpill .fbtn { background:transparent; border:0; padding:3px; border-radius:5px; color:var(--muted);
                    cursor:pointer; display:inline-flex; align-items:center; }
  .findpill .fbtn:hover:not(:disabled) { background:var(--line); color:var(--txt); }
  .findpill .fbtn:disabled { opacity:.35; cursor:default; }
  .findpill .fic { width:13px; height:13px; fill:none; stroke:currentColor; stroke-width:2.2; stroke-linecap:round; stroke-linejoin:round; }
  /* compact page-nav pill: ‹ · editable n / N · › */
  .navpill { display:inline-flex; align-items:center; background:var(--bg); border:1px solid var(--line);
             border-radius:8px; overflow:hidden; }
  .navpill .npbtn { background:transparent; border:0; padding:6px 8px; color:var(--muted); cursor:pointer; display:inline-flex; align-items:center; }
  .navpill .npbtn:hover:not(:disabled) { background:var(--line); color:var(--txt); }
  .navpill .npbtn:disabled { opacity:.35; cursor:default; }
  .navpill .npbtn svg { width:14px; height:14px; fill:none; stroke:currentColor; stroke-width:2.2; stroke-linecap:round; stroke-linejoin:round; }
  .navpill .npmid { display:inline-flex; align-items:center; gap:2px; padding:0 6px; border-left:1px solid var(--line); border-right:1px solid var(--line); }
  .navpill input.pagebox2 { background:transparent; border:0; outline:0; color:var(--txt); font-size:11px;
                            width:26px; text-align:center; padding:5px 0; -moz-appearance:textfield; }
  .navpill input.pagebox2::-webkit-inner-spin-button, .navpill input.pagebox2::-webkit-outer-spin-button { -webkit-appearance:none; margin:0; }
  .navpill #pageTotal { color:var(--muted); font-size:11px; }
  /* Find-in-text: a selectable text panel where EVERY match is highlighted */
  .txtbox { width:100%; min-height:48vh; max-height:62vh; overflow:auto; position:relative;
            background:var(--bg); color:var(--txt); border:1px solid var(--line); border-radius:7px;
            padding:8px 10px; font-size:13px; line-height:1.55; white-space:pre-wrap; word-break:break-word; }
  .txtbox mark { background:rgba(255,214,0,.55); color:inherit; border-radius:2px; padding:0 1px; }
  .txtbox mark.cur { background:#ff9100; color:#111; }
  select.lvl { background:var(--bg); color:var(--txt); border:1px solid var(--line);
               border-radius:6px; padding:5px 4px; font-size:11px; cursor:pointer; width:58px; }
  select.lvl:disabled { opacity:.4; cursor:not-allowed; }
  input.pagebox { background:var(--bg); color:var(--txt); border:1px solid var(--line);
               border-radius:6px; padding:5px 4px; font-size:11px; width:40px; text-align:center; }
  input.pagebox:focus { outline:none; border-color:var(--accent); }
  input.pagebox:disabled { opacity:.4; cursor:not-allowed; }
  /* hide number-spinner arrows so the box stays compact */
  input.pagebox::-webkit-inner-spin-button, input.pagebox::-webkit-outer-spin-button {
               -webkit-appearance:none; margin:0; }
  button { background:var(--accent2); color:#fff; border:0; border-radius:6px; padding:5px 6px;
           font-size:11px; cursor:pointer; }
  button:hover { background:var(--accent); }
  button.ghost { background:transparent; border:1px solid var(--line); color:var(--txt); }
  button.ghost:hover { border-color:var(--accent); }
  button.on { background:var(--ok); }
  button:disabled { opacity:.4; cursor:not-allowed; }
  .main { flex:1; display:flex; min-height:0; }
  .viewer { flex:1; overflow:auto; padding:24px 20px; display:flex; flex-direction:column;
            align-items:center; gap:22px; scroll-behavior:smooth; }
  /* keep pages centred when they fit, but stay scrollable to the LEFT when
     zoomed wider than the pane (plain center clips the left edge) */
  .viewer { align-items:safe center; }
  /* distinct scrollbars so they stand out against the dark theme */
  .viewer::-webkit-scrollbar { width:14px; height:14px; }
  .viewer::-webkit-scrollbar-track { background:var(--scrolltrack); }
  .viewer::-webkit-scrollbar-thumb { background:var(--scrollthumb); border-radius:8px;
            border:3px solid var(--scrolltrack); }
  .viewer::-webkit-scrollbar-thumb:hover { background:var(--accent); }
  .viewer::-webkit-scrollbar-corner { background:var(--scrolltrack); }
  .stage { position:relative; box-shadow:0 6px 30px rgba(0,0,0,.5); background:#fff; }
  .stage .plabel { position:absolute; top:-17px; left:0; font-size:11px; color:var(--muted); }
  .stage img { display:block; }
  .stage.signing { cursor:crosshair; }
  /* Read-only by default: text spans are inert until edit mode adds .editing. */
  .span { position:absolute; cursor:default; border:1px solid transparent; border-radius:2px; pointer-events:none; }
  .editing .span { pointer-events:auto; cursor:pointer; }
  .editing .span:hover { background:rgba(79,140,255,.18); border-color:var(--accent); }
  .span.sel { background:rgba(63,185,80,.22); border-color:var(--ok); }
  .signing .span { pointer-events:none; }
  .selrect { position:absolute; border:1.5px dashed var(--ok); background:rgba(63,185,80,.15); pointer-events:none; }
  /* search highlights */
  .findhl { position:absolute; background:rgba(255,214,0,.40); border-radius:2px; pointer-events:none; mix-blend-mode:multiply; }
  .findhl.cur { background:rgba(255,138,0,.55); outline:1.5px solid #ff8a00; }
  input.findbox { background:var(--bg); color:var(--txt); border:1px solid var(--line);
               border-radius:6px; padding:5px 7px; font-size:11px; width:120px; }
  input.findbox:focus { outline:none; border-color:var(--accent); }
  input.findbox:disabled { opacity:.4; cursor:not-allowed; }
  .findcount { font-size:11px; color:var(--muted); min-width:34px; text-align:center; }
  /* signature placement preview (drag to move · corner to resize · confirm) */
  .sigprev { position:absolute; border:1.5px solid var(--accent); background:rgba(79,140,255,.08);
             cursor:move; z-index:20; box-shadow:0 2px 10px rgba(0,0,0,.35); }
  .sigprev img { width:100%; height:100%; object-fit:contain; display:block; pointer-events:none; }
  .sigprev .rz { position:absolute; right:-7px; bottom:-7px; width:14px; height:14px; border-radius:50%;
                 background:var(--accent); border:2px solid #fff; cursor:nwse-resize; }
  .sigprev .acts { position:absolute; left:0; top:-30px; display:flex; gap:6px; }
  .sigprev .acts button { padding:3px 8px; font-size:11px; }
  .side { width:248px; flex:0 0 248px; background:var(--panel); border-left:1px solid var(--line); overflow:auto; padding:12px; }
  .side .path { font-size:11px; color:var(--muted); word-break:break-all; line-height:1.45; margin-top:4px; }
  .side h2 { font-size:12px; text-transform:uppercase; letter-spacing:.6px; color:var(--muted); margin:18px 0 8px; }
  .side h2:first-child { margin-top:0; }
  textarea { width:100%; background:var(--bg); color:var(--txt); border:1px solid var(--line);
             border-radius:7px; padding:8px; font-size:13px; min-height:70px; resize:vertical; font-family:inherit; }
  .row { display:flex; gap:8px; align-items:center; margin:8px 0; flex-wrap:wrap; }
  .check { display:flex; align-items:center; gap:6px; font-size:12px; color:var(--txt); margin:6px 0; cursor:pointer; }
  .pill { font-size:11px; color:var(--muted); }
  .status { font-size:12px; padding:8px 16px; border-top:1px solid var(--line); background:var(--panel);
            color:var(--muted); min-height:18px; }
  .status.ok { color:var(--ok); } .status.err { color:var(--err); }
  /* drop-anything-here cue while dragging files over the window */
  body.dragging .viewer { outline:2px dashed var(--accent); outline-offset:-10px; }
  /* busy overlay for long operations (sits above sheets) */
  .busy { position:fixed; inset:0; background:rgba(0,0,0,.45); display:none;
          flex-direction:column; align-items:center; justify-content:center; gap:14px; z-index:200; }
  .busy.show { display:flex; }
  .busy .spin { width:42px; height:42px; border-radius:50%;
                border:4px solid rgba(255,255,255,.25); border-top-color:#fff;
                animation:busyspin .9s linear infinite; }
  .busy .bmsg { color:#fff; font-size:13px; }
  @keyframes busyspin { to { transform:rotate(360deg); } }
  .empty { color:var(--muted); text-align:center; margin-top:80px; font-size:14px; line-height:1.6; }
  .pagedots { display:flex; flex-wrap:wrap; gap:6px; }
  .pagedots label { font-size:12px; display:flex; align-items:center; gap:4px; background:var(--bg);
                    border:1px solid var(--line); border-radius:6px; padding:4px 7px; cursor:pointer; }
  .hint { font-size:11px; color:var(--muted); margin-top:4px; line-height:1.5; }
  #sigPreview { max-width:100%; max-height:70px; background:#fff; border:1px solid var(--line);
                border-radius:6px; margin-top:6px; display:none; }
  a.dl { display:none; }
  .overlay { position:fixed; inset:0; background:rgba(0,0,0,.6); display:none;
             align-items:center; justify-content:center; z-index:50; }
  .overlay.show { display:flex; }
  .modal { background:var(--panel); border:1px solid var(--line); border-radius:12px;
           width:min(920px,92vw); max-height:88vh; display:flex; flex-direction:column; overflow:hidden; }
  .modal .mhead { padding:12px 16px; border-bottom:1px solid var(--line); display:flex; align-items:center; gap:10px; }
  .modal .mhead h3 { font-size:14px; margin:0; font-weight:600; }
  .modal .mbody { padding:14px 16px; overflow:auto; }
  .modal .mfoot { padding:12px 16px; border-top:1px solid var(--line); display:flex; gap:8px; justify-content:flex-end; }
  .flist { list-style:none; margin:8px 0 0; padding:0; }
  .flist li { display:flex; align-items:center; gap:10px; padding:8px 10px; border:1px solid var(--line);
              border-radius:8px; margin-bottom:8px; background:var(--bg); cursor:grab; }
  .flist li.over { border-color:var(--accent); }
  .flist li .num { width:22px; color:var(--muted); font-size:12px; text-align:center; }
  .flist li .nm { flex:1; font-size:13px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .flist li button { padding:4px 9px; }
  .thumbs { display:flex; flex-wrap:wrap; gap:14px; }
  .thumb { position:relative; border:2px solid var(--line); border-radius:8px; background:#fff;
           cursor:grab; overflow:hidden; }
  .thumb.over { border-color:var(--accent); }
  .thumb img { display:block; }
  .thumb .badge { position:absolute; left:5px; top:5px; background:rgba(0,0,0,.72); color:#fff;
                  font-size:11px; padding:1px 6px; border-radius:6px; z-index:2; }
  .thumb .rotbtn { position:absolute; right:38px; top:5px; z-index:2; padding:2px 8px;
                   font-size:13px; background:rgba(0,0,0,.72); border:0; color:#fff;
                   border-radius:6px; cursor:pointer; }
  .thumb .rotbtn:hover { background:var(--accent2); }
  .thumb .delbtn { position:absolute; right:5px; top:5px; z-index:2; padding:2px 8px;
                   font-size:13px; background:rgba(0,0,0,.72); border:0; color:#fff;
                   border-radius:6px; cursor:pointer; }
  .thumb .delbtn:hover { background:#c93c37; }
  .thumb .delbtn:disabled { opacity:.35; cursor:not-allowed; }
  .thumb.picked { border-color:var(--ok); }
  .thumb .pickmark { position:absolute; right:5px; top:5px; z-index:2; background:var(--ok);
                     color:#fff; border-radius:50%; width:20px; height:20px; display:flex;
                     align-items:center; justify-content:center; font-size:12px; }
  .welcome { display:flex; flex-direction:column; align-items:center; gap:14px; }
  .welcome .big { font-size:15px; padding:14px 28px; border-radius:12px; min-width:260px; }
  .welcome .note { color:var(--muted); font-size:12px; line-height:1.7; margin-top:6px; }
  /* pages live in a wrapper so a pinch can scale them all live with one CSS transform */
  .pwrap { display:flex; flex-direction:column; align-items:safe center; gap:22px; width:100%; }
</style>
</head>
<body>
<header>
  <h1>📄 PyPDF for Mac</h1>
  <span class="meta" id="meta">No document open</span>
  <button class="ghost" onclick="openAbout()" title="About this app" aria-label="About this app" style="padding:4px 9px">ⓘ</button>
</header>

<div class="toolbar" role="toolbar" aria-label="Document tools">
  <button class="tile" onclick="guardThen(()=>fileInput.click())" title="Open a PDF" aria-label="Open a PDF"><svg class="ic" viewBox="0 0 24 24"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg><span class="lbl">Open</span></button>
  <span class="sep"></span>
  <button class="ghost tile" id="editBtn" onclick="toggleEdit()" title="Edit — turn on to change text or place a signature (PDF opens read-only)" aria-label="Toggle edit mode" disabled><svg class="ic" viewBox="0 0 24 24"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z"/></svg><span class="lbl">Edit</span></button>
  <button class="ghost tile" id="signBtn" onclick="toggleSign()" title="Place signature" aria-label="Place signature" disabled><svg class="ic" viewBox="0 0 24 24"><path d="M3 17c3 0 3-7 6-7s2 5 4 5 2-3 5-3"/><path d="M3 21h18"/></svg><span class="lbl">Sign</span></button>
  <button class="ghost tile" id="undoBtn" onclick="undo()" title="Undo (⌘Z)" aria-label="Undo" disabled><svg class="ic" viewBox="0 0 24 24"><path d="M3 7v6h6"/><path d="M3.5 13a9 9 0 1 0 2-7.4"/></svg><span class="lbl">Undo</span></button>
  <span class="sep"></span>
  <button class="ghost tile" onclick="guardThen(()=>mergeInput.click())" title="Merge PDFs" aria-label="Merge PDFs"><svg class="ic" viewBox="0 0 24 24"><path d="M12 2 2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/></svg><span class="lbl">Merge</span></button>
  <button class="ghost tile" onclick="guardThen(()=>imgInput.click())" title="Create a PDF from images" aria-label="Create from images"><svg class="ic" viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/></svg><span class="lbl">Images</span></button>
  <button class="ghost tile" onclick="guardThen(()=>unlockInput.click())" title="Remove the password from a protected PDF" aria-label="Unlock PDF"><svg class="ic" viewBox="0 0 24 24"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 9.9-1"/></svg><span class="lbl">Unlock</span></button>
  <button class="ghost tile" id="orgBtn" onclick="openOrganize()" title="Organize pages — reorder, rotate, delete" aria-label="Organize pages" disabled><svg class="ic" viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/></svg><span class="lbl">Organize</span></button>
  <button class="ghost tile" id="copyBtn" onclick="openCopyPages()" title="Copy chosen pages into a new PDF" aria-label="Copy pages" disabled><svg class="ic" viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg><span class="lbl">Copy</span></button>
  <button class="ghost tile" id="textBtn" onclick="openTextModal()" title="View and copy the document's text" aria-label="View text" disabled><svg class="ic" viewBox="0 0 24 24"><path d="M4 7V4h16v3"/><path d="M9 20h6"/><path d="M12 4v16"/></svg><span class="lbl">Text</span></button>
  <span class="sep"></span>
  <span class="navpill" id="navPill">
    <button class="npbtn" id="pagePrev" onclick="go(-1)" title="Previous page" aria-label="Previous page" disabled><svg viewBox="0 0 24 24"><path d="M15 18l-6-6 6-6"/></svg></button>
    <span class="npmid">
      <input type="number" id="pageInput" class="pagebox2" min="1" value="" placeholder="–"
             aria-label="Go to page number" title="Type a page number and press Enter to jump to it" disabled>
      <span class="pill" id="pageTotal">/ –</span>
    </span>
    <button class="npbtn" id="pageNext" onclick="go(1)" title="Next page" aria-label="Next page" disabled><svg viewBox="0 0 24 24"><path d="M9 6l6 6-6 6"/></svg></button>
  </span>
  <span class="sep"></span>
  <span class="findpill dis" id="findPill">
    <svg class="micon" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg>
    <input type="text" id="findBox" placeholder="Find" autocomplete="off"
           aria-label="Find text in document"
           title="Type text and press Enter to find it in the document" disabled>
    <span class="findcount" id="findCount" role="status" aria-live="polite"></span>
    <button class="fbtn" id="findPrev" onclick="gotoMatch(-1)" aria-label="Previous match" title="Previous match (⇧⏎)" disabled><svg class="fic" viewBox="0 0 24 24"><path d="M18 15l-6-6-6 6"/></svg></button>
    <button class="fbtn" id="findNext" onclick="gotoMatch(1)" aria-label="Next match" title="Next match (⏎)" disabled><svg class="fic" viewBox="0 0 24 24"><path d="M6 9l6 6 6-6"/></svg></button>
    <button class="fbtn" id="findClear" onclick="clearFindBox()" aria-label="Clear search" title="Clear" style="display:none"><svg class="fic" viewBox="0 0 24 24"><path d="M18 6 6 18M6 6l12 12"/></svg></button>
  </span>
  <span class="sep"></span>
  <button class="ghost" onclick="zoomBy(-0.25)" aria-label="Zoom out" title="Zoom out"><svg class="ic" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/><path d="M8 11h6"/></svg></button>
  <span class="pill" id="zoomLabel" aria-live="polite">100%</span>
  <button class="ghost" onclick="zoomBy(0.25)" aria-label="Zoom in" title="Zoom in"><svg class="ic" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/><path d="M8 11h6"/><path d="M11 8v6"/></svg></button>
  <span class="sep"></span>
  <button class="ghost tile" id="compressBtn" onclick="openCompressMenu()" title="Compress — choose a level" aria-label="Compress" aria-haspopup="menu" disabled><svg class="ic" viewBox="0 0 24 24"><path d="M4 14h6v6"/><path d="M20 10h-6V4"/><path d="M14 10l7-7"/><path d="M3 21l7-7"/></svg><span class="lbl">Compress</span></button>
  <button class="ghost tile" id="pngBtn" onclick="exportPng()" title="Export the current page as a PNG image" aria-label="Export page as PNG" disabled><svg class="ic" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/></svg><span class="lbl">PNG</span></button>
  <button class="ghost tile" id="printBtn" onclick="printDoc()" title="Print the document" aria-label="Print" disabled><svg class="ic" viewBox="0 0 24 24"><path d="M6 9V2h12v7"/><path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/><rect x="6" y="14" width="12" height="8"/></svg><span class="lbl">Print</span></button>
  <span class="sep"></span>
  <button class="tile" id="saveBtn" onclick="openSaveModal()" title="Save / download the PDF" aria-label="Save" disabled><svg class="ic" viewBox="0 0 24 24"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><path d="M17 21v-8H7v8"/><path d="M7 3v5h8"/></svg><span class="lbl">Save</span></button>
  <button class="ghost tile" id="closeBtn" onclick="closeDoc()" title="Close the open document" aria-label="Close document" disabled><svg class="ic" viewBox="0 0 24 24"><path d="M18 6 6 18"/><path d="M6 6l12 12"/></svg><span class="lbl">Close</span></button>
</div>

<div class="main">
  <div class="viewer" id="viewer">
    <div class="empty" id="emptyMsg">
      <div class="welcome">
        <div style="font-size:16px;color:var(--txt);font-weight:600">What would you like to do?</div>
        <button class="big" onclick="guardThen(()=>fileInput.click())">📄 Open a PDF</button>
        <button class="big ghost" onclick="guardThen(()=>imgInput.click())">🖼 Create a PDF from images</button>
        <div class="note">Everything stays on your Mac — nothing is uploaded.<br>
        Tip: drag a PDF (or images) anywhere into this window.<br>
        PDFs open read-only — turn on “✎ Edit” to change text or sign · pinch or double-click to zoom.</div>
      </div>
    </div>
    <div id="pwrap" class="pwrap"></div>
    <!-- page stages injected here -->
  </div>

  <div class="side">
    <h2>Signature</h2>
    <div class="row" style="margin:0">
      <button class="ghost" onclick="sigInput.click()">Upload signature image</button>
      <button class="ghost" id="clearSigBtn" onclick="clearSignature()" title="Remove the loaded signature" style="display:none">✕ Clear</button>
    </div>
    <img id="sigPreview" alt="signature" draggable="false">
    <!-- kept for the API but hidden: signatures are placed as-is (off by default) -->
    <label class="check" style="display:none"><input type="checkbox" id="sigKnockout"> Remove white background</label>
    <div class="hint">Turn on “✎ Edit”, click “Place Signature”, then drag a box on the page. Move the box or drag its corner to resize, then click <b>Place</b>. Use “Undo” to remove it later.</div>

    <h2>Edit text</h2>
    <div class="pill" id="editHint">Turn on “✎ Edit”, then click a text span on the page.</div>
    <textarea id="editText" placeholder="(turn on Edit, then select text)" disabled></textarea>
    <div class="hint" id="fontInfo"></div>
    <div class="row">
      <button id="applyBtn" onclick="applyEdit()" disabled>Apply change</button>
      <button class="ghost" onclick="clearSel()">Clear</button>
    </div>

    <h2>Delete pages</h2>
    <div class="pagedots" id="pageDots"><span class="pill">No document.</span></div>
    <div class="row"><button class="ghost" id="delBtn" onclick="deletePages()" disabled>Delete selected</button></div>

    <h2>Document</h2>
    <div class="pill" id="docInfo">—</div>
    <div class="path" id="docPath"></div>
  </div>
</div>

<div class="status" id="status" role="status" aria-live="polite">Ready.</div>

<div class="overlay" id="overlay"><div class="modal" id="modal" role="dialog" aria-modal="true"></div></div>
<div class="busy" id="busy" role="alertdialog" aria-busy="true" aria-label="Working"><div class="spin"></div><div class="bmsg" id="busyMsg">Working…</div><button class="ghost" id="busyCancel" style="display:none;margin-top:14px">Cancel</button></div>

<input type="file" id="fileInput" accept="application/pdf,image/*" style="display:none">
<input type="file" id="mergeInput" accept="application/pdf" multiple style="display:none">
<input type="file" id="imgInput" accept="image/*" multiple style="display:none">
<input type="file" id="unlockInput" accept="application/pdf" style="display:none">
<input type="file" id="sigInput" accept="image/png,image/jpeg" style="display:none">
<a id="dl" class="dl"></a>

<script>
// Per-process CSRF token, injected by the server. Sent on every API call so
// the local server can reject requests forged by other web pages.
const APP_NONCE = "__APP_NONCE__";
let pages = 0, cur = 0, zoomMult = 1.0;
let curEpoch = -1;   // last document version this tab has rendered
let sizes = [], scaleByPage = {};
let _sizesEpoch = -1;   // epoch the cached `sizes`/`rots` belong to (epoch-gates /api/state)
let spansByPage = {}, selPage = -1, selIdx = -1;
let sigB64 = null, signMode = false;
let editMode = false;        // PDFs open READ-ONLY; the toolbar "✎ Edit" toggle turns this on
let rots = [];                                      // per-page rotation (degrees)
let mergeFiles = [], mergeIncludeCurrent = false;   // merge-order modal
let orgOrder = [], thumbW = 150;                    // organize-pages modal
let findMatches = [], findIdx = -1, _findEpoch = -1, _lastFindQ = "";   // find-in-document

const $ = id => document.getElementById(id);
function setStatus(m, k=""){ const s=$("status"); s.textContent=m; s.className="status "+k; }

// HTML-escape any value interpolated into innerHTML (file names are attacker-
// controlled: a crafted name could otherwise inject markup into dialogs).
function esc(s){ return String(s).replace(/[&<>"']/g,
  c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }

// ---------- busy overlay (long operations) ----------
// Shows after 250ms so quick operations never flicker; blocks stray clicks
// while the engine is working.
let _busyT=null;
function busy(msg){
  clearTimeout(_busyT);
  _busyT=setTimeout(()=>{ $("busyMsg").textContent=msg||"Working…"; $("busy").classList.add("show"); },250);
}
function unbusy(){ clearTimeout(_busyT); _busyT=null; $("busy").classList.remove("show"); }

// ---------- unsaved-changes protection ----------
let dirty = false;           // mirrors the server's dirty flag
let curName = "document.pdf";
let curPath = "";            // on-disk path when opened from a file (else "")

// Ask before any action that would REPLACE or CLOSE an unsaved document.
// Resolves the action immediately when there is nothing to lose.
function guardThen(action){
  if(!dirty || pages<=0){ action(); return; }
  $("modal").innerHTML=`
    <div class="mhead"><h3>Unsaved changes</h3></div>
    <div class="mbody">
      <div style="font-size:13px;line-height:1.6">“${esc(curName)}” has changes that
      haven’t been saved. If you continue, those changes will be lost.</div>
    </div>
    <div class="mfoot">
      <button class="ghost" onclick="closeModal()">Cancel</button>
      <button class="ghost" id="guardContinue">Continue without saving</button>
      <button id="guardSave">Save first</button>
    </div>`;
  $("guardContinue").onclick=()=>{ closeModal(); action(); };
  $("guardSave").onclick=()=>{ openSaveModal(action); };
  $("overlay").classList.add("show");
}

window.addEventListener("beforeunload", e=>{
  if(dirty && pages>0){ e.preventDefault(); e.returnValue=""; }
});

let _localOps=0, _lastLocalOp=0;   // track in-flight local mutations (POSTs)
async function api(path, opts){
  opts = opts || {};
  // Attach the CSRF token to every API call (harmless on GETs, required on POSTs).
  opts.headers = Object.assign({}, opts.headers, {"X-PyPDF-Token": APP_NONCE});
  const isPost = String(opts.method||"").toUpperCase()==="POST";
  // Every POST carries a body + content-type (matches the rest, avoids any
  // bodyless-POST quirk).
  if(isPost && opts.body==null){ opts.body="{}"; if(!opts.headers["Content-Type"]) opts.headers["Content-Type"]="application/json"; }
  if(isPost) _localOps++;
  try{
    let r;
    try{
      r = await fetch(path, opts);
    }catch(netErr){
      // A network-level failure (TypeError "Load failed"/"Failed to fetch")
      // almost always means the local helper isn't reachable — turn it into an
      // actionable message instead of a cryptic one.
      throw new Error("Couldn’t reach the local helper — it may have shut down after idling. Reload this page (⌘R) to restart it.");
    }
    const ct = r.headers.get("Content-Type")||"";
    if (ct.includes("application/json")){ const j=await r.json(); if(!j.ok) throw new Error(j.error||"error"); return j; }
    return r;
  } finally {
    if(isPost){ _localOps--; _lastLocalOp=Date.now(); }
  }
}
function fileToB64(file){return new Promise((res,rej)=>{const fr=new FileReader();
  fr.onload=()=>res(fr.result.split(",")[1]); fr.onerror=rej; fr.readAsDataURL(file);});}

// base display width: all pages render to this many CSS px (× zoom multiplier)
function targetWidth(){
  const avail = $("viewer").clientWidth - 48;
  return Math.round(Math.max(320, Math.min(1100, avail)) * zoomMult);
}
// Retina-sharp rendering: request the image at device-pixel resolution (the
// browser otherwise upscales a CSS-pixel render 2× on HiDPI screens, which is
// what makes text look soft). Capped so extreme zoom doesn't explode memory.
function renderWidth(cssW){
  const dpr=Math.min(window.devicePixelRatio||1, 2);
  return Math.min(Math.round(cssW*dpr), 3200);
}

// ---------- open / merge / create / signature upload ----------
async function openPdfFile(f){
  setStatus("Opening "+f.name+" ..."); busy("Opening "+f.name+" …");
  try{ const j=await api("/api/open",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({filename:f.name,data_b64:await fileToB64(f)})});
    if(j.locked){ unbusy(); await promptUnlock(f.name); return; }
    pages=j.pages; cur=0; await rebuild(); setEdit(false);   // PDFs open read-only
    setStatus("Opened "+f.name+" ("+pages+" pages). Read-only — turn on “✎ Edit” to make changes.","ok");
  }catch(err){ setStatus(err.message,"err"); }
  finally{ unbusy(); }
}
async function startMerge(files){
  setStatus("Reading "+files.length+" file(s) ...");
  mergeFiles=[];
  for(const f of files) mergeFiles.push({name:f.name, b64:await fileToB64(f)});
  mergeIncludeCurrent=false;
  openMergeModal();
  setStatus("Set the merge order, then click Merge.","");
}
// Standard / Small-file choice before building a PDF from images (remembered).
function askImageQuality(){
  return new Promise(res=>{
    let q="normal"; try{ q=localStorage.getItem("imgQuality")||"normal"; }catch(e){}
    $("modal").innerHTML=`
      <div class="mhead"><h3>Create PDF from images</h3></div>
      <div class="mbody">
        <label class="check"><input type="radio" name="iq" value="normal" ${q==="normal"?"checked":""}>
          Standard — full image quality</label>
        <label class="check"><input type="radio" name="iq" value="small" ${q==="small"?"checked":""}>
          Small file — noticeably lighter PDF, great for sharing</label>
      </div>
      <div class="mfoot">
        <button class="ghost" id="iqCancel">Cancel</button>
        <button id="iqGo">Create PDF</button>
      </div>`;
    _modalDismiss=()=>res(null);            // Escape / backdrop = Cancel
    $("iqCancel").onclick=()=>{ _modalDismiss=null; closeModal(); res(null); };
    $("iqGo").onclick=()=>{
      const v=(document.querySelector('input[name="iq"]:checked')||{}).value||"normal";
      try{ localStorage.setItem("imgQuality",v); }catch(e){}
      _modalDismiss=null; closeModal(); res(v);
    };
    $("overlay").classList.add("show");
  });
}
async function createFromImageFiles(files){
  const quality=await askImageQuality(); if(!quality) return;
  setStatus("Building PDF from "+files.length+" image(s) ...");
  busy("Building PDF from "+files.length+" image(s) …");
  try{ const payload=[]; for(const f of files) payload.push({filename:f.name,data_b64:await fileToB64(f)});
    const j=await api("/api/create_from_images",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({files:payload, quality})});
    pages=j.pages; cur=0; await rebuild(); setEdit(false);   // open read-only
    setStatus("Created PDF from images ("+pages+" pages).","ok");
  }catch(err){ setStatus(err.message,"err"); }
  finally{ unbusy(); }
}
$("fileInput").onchange = e => { const f=e.target.files[0]; e.target.value=""; if(f) openPdfFile(f); };
$("mergeInput").onchange = e => { const files=[...e.target.files]; e.target.value=""; if(files.length) startMerge(files); };
$("imgInput").onchange = e => { const files=[...e.target.files]; e.target.value=""; if(files.length) createFromImageFiles(files); };

// ---------- drag & drop anywhere onto the window ----------
// One PDF opens it; several PDFs open the merge dialog; images become a new
// PDF. All replacement paths go through the unsaved-changes guard.
// IMPORTANT: drags that START inside the app (reordering thumbnails in
// Organize, dragging a page image) must never be treated as a file drop —
// the browser exposes a dragged <img> as a "file" on drop.
let _dragDepth=0, _internalDrag=false;
window.addEventListener("dragstart", ()=>{ _internalDrag=true; });
window.addEventListener("dragend",   ()=>{ _internalDrag=false; });
const _externalFileDrag=e=>!_internalDrag && e.dataTransfer
  && [...(e.dataTransfer.types||[])].includes("Files");
window.addEventListener("dragenter", e=>{ e.preventDefault();
  if(!_externalFileDrag(e)) return;
  _dragDepth++; document.body.classList.add("dragging"); });
window.addEventListener("dragleave", e=>{ if(--_dragDepth<=0){ _dragDepth=0; document.body.classList.remove("dragging"); } });
window.addEventListener("dragover", e=>{ e.preventDefault(); });
window.addEventListener("drop", e=>{
  e.preventDefault(); _dragDepth=0; document.body.classList.remove("dragging");
  if(_internalDrag){ _internalDrag=false; return; }            // app-internal drag
  if($("overlay").classList.contains("show")) return;          // a sheet is open
  const files=[...((e.dataTransfer&&e.dataTransfer.files)||[])];
  if(!files.length) return;
  const pdfs=files.filter(f=>/\.pdf$/i.test(f.name));
  const imgs=files.filter(f=>/\.(png|jpe?g)$/i.test(f.name));
  if(pdfs.length===1 && !imgs.length)      guardThen(()=>openPdfFile(pdfs[0]));
  else if(pdfs.length>1 && !imgs.length)   guardThen(()=>startMerge(pdfs));
  else if(imgs.length && !pdfs.length)     guardThen(()=>createFromImageFiles(imgs));
  else setStatus("Drop PDFs or images — not a mix of both.","err");
});

// ---------- keyboard shortcuts ----------
// Cmd+S save · Cmd+O open · Cmd+Z undo · arrows / PageUp-Down pages · + / - zoom
document.addEventListener("keydown", e=>{
  const mod=e.metaKey||e.ctrlKey;
  const tag=(e.target&&e.target.tagName)||"";
  const inField=/^(INPUT|TEXTAREA|SELECT)$/.test(tag);
  if(mod && (e.key==="s"||e.key==="S")){ e.preventDefault(); if(pages>0) openSaveModal(); return; }
  if(mod && (e.key==="o"||e.key==="O")){ e.preventDefault(); guardThen(()=>fileInput.click()); return; }
  if(mod && (e.key==="f"||e.key==="F")){ e.preventDefault(); if(pages>0){ const fb=$("findBox"); fb.focus(); fb.select(); } return; }
  if(mod && (e.key==="z"||e.key==="Z") && !inField){ e.preventDefault(); if(pages>0 && !$("undoBtn").disabled) undo(); return; }
  if(mod || inField || pages<=0) return;
  if($("overlay").classList.contains("show")) return;   // a dialog has the keys
  if(e.key==="ArrowRight"||e.key==="PageDown"){ e.preventDefault(); go(1); }
  else if(e.key==="ArrowLeft"||e.key==="PageUp"){ e.preventDefault(); go(-1); }
  else if(e.key==="+"||e.key==="="){ e.preventDefault(); zoomBy(0.25); }
  else if(e.key==="-"){ e.preventDefault(); zoomBy(-0.25); }
});

$("unlockInput").onchange = async e => {
  const f=e.target.files[0]; e.target.value=""; if(!f) return;
  // Most protected PDFs need the user password; ask for it. An empty entry
  // still works for files locked only with an owner (permissions) password.
  const pw = prompt("Password for \""+f.name+"\"\n(leave blank if it only has owner/permission restrictions):", "");
  if(pw===null) return;  // cancelled
  setStatus("Unlocking "+f.name+" ...");
  try{ const j=await api("/api/unlock",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({filename:f.name,data_b64:await fileToB64(f),password:pw})});
    pages=j.pages; cur=0; await rebuild(); setEdit(false);   // open read-only
    setStatus(j.was_encrypted
      ? "Unlocked "+f.name+" ("+pages+" pages). Save to keep the password-free copy."
      : f.name+" was not password-protected; opened as-is ("+pages+" pages).","ok");
  }catch(err){ setStatus(err.message,"err"); }
};

$("sigInput").onchange = async e => {
  const f=e.target.files[0]; if(!f) return;
  sigB64=await fileToB64(f);
  const pv=$("sigPreview"); pv.src="data:image/png;base64,"+sigB64; pv.style.display="block";
  $("clearSigBtn").style.display="";                 // allow clearing/replacing it
  $("signBtn").disabled = pages<=0 || !editMode;
  setStatus("Signature loaded. Turn on “✎ Edit”, click “Place Signature”, then drag a box on the page.","ok");
  e.target.value="";
};
// Remove the loaded signature so a different one can be used (and stop signing).
function clearSignature(){
  sigB64=null;
  const pv=$("sigPreview"); pv.src=""; pv.style.display="none";
  $("clearSigBtn").style.display="none";
  cancelSigPreview();
  setSign(false);
  $("signBtn").disabled=true;
  setStatus("Signature cleared. Upload another to sign.","");
}

// ---------- build continuous page list ----------
async function refreshMeta(){
  const st=await api("/api/state?se="+_sizesEpoch);
  // The server omits sizes/rotations when our cached copy is still current
  // (same epoch) — reuse what we have; otherwise adopt the fresh arrays.
  if(st.sizes){ sizes=st.sizes; rots=st.rotations||[]; _sizesEpoch=st.epoch; }
  if(typeof st.epoch==="number") curEpoch=st.epoch;   // keep tab-sync in step
  // Search hits are tied to the document version they were found in. A zoom-only
  // rebuild keeps the same epoch (highlights re-place at the new scale); a real
  // edit/delete/reorder bumps the epoch, so drop now-stale highlights.
  if(findMatches.length && curEpoch!==_findEpoch){ clearFinds(); _lastFindQ=""; if($("findBox")) $("findBox").value=""; }
  dirty=!!st.dirty; curName=st.filename||"document.pdf";
  curPath=st.path||"";
  if(curEpoch!==_thumbEpoch){ clearThumbCache(); _thumbEpoch=curEpoch; }
  $("meta").textContent=st.filename+"  •  "+st.pages+" pages  •  "+st.size_kb+" KB"+(dirty?"  •  Edited":"");
  $("docInfo").textContent=st.filename+" — "+st.pages+" pages, "+st.size_kb+" KB";
  const dp=$("docPath"); if(dp){ dp.textContent = st.path ? st.path : "(opened from upload — no file path)"; }
  $("undoBtn").disabled=!st.can_undo;
}

let pageObserver=null;
async function rebuild(){
  const v=$("viewer"), w=$("pwrap");
  w.style.transform=""; w.style.transformOrigin="";
  v.querySelectorAll(".stage").forEach(s=>s.remove());
  spansByPage={}; scaleByPage={};
  if(pageObserver){ pageObserver.disconnect(); pageObserver=null; }
  if(pages<=0){ $("emptyMsg").style.display="block"; updateButtons(); return; }
  $("emptyMsg").style.display="none";
  await refreshMeta();
  const tw=targetWidth(); _lastTW=tw;
  // Lazy render: only fetch a page image when it scrolls near the viewport.
  // This makes the first paint fast even for large documents.
  pageObserver=new IntersectionObserver((entries)=>{
    for(const en of entries){
      if(!en.isIntersecting) continue;
      const img=en.target.querySelector("img");
      if(img && !img.src && img.dataset.src){ img.src=img.dataset.src; }
      pageObserver.unobserve(en.target);
    }
  },{root:null,rootMargin:"1200px 0px",threshold:0});
  for(let i=0;i<pages;i++){
    const stage=document.createElement("div");
    stage.className="stage"+(signMode?" signing":""); stage.dataset.page=i;
    const ph = sizes[i] ? Math.round(tw*(sizes[i][1]/sizes[i][0])) : Math.round(tw*1.3);
    stage.innerHTML=`<span class="plabel">Page ${i+1}</span><img alt="page ${i+1}" draggable="false" style="width:${tw}px;height:${ph}px;background:#fff">`;
    const img=stage.querySelector("img");
    // spans/signature boxes are positioned in CSS px, so the scale is the
    // DISPLAYED width per PDF point (the image itself is higher-res)
    img.onload=()=>{ img.style.height="auto"; scaleByPage[i]= sizes[i] ? tw/sizes[i][0] : 1;
      if(editMode) drawSpans(stage,i);   // spans only matter (and are only clickable) in Edit mode
      drawFinds(stage,i); };             // search highlights, if any matches on this page
    img.dataset.src="/api/page?n="+i+"&w="+renderWidth(tw)+"&t="+Date.now();
    attachSign(stage, i);
    w.appendChild(stage);
    pageObserver.observe(stage);
  }
  buildDots(); updateButtons(); clearSel();
}

async function reloadPage(i){
  const stage=$("viewer").querySelector('.stage[data-page="'+i+'"]'); if(!stage) return;
  const img=stage.querySelector("img");
  const tw=targetWidth();
  img.style.width=tw+"px";
  img.onload=()=>{ scaleByPage[i]= sizes[i] ? tw/sizes[i][0] : 1;
    if(editMode) drawSpans(stage,i); drawFinds(stage,i); };
  img.src="/api/page?n="+i+"&w="+renderWidth(tw)+"&t="+Date.now();
  await refreshMeta();
}

async function drawSpans(stage, i){
  stage.querySelectorAll(".span").forEach(s=>s.remove());
  const sc=scaleByPage[i]||1;
  const j=await api("/api/spans?n="+i); spansByPage[i]=j.spans;
  for(const sp of j.spans){
    if(!sp.text.trim()) continue;
    const [x0,y0,x1,y1]=sp.bbox;
    const d=document.createElement("div");
    d.className="span"; d.dataset.page=i; d.dataset.i=sp.index;
    d.style.left=(x0*sc)+"px"; d.style.top=(y0*sc)+"px";
    d.style.width=Math.max(2,(x1-x0)*sc)+"px"; d.style.height=Math.max(2,(y1-y0)*sc)+"px";
    d.title=sp.text;
    d.onclick=()=>selectSpan(i, sp.index);
    stage.appendChild(d);
  }
}

// ---------- find in document ----------
// Searches server-side with PyMuPDF (page.search_for) and paints highlight
// boxes over the matches, positioned the same way spans are. Works in read-only
// mode — no need to enter Edit.
async function doFind(){
  const q=($("findBox").value||"").trim();
  _lastFindQ=q;
  if(!q){ clearFinds(); return; }
  try{
    const j=await api("/api/find?q="+encodeURIComponent(q));
    findMatches=j.matches||[]; _findEpoch=curEpoch;
    findIdx=findMatches.length?0:-1;
    document.querySelectorAll(".stage").forEach(st=>drawFinds(st,+st.dataset.page));
    updateFindUI();
    if(findIdx>=0){ scrollToMatch(); setStatus(findMatches.length+" match(es) for “"+q+"”.","ok"); }
    else setStatus("No matches for “"+q+"”.","");
  }catch(err){ setStatus(err.message,"err"); }
}
function clearFinds(){
  findMatches=[]; findIdx=-1;
  document.querySelectorAll(".findhl").forEach(h=>h.remove());
  updateFindUI();
}
function drawFinds(stage,i){
  stage.querySelectorAll(".findhl").forEach(h=>h.remove());
  if(!findMatches.length) return;
  const sc=scaleByPage[i]||1;
  findMatches.forEach((m,idx)=>{
    if(m.page!==i) return;
    const [x0,y0,x1,y1]=m.rect;
    const d=document.createElement("div");
    d.className="findhl"+(idx===findIdx?" cur":"");
    d.dataset.mi=idx;
    d.style.left=(x0*sc)+"px"; d.style.top=(y0*sc)+"px";
    d.style.width=Math.max(2,(x1-x0)*sc)+"px"; d.style.height=Math.max(2,(y1-y0)*sc)+"px";
    stage.appendChild(d);
  });
}
function updateFindUI(){
  const n=findMatches.length;
  $("findCount").textContent = n ? ((findIdx+1)+"/"+n) : (($("findBox").value||"").trim()?"0":"");
  $("findPrev").disabled = n<2; $("findNext").disabled = n<2;
}
function markCurrentFind(){
  document.querySelectorAll(".findhl").forEach(h=>h.classList.toggle("cur", +h.dataset.mi===findIdx));
}
function scrollToMatch(){
  const m=findMatches[findIdx]; if(!m) return;
  const v=$("viewer");
  const stage=v.querySelector('.stage[data-page="'+m.page+'"]');
  if(stage){
    const img=stage.querySelector("img");
    if(img && !img.src && img.dataset.src) img.src=img.dataset.src;   // pull in a lazy page
    // Scroll to the MATCH's own vertical position inside the page, not just the
    // page top — on a tall page the hit can sit far down, which made it land
    // a screen or two away. scale = displayed px per PDF point.
    const sc = scaleByPage[m.page] || (sizes[m.page] ? (_lastTW||targetWidth())/sizes[m.page][0] : 1);
    const matchY = m.rect[1]*sc;
    const vr=v.getBoundingClientRect(), sr=stage.getBoundingClientRect();
    const stageTopInContent = (sr.top - vr.top) + v.scrollTop;
    const target = stageTopInContent + matchY - v.clientHeight/2;
    v.scrollTo({top:Math.max(0,target), behavior:"smooth"});
  }
  cur=m.page; updateButtons(); markCurrentFind();
}
function gotoMatch(dir){
  if(findMatches.length<1) return;
  findIdx=(findIdx+dir+findMatches.length)%findMatches.length;
  updateFindUI(); scrollToMatch();
}

// ---------- edit text ----------
function selectSpan(page, i){
  if(!editMode) return;        // read-only: text isn't editable until Edit is on
  selPage=page; selIdx=i; const sp=spansByPage[page][i];
  document.querySelectorAll(".span").forEach(s=>s.classList.remove("sel"));
  const el=document.querySelector('.span[data-page="'+page+'"][data-i="'+i+'"]'); if(el) el.classList.add("sel");
  const t=$("editText"); t.disabled=false; t.value=sp.text; t.focus(); $("applyBtn").disabled=false;
  const bold=sp.flags&16?"Bold ":"", ital=sp.flags&2?"Italic ":"";
  $("fontInfo").textContent=`p${page+1} • ${sp.font||"font"} • ${sp.size.toFixed(1)}pt ${bold}${ital}`.trim();
  $("editHint").textContent="Editing selected text:";
}
function clearSel(){ selPage=selIdx=-1;
  $("editText").value=""; $("editText").disabled=true;
  $("applyBtn").disabled=true; $("fontInfo").textContent="";
  $("editHint").textContent = editMode ? "Click a text span on the page." : "Turn on “✎ Edit”, then click a text span on the page.";
  document.querySelectorAll(".span").forEach(s=>s.classList.remove("sel")); }
async function applyEdit(){
  if(!editMode || selIdx<0) return;
  // (Rotated pages are handled correctly — text edits land at the original
  // position regardless of page rotation, so no warning is needed.)
  setStatus("Applying edit ...");
  try{ const j=await api("/api/edit_text",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({page:selPage,span_index:selIdx,new_text:$("editText").value})});
    await reloadPage(selPage); clearSel();
    if(j && j.font_preserved===false){
      setStatus("Text updated — note: the original font “"+(j.font||"")+"” couldn’t be reused, so this edit uses a close standard font and may look slightly different.","warn");
    } else {
      setStatus("Text updated.","ok");
    }
  }catch(err){ setStatus(err.message,"err"); }
}

// ---------- signature placement (drag a box) ----------
function toggleSign(){ if(!editMode){ setStatus("Turn on “✎ Edit” first to place a signature.","err"); return; } setSign(!signMode); }
function setSign(on){
  signMode=on && !!sigB64 && editMode;    // signing is an edit action
  if(!signMode) cancelSigPreview();       // drop any in-progress placement preview
  const sb=$("signBtn");
  sb.classList.toggle("on",signMode);
  sb.title = signMode ? "Signing… click to stop" : "Place signature";
  sb.setAttribute("aria-label", sb.title);
  document.querySelectorAll(".stage").forEach(s=>s.classList.toggle("signing",signMode));
  setStatus(signMode ? "Sign mode ON — drag a box where the signature should go." : "Sign mode off.", signMode?"ok":"");
}
function attachSign(stage, i){
  let start=null, rectEl=null;
  const img=()=>stage.querySelector("img");
  stage.addEventListener("mousedown", e=>{
    if(!signMode || e.button!==0 || _sigPreview) return;   // a preview is being positioned
    const b=img().getBoundingClientRect();
    start={x:e.clientX-b.left, y:e.clientY-b.top};
    rectEl=document.createElement("div"); rectEl.className="selrect"; stage.appendChild(rectEl);
    e.preventDefault();
  });
  stage.addEventListener("mousemove", e=>{
    if(!start||!rectEl) return;
    const b=img().getBoundingClientRect();
    const x=e.clientX-b.left, y=e.clientY-b.top;
    rectEl.style.left=Math.min(start.x,x)+"px"; rectEl.style.top=Math.min(start.y,y)+"px";
    rectEl.style.width=Math.abs(x-start.x)+"px"; rectEl.style.height=Math.abs(y-start.y)+"px";
  });
  window.addEventListener("mouseup", e=>{
    if(!start||!rectEl) return;
    const b=img().getBoundingClientRect();
    const x=e.clientX-b.left, y=e.clientY-b.top;
    const px0=Math.min(start.x,x), py0=Math.min(start.y,y), pw=Math.abs(x-start.x), ph=Math.abs(y-start.y);
    start=null; const el=rectEl; rectEl=null; el.remove();
    if(pw<8||ph<8){ return; }
    // Don't commit yet — drop a movable/resizable preview so the user can place
    // the signature exactly before it's burned in.
    startSigPreview(stage, i, px0, py0, pw, ph);
  });
}

// ---------- signature placement preview (drag to move · corner to resize) ----
// State (x/y/w/h in display px, relative to the page image) is tracked
// explicitly rather than read back from the DOM, and the move/resize listeners
// live on `document` only for the duration of one drag — the robust pattern that
// avoids stale globals and cross-listener interference.
let _sigPreview=null;   // {el, stage, i, x, y, w, h, maxW, maxH}
function applySigPreview(){
  const S=_sigPreview; if(!S) return;
  S.el.style.left=S.x+"px"; S.el.style.top=S.y+"px";
  S.el.style.width=S.w+"px"; S.el.style.height=S.h+"px";
  // keep the Place/Cancel bar on-screen: above the box, or below if it'd clip the top
  const acts=S.el.querySelector(".acts");
  if(acts) acts.style.top = (S.y < 34) ? "calc(100% + 6px)" : "-30px";
}
function startSigPreview(stage, i, x, y, w, h){
  cancelSigPreview();
  const imgEl=stage.querySelector("img");
  const maxW=imgEl.clientWidth||imgEl.naturalWidth||w, maxH=imgEl.clientHeight||imgEl.naturalHeight||h;
  x=Math.max(0,Math.min(x,maxW-20)); y=Math.max(0,Math.min(y,maxH-20));
  w=Math.max(20,Math.min(w,maxW-x)); h=Math.max(16,Math.min(h,maxH-y));
  const el=document.createElement("div");
  el.className="sigprev";
  el.innerHTML=`<img src="data:image/png;base64,${sigB64}" alt="signature" draggable="false">
    <div class="rz" title="Drag to resize"></div>
    <div class="acts"><button id="sigPlace" type="button">Place</button><button class="ghost" id="sigCancel" type="button">Cancel</button></div>`;
  stage.appendChild(el);
  _sigPreview={el, stage, i, x, y, w, h, maxW, maxH};
  applySigPreview();
  el.addEventListener("mousedown", ev=>{
    if(ev.target.closest(".acts")) return;          // let the buttons work
    ev.preventDefault(); ev.stopPropagation();
    const S=_sigPreview; if(!S) return;
    const resize=ev.target.classList.contains("rz");
    const sx=ev.clientX, sy=ev.clientY, ox=S.x, oy=S.y, ow=S.w, oh=S.h;
    const onMove=mm=>{
      const dx=mm.clientX-sx, dy=mm.clientY-sy;
      if(resize){
        S.w=Math.max(20, Math.min(ow+dx, S.maxW-S.x));
        S.h=Math.max(16, Math.min(oh+dy, S.maxH-S.y));
      } else {
        S.x=Math.max(0, Math.min(ox+dx, S.maxW-S.w));
        S.y=Math.max(0, Math.min(oy+dy, S.maxH-S.h));
      }
      applySigPreview();
    };
    const onUp=()=>{ document.removeEventListener("mousemove",onMove); document.removeEventListener("mouseup",onUp); };
    document.addEventListener("mousemove",onMove);
    document.addEventListener("mouseup",onUp);
  });
  document.getElementById("sigPlace").onclick=commitSigPreview;
  document.getElementById("sigCancel").onclick=cancelSigPreview;
  setStatus("Position the signature — drag to move, corner to resize — then click Place (Esc cancels).","");
}
function cancelSigPreview(){
  if(_sigPreview){ _sigPreview.el.remove(); _sigPreview=null; }
}
async function commitSigPreview(){
  if(!_sigPreview) return;
  const {stage, i, x, y, w, h}=_sigPreview;
  const sc=scaleByPage[i]||1;
  cancelSigPreview();
  const rect=[x/sc, y/sc, (x+w)/sc, (y+h)/sc];
  setStatus("Placing signature on page "+(i+1)+" ...");
  try{ await api("/api/sign",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({page:i, rect, data_b64:sigB64, remove_white:$("sigKnockout").checked})});
    await reloadPage(i); setStatus("Signature added to page "+(i+1)+". Use Undo to remove it.","ok");
  }catch(err){ setStatus(err.message,"err"); }
}

// ---------- edit mode toggle (read-only by default) ----------
function toggleEdit(){ if(pages<=0) return; setEdit(!editMode); }
function setEdit(on){
  editMode = !!on && pages>0;
  if(!editMode) setSign(false);            // leaving edit mode cancels signing
  const b=$("editBtn");
  b.classList.toggle("on", editMode);      // green when active
  b.title = editMode ? "Editing — click to lock (read-only)" : "Edit — turn on to change text or place a signature";
  b.setAttribute("aria-label", b.title);
  // spans only become clickable when an .editing ancestor is present
  $("viewer").classList.toggle("editing", editMode);
  $("signBtn").disabled = !editMode || !sigB64;
  clearSel();
  refreshSpanVisibility();   // draw spans now that we're editing (or remove them when leaving)
  setStatus(editMode ? "Edit mode ON — click text to edit, or place a signature."
                     : "Read-only. Turn on “✎ Edit” to make changes.", editMode?"ok":"");
}

// Spans (the clickable text boxes) are only fetched/drawn while editing. In the
// default read-only view this avoids a /api/spans request and a div-per-span for
// every page the user scrolls past — a real saving on large, text-dense PDFs.
function refreshSpanVisibility(){
  if(editMode){
    document.querySelectorAll(".stage").forEach(st=>{
      const img=st.querySelector("img");
      if(img && img.complete && img.naturalWidth && !st.querySelector(".span")) drawSpans(st, +st.dataset.page);
    });
  } else {
    document.querySelectorAll(".span").forEach(s=>s.remove());
    spansByPage={};
  }
}

async function undo(){
  setStatus("Undoing ...");
  try{ const j=await api("/api/undo",{method:"POST"});
    pages=j.pages; cur=Math.min(cur,pages-1); await rebuild();
    setStatus("Undid last change ("+j.undone+").","ok");
  }catch(err){ setStatus(err.message,"err"); }
}

// ---------- navigation / zoom (continuous scroll, fit-to-width) ----------
function scrollToPage(i){
  const v=$("viewer");
  const stage=v.querySelector('.stage[data-page="'+i+'"]');
  if(!stage) return;
  const img=stage.querySelector("img");
  if(img && !img.src && img.dataset.src) img.src=img.dataset.src;   // pull in a lazy page
  // Scroll the viewer directly (more reliable than scrollIntoView inside the
  // centered flex pane, where smooth scrollIntoView sometimes didn't move).
  const vr=v.getBoundingClientRect(), sr=stage.getBoundingClientRect();
  const top=(sr.top - vr.top) + v.scrollTop - 14;   // small gap above the page
  v.scrollTo({top:Math.max(0,top), behavior:"smooth"});
}
function go(d){ cur=Math.min(pages-1,Math.max(0,cur+d)); scrollToPage(cur); updateButtons(); }
function gotoPageInput(){
  if(pages<=0) return;
  let v=parseInt($("pageInput").value,10);
  if(isNaN(v)){ updateButtons(); return; }          // restore valid value
  cur=Math.min(pages,Math.max(1,v))-1;               // clamp into range
  scrollToPage(cur); updateButtons();
}
function zoomBy(d){ if(pages<=0) return; zoomTo(zoomMult+d); }

// Re-render at a new zoom, keeping the point at viewer-height `cy` (px from the
// top of the viewer; defaults to centre) over the same spot in the document.
function zoomTo(mult, cy){
  const v=$("viewer"), w=$("pwrap");
  mult=Math.min(3,Math.max(0.5,+mult.toFixed(2)));
  if(mult===zoomMult) return;
  if(cy===undefined) cy=v.clientHeight/2;
  const frac=(v.scrollTop+cy)/Math.max(1,w.scrollHeight);
  zoomMult=mult;
  rebuild().then(()=>{ v.scrollTop=Math.max(0, frac*w.scrollHeight - cy); });
}

// ---------- trackpad pinch zoom (live CSS scale, sharp re-render on settle) --
// macOS trackpad pinches arrive as ctrl+wheel (Chrome/Edge/Firefox) or as
// gesture* events (Safari). While pinching, one CSS transform scales every
// page instantly; when fingers settle the document re-renders sharp, anchored
// at the pinch point.
let pinch={scale:1, t:null, ox:0, oy:0, cy:0, active:false};
function pinchStart(cx, cyv){
  const w=$("pwrap"), v=$("viewer");
  const r=w.getBoundingClientRect();
  pinch.ox=cx-r.left; pinch.oy=cyv-r.top;
  pinch.cy=cyv-v.getBoundingClientRect().top;
  pinch.active=true;
}
function pinchApply(){
  const w=$("pwrap");
  w.style.transformOrigin=pinch.ox+"px "+pinch.oy+"px";
  w.style.transform="scale("+pinch.scale+")";
  $("zoomLabel").textContent=Math.round(zoomMult*pinch.scale*100)+"%";
}
function pinchCommit(){
  if(!pinch.active) return;
  const s=pinch.scale;
  pinch.scale=1; pinch.t=null; pinch.active=false;
  const w=$("pwrap"); w.style.transform=""; w.style.transformOrigin="";
  if(Math.abs(s-1)<0.01){ updateButtons(); return; }
  zoomTo(zoomMult*s, pinch.cy);
}
const pinchClamp=s=>Math.min(3/zoomMult, Math.max(0.5/zoomMult, s));
$("viewer").addEventListener("wheel", e=>{
  if(!e.ctrlKey || pages<=0) return;          // plain scrolling passes through
  e.preventDefault();
  if(!pinch.active) pinchStart(e.clientX, e.clientY);
  else clearTimeout(pinch.t);
  pinch.scale=pinchClamp(pinch.scale*Math.exp(-e.deltaY*0.01));
  pinchApply();
  pinch.t=setTimeout(pinchCommit, 220);
},{passive:false});
// Safari's native pinch events
let gBase=1;
$("viewer").addEventListener("gesturestart", e=>{
  if(pages<=0) return; e.preventDefault();
  gBase=1; pinchStart(e.clientX, e.clientY);
},{passive:false});
$("viewer").addEventListener("gesturechange", e=>{
  if(pages<=0 || !pinch.active) return; e.preventDefault();
  pinch.scale=pinchClamp(gBase*e.scale); pinchApply();
},{passive:false});
$("viewer").addEventListener("gestureend", e=>{
  if(pages<=0) return; e.preventDefault(); pinchCommit();
},{passive:false});

// Double-click toggles 100% <-> 200%, centred on the click (not on text spans,
// where a double-click means "I'm selecting text to edit").
$("viewer").addEventListener("dblclick", e=>{
  if(pages<=0 || signMode) return;
  if(e.target.closest(".span")) return;
  const cy=e.clientY-$("viewer").getBoundingClientRect().top;
  zoomTo(Math.abs(zoomMult-1)<0.05 ? 2.0 : 1.0, cy);
});

$("viewer").addEventListener("scroll", ()=>{
  if(pages<=0) return;
  const vt=$("viewer").getBoundingClientRect().top + 80;
  let best=0, bestDist=1e9;
  document.querySelectorAll(".stage").forEach(s=>{
    const d=Math.abs(s.getBoundingClientRect().top - vt);
    if(d<bestDist){ bestDist=d; best=+s.dataset.page; }
  });
  if(best!==cur){ cur=best; updateButtons(); }
});

let resizeT=null, _lastTW=0;
window.addEventListener("resize", ()=>{
  if(pages<=0) return; clearTimeout(resizeT);
  // only re-render when the usable width actually changed (height-only
  // resizes are free — pages are laid out vertically anyway)
  resizeT=setTimeout(()=>{ if(targetWidth()!==_lastTW) rebuild(); },300);
});

function updateButtons(){
  const has=pages>0;
  const pi=$("pageInput");
  if(document.activeElement!==pi) pi.value = has ? (cur+1) : "";   // don't fight typing
  pi.max = has ? pages : 1;
  pi.disabled = !has;
  $("pageTotal").textContent = has ? "/ "+pages : "/ –";
  $("pagePrev").disabled = !has || cur<=0;
  $("pageNext").disabled = !has || cur>=pages-1;
  $("zoomLabel").textContent = Math.round(zoomMult*100)+"%";
  for(const id of ["compressBtn","pngBtn","editBtn","saveBtn","delBtn","orgBtn","closeBtn","copyBtn","printBtn","textBtn"]) $(id).disabled=!has;
  if(!has) closeCompressMenu();
  $("signBtn").disabled = !has || !sigB64 || !editMode;
  $("findBox").disabled = !has;
  $("findPill").classList.toggle("dis", !has);
  if(!has){ if($("findBox").value){ $("findBox").value=""; _lastFindQ=""; } clearFinds(); $("findClear").style.display="none"; }
  updateFindUI();
  if(!has && editMode) setEdit(false);     // closing the doc returns to read-only
}

// ---------- modal infrastructure ----------
let _modalDismiss=null;   // lets promise-based dialogs resolve on Escape/backdrop
function closeModal(){
  $("overlay").classList.remove("show"); $("modal").innerHTML="";
  if(modalObserver){ modalObserver.disconnect(); modalObserver=null; }
  if(_modalDismiss){ const f=_modalDismiss; _modalDismiss=null; f(); }
}
$("overlay").addEventListener("mousedown", e=>{ if(e.target.id==="overlay") closeModal(); });
document.addEventListener("keydown", e=>{ if(e.key==="Escape"){ if($("compPop")){ closeCompressMenu(); return; } if(_sigPreview){ cancelSigPreview(); return; } closeModal(); } });

// ---------- thumbnail cache + lazy loading (Organize / Copy Pages) ----------
// Thumbnails are fetched once per document version (epoch) and kept as object
// URLs, so re-rendering the sheet after a drag/rotate/select is instant and a
// 100-page PDF only loads the thumbnails you actually scroll to.
const thumbCache=new Map();          // "epoch:page:width" -> object URL
let modalObserver=null, _thumbEpoch=-1;
function clearThumbCache(){
  for(const u of thumbCache.values()) URL.revokeObjectURL(u);
  thumbCache.clear();
}
function loadThumb(img){
  const key=img.dataset.key;
  if(thumbCache.has(key)){ img.src=thumbCache.get(key); return; }
  fetch(img.dataset.url).then(r=>{ if(!r.ok) throw 0; return r.blob(); }).then(b=>{
    if(!thumbCache.has(key)) thumbCache.set(key, URL.createObjectURL(b));
    img.src=thumbCache.get(key);
  }).catch(()=>{});
}
function lazyThumbs(){
  if(modalObserver) modalObserver.disconnect();
  const imgs=[...$("modal").querySelectorAll("img[data-url]")];
  // first screenful loads immediately (works even if the observer misbehaves
  // while the sheet is animating in); the rest load lazily as you scroll
  imgs.slice(0,12).forEach(loadThumb);
  if(!window.IntersectionObserver){ imgs.forEach(loadThumb); return; }
  const root=$("modal").querySelector(".mbody");
  modalObserver=new IntersectionObserver(es=>{
    for(const en of es){
      if(!en.isIntersecting) continue;
      loadThumb(en.target); modalObserver.unobserve(en.target);
    }
  },{root, rootMargin:"600px 0px"});
  imgs.slice(12).forEach(i=>modalObserver.observe(i));
}

// generic drag-to-reorder over an array; rerender() rebuilds the list DOM
function enableDragReorder(containerId, arr, rerender){
  const c=$(containerId); if(!c) return; let from=null;
  c.querySelectorAll("[draggable='true']").forEach(el=>{
    el.ondragstart=()=>{ from=+el.dataset.pos; };
    el.ondragover=ev=>{ ev.preventDefault(); el.classList.add("over"); };
    el.ondragleave=()=>el.classList.remove("over");
    el.ondrop=ev=>{ ev.preventDefault(); el.classList.remove("over");
      const to=+el.dataset.pos;
      if(from===null||to===from) return;
      const [m]=arr.splice(from,1); arr.splice(to,0,m); from=null; rerender(); };
  });
}

// ---------- merge order modal ----------
function openMergeModal(){ renderMergeModal(); $("overlay").classList.add("show"); }
function renderMergeModal(){
  const curOpt = pages>0
    ? `<label class="check"><input type="checkbox" id="mInc" ${mergeIncludeCurrent?"checked":""}
        onchange="mergeIncludeCurrent=this.checked"> Put the currently open document first (${pages} pages)</label>`
    : "";
  const items = mergeFiles.map((f,i)=>`
    <li draggable="true" data-pos="${i}">
      <span class="num">${i+1}</span>
      <span class="nm" title="${esc(f.name)}">${esc(f.name)}</span>
      <button class="ghost" onclick="mMove(${i},-1)">↑</button>
      <button class="ghost" onclick="mMove(${i},1)">↓</button>
      <button class="ghost" onclick="mDel(${i})">✕</button>
    </li>`).join("");
  $("modal").innerHTML=`
    <div class="mhead"><h3>Merge PDFs — set the order</h3></div>
    <div class="mbody">
      <div class="hint">Files are merged top → bottom. Drag a row, or use ↑ ↓.</div>
      ${curOpt}
      <ul class="flist" id="mList">${items}</ul>
    </div>
    <div class="mfoot">
      <button class="ghost" onclick="closeModal()">Cancel</button>
      <button onclick="doMerge()">Merge ${mergeFiles.length} file(s)</button>
    </div>`;
  enableDragReorder("mList", mergeFiles, renderMergeModal);
}
function mMove(i,d){ const j=i+d; if(j<0||j>=mergeFiles.length) return;
  [mergeFiles[i],mergeFiles[j]]=[mergeFiles[j],mergeFiles[i]]; renderMergeModal(); }
function mDel(i){ mergeFiles.splice(i,1); if(!mergeFiles.length){ closeModal(); return; } renderMergeModal(); }
async function doMerge(){
  if(!mergeFiles.length) return;
  setStatus("Merging "+mergeFiles.length+" file(s) ...");
  busy("Merging "+mergeFiles.length+" file(s) …");
  try{
    const payload=mergeFiles.map(f=>({filename:f.name,data_b64:f.b64}));
    const j=await api("/api/merge",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({files:payload, include_current:mergeIncludeCurrent})});
    pages=j.pages; cur=0; closeModal(); await rebuild();
    setStatus("Merged into one PDF ("+pages+" pages). Undo brings the previous document back.","ok");
  }catch(err){ setStatus(err.message,"err"); }
  finally{ unbusy(); }
}

// ---------- organize pages modal (thumbnails, drag-reorder, zoom) ----------
let orgRot={};   // pending rotation per ORIGINAL page index (0/90/180/270)
function openOrganize(){
  if(pages<=0) return;
  orgOrder=[...Array(pages).keys()]; orgRot={};
  $("overlay").classList.add("show"); renderOrganize();
}
function renderOrganize(){
  const thumbs = orgOrder.map((pageIdx,pos)=>{
    const deg=orgRot[pageIdx]||0;
    const [pw,ph]=sizes[pageIdx]||[595,842];
    // container keeps width=thumbW; height follows the page's aspect, swapped
    // when the pending rotation turns the page on its side
    const boxH=Math.round(thumbW*((deg%180===0)?(ph/pw):(pw/ph)));
    const imgW=(deg%180===0)?thumbW:boxH;
    return `
    <div class="thumb" draggable="true" data-pos="${pos}" style="width:${thumbW}px;height:${boxH}px">
      <span class="badge">${pos+1}${pageIdx!==pos?` ← p${pageIdx+1}`:""}${deg?` ⟳${deg}°`:""}</span>
      <button class="rotbtn" draggable="false" title="Rotate this page 90°"
        onclick="event.stopPropagation();orgRotate(${pageIdx})">⟳</button>
      <button class="delbtn" draggable="false" title="Remove this page"
        ${orgOrder.length<=1?"disabled":""}
        onclick="event.stopPropagation();orgDel(${pos})">✕</button>
      <img data-url="/api/page?n=${pageIdx}&w=${renderWidth(thumbW)}" data-key="${curEpoch}:${pageIdx}:${renderWidth(thumbW)}"
        alt="page ${pageIdx+1}" draggable="false"
        style="position:absolute;left:50%;top:50%;width:${imgW}px;background:#fff;
               transform:translate(-50%,-50%) rotate(${deg}deg)">
    </div>`;}).join("");
  $("modal").innerHTML=`
    <div class="mhead">
      <h3>Organize Pages — drag to reorder · ⟳ rotate · ✕ remove</h3>
      <span style="margin-left:auto"></span>
      <button class="ghost" onclick="thumbZoom(-30)">– thumb</button>
      <button class="ghost" onclick="thumbZoom(30)">+ thumb</button>
    </div>
    <div class="mbody"><div class="thumbs" id="thumbs">${thumbs}</div></div>
    <div class="mfoot">
      <button class="ghost" onclick="closeModal()">Cancel</button>
      <button onclick="applyOrder()">Apply changes</button>
    </div>`;
  enableDragReorder("thumbs", orgOrder, renderOrganize);
  lazyThumbs();
}
function orgRotate(pageIdx){ orgRot[pageIdx]=((orgRot[pageIdx]||0)+90)%360; renderOrganize(); }
function orgDel(pos){ if(orgOrder.length<=1) return; orgOrder.splice(pos,1); renderOrganize(); }
function thumbZoom(d){ thumbW=Math.min(380,Math.max(90,thumbW+d)); renderOrganize(); }
async function applyOrder(){
  setStatus("Updating pages ...");
  busy("Updating pages …");
  try{ const rotations=orgOrder.map(p=>orgRot[p]||0);
    const j=await api("/api/reorder",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({order:orgOrder, rotations})});
    pages=j.pages; cur=0; closeModal(); await rebuild();
    setStatus("Pages updated.","ok");
  }catch(err){ setStatus(err.message,"err"); }
  finally{ unbusy(); }
}

// ---------- copy pages -> brand-new PDF (open document untouched) ----------
let copySel=new Set();
function openCopyPages(){
  if(pages<=0) return;
  copySel=new Set();
  $("overlay").classList.add("show"); renderCopyModal();
}
function renderCopyModal(){
  const thumbs=[...Array(pages).keys()].map(i=>{
    const [pw,ph]=sizes[i]||[595,842];
    const h=Math.round(120*ph/pw);
    return `
    <div class="thumb ${copySel.has(i)?"picked":""}" onclick="copyToggle(${i})"
         style="width:120px;height:${h}px;cursor:pointer">
      <span class="badge">${i+1}</span>
      ${copySel.has(i)?'<span class="pickmark">✓</span>':""}
      <img data-url="/api/page?n=${i}&w=${renderWidth(120)}" data-key="${curEpoch}:${i}:${renderWidth(120)}"
        width="120" alt="page ${i+1}" draggable="false" style="background:#fff">
    </div>`;}).join("");
  $("modal").innerHTML=`
    <div class="mhead"><h3>Copy pages → new PDF</h3>
      <span class="pill" style="margin-left:auto">${copySel.size} selected</span></div>
    <div class="mbody">
      <div class="hint" style="margin-bottom:10px">Click pages to select them. They are
        copied into a brand-new PDF — the open document is untouched.</div>
      <div class="thumbs">${thumbs}</div>
    </div>
    <div class="mfoot">
      <button class="ghost" onclick="closeModal()">Cancel</button>
      <button ${copySel.size?"":"disabled"} onclick="doCopyPages()">Copy ${copySel.size||"0"} page(s)</button>
    </div>`;
  lazyThumbs();
}
function copyToggle(i){ copySel.has(i)?copySel.delete(i):copySel.add(i); renderCopyModal(); }
function doCopyPages(){
  if(!copySel.size) return;
  const list=[...copySel].sort((a,b)=>a-b).join(",");
  const n=copySel.size;
  closeModal();
  download("/api/copy_pages?pages="+list);
  setStatus("Copied "+n+" page(s) into a new PDF — download started.","ok");
}

// ---------- view / copy text ----------
// The viewer renders pages as images (sharp, exact), so text isn't directly
// selectable on the page. This panel pulls the real text out so it can be read,
// selected, and copied — current page by default, whole document on demand.
function openTextModal(){ if(pages>0) showTextModal(true); }   // whole document by default
async function showTextModal(all){
  setStatus("Extracting text …");
  let txt="";
  try{ const j=await api("/api/text?"+(all?"all=1":("n="+cur))); txt=j.text||""; }
  catch(err){ setStatus(err.message,"err"); return; }
  setStatus("");
  $("modal").innerHTML=`
    <div class="mhead"><h3 style="white-space:nowrap">Text</h3>
      <input type="search" id="txtFind" class="findbox" placeholder="🔍 Find in text" autocomplete="off"
        aria-label="Find in extracted text" style="margin-left:auto;width:150px">
      <span class="findcount" id="txtFindCount"></span>
      <button class="ghost" id="txtFindPrev" aria-label="Previous match" title="Previous (⇧⏎)">‹</button>
      <button class="ghost" id="txtFindNext" aria-label="Next match" title="Next (⏎)">›</button>
      <label class="check" style="margin-left:12px;white-space:nowrap"><input type="checkbox" id="txtAll" ${all?"checked":""}> Whole document</label></div>
    <div class="mbody">
      <div id="txtOut" class="txtbox" tabindex="0" aria-label="Extracted document text">${esc(txt)}</div>
      <div class="hint" style="margin-top:8px">${txt.trim()?"Type in “Find in text” to highlight every match in yellow (the current one in orange), or use “Copy all”.":"No selectable text here — these pages may be scanned images."}</div>
    </div>
    <div class="mfoot">
      <button class="ghost" onclick="closeModal()">Close</button>
      <button id="txtCopy" ${txt.trim()?"":"disabled"}>Copy all</button>
    </div>`;
  $("txtAll").onchange=e=>showTextModal(e.target.checked);
  $("txtCopy").onclick=()=>{
    const done=()=>setStatus("Text copied to the clipboard.","ok");
    const fallback=()=>{ const t=document.createElement("textarea"); t.value=txt;
      document.body.appendChild(t); t.select(); try{ document.execCommand("copy"); }catch(e){} t.remove(); done(); };
    if(navigator.clipboard && navigator.clipboard.writeText){
      navigator.clipboard.writeText(txt).then(done).catch(fallback);
    } else fallback();
  };
  // ---- find within the extracted text: highlight EVERY match (yellow), with
  // the current match in orange. Focus stays in the find box throughout. ----
  const box=$("txtOut"), lc=txt.toLowerCase();
  let tHits=[], tIdx=-1;
  const updTC=()=>{ $("txtFindCount").textContent = tHits.length ? ((tIdx+1)+"/"+tHits.length)
                      : (($("txtFind").value||"").trim()?"0":"");
    $("txtFindPrev").disabled=tHits.length<2; $("txtFindNext").disabled=tHits.length<2; };
  const renderMarks=()=>{
    const n=($("txtFind").value||"").length;
    if(!n || !tHits.length){ box.innerHTML=esc(txt); return; }
    let out="", last=0;
    tHits.forEach((p,i)=>{ out+=esc(txt.slice(last,p))
        +'<mark'+(i===tIdx?' class="cur"':'')+' id="thit'+i+'">'+esc(txt.slice(p,p+n))+'</mark>'; last=p+n; });
    out+=esc(txt.slice(last)); box.innerHTML=out;
  };
  const scrollToCur=()=>{ const el=document.getElementById("thit"+tIdx);
    if(el) box.scrollTop=Math.max(0, el.offsetTop - box.clientHeight/2); };
  const runFind=()=>{
    const q=($("txtFind").value||"").toLowerCase();
    tHits=[];
    if(q){ let p=lc.indexOf(q); while(p>=0){ tHits.push(p); p=lc.indexOf(q, p+q.length); if(tHits.length>=5000) break; } }
    tIdx=tHits.length?0:-1;
    renderMarks(); scrollToCur(); updTC();
  };
  const step=d=>{ if(tHits.length<1) return;
    const old=box.querySelector("mark.cur"); if(old) old.classList.remove("cur");
    tIdx=(tIdx+d+tHits.length)%tHits.length;
    const el=document.getElementById("thit"+tIdx); if(el) el.classList.add("cur");
    scrollToCur(); updTC();
  };
  let _tfT=null;
  $("txtFind").addEventListener("input", ()=>{ clearTimeout(_tfT); _tfT=setTimeout(runFind,120); });
  $("txtFind").addEventListener("keydown", e=>{ if(e.key==="Enter"){ e.preventDefault();
    if(tHits.length) step(e.shiftKey?-1:1); else { clearTimeout(_tfT); runFind(); } } });
  $("txtFindPrev").onclick=()=>step(-1);
  $("txtFindNext").onclick=()=>step(1);
  updTC();
  $("overlay").classList.add("show");
  setTimeout(()=>{ try{ $("txtFind").focus(); }catch(e){} }, 30);
}

// ---------- delete pages ----------
function buildDots(){
  const box=$("pageDots"); box.innerHTML="";
  for(let i=0;i<pages;i++){ const l=document.createElement("label");
    l.innerHTML=`<input type="checkbox" value="${i}"> ${i+1}`; box.appendChild(l); }
}
async function deletePages(){
  const sel=[...document.querySelectorAll("#pageDots input:checked")].map(c=>+c.value);
  if(!sel.length){ setStatus("Select at least one page to delete.","err"); return; }
  if(sel.length>=pages){ setStatus("Cannot delete every page.","err"); return; }
  setStatus("Deleting "+sel.length+" page(s) ...");
  try{ const j=await api("/api/delete_pages",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({pages:sel})});
    pages=j.pages; cur=Math.min(cur,pages-1); await rebuild();
    setStatus("Deleted. "+pages+" pages remain.","ok");
  }catch(err){ setStatus(err.message,"err"); }
}

// ---------- compress / export ----------
// Compress level is chosen from a popover off the Compress tile; the last choice
// is remembered.
let lastCompress="medium";
try{ lastCompress=localStorage.getItem("compressLevel")||"medium"; }catch(e){}
const COMPRESS_OPTS=[
  ["high","High","best quality · &lt;1 MB"],
  ["medium","Medium","balanced · &lt;700 KB"],
  ["low","Low","smallest · &lt;200 KB"],
];
function openCompressMenu(){
  if(pages<=0) return;
  if($("compPop")){ closeCompressMenu(); return; }   // toggle
  const btn=$("compressBtn");
  const pop=document.createElement("div"); pop.className="popover"; pop.id="compPop";
  pop.setAttribute("role","menu");
  pop.innerHTML='<div class="cap">COMPRESSION LEVEL</div>'+COMPRESS_OPTS.map(([v,n,s])=>
    `<div class="opt${v===lastCompress?' cur':''}" role="menuitem" tabindex="0" data-v="${v}"><b style="font-weight:500">${n}</b><span class="sub">${s}</span></div>`).join("");
  document.body.appendChild(pop);
  const r=btn.getBoundingClientRect();
  pop.style.top=(r.bottom+6)+"px";
  let left=r.left, pw=pop.offsetWidth;
  if(left+pw>window.innerWidth-8) left=window.innerWidth-8-pw;
  pop.style.left=Math.max(8,left)+"px";
  pop.querySelectorAll(".opt").forEach(o=>{
    const pick=()=>{ const v=o.dataset.v; lastCompress=v; try{localStorage.setItem("compressLevel",v);}catch(e){}
      closeCompressMenu(); runCompress(v); };
    o.onclick=pick;
    o.onkeydown=e=>{ if(e.key==="Enter"||e.key===" "){ e.preventDefault(); pick(); } };
  });
  setTimeout(()=>document.addEventListener("mousedown",compMenuOutside),0);
}
function compMenuOutside(e){ const p=$("compPop"); if(p && !p.contains(e.target) && !$("compressBtn").contains(e.target)) closeCompressMenu(); }
function closeCompressMenu(){ const p=$("compPop"); if(p) p.remove(); document.removeEventListener("mousedown",compMenuOutside); }
async function runCompress(level){
  if(pages<=0) return;
  level=level||lastCompress||"medium";
  setStatus("Compressing ("+level+") ...");
  busy("Compressing — this can take a moment on long documents …");
  try{ const j=await api("/api/compress",{method:"POST",
        headers:{"Content-Type":"application/json"}, body:JSON.stringify({level})});
    await rebuild();
    const note = j.met_target ? "" :
      `  — couldn't get under ${j.target_kb} KB without dropping below readable quality; this is the smallest at this level`;
    setStatus(`Compressed (${j.level}, target <${j.target_kb} KB): ${j.before_kb} KB → ${j.after_kb} KB  (${j.saved_pct}% smaller).`+note,"ok");
  }catch(err){ setStatus(err.message,"err"); }
  finally{ unbusy(); }
}
function download(path){ const a=$("dl"); a.href=path; a.download=""; a.click(); setStatus("Download started.","ok"); }
function exportPng(){ download("/api/export_png?n="+cur+"&zoom=4.0"); }
// ---------- print ----------
// Prints the REAL PDF (vector) — the browser renders it natively at the
// printer's own resolution, so text/lines stay perfectly sharp. The document
// is loaded as a blob into a HIDDEN iframe (never a new tab) for EVERY browser,
// then the macOS system print dialog (⌘P) is opened on it. Reflects all current
// edits via /api/pdf. If a browser can't auto-trigger print on the iframe, the
// user is told to press ⌘P — we never punt the PDF into a separate tab.
let _printing=false;
async function printDoc(){
  if(pages<=0 || _printing) return;       // guard against double-trigger
  _printing=true;
  setStatus("Preparing document for printing …","");
  try{
    // fit=1 -> content scaled to fill the page; cache-busted each time.
    const res=await fetch(location.origin+"/api/pdf?fit=1&t="+Date.now());
    if(!res.ok) throw new Error("HTTP "+res.status);
    const url=URL.createObjectURL(await res.blob());
    // Fresh hidden iframe every time — reusing one is what made the 2nd
    // print silently do nothing. Old frame + blob URL are released first.
    let old=document.getElementById("printFrame");
    if(old){ if(old._url) URL.revokeObjectURL(old._url); old.remove(); }
    const fr=document.createElement("iframe");
    fr.id="printFrame"; fr._url=url;
    fr.setAttribute("aria-hidden","true");
    fr.style.cssText="position:fixed;right:0;bottom:0;width:0;height:0;border:0;visibility:hidden;";
    let fired=false;
    const fire=()=>{
      if(fired) return; fired=true; _printing=false;
      setStatus("Opening the print dialog …","ok");
      try{
        const w=fr.contentWindow;
        w.focus();
        w.print();                       // -> native macOS print panel
      }catch(e){
        // Some Safari builds won't script-print a plugin-rendered PDF iframe.
        setStatus("Press ⌘P to print the document.","warn");
      }
    };
    // Append FIRST, then set src, so the load event fires reliably; a timeout
    // fallback covers PDF viewers that don't emit load.
    fr.onload=()=>setTimeout(fire,250);
    document.body.appendChild(fr);
    fr.src=url;
    setTimeout(()=>{ if(!fired) fire(); },1500);
  }catch(err){
    _printing=false;
    setStatus("Print failed: "+(err.message||err),"err");
  }
}

// ---------- Save dialog (rename + download) ----------
// afterSave: optional action to run once the save has started (used by the
// unsaved-changes guard's "Save first" button to resume the original action).
function openSaveModal(afterSave){
  if(pages<=0) return;
  const hasPath = !!curPath;
  const overwriteRow = hasPath ? `
      <div class="hint" style="margin-bottom:8px">This file:</div>
      <div class="hint" style="word-break:break-all;color:var(--txt);margin-bottom:10px">${esc(curPath)}</div>
      <button id="saveOver" style="width:100%;margin-bottom:14px">Save (overwrite original)</button>
      <div class="hint" style="margin-bottom:8px">— or save a copy to Downloads —</div>` : "";
  $("modal").innerHTML=`
    <div class="mhead"><h3>Save PDF</h3></div>
    <div class="mbody">
      ${overwriteRow}
      <div class="hint" style="margin-bottom:8px">File name</div>
      <input id="saveName" type="text" value="${esc(curName)}" spellcheck="false"
        style="width:100%;background:var(--bg);color:var(--txt);border:1px solid var(--line);
               border-radius:7px;padding:9px 10px;font-size:13px">
      <div class="hint" style="margin-top:10px">A copy is downloaded to your Downloads
        folder under this name; the original file on disk is not modified.</div>
    </div>
    <div class="mfoot">
      <button class="ghost" onclick="closeModal()">Cancel</button>
      <button id="saveGo">${hasPath?"Save a copy":"Save"}</button>
    </div>`;
  const copy=()=>{
    let name=($("saveName").value||"").trim() || curName;
    if(!name.toLowerCase().endsWith(".pdf")) name+=".pdf";
    closeModal();
    download("/api/save?name="+encodeURIComponent(name));
    curName=name; dirty=false;
    // Record the name + clear dirty server-side via a tokened POST (the GET
    // download no longer mutates state). Then refresh the header to match.
    api("/api/mark_saved",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({name})}).then(()=>setTimeout(refreshMeta,300)).catch(()=>{});
    setStatus("Saved a copy of “"+name+"” to Downloads.","ok");
    if(typeof afterSave==="function") setTimeout(afterSave, 250);
  };
  const overwrite=async()=>{
    closeModal();
    setStatus("Saving …");
    try{
      const j=await api("/api/save_inplace",{method:"POST"});
      dirty=false; setStatus("Saved to “"+j.name+"”.","ok");
      setTimeout(refreshMeta, 300);
      if(typeof afterSave==="function") setTimeout(afterSave, 250);
    }catch(err){ setStatus(err.message,"err"); }
  };
  $("saveGo").onclick=copy;
  if(hasPath) $("saveOver").onclick=overwrite;
  $("overlay").classList.add("show");
  const inp=$("saveName");
  inp.focus(); inp.setSelectionRange(0, Math.max(0,(inp.value.lastIndexOf(".pdf")+4)-4));
  inp.addEventListener("keydown", e=>{ if(e.key==="Enter") copy(); });
}

// ---------- About dialog + recent-errors log ----------
// Unexpected errors no longer die silently: they show in the status bar and
// the last 3 are kept for this session, visible under ⓘ About.
const recentErrors=[];
function noteError(msg){
  msg=String(msg||"unknown error");
  recentErrors.unshift(new Date().toLocaleTimeString()+" — "+msg);
  if(recentErrors.length>3) recentErrors.pop();
  setStatus("Unexpected error: "+msg,"err");
}
window.addEventListener("error", e=>{
  const where=e.filename?` (${(e.filename||"").split("/").pop()}:${e.lineno||"?"})`:"";
  noteError((e.message||"Script error")+where);
});
window.addEventListener("unhandledrejection", e=>{
  noteError((e.reason&&e.reason.message)||String(e.reason||"unhandled rejection"));
});

async function openAbout(){
  let info={};
  try{ info=await api("/api/about"); }catch(e){}
  const errs=recentErrors.length
    ? recentErrors.map(x=>`<div class="hint">${esc(x)}</div>`).join("")
    : '<div class="hint">None this session.</div>';
  $("modal").innerHTML=`
    <div class="mhead"><h3>About PyPDF for Mac</h3></div>
    <div class="mbody" style="font-size:13px;line-height:1.8">
      <div><b>Version:</b> ${esc(info.version||"?")}</div>
      <div><b>Engine:</b> ${esc(info.engine||"PyMuPDF")} · Python ${esc(info.python||"?")}</div>
      <div><b>Session started:</b> ${esc(info.started||"?")}</div>
      <div class="hint" style="margin-top:10px">Your PDFs are processed entirely on this Mac
        by a local helper — nothing is uploaded anywhere.</div>
      <h2 style="font-size:12px;text-transform:uppercase;letter-spacing:.6px;color:var(--muted);margin:16px 0 6px">Recent errors</h2>
      ${errs}
    </div>
    <div class="mfoot"><button onclick="closeModal()">Close</button></div>`;
  $("overlay").classList.add("show");
}

// ---------- Close PDF ----------
function closeDoc(){
  if(pages<=0) return;
  guardThen(async ()=>{
    try{
      const j=await api("/api/close",{method:"POST"});
      if(typeof j.epoch==="number") curEpoch=j.epoch;
      resetToEmpty();
      setStatus("Document closed.","ok");
    }catch(err){ setStatus(err.message,"err"); }
  });
}
function resetToEmpty(){
  pages=0; cur=0; zoomMult=1.0; dirty=false; curName="document.pdf";
  sizes=[]; rots=[]; _sizesEpoch=-1; spansByPage={}; scaleByPage={};
  clearThumbCache();
  if($("findBox")) $("findBox").value=""; _lastFindQ=""; clearFinds();
  setEdit(false); setSign(false); clearSel();
  const v=$("viewer"); v.querySelectorAll(".stage").forEach(s=>s.remove());
  if(pageObserver){ pageObserver.disconnect(); pageObserver=null; }
  $("emptyMsg").style.display="block";
  $("meta").textContent="No document open";
  $("docInfo").textContent="—"; $("docPath").textContent="";
  $("pageDots").innerHTML='<span class="pill">No document.</span>';
  $("undoBtn").disabled=true;
  updateButtons();
}

updateButtons();

// Page-jump box: Enter jumps, blur snaps the value back into range.
$("pageInput").addEventListener("keydown", e=>{
  if(e.key==="Enter"){ e.preventDefault(); gotoPageInput(); $("pageInput").blur(); }
});
$("pageInput").addEventListener("blur", ()=>gotoPageInput());

// Find box: Enter searches (or steps to the next hit when the query is unchanged);
// Shift+Enter steps back; Escape clears.
$("findBox").addEventListener("keydown", e=>{
  if(e.key==="Enter"){ e.preventDefault();
    const q=($("findBox").value||"").trim();
    if(q && q===_lastFindQ && findMatches.length) gotoMatch(e.shiftKey?-1:1);
    else doFind();
  } else if(e.key==="Escape"){ e.preventDefault(); clearFindBox(); $("findBox").blur(); }
});
// show the × only while there's text; clear the highlights when emptied
$("findBox").addEventListener("input", ()=>{
  const has=!!($("findBox").value||"").trim();
  $("findClear").style.display = has ? "" : "none";
  if(!has){ _lastFindQ=""; clearFinds(); }
});
$("findBox").addEventListener("focus", ()=>$("findPill").classList.add("focus"));
$("findBox").addEventListener("blur",  ()=>$("findPill").classList.remove("focus"));
// Clear the find field + highlights (the pill's × button).
function clearFindBox(){
  $("findBox").value=""; _lastFindQ=""; clearFinds();
  $("findClear").style.display="none";
}

// ---------- password prompt for locked PDFs --------------------------------
let _unlocking=false;
async function promptUnlock(fname){
  if(_unlocking) return;        // already prompting for this document
  _unlocking=true;
  $("emptyMsg").style.display="none";
  pages=0; updateButtons();
  setStatus("“"+fname+"” is password-protected.","warn");
  let msg="";
  try{
    while(true){
      const pw = prompt(msg+"“"+fname+"” is password-protected.\nEnter the password (leave blank if it only has an owner/permissions lock):","");
      if(pw===null){
        setStatus("“"+fname+"” is locked — reopen it to enter the password.","warn");
        try{ const pg=await api("/api/ping"); curEpoch=pg.epoch; }catch(e){}  // don't immediately re-prompt
        return;
      }
      const j = await api("/api/authenticate",{method:"POST",headers:{"Content-Type":"application/json"},
            body:JSON.stringify({password:pw})});
      if(j.authenticated){
        curEpoch=-1; pages=j.pages; cur=0; await rebuild(); setEdit(false);
        setStatus("Unlocked "+j.filename+" ("+pages+" pages).","ok");
        return;
      }
      msg="Incorrect password — try again.\n\n";
    }
  } finally { _unlocking=false; }
}

// ---------- auto-load a PDF passed on launch (macOS Open With / double-click) ----------
(async function boot(){
  try{
    const st=await api("/api/state");
    if(typeof st.epoch==="number") curEpoch=st.epoch;
    if(st && st.locked){
      await promptUnlock(st.filename);
    } else if(st && st.open){
      pages=st.pages; cur=0; await rebuild(); setEdit(false);
      setStatus("Opened "+st.filename+" ("+pages+" pages).","ok");
    }
  }catch(e){ /* no preloaded document */ }
})();

// ---------- single-instance: pick up PDFs opened into this running server ----
// When another launch hands a new PDF to this server, the document version
// (epoch) changes; this tab notices and loads it instead of opening a new tab.
let _polling=false;
async function pollForNewDoc(){
  // don't fire while a local operation is running or just finished — its
  // epoch bump is ours, not a new document opened from another launch.
  // Also skip while the tab is hidden (no point polling in the background).
  if(_polling || _localOps>0 || document.hidden || (Date.now()-_lastLocalOp)<1200) return;
  _polling=true;
  try{
    const pg=await api("/api/ping");
    if(typeof pg.epoch==="number" && pg.epoch!==curEpoch){
      const st=await api("/api/state");
      curEpoch=pg.epoch;
      if(st.locked){
        try{ window.focus(); }catch(e){}
        promptUnlock(st.filename);
      } else if(st.open){
        pages=st.pages; cur=0; await rebuild(); setEdit(false);
        setStatus("Opened "+st.filename+" ("+pages+" pages).","ok");
        try{ window.focus(); }catch(e){}
      } else if(pages>0){
        // The document was closed from another tab — clear this one too.
        resetToEmpty();
        setStatus("Document closed.","");
      }
    }
  }catch(e){ /* server not reachable */ }
  _polling=false;
}
setInterval(pollForNewDoc, 1500);

// Steady heartbeat so the server knows this tab is still open. When every tab
// is closed these stop, and the server shuts itself down to free memory/CPU.
function beat(){ fetch("/api/ping",{cache:"no-store"}).catch(()=>{}); }
// Self-scheduling so the rate can back off when the tab is hidden: 3s while
// visible (keeps an open tab comfortably alive), 15s in the background (well
// under the 90s idle-shutdown window, and less wasted chatter). Browsers
// throttle background timers anyway; this just trims the visible-but-idle case.
let _beatTimer=null;
function scheduleBeat(){
  clearTimeout(_beatTimer);
  _beatTimer=setTimeout(()=>{ beat(); scheduleBeat(); }, document.hidden ? 15000 : 3000);
}
beat(); scheduleBeat();   // beat immediately on load
// Beat the moment the tab is shown or focused again — this revives the server's
// liveness tracking right before the user interacts, avoiding a stale
// "couldn't reach the helper".
document.addEventListener("visibilitychange", ()=>{ if(!document.hidden) beat(); scheduleBeat(); });
window.addEventListener("focus", ()=>{ beat(); scheduleBeat(); });
window.addEventListener("pageshow", ()=>{ beat(); scheduleBeat(); });
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Server bootstrap
# ---------------------------------------------------------------------------
def find_free_port(preferred):
    for p in [preferred, 8081, 8082, 8090, 0]:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((HOST, p))
            port = s.getsockname()[1]
            s.close()
            return port
        except OSError:
            continue
    return preferred


def _argv_pdf():
    """Return the PDF path passed on launch (double-click / Open With), if any."""
    for arg in sys.argv[1:]:
        if not arg or arg.startswith("-"):
            continue
        if os.path.isfile(arg) and arg.lower().endswith(".pdf"):
            return os.path.abspath(arg)
    return None


def _find_running_instance():
    """Return the port of an already-running editor, or None."""
    for port in CANDIDATE_PORTS:
        try:
            req = urllib.request.urlopen(f"http://{HOST}:{port}/api/ping", timeout=0.4)
            info = json.loads(req.read().decode("utf-8"))
            if info.get("app") == APP_TOKEN:
                return port
        except Exception:
            continue
    return None


def _handoff_file(port):
    return os.path.join(tempfile.gettempdir(), "pypdf-handoff-%d.tok" % port)


def _handoff_to_instance(port, pdf_path):
    """Send a PDF to the running editor so it reuses the same server + tab."""
    secret = ""
    try:
        with open(_handoff_file(port), "r") as fh:
            secret = fh.read().strip()
    except Exception:
        pass
    body = json.dumps({"path": pdf_path}).encode("utf-8")
    req = urllib.request.Request(
        f"http://{HOST}:{port}/api/open_path", data=body,
        headers={"Content-Type": "application/json", "X-PyPDF-Handoff": secret},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=5).read()


def _idle_watchdog(server):
    """Quit the process once every browser tab has been closed for a while, so
    a closed app stops consuming memory/CPU. A heartbeat (any /api/ping) keeps
    it alive; the existing tab poller pings every few seconds while open."""
    start = time.time()
    while True:
        time.sleep(3)
        now = time.time()
        if not HEARTBEAT["seen"]:
            # Browser hasn't connected yet — give it a generous grace period in
            # case it is slow to launch, but don't hang around forever.
            if now - start > STARTUP_GRACE_SEC:
                print("  No browser connected — shutting down.")
                os._exit(0)
            continue
        if now - HEARTBEAT["last"] > IDLE_SHUTDOWN_SEC:
            # Re-check after a short pause to avoid a false positive right after
            # the machine wakes from sleep (timers were frozen).
            time.sleep(2)
            if time.time() - HEARTBEAT["last"] > IDLE_SHUTDOWN_SEC:
                print("  All tabs closed — shutting down to free memory/CPU.")
                os._exit(0)


def main():
    pdf_path = _argv_pdf()

    # ---- single instance: hand the file to an already-running editor --------
    running = _find_running_instance()
    if running is not None:
        if pdf_path:
            try:
                _handoff_to_instance(running, pdf_path)
                print(f"  Sent {os.path.basename(pdf_path)} to the running editor "
                      f"(port {running}).")
            except Exception as e:  # noqa
                print(f"  Could not reach the running editor: {e}")
        # Always surface a browser tab pointed at the running server — even if
        # every window/tab was closed (e.g. Safari was quit). Without this, a
        # double-clicked PDF gets handed off but nothing visible opens.
        webbrowser.open(f"http://{HOST}:{running}")
        return

    # ---- otherwise we become the single server instance ---------------------
    if pdf_path:
        try:
            with open(pdf_path, "rb") as fh:
                STATE.open_bytes(fh.read(), os.path.basename(pdf_path), path=pdf_path)
            print(f"  Loaded: {pdf_path}")
        except Exception as e:  # noqa
            print(f"  Could not open {pdf_path}: {e}")

    port = find_free_port(PORT)
    url = f"http://{HOST}:{port}"
    server = ThreadingHTTPServer((HOST, port), Handler)
    # Write the hand-off secret so a later launch can authorise /api/open_path.
    try:
        hf = _handoff_file(port)
        fd = os.open(hf, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(HANDOFF_SECRET)
        import atexit
        atexit.register(lambda: os.path.exists(hf) and os.remove(hf))
    except Exception:
        pass
    print("=" * 56)
    print("  PyPDF for Mac is running")
    print(f"  PyMuPDF {getattr(fitz, 'VersionBind', '?')}")
    print(f"  Open in your browser:  {url}")
    print("  Press Ctrl+C here to stop.")
    print("=" * 56)
    # Open the browser as soon as the socket is accepting connections.
    threading.Thread(target=lambda: (time.sleep(0.2), webbrowser.open(url)), daemon=True).start()
    # Auto-shutdown when the browser is closed (frees memory/CPU).
    threading.Thread(target=_idle_watchdog, args=(server,), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down. Bye!")
        server.shutdown()


if __name__ == "__main__":
    main()
