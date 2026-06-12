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
import time
import urllib.parse
import urllib.request
import webbrowser
from collections import OrderedDict
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
APP_VERSION = "3.4"          # thumbnail-drag fix
SERVER_STARTED = time.strftime("%Y-%m-%d %H:%M")

UNDO_LIMIT = 15
# Undo snapshots are full document copies; cap their TOTAL size too, so a huge
# PDF can't pile up 15 multi-hundred-MB copies in memory.
UNDO_MAX_BYTES = 120 * 1024 * 1024

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

# Idle auto-shutdown: the browser tab sends a heartbeat every few seconds while
# it is open. When every tab has been closed for IDLE_SHUTDOWN_SEC, the server
# quits so it stops using memory/CPU. Opening a PDF again starts a fresh server.
IDLE_SHUTDOWN_SEC = 12
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
        doc = fitz.open(stream=data, filetype="pdf")
        # Password-protected PDFs open but cannot be rendered/edited until
        # authenticated. Try an empty owner/user password first; if that fails,
        # hold the bytes in a "locked" state so the UI can prompt for a password
        # (rather than crashing or silently failing).
        if doc.is_encrypted and not doc.authenticate(""):
            doc.close()
            self.doc = None
            self.locked = True
            self.locked_data = data
            self.filename = filename or "document.pdf"
            self.path = path or ""
            self.undo = []
            self.dirty = False
            self.epoch += 1
            return
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
        self.undo.append((label, self.doc.tobytes()))
        if len(self.undo) > UNDO_LIMIT:
            self.undo.pop(0)
        # Cap by total memory as well as steps (always keep the newest snapshot).
        total = sum(len(d) for _, d in self.undo)
        while len(self.undo) > 1 and total > UNDO_MAX_BYTES:
            _, dropped = self.undo.pop(0)
            total -= len(dropped)

    def pop_undo(self):
        if not self.undo:
            raise RuntimeError("Nothing to undo.")
        label, data = self.undo.pop()
        self.doc = fitz.open(stream=data, filetype="pdf")
        return label

    def to_bytes(self, compress=False):
        if compress:
            return self.doc.tobytes(
                garbage=4, deflate=True, clean=True, deflate_images=True
            )
        return self.doc.tobytes(garbage=3, deflate=True)


STATE = State()


# /api/state is called after every operation AND on every zoom/resize rebuild;
# serialising the whole document each time just to show its size is the single
# hottest wasted cost on large PDFs. Cache it per document version instead.
_SIZE_CACHE = {"epoch": -1, "kb": 0.0}


def doc_size_kb():
    if _SIZE_CACHE["epoch"] != STATE.epoch:
        _SIZE_CACHE["kb"] = round(len(STATE.to_bytes()) / 1024, 1)
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
    page = doc[page_num]
    if target_w:
        z = max(0.1, min(8.0, float(target_w) / page.rect.width))
    else:
        z = float(zoom or 1.5)
    pix = page.get_pixmap(matrix=fitz.Matrix(z, z), alpha=False)
    if fmt == "jpeg":
        # JPEG encodes far faster than PNG and transfers smaller -> quicker open
        return pix.tobytes("jpeg", jpg_quality=82), pix.width, pix.height, "image/jpeg"
    return pix.tobytes("png"), pix.width, pix.height, "image/png"


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


def edit_span(page_num, span_index, new_text):
    doc = STATE.require()
    STATE.snapshot("text edit")
    page = doc[page_num]
    spans = page_spans(page_num)
    if not (0 <= span_index < len(spans)):
        raise RuntimeError("Span no longer exists - re-open the page.")
    sp = spans[span_index]
    page.add_redact_annot(fitz.Rect(sp["bbox"]), fill=(1, 1, 1))
    page.apply_redactions()
    page.insert_text(
        fitz.Point(sp["origin"][0], sp["origin"][1]),
        new_text,
        fontname=pick_font(sp["flags"]),
        fontsize=float(sp["size"]) or 11.0,
        color=int_to_rgb(sp["color"]),
    )


