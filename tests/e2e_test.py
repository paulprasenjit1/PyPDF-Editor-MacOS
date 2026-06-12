"""End-to-end test: the REAL app (python server + real PyMuPDF) driven by a
REAL Chromium browser. Validates every user-facing feature.

Run:  python3 e2e_test.py            (needs: pip install playwright pymupdf
                                      and:   python3 -m playwright install chromium)
"""
import importlib, threading, sys, os, tempfile, time
import fitz
from http.server import ThreadingHTTPServer
from playwright.sync_api import sync_playwright

mod = importlib.import_module("pdf_editor_app")
port = mod.find_free_port(8123)
server = ThreadingHTTPServer(("127.0.0.1", port), mod.Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{port}"

fails, errors = [], []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else (" — " + str(extra))))
    if not cond: fails.append(name)

# ---- fixtures --------------------------------------------------------------
tmp = tempfile.mkdtemp()
pdf_path = os.path.join(tmp, "sample.pdf")
doc = fitz.open()
for i in range(4):
    pg = doc.new_page()
    pg.insert_text((72, 100), f"Hello page {i+1}", fontsize=18)
doc.save(pdf_path)

png_path = os.path.join(tmp, "photo.png")
pm = fitz.Pixmap(fitz.csRGB, 1200, 900, os.urandom(1200 * 900 * 3), 0)
with open(png_path, "wb") as fh:
    fh.write(pm.tobytes("png"))

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.goto(BASE)
    page.wait_for_timeout(600)

    # ---- welcome screen ----
    check("welcome: two big buttons", page.locator(".welcome .big").count() == 2)
    check("welcome: privacy note", "stays on your Mac" in page.locator(".welcome .note").inner_text())

    # ---- open a PDF ----
    page.set_input_files("#fileInput", pdf_path)
    page.wait_for_selector(".stage img[src]", timeout=8000)
    check("open: 4 pages rendered", page.locator(".stage").count() == 4)
    check("open: header shows name", "sample.pdf" in page.locator("#meta").inner_text())

    # ---- Organize Pages: thumbnails, rotate, apply ----
    page.click("#orgBtn")
    page.wait_for_selector("#thumbs .thumb", timeout=5000)
    check("organize: modal opens with 4 thumbs", page.locator("#thumbs .thumb").count() == 4)
    page.wait_for_function("document.querySelectorAll('#thumbs img[src^=\"blob:\"]').length === 4", timeout=5000)
    check("organize: all thumbnails loaded", True)
    page.locator("#thumbs .thumb .rotbtn").first.click()
    page.wait_for_timeout(200)
    check("organize: rotate badge", "90" in page.locator("#thumbs .badge").first.inner_text())
    page.get_by_role("button", name="Apply changes").click()
    page.wait_for_timeout(800)
    check("organize: applied (modal closed)", not page.locator("#overlay").evaluate("e=>e.classList.contains('show')"))
    check("organize: server has 90° on page 0", mod.STATE.doc[0].rotation == 90, mod.STATE.doc[0].rotation)
    check("organize: doc marked edited", "Edited" in page.locator("#meta").inner_text())

    # ---- Organize: DRAG-reorder must not open the image dialog (regression) --
    page.click("#orgBtn")
    page.wait_for_selector("#thumbs .thumb", timeout=5000)
    thumbs = page.locator("#thumbs .thumb")
    n = thumbs.count()
    # drag the LAST thumbnail onto the FIRST
    thumbs.nth(n - 1).drag_to(thumbs.nth(0))
    page.wait_for_timeout(400)
    check("drag-reorder: no image-quality dialog appears", page.locator("#iqGo").count() == 0)
    check("drag-reorder: organize sheet still open", page.locator("#thumbs .thumb").count() == n)
    first_badge = page.locator("#thumbs .badge").first.inner_text()
    check("drag-reorder: last page moved to slot 1", ("p" + str(n)) in first_badge, first_badge)
    page.get_by_role("button", name="Apply changes").click()
    page.wait_for_timeout(800)
    order_text = mod.STATE.doc[0].get_text().strip()
    check("drag-reorder: server applied new order", f"page {n}" in order_text, order_text)
    # undo back to the original order for the next steps
    page.click("#undoBtn"); page.wait_for_timeout(600)

    # ---- Copy Pages ----
    page.click("#copyBtn")
    page.wait_for_selector(".thumb", timeout=5000)
    page.locator(".thumb").nth(1).click()
    page.wait_for_timeout(200)
    check("copy: selection marked", page.locator(".thumb.picked").count() == 1)
    with page.expect_download() as dl:
        page.get_by_role("button", name="Copy 1 page(s)").click()
    out = dl.value.path()
    copied = fitz.open(out)
    check("copy: downloaded a 1-page PDF", copied.page_count == 1)

    # ---- undo (Cmd/Ctrl+Z works through the API) ----
    page.click("#undoBtn")
    page.wait_for_timeout(600)
    check("undo: rotation reverted", mod.STATE.doc[0].rotation == 0, mod.STATE.doc[0].rotation)

    # ---- unsaved-changes guard on Open ----
    # document is dirty again? undo keeps it dirty -> Open must warn first
    page.locator(".toolbar button", has_text="Open").first.click()
    page.wait_for_timeout(300)
    guard_shown = page.locator("#modal").inner_text()
    check("guard: warns before replacing unsaved doc", "Unsaved changes" in guard_shown, guard_shown[:80])
    page.get_by_role("button", name="Cancel").click()

    # ---- Save dialog with rename ----
    page.click("#saveBtn")
    page.wait_for_selector("#saveName", timeout=4000)
    page.fill("#saveName", "renamed-output")
    with page.expect_download() as dl2:
        page.click("#saveGo")
    name = dl2.value.suggested_filename
    check("save: rename respected", name == "renamed-output.pdf", name)
    page.wait_for_timeout(700)
    check("save: 'Edited' cleared", "Edited" not in page.locator("#meta").inner_text())

    # ---- Create from Images with quality dialog ----
    page.set_input_files("#imgInput", png_path)
    page.wait_for_selector("#iqGo", timeout=4000)
    check("images: quality dialog opens", page.locator('input[name="iq"]').count() == 2)
    page.check('input[name="iq"][value="small"]')
    page.click("#iqGo")
    page.wait_for_timeout(1200)
    check("images: new 1-page doc open", page.locator(".stage").count() == 1)
    check("images: marked unsaved (new in-memory doc)", "Edited" in page.locator("#meta").inner_text())

    # quality dialog Escape leaves things usable (regression for promise leak)
    page.set_input_files("#imgInput", png_path)
    page.wait_for_selector("#iqGo", timeout=4000)
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    page.set_input_files("#imgInput", png_path)
    ok_after_escape = True
    try:
        page.wait_for_selector("#iqGo", timeout=4000)
    except Exception:
        ok_after_escape = False
    check("images: still works after Escape on dialog", ok_after_escape)
    page.keyboard.press("Escape")

    # ---- zoom buttons keep working ----
    z0 = page.locator("#zoomLabel").inner_text()
    page.locator(".toolbar button", has_text="+").first.click()
    page.wait_for_timeout(500)
    check("zoom: label changes", page.locator("#zoomLabel").inner_text() != z0)

    # ---- About dialog ----
    page.locator("header button").click()
    page.wait_for_timeout(400)
    about = page.locator("#modal").inner_text()
    check("about: shows version + engine", mod.APP_VERSION in about and "PyMuPDF" in about, about[:80])
    check("about: privacy note", "nothing is uploaded" in about)
    page.keyboard.press("Escape")

    # ---- Close PDF (continue without saving) ----
    page.click("#closeBtn")
    page.wait_for_timeout(300)
    if "Unsaved changes" in page.locator("#modal").inner_text():
        page.get_by_role("button", name="Continue without saving").click()
    page.wait_for_timeout(500)
    check("close: back to welcome", page.locator(".welcome .big").count() == 2)
    check("close: server state cleared", mod.STATE.doc is None)

    # ---- no JS errors throughout ----
    real_errors = [e for e in errors if "favicon" not in e]
    check("no browser JS errors in entire run", not real_errors, real_errors[:2])

    browser.close()

server.shutdown()
print("\n%d E2E checks failed" % len(fails))
sys.exit(1 if fails else 0)
