#!/usr/bin/env python3
"""
PyPDF Editor - Local Web App
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
import webbrowser
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
UNDO_LIMIT = 15

# ---------------------------------------------------------------------------
# In-memory document state (single working document) + undo stack
# ---------------------------------------------------------------------------
class State:
    def __init__(self):
        self.doc = None
        self.filename = "document.pdf"
        self.undo = []   # list of (label, pdf_bytes)

    def open_bytes(self, data, filename="document.pdf"):
        self.doc = fitz.open(stream=data, filetype="pdf")
        self.filename = filename or "document.pdf"
        self.undo = []

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
def render_page(page_num, zoom=None, target_w=None):
    doc = STATE.require()
    page = doc[page_num]
    if target_w:
        z = max(0.1, min(8.0, float(target_w) / page.rect.width))
    else:
        z = float(zoom or 1.5)
    pix = page.get_pixmap(matrix=fitz.Matrix(z, z), alpha=False)
    return pix.tobytes("png"), pix.width, pix.height


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


def compress():
    doc = STATE.require()
    STATE.snapshot("compress")
    before = len(STATE.to_bytes(compress=False))
    data = STATE.to_bytes(compress=True)
    STATE.doc = fitz.open(stream=data, filetype="pdf")
    return before, len(data)


def create_from_images(images):
    new = fitz.open()
    for _name, raw in images:
        img = fitz.open(stream=raw, filetype=None)
        rect = img[0].rect
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


def reorder_pages(order):
    doc = STATE.require()
    order = [int(x) for x in order]
    if sorted(order) != list(range(doc.page_count)):
        raise RuntimeError("Page order must list every page exactly once.")
    STATE.snapshot("reorder pages")
    doc.select(order)


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

            if path == "/api/state":
                if STATE.doc is None:
                    return self._send(200, {"ok": True, "open": False})
                return self._send(200, {
                    "ok": True, "open": True,
                    "pages": STATE.doc.page_count,
                    "filename": STATE.filename,
                    "size_kb": round(len(STATE.to_bytes()) / 1024, 1),
                    "sizes": page_sizes(),
                    "can_undo": len(STATE.undo) > 0,
                })

            if path == "/api/page":
                n = int(query.get("n", 0))
                w = query.get("w")
                png, pw, ph = render_page(n, zoom=query.get("zoom"), target_w=w)
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("X-Page-Width", str(pw))
                self.send_header("X-Page-Height", str(ph))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(png)))
                self.end_headers()
                return self.wfile.write(png)

            if path == "/api/spans":
                return self._send(200, {"ok": True, "spans": page_spans(int(query.get("n", 0)))})

            if path in ("/api/export", "/api/save"):
                return self._download(STATE.to_bytes(), STATE.filename, "application/pdf")

            if path == "/api/export_png":
                n = int(query.get("n", 0))
                png, _w, _h = render_page(n, zoom=float(query.get("zoom", 2.0)))
                name = os.path.splitext(STATE.filename)[0] + f"_p{n+1}.png"
                return self._download(png, name, "image/png")

            return self._err("Unknown endpoint: " + path, 404)
        except Exception as e:  # noqa
            return self._err(e, 500)

    def do_POST(self):
        try:
            path = self.path.split("?")[0]

            if path == "/api/open":
                p = self._read_json()
                STATE.open_bytes(base64.b64decode(p["data_b64"]), p.get("filename", "document.pdf"))
                return self._send(200, {"ok": True, "pages": STATE.doc.page_count})

            if path == "/api/edit_text":
                p = self._read_json()
                edit_span(int(p["page"]), int(p["span_index"]), p["new_text"])
                return self._send(200, {"ok": True})

            if path == "/api/sign":
                p = self._read_json()
                sign_page(
                    int(p["page"]), p["rect"], base64.b64decode(p["data_b64"]),
                    remove_white=bool(p.get("remove_white", True)),
                    stretch=bool(p.get("stretch", False)),
                )
                return self._send(200, {"ok": True})

            if path == "/api/undo":
                label = STATE.pop_undo()
                return self._send(200, {"ok": True, "undone": label, "pages": STATE.doc.page_count})

            if path == "/api/merge":
                p = self._read_json()
                files = [(f["filename"], base64.b64decode(f["data_b64"])) for f in p["files"]]
                merge_pdfs(files, bool(p.get("include_current", False)))
                return self._send(200, {"ok": True, "pages": STATE.doc.page_count})

            if path == "/api/compress":
                before, after = compress()
                pct = round(100 * (1 - after / before)) if before else 0
                return self._send(200, {
                    "ok": True,
                    "before_kb": round(before / 1024, 1),
                    "after_kb": round(after / 1024, 1),
                    "saved_pct": pct,
                })

            if path == "/api/create_from_images":
                p = self._read_json()
                imgs = [(f["filename"], base64.b64decode(f["data_b64"])) for f in p["files"]]
                create_from_images(imgs)
                return self._send(200, {"ok": True, "pages": STATE.doc.page_count})

            if path == "/api/delete_pages":
                p = self._read_json()
                delete_pages(p["pages"])
                return self._send(200, {"ok": True, "pages": STATE.doc.page_count})

            if path == "/api/reorder":
                p = self._read_json()
                reorder_pages(p["order"])
                return self._send(200, {"ok": True, "pages": STATE.doc.page_count})

            return self._err("Unknown endpoint: " + path, 404)
        except Exception as e:  # noqa
            return self._err(e, 500)


# ---------------------------------------------------------------------------
# Frontend (single page)
# ---------------------------------------------------------------------------
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PyPDF Editor</title>
<style>
  :root { --bg:#0f1115; --panel:#171a21; --line:#262b36; --txt:#e6e9ef; --muted:#8b93a3;
          --accent:#4f8cff; --accent2:#1f6feb; --ok:#3fb950; --warn:#d29922; }
  * { box-sizing:border-box; }
  body { margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
         background:var(--bg); color:var(--txt); height:100vh; display:flex; flex-direction:column; }
  header { display:flex; align-items:center; gap:12px; padding:10px 16px; background:var(--panel);
           border-bottom:1px solid var(--line); }
  header h1 { font-size:15px; margin:0; font-weight:600; letter-spacing:.3px; }
  header .meta { color:var(--muted); font-size:12px; margin-left:auto; }
  .toolbar { display:flex; flex-wrap:wrap; gap:8px; padding:8px 16px; background:var(--panel);
             border-bottom:1px solid var(--line); align-items:center; }
  .sep { width:1px; height:22px; background:var(--line); }
  button { background:var(--accent2); color:#fff; border:0; border-radius:7px; padding:7px 12px;
           font-size:13px; cursor:pointer; }
  button:hover { background:var(--accent); }
  button.ghost { background:transparent; border:1px solid var(--line); color:var(--txt); }
  button.ghost:hover { border-color:var(--accent); }
  button.on { background:var(--ok); }
  button:disabled { opacity:.4; cursor:not-allowed; }
  .main { flex:1; display:flex; min-height:0; }
  .viewer { flex:1; overflow:auto; padding:24px 20px; display:flex; flex-direction:column;
            align-items:center; gap:22px; scroll-behavior:smooth; }
  .stage { position:relative; box-shadow:0 6px 30px rgba(0,0,0,.5); background:#fff; }
  .stage .plabel { position:absolute; top:-17px; left:0; font-size:11px; color:var(--muted); }
  .stage img { display:block; }
  .stage.signing { cursor:crosshair; }
  .span { position:absolute; cursor:pointer; border:1px solid transparent; border-radius:2px; }
  .span:hover { background:rgba(79,140,255,.18); border-color:var(--accent); }
  .span.sel { background:rgba(63,185,80,.22); border-color:var(--ok); }
  .signing .span { pointer-events:none; }
  .selrect { position:absolute; border:1.5px dashed var(--ok); background:rgba(63,185,80,.15); pointer-events:none; }
  .side { width:320px; background:var(--panel); border-left:1px solid var(--line); overflow:auto; padding:14px; }
  .side h2 { font-size:12px; text-transform:uppercase; letter-spacing:.6px; color:var(--muted); margin:18px 0 8px; }
  .side h2:first-child { margin-top:0; }
  textarea { width:100%; background:var(--bg); color:var(--txt); border:1px solid var(--line);
             border-radius:7px; padding:8px; font-size:13px; min-height:70px; resize:vertical; font-family:inherit; }
  .row { display:flex; gap:8px; align-items:center; margin:8px 0; flex-wrap:wrap; }
  .check { display:flex; align-items:center; gap:6px; font-size:12px; color:var(--txt); margin:6px 0; cursor:pointer; }
  .pill { font-size:11px; color:var(--muted); }
  .status { font-size:12px; padding:8px 16px; border-top:1px solid var(--line); background:var(--panel);
            color:var(--muted); min-height:18px; }
  .status.ok { color:var(--ok); } .status.err { color:#f85149; }
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
                  font-size:11px; padding:1px 6px; border-radius:6px; }
</style>
</head>
<body>
<header>
  <h1>📄 PyPDF Editor</h1>
  <span class="meta" id="meta">No document open</span>
</header>

<div class="toolbar">
  <button onclick="fileInput.click()">Open PDF</button>
  <button class="ghost" onclick="mergeInput.click()">Merge PDFs</button>
  <button class="ghost" onclick="imgInput.click()">Create from Images</button>
  <button class="ghost" id="orgBtn" onclick="openOrganize()" disabled>⊞ Organize Pages</button>
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
  <button class="ghost" id="compressBtn" onclick="compress()" disabled>Compress</button>
  <button class="ghost" id="pngBtn" onclick="exportPng()" disabled>Page → PNG</button>
  <button id="saveBtn" onclick="download('/api/save')" disabled>Save / Download</button>
</div>

<div class="main">
  <div class="viewer" id="viewer">
    <div class="empty" id="emptyMsg">
      Open a PDF to begin, or merge several PDFs into one.<br>
      Scroll with your mouse to move between pages. Click text to edit it.<br>
      Upload a signature image, then draw a box on a page to sign.
    </div>
    <!-- page stages injected here -->
  </div>

  <div class="side">
    <h2>Signature</h2>
    <button class="ghost" onclick="sigInput.click()">Upload signature image</button>
    <img id="sigPreview" alt="signature">
    <label class="check"><input type="checkbox" id="sigKnockout" checked> Remove white background</label>
    <label class="check"><input type="checkbox" id="sigStretch"> Stretch to fill box (may distort)</label>
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
  </div>
</div>

<div class="status" id="status">Ready.</div>

<div class="overlay" id="overlay"><div class="modal" id="modal"></div></div>

<input type="file" id="fileInput" accept="application/pdf" style="display:none">
<input type="file" id="mergeInput" accept="application/pdf" multiple style="display:none">
<input type="file" id="imgInput" accept="image/png,image/jpeg" multiple style="display:none">
<input type="file" id="sigInput" accept="image/png,image/jpeg" style="display:none">
<a id="dl" class="dl"></a>

<script>
let pages = 0, cur = 0, zoomMult = 1.0;
let sizes = [], scaleByPage = {};
let spansByPage = {}, selPage = -1, selIdx = -1;
let sigB64 = null, signMode = false;
let mergeFiles = [], mergeIncludeCurrent = false;   // merge-order modal
let orgOrder = [], thumbW = 150;                    // organize-pages modal

const $ = id => document.getElementById(id);
function setStatus(m, k=""){ const s=$("status"); s.textContent=m; s.className="status "+k; }

async function api(path, opts){
  const r = await fetch(path, opts);
  const ct = r.headers.get("Content-Type")||"";
  if (ct.includes("application/json")){ const j=await r.json(); if(!j.ok) throw new Error(j.error||"error"); return j; }
  return r;
}
function fileToB64(file){return new Promise((res,rej)=>{const fr=new FileReader();
  fr.onload=()=>res(fr.result.split(",")[1]); fr.onerror=rej; fr.readAsDataURL(file);});}

// base display width: all pages render to this many CSS px (× zoom multiplier)
function targetWidth(){
  const avail = $("viewer").clientWidth - 48;
  return Math.round(Math.max(320, Math.min(1100, avail)) * zoomMult);
}

// ---------- open / merge / create / signature upload ----------
$("fileInput").onchange = async e => {
  const f=e.target.files[0]; if(!f) return; setStatus("Opening "+f.name+" ...");
  try{ const j=await api("/api/open",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({filename:f.name,data_b64:await fileToB64(f)})});
    pages=j.pages; cur=0; await rebuild(); setStatus("Opened "+f.name+" ("+pages+" pages).","ok");
  }catch(err){ setStatus(err.message,"err"); } e.target.value="";
};

$("mergeInput").onchange = async e => {
  const files=[...e.target.files]; e.target.value=""; if(!files.length) return;
  setStatus("Reading "+files.length+" file(s) ...");
  mergeFiles=[];
  for(const f of files) mergeFiles.push({name:f.name, b64:await fileToB64(f)});
  mergeIncludeCurrent=false;
  openMergeModal();
  setStatus("Set the merge order, then click Merge.","");
};

$("imgInput").onchange = async e => {
  const files=[...e.target.files]; if(!files.length) return; setStatus("Building PDF from "+files.length+" image(s) ...");
  try{ const payload=[]; for(const f of files) payload.push({filename:f.name,data_b64:await fileToB64(f)});
    const j=await api("/api/create_from_images",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({files:payload})});
    pages=j.pages; cur=0; await rebuild(); setStatus("Created PDF from images ("+pages+" pages).","ok");
  }catch(err){ setStatus(err.message,"err"); } e.target.value="";
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
  $("meta").textContent=st.filename+"  •  "+st.pages+" pages  •  "+st.size_kb+" KB";
  $("docInfo").textContent=st.filename+" — "+st.pages+" pages, "+st.size_kb+" KB";
  $("undoBtn").disabled=!st.can_undo;
}

async function rebuild(){
  const v=$("viewer");
  v.querySelectorAll(".stage").forEach(s=>s.remove());
  spansByPage={}; scaleByPage={};
  if(pages<=0){ $("emptyMsg").style.display="block"; updateButtons(); return; }
  $("emptyMsg").style.display="none";
  await refreshMeta();
  const tw=targetWidth();
  for(let i=0;i<pages;i++){
    const stage=document.createElement("div");
    stage.className="stage"+(signMode?" signing":""); stage.dataset.page=i;
    stage.innerHTML=`<span class="plabel">Page ${i+1}</span><img alt="page ${i+1}">`;
    const img=stage.querySelector("img");
    img.onload=()=>{ scaleByPage[i]= sizes[i] ? img.naturalWidth/sizes[i][0] : 1; drawSpans(stage,i); };
    img.src="/api/page?n="+i+"&w="+tw+"&t="+Date.now();
    attachSign(stage, i);
    v.appendChild(stage);
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
  if(selIdx<0) return; setStatus("Applying edit ...");
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
            remove_white:$("sigKnockout").checked, stretch:$("sigStretch").checked})});
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
function zoomBy(d){ if(pages<=0) return; zoomMult=Math.min(3,Math.max(0.5,+(zoomMult+d).toFixed(2))); rebuild(); }

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

let resizeT=null;
window.addEventListener("resize", ()=>{ if(pages<=0) return; clearTimeout(resizeT); resizeT=setTimeout(rebuild,300); });

function updateButtons(){
  const has=pages>0;
  $("pageLabel").textContent = has ? (cur+1)+" / "+pages : "– / –";
  $("zoomLabel").textContent = Math.round(zoomMult*100)+"%";
  $("prevBtn").disabled = !has || cur<=0;
  $("nextBtn").disabled = !has || cur>=pages-1;
  for(const id of ["compressBtn","pngBtn","saveBtn","delBtn","orgBtn"]) $(id).disabled=!has;
  $("signBtn").disabled = !has || !sigB64;
}

// ---------- modal infrastructure ----------
function closeModal(){ $("overlay").classList.remove("show"); $("modal").innerHTML=""; }
$("overlay").addEventListener("mousedown", e=>{ if(e.target.id==="overlay") closeModal(); });
document.addEventListener("keydown", e=>{ if(e.key==="Escape") closeModal(); });

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
      <span class="nm" title="${f.name}">${f.name}</span>
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
function openOrganize(){
  if(pages<=0) return;
  orgOrder=[...Array(pages).keys()];
  renderOrganize(); $("overlay").classList.add("show");
}
function renderOrganize(){
  const thumbs = orgOrder.map((pageIdx,pos)=>`
    <div class="thumb" draggable="true" data-pos="${pos}" style="width:${thumbW}px">
      <span class="badge">${pos+1}${pageIdx!==pos?` ← p${pageIdx+1}`:""}</span>
      <img src="/api/page?n=${pageIdx}&w=${thumbW}&t=org" width="${thumbW}" alt="page ${pageIdx+1}">
    </div>`).join("");
  $("modal").innerHTML=`
    <div class="mhead">
      <h3>Organize Pages — drag thumbnails to reorder</h3>
      <span style="margin-left:auto"></span>
      <button class="ghost" onclick="thumbZoom(-30)">– thumb</button>
      <button class="ghost" onclick="thumbZoom(30)">+ thumb</button>
    </div>
    <div class="mbody"><div class="thumbs" id="thumbs">${thumbs}</div></div>
    <div class="mfoot">
      <button class="ghost" onclick="closeModal()">Cancel</button>
      <button onclick="applyOrder()">Apply order</button>
    </div>`;
  enableDragReorder("thumbs", orgOrder, renderOrganize);
}
function thumbZoom(d){ thumbW=Math.min(380,Math.max(90,thumbW+d)); renderOrganize(); }
async function applyOrder(){
  setStatus("Reordering pages ...");
  try{ const j=await api("/api/reorder",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({order:orgOrder})});
    pages=j.pages; cur=0; closeModal(); await rebuild();
    setStatus("Pages reordered.","ok");
  }catch(err){ setStatus(err.message,"err"); }
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
  setStatus("Compressing ...");
  try{ const j=await api("/api/compress",{method:"POST"}); await rebuild();
    setStatus(`Compressed: ${j.before_kb} KB → ${j.after_kb} KB  (${j.saved_pct}% smaller).`,"ok");
  }catch(err){ setStatus(err.message,"err"); }
}
function download(path){ const a=$("dl"); a.href=path; a.download=""; a.click(); setStatus("Download started.","ok"); }
function exportPng(){ download("/api/export_png?n="+cur+"&zoom=2.0"); }

updateButtons();
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


def main():
    port = find_free_port(PORT)
    url = f"http://{HOST}:{port}"
    server = ThreadingHTTPServer((HOST, port), Handler)
    print("=" * 56)
    print("  PyPDF Editor is running")
    print(f"  PyMuPDF {getattr(fitz, 'VersionBind', '?')}")
    print(f"  Open in your browser:  {url}")
    print("  Press Ctrl+C here to stop.")
    print("=" * 56)
    threading.Thread(target=lambda: (time.sleep(1), webbrowser.open(url)), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down. Bye!")
        server.shutdown()


if __name__ == "__main__":
    main()