def sign_page(page_num, rect, img_bytes, remove_white=True, stretch=False):
    doc = STATE.require()
    STATE.snapshot("signature")
    page = doc[page_num]
    if remove_white:
        img_bytes = knockout_white(img_bytes)
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
    STATE.doc = new
    STATE.filename = "merged.pdf"
    STATE.undo = []


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
    STATE.snapshot("compress")
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

    # 2) otherwise step through image recompression, gentlest first
    if len(best) > target:
        for thr, tgt, q in steps:
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
            if len(data) < len(best):
                best = data
            if len(data) <= target:
                break                              # gentlest step that meets target

    STATE.doc = fitz.open(stream=best, filetype="pdf")
    return before, len(best), target_kb


def create_from_images(images, quality="normal"):
    """Build a new PDF from images. quality="small" downscales each image to
    ~1600px and re-encodes as JPEG — a much lighter file, same page sizes."""
    new = fitz.open()
    for _name, raw in images:
        img = fitz.open(stream=raw, filetype=None)
        rect = img[0].rect
        if quality == "small":
            scale = min(1.0, 1600.0 / max(rect.width, rect.height))
            pm = img[0].get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            jpg = pm.tobytes("jpeg", jpg_quality=72)
            # only swap in the JPEG when it actually IS smaller (graphics-heavy
            # PNGs can re-encode larger; photos shrink a lot)
            if len(jpg) < len(raw):
                img.close()
                img = fitz.open(stream=jpg, filetype=None)
        pdfbytes = img.convert_to_pdf()
        img.close()
        imgpdf = fitz.open("pdf", pdfbytes)
        page = new.new_page(width=rect.width, height=rect.height)
        page.show_pdf_page(page.rect, imgpdf, 0)
        imgpdf.close()
    if new.page_count == 0:
        raise RuntimeError("No valid images provided.")
    STATE.doc = new
    STATE.filename = "from_images.pdf"
    STATE.undo = []


def delete_pages(pages):
    doc = STATE.require()
    pages = sorted(set(int(p) for p in pages))
    if len(pages) >= doc.page_count:
        raise RuntimeError("Cannot delete every page.")
    STATE.snapshot("delete pages")
    doc.delete_pages(pages)


def reorder_pages(order, rotations=None):
    """Apply a new page order and optional per-position rotations (degrees,
    multiples of 90, aligned with `order`) as one undoable step."""
    doc = STATE.require()
    order = [int(x) for x in order]
    if sorted(order) != list(range(doc.page_count)):
        raise RuntimeError("Page order must list every page exactly once.")
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
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
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
        self.wfile.write(data)

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
                return self._send(200, INDEX_HTML, "text/html; charset=utf-8")

            if path == "/api/ping":
                # Every ping is also a heartbeat: it tells the idle watchdog a
                # browser tab is still open.
                HEARTBEAT["last"] = time.time()
                HEARTBEAT["seen"] = True
                return self._send(200, {"ok": True, "app": APP_TOKEN, "epoch": STATE.epoch})

            if path == "/api/state":
                if STATE.locked:
                    return self._send(200, {
                        "ok": True, "open": False, "locked": True,
                        "filename": STATE.filename, "path": STATE.path,
                        "epoch": STATE.epoch,
                    })
                if STATE.doc is None:
                    return self._send(200, {"ok": True, "open": False, "epoch": STATE.epoch})
                return self._send(200, {
                    "ok": True, "open": True,
                    "epoch": STATE.epoch,
                    "pages": STATE.doc.page_count,
                    "filename": STATE.filename,
                    "path": STATE.path,
                    "size_kb": doc_size_kb(),
                    "sizes": page_sizes(),
                    "can_undo": len(STATE.undo) > 0,
                    "dirty": STATE.dirty,
                    "rotations": [p.rotation for p in STATE.doc],
                })

            if path == "/api/page":
                n = int(query.get("n", 0))
                w = query.get("w")
                if w and not query.get("zoom"):
                    img, pw, ph, mime = render_page_cached(n, w)
                else:
                    img, pw, ph, mime = render_page(n, zoom=query.get("zoom"), target_w=w, fmt="jpeg")
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("X-Page-Width", str(pw))
                self.send_header("X-Page-Height", str(ph))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(img)))
                self.end_headers()
                return self.wfile.write(img)

            if path == "/api/spans":
                return self._send(200, {"ok": True, "spans": page_spans(int(query.get("n", 0)))})

            if path in ("/api/export", "/api/save"):
                name = query.get("name")
                if name:
                    name = safe_filename(urllib.parse.unquote_plus(name), STATE.filename)
                    STATE.filename = name
                else:
                    name = STATE.filename
                data = STATE.to_bytes()
                STATE.dirty = False        # Saved: the on-disk copy is now current
                return self._download(data, name, "application/pdf")

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
                png, _w, _h, _m = render_page(n, zoom=float(query.get("zoom", 2.0)), fmt="png")
                name = os.path.splitext(STATE.filename)[0] + f"_p{n+1}.png"
                return self._download(png, name, "image/png")

            return self._err("Unknown endpoint: " + path, 404)
        except Exception as e:  # noqa
            return self._err(friendly_error(e), 500)

    def do_POST(self):
        try:
            path = self.path.split("?")[0]
            # Any POST mutates the working document; bump epoch so cached page
            # renders are invalidated and other browser tabs notice the change.
            STATE.epoch += 1

            if path == "/api/open_path":
                # Used by single-instance hand-off: load a PDF already on disk.
                p = self._read_json()
                with open(p["path"], "rb") as fh:
                    STATE.open_bytes(fh.read(), os.path.basename(p["path"]), path=os.path.abspath(p["path"]))
                return self._send(200, {"ok": True, "pages": STATE.doc.page_count, "epoch": STATE.epoch})

            if path == "/api/open":
                p = self._read_json()
                STATE.open_bytes(base64.b64decode(p["data_b64"]), p.get("filename", "document.pdf"))
                return self._send(200, {"ok": True, "pages": STATE.doc.page_count})

            if path == "/api/edit_text":
                p = self._read_json()
                edit_span(int(p["page"]), int(p["span_index"]), p["new_text"])
                STATE.dirty = True
                return self._send(200, {"ok": True})

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

            return self._err("Unknown endpoint: " + path, 404)
        except Exception as e:  # noqa
            return self._err(friendly_error(e), 500)


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
  header { display:flex; align-items:center; gap:12px; padding:10px 16px; background:var(--panel);
           border-bottom:1px solid var(--line); }
  header h1 { font-size:15px; margin:0; font-weight:600; letter-spacing:.3px; }
  header .meta { color:var(--muted); font-size:12px; margin-left:auto; }
  .toolbar { display:flex; flex-wrap:nowrap; gap:4px; padding:6px 8px; background:var(--panel);
             border-bottom:1px solid var(--line); align-items:center; overflow-x:auto; }
  .toolbar > * { flex:0 0 auto; white-space:nowrap; }
  .toolbar::-webkit-scrollbar { height:8px; }
  .toolbar::-webkit-scrollbar-thumb { background:var(--line); border-radius:4px; }
  .sep { width:1px; height:20px; background:var(--line); margin:0 1px; }
  select.lvl { background:var(--bg); color:var(--txt); border:1px solid var(--line);
               border-radius:6px; padding:5px 5px; font-size:12px; cursor:pointer; width:68px; }
  select.lvl:disabled { opacity:.4; cursor:not-allowed; }
  button { background:var(--accent2); color:#fff; border:0; border-radius:6px; padding:6px 8px;
           font-size:12px; cursor:pointer; }
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
  .span { position:absolute; cursor:pointer; border:1px solid transparent; border-radius:2px; }
  .span:hover { background:rgba(79,140,255,.18); border-color:var(--accent); }
  .span.sel { background:rgba(63,185,80,.22); border-color:var(--ok); }
  .signing .span { pointer-events:none; }
  .selrect { position:absolute; border:1.5px dashed var(--ok); background:rgba(63,185,80,.15); pointer-events:none; }
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
  .thumb .rotbtn { position:absolute; right:5px; top:5px; z-index:2; padding:2px 8px;
                   font-size:13px; background:rgba(0,0,0,.72); border:0; color:#fff;
                   border-radius:6px; cursor:pointer; }
  .thumb .rotbtn:hover { background:var(--accent2); }
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
  <button class="ghost" onclick="openAbout()" title="About this app" style="padding:4px 9px">ⓘ</button>
</header>

<div class="toolbar">
  <button onclick="guardThen(()=>fileInput.click())" title="Open a PDF">Open</button>
  <button class="ghost" onclick="guardThen(()=>mergeInput.click())">Merge PDFs</button>
  <button class="ghost" onclick="guardThen(()=>imgInput.click())">Create from Images</button>
  <button class="ghost" onclick="guardThen(()=>unlockInput.click())" title="Remove the password from a protected PDF">🔓 Unlock PDF</button>
  <button class="ghost" id="orgBtn" onclick="openOrganize()" disabled>⊞ Organize Pages</button>
  <button class="ghost" id="copyBtn" onclick="openCopyPages()" title="Copy chosen pages into a brand-new PDF" disabled>⧉ Copy Pages</button>
  <span class="sep"></span>
  <button class="ghost" id="prevBtn" onclick="go(-1)" disabled>◀ Prev</button>
  <span class="pill" id="pageLabel">– / –</span>
  <button class="ghost" id="nextBtn" onclick="go(1)" disabled>Next ▶</button>
  <span class="sep"></span>
  <button class="ghost" onclick="zoomBy(-0.25)">–</button>
  <span class="pill" id="zoomLabel">100%</span>
  <button class="ghost" onclick="zoomBy(0.25)">+</button>
  <span class="sep"></span>
  <button class="ghost" id="signBtn" onclick="toggleSign()" disabled>✍ Place Signature</button>
  <button class="ghost" id="undoBtn" onclick="undo()" disabled>↶ Undo</button>
  <select id="compLevel" class="lvl" title="Compression level — High &lt;1MB · Medium &lt;700KB · Low &lt;200KB" disabled>
    <option value="high">High</option>
    <option value="medium" selected>Medium</option>
    <option value="low">Low</option>
  </select>
  <button class="ghost" id="compressBtn" onclick="compress()" disabled>Compress</button>
  <button class="ghost" id="pngBtn" onclick="exportPng()" disabled>Page → PNG</button>
  <button id="saveBtn" onclick="openSaveModal()" title="Save / download the PDF" disabled>Save</button>
  <button class="ghost" id="closeBtn" onclick="closeDoc()" title="Close the open document" disabled>✕ Close</button>
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
        Click text on a page to edit it · pinch or double-click to zoom.</div>
      </div>
    </div>
    <div id="pwrap" class="pwrap"></div>
    <!-- page stages injected here -->
  </div>

  <div class="side">
    <h2>Signature</h2>
    <button class="ghost" onclick="sigInput.click()">Upload signature image</button>
    <img id="sigPreview" alt="signature" draggable="false">
    <!-- kept for the API but hidden: signatures are placed as-is (off by default) -->
    <label class="check" style="display:none"><input type="checkbox" id="sigKnockout"> Remove white background</label>
    <div class="hint">Click “Place Signature”, then drag a box on the page. The signature fills the box. Use “Undo” to remove it if it lands in the wrong place.</div>

    <h2>Edit text</h2>
    <div class="pill" id="editHint">Click a text span on the page.</div>
    <textarea id="editText" placeholder="(select text first)" disabled></textarea>
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

<div class="status" id="status">Ready.</div>

<div class="overlay" id="overlay"><div class="modal" id="modal"></div></div>

<input type="file" id="fileInput" accept="application/pdf" style="display:none">
<input type="file" id="mergeInput" accept="application/pdf" multiple style="display:none">
<input type="file" id="imgInput" accept="image/png,image/jpeg" multiple style="display:none">
<input type="file" id="unlockInput" accept="application/pdf" style="display:none">
<input type="file" id="sigInput" accept="image/png,image/jpeg" style="display:none">
<a id="dl" class="dl"></a>

<script>
let pages = 0, cur = 0, zoomMult = 1.0;
let curEpoch = -1;   // last document version this tab has rendered
let sizes = [], scaleByPage = {};
let spansByPage = {}, selPage = -1, selIdx = -1;
let sigB64 = null, signMode = false;
let rots = [];                                      // per-page rotation (degrees)
let mergeFiles = [], mergeIncludeCurrent = false;   // merge-order modal
let orgOrder = [], thumbW = 150;                    // organize-pages modal

const $ = id => document.getElementById(id);
function setStatus(m, k=""){ const s=$("status"); s.textContent=m; s.className="status "+k; }

// HTML-escape any value interpolated into innerHTML (file names are attacker-
// controlled: a crafted name could otherwise inject markup into dialogs).
function esc(s){ return String(s).replace(/[&<>"']/g,
  c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }

// ---------- unsaved-changes protection ----------
let dirty = false;           // mirrors the server's dirty flag
let curName = "document.pdf";

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
  const isPost = opts && String(opts.method||"").toUpperCase()==="POST";
  if(isPost) _localOps++;
  try{
    const r = await fetch(path, opts);
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

// ---------- open / merge / create / signature upload ----------
async function openPdfFile(f){
  setStatus("Opening "+f.name+" ...");
  try{ const j=await api("/api/open",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({filename:f.name,data_b64:await fileToB64(f)})});
    pages=j.pages; cur=0; await rebuild(); setStatus("Opened "+f.name+" ("+pages+" pages).","ok");
  }catch(err){ setStatus(err.message,"err"); }
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
  try{ const payload=[]; for(const f of files) payload.push({filename:f.name,data_b64:await fileToB64(f)});
    const j=await api("/api/create_from_images",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({files:payload, quality})});
    pages=j.pages; cur=0; await rebuild(); setStatus("Created PDF from images ("+pages+" pages).","ok");
  }catch(err){ setStatus(err.message,"err"); }
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
    pages=j.pages; cur=0; await rebuild();
    setStatus(j.was_encrypted
      ? "Unlocked "+f.name+" ("+pages+" pages). Save to keep the password-free copy."
      : f.name+" was not password-protected; opened as-is ("+pages+" pages).","ok");
  }catch(err){ setStatus(err.message,"err"); }
};

$("sigInput").onchange = async e => {
  const f=e.target.files[0]; if(!f) return;
  sigB64=await fileToB64(f);
  const pv=$("sigPreview"); pv.src="data:image/png;base64,"+sigB64; pv.style.display="block";
  $("signBtn").disabled = pages<=0;
  setStatus("Signature loaded. Click “Place Signature”, then drag a box on the page.","ok");
  e.target.value="";
};

// ---------- build continuous page list ----------
async function refreshMeta(){
  const st=await api("/api/state");
  sizes=st.sizes||[];
  if(typeof st.epoch==="number") curEpoch=st.epoch;   // keep tab-sync in step
  dirty=!!st.dirty; curName=st.filename||"document.pdf";
  rots=st.rotations||[];
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
    img.onload=()=>{ img.style.height="auto"; scaleByPage[i]= sizes[i] ? img.naturalWidth/sizes[i][0] : 1; drawSpans(stage,i); };
    img.dataset.src="/api/page?n="+i+"&w="+tw+"&t="+Date.now();
    attachSign(stage, i);
    w.appendChild(stage);
    pageObserver.observe(stage);
  }
  buildDots(); updateButtons(); clearSel();
}

async function reloadPage(i){
  const stage=$("viewer").querySelector('.stage[data-page="'+i+'"]'); if(!stage) return;
  const img=stage.querySelector("img");
  img.onload=()=>{ scaleByPage[i]= sizes[i] ? img.naturalWidth/sizes[i][0] : 1; drawSpans(stage,i); };
  img.src="/api/page?n="+i+"&w="+targetWidth()+"&t="+Date.now();
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

// ---------- edit text ----------
function selectSpan(page, i){
  selPage=page; selIdx=i; const sp=spansByPage[page][i];
  document.querySelectorAll(".span").forEach(s=>s.classList.remove("sel"));
  const el=document.querySelector('.span[data-page="'+page+'"][data-i="'+i+'"]'); if(el) el.classList.add("sel");
  const t=$("editText"); t.disabled=false; t.value=sp.text; t.focus(); $("applyBtn").disabled=false;
  const bold=sp.flags&16?"Bold ":"", ital=sp.flags&2?"Italic ":"";
  $("fontInfo").textContent=`p${page+1} • ${sp.font||"font"} • ${sp.size.toFixed(1)}pt ${bold}${ital}`.trim();
  $("editHint").textContent="Editing selected text:";
}
function clearSel(){ selPage=selIdx=-1; $("editText").value=""; $("editText").disabled=true;
  $("applyBtn").disabled=true; $("fontInfo").textContent="";
  $("editHint").textContent="Click a text span on the page.";
  document.querySelectorAll(".span").forEach(s=>s.classList.remove("sel")); }
async function applyEdit(){
  if(selIdx<0) return;
  // Text edits assume an upright page (same limit as the iPhone app) — warn first.
  if(rots[selPage]){
    if(!confirm("Page "+(selPage+1)+" is rotated ("+rots[selPage]+"°). Text edits assume an upright page and may land in the wrong place.\n\nApply anyway? (Tip: rotate the page back in Organize Pages first.)")) return;
  }
  setStatus("Applying edit ...");
  try{ await api("/api/edit_text",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({page:selPage,span_index:selIdx,new_text:$("editText").value})});
    await reloadPage(selPage); clearSel(); setStatus("Text updated.","ok");
  }catch(err){ setStatus(err.message,"err"); }
}

// ---------- signature placement (drag a box) ----------
function toggleSign(){ setSign(!signMode); }
function setSign(on){
  signMode=on && !!sigB64;
  $("signBtn").classList.toggle("on",signMode);
  $("signBtn").textContent = signMode ? "✍ Signing… (click to stop)" : "✍ Place Signature";
  document.querySelectorAll(".stage").forEach(s=>s.classList.toggle("signing",signMode));
  setStatus(signMode ? "Sign mode ON — drag a box where the signature should go." : "Sign mode off.", signMode?"ok":"");
}
function attachSign(stage, i){
  let start=null, rectEl=null;
  const img=()=>stage.querySelector("img");
  stage.addEventListener("mousedown", e=>{
    if(!signMode || e.button!==0) return;
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
  window.addEventListener("mouseup", async e=>{
    if(!start||!rectEl) return;
    const b=img().getBoundingClientRect();
    const x=e.clientX-b.left, y=e.clientY-b.top;
    const px0=Math.min(start.x,x), py0=Math.min(start.y,y), pw=Math.abs(x-start.x), ph=Math.abs(y-start.y);
    start=null; const el=rectEl; rectEl=null; el.remove();
    if(pw<8||ph<8){ return; }
    const sc=scaleByPage[i]||1;            // CSS px per PDF point on this page
    const rect=[px0/sc, py0/sc, (px0+pw)/sc, (py0+ph)/sc];
    setStatus("Placing signature on page "+(i+1)+" ...");
    try{ await api("/api/sign",{method:"POST",headers:{"Content-Type":"application/json"},
          body:JSON.stringify({page:i, rect, data_b64:sigB64,
            remove_white:$("sigKnockout").checked})});
      await reloadPage(i); setStatus("Signature added to page "+(i+1)+". Use Undo to remove it.","ok");
    }catch(err){ setStatus(err.message,"err"); }
  });
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
  const stage=$("viewer").querySelector('.stage[data-page="'+i+'"]');
  if(stage) stage.scrollIntoView({behavior:"smooth", block:"start"});
}
function go(d){ cur=Math.min(pages-1,Math.max(0,cur+d)); scrollToPage(cur); updateButtons(); }
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
  $("pageLabel").textContent = has ? (cur+1)+" / "+pages : "– / –";
  $("zoomLabel").textContent = Math.round(zoomMult*100)+"%";
  $("prevBtn").disabled = !has || cur<=0;
  $("nextBtn").disabled = !has || cur>=pages-1;
  for(const id of ["compressBtn","compLevel","pngBtn","saveBtn","delBtn","orgBtn","closeBtn","copyBtn"]) $(id).disabled=!has;
  $("signBtn").disabled = !has || !sigB64;
}

// ---------- modal infrastructure ----------
let _modalDismiss=null;   // lets promise-based dialogs resolve on Escape/backdrop
function closeModal(){
  $("overlay").classList.remove("show"); $("modal").innerHTML="";
  if(modalObserver){ modalObserver.disconnect(); modalObserver=null; }
  if(_modalDismiss){ const f=_modalDismiss; _modalDismiss=null; f(); }
}
$("overlay").addEventListener("mousedown", e=>{ if(e.target.id==="overlay") closeModal(); });
document.addEventListener("keydown", e=>{ if(e.key==="Escape") closeModal(); });

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
  try{
    const payload=mergeFiles.map(f=>({filename:f.name,data_b64:f.b64}));
    const j=await api("/api/merge",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({files:payload, include_current:mergeIncludeCurrent})});
    pages=j.pages; cur=0; closeModal(); await rebuild();
    setStatus("Merged into one PDF ("+pages+" pages).","ok");
  }catch(err){ setStatus(err.message,"err"); }
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
      <img data-url="/api/page?n=${pageIdx}&w=${thumbW}" data-key="${curEpoch}:${pageIdx}:${thumbW}"
        alt="page ${pageIdx+1}" draggable="false"
        style="position:absolute;left:50%;top:50%;width:${imgW}px;background:#fff;
               transform:translate(-50%,-50%) rotate(${deg}deg)">
    </div>`;}).join("");
  $("modal").innerHTML=`
    <div class="mhead">
      <h3>Organize Pages — drag to reorder, ⟳ to rotate</h3>
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
function thumbZoom(d){ thumbW=Math.min(380,Math.max(90,thumbW+d)); renderOrganize(); }
async function applyOrder(){
  setStatus("Updating pages ...");
  try{ const rotations=orgOrder.map(p=>orgRot[p]||0);
    const j=await api("/api/reorder",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({order:orgOrder, rotations})});
    pages=j.pages; cur=0; closeModal(); await rebuild();
    setStatus("Pages updated.","ok");
  }catch(err){ setStatus(err.message,"err"); }
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
      <img data-url="/api/page?n=${i}&w=120" data-key="${curEpoch}:${i}:120"
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
async function compress(){
  const sel=$("compLevel");
  const level = sel ? sel.value : "medium";
  setStatus("Compressing ("+level+") ...");
  try{ const j=await api("/api/compress",{method:"POST",
        headers:{"Content-Type":"application/json"}, body:JSON.stringify({level})});
    await rebuild();
    const note = j.met_target ? "" :
      `  — couldn't get under ${j.target_kb} KB without dropping below readable quality; this is the smallest at this level`;
    setStatus(`Compressed (${j.level}, target <${j.target_kb} KB): ${j.before_kb} KB → ${j.after_kb} KB  (${j.saved_pct}% smaller).`+note,"ok");
  }catch(err){ setStatus(err.message,"err"); }
}
function download(path){ const a=$("dl"); a.href=path; a.download=""; a.click(); setStatus("Download started.","ok"); }
function exportPng(){ download("/api/export_png?n="+cur+"&zoom=2.0"); }

// ---------- Save dialog (rename + download) ----------
// afterSave: optional action to run once the save has started (used by the
// unsaved-changes guard's "Save first" button to resume the original action).
function openSaveModal(afterSave){
  if(pages<=0) return;
  $("modal").innerHTML=`
    <div class="mhead"><h3>Save PDF</h3></div>
    <div class="mbody">
      <div class="hint" style="margin-bottom:8px">File name</div>
      <input id="saveName" type="text" value="${esc(curName)}" spellcheck="false"
        style="width:100%;background:var(--bg);color:var(--txt);border:1px solid var(--line);
               border-radius:7px;padding:9px 10px;font-size:13px">
      <div class="hint" style="margin-top:10px">The PDF is downloaded to your Downloads
        folder under this name. The original file on disk is not modified.</div>
    </div>
    <div class="mfoot">
      <button class="ghost" onclick="closeModal()">Cancel</button>
      <button id="saveGo">Save</button>
    </div>`;
  const go=()=>{
    let name=($("saveName").value||"").trim() || curName;
    if(!name.toLowerCase().endsWith(".pdf")) name+=".pdf";
    closeModal();
    download("/api/save?name="+encodeURIComponent(name));
    curName=name; dirty=false;
    setStatus("Saved “"+name+"” to Downloads.","ok");
    setTimeout(refreshMeta, 600);                 // pick up server-side state
    if(typeof afterSave==="function") setTimeout(afterSave, 250);
  };
  $("saveGo").onclick=go;
  $("overlay").classList.add("show");
  const inp=$("saveName");
  inp.focus(); inp.setSelectionRange(0, Math.max(0,(inp.value.lastIndexOf(".pdf")+4)-4));
  inp.addEventListener("keydown", e=>{ if(e.key==="Enter") go(); });
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
  sizes=[]; rots=[]; spansByPage={}; scaleByPage={};
  clearThumbCache();
  setSign(false); clearSel();
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
        curEpoch=-1; pages=j.pages; cur=0; await rebuild();
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
      pages=st.pages; cur=0; await rebuild();
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
  // epoch bump is ours, not a new document opened from another launch
  if(_polling || _localOps>0 || (Date.now()-_lastLocalOp)<1200) return;
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
        pages=st.pages; cur=0; await rebuild();
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
setInterval(()=>{ fetch("/api/ping",{cache:"no-store"}).catch(()=>{}); }, 3000);
fetch("/api/ping",{cache:"no-store"}).catch(()=>{});   // beat immediately on load
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


def _handoff_to_instance(port, pdf_path):
    """Send a PDF to the running editor so it reuses the same server + tab."""
    body = json.dumps({"path": pdf_path}).encode("utf-8")
    req = urllib.request.Request(
        f"http://{HOST}:{port}/api/open_path", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
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
