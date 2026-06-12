"""Deep-scenario E2E: the features the basic suite doesn't reach —
text editing, signature placement, merge, encrypted PDFs, delete pages,
compress, PNG export, keyboard shortcuts, double-click zoom, damaged files,
simulated external file drop, and two-tab sync.

Run: python3 e2e_scenarios.py   (playwright + chromium + pymupdf required)
"""
import importlib, threading, sys, os, tempfile
import fitz
from http.server import ThreadingHTTPServer
from playwright.sync_api import sync_playwright

mod = importlib.import_module("pdf_editor_app")
port = mod.find_free_port(8200)
server = ThreadingHTTPServer(("127.0.0.1", port), mod.Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{port}"

fails, errors = [], []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else (" — " + str(extra)[:150])))
    if not cond: fails.append(name)

tmp = tempfile.mkdtemp()
def make_pdf(name, n, text="Sample text line"):
    p = os.path.join(tmp, name)
    d = fitz.open()
    for i in range(n):
        d.new_page().insert_text((72, 120), f"{text} {i+1}", fontsize=16)
    d.save(p); d.close()
    return p

pdf3 = make_pdf("three.pdf", 3)
pdf2 = make_pdf("two.pdf", 2, "Other doc")
enc_path = os.path.join(tmp, "locked.pdf")
e = fitz.open(); e.new_page().insert_text((72, 120), "Top secret", fontsize=16)
e.save(enc_path, encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="pw123", owner_pw="pw123"); e.close()
bad_path = os.path.join(tmp, "bad.pdf")
open(bad_path, "w").write("this is not a pdf at all")
sig_path = os.path.join(tmp, "sig.png")
sp = fitz.Pixmap(fitz.csRGB, 300, 100, b"\x10\x10\x60" * (300*100), 0)
open(sig_path, "wb").write(sp.tobytes("png"))

with sync_playwright() as p:
    browser = p.chromium.launch()
    ctx = browser.new_context()
    page = ctx.new_page()
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("dialog", lambda d: d.accept("pw123"))   # password prompts
    page.goto(BASE); page.wait_for_timeout(500)

    def dismiss_guard_continue():
        if "Unsaved changes" in (page.locator("#modal").inner_text() or ""):
            page.get_by_role("button", name="Continue without saving").click()
            page.wait_for_timeout(300)

    # ================= text editing =================
    page.set_input_files("#fileInput", pdf3)
    page.wait_for_selector(".stage .span", timeout=8000)
    page.locator(".stage .span").first.click()
    page.wait_for_timeout(200)
    check("edit: span selected, editor enabled", page.locator("#editText").is_enabled())
    page.fill("#editText", "CHANGED BY TEST")
    page.click("#applyBtn")
    page.wait_for_timeout(900)
    check("edit: text replaced in document", "CHANGED BY TEST" in mod.STATE.doc[0].get_text(),
          mod.STATE.doc[0].get_text()[:60])
    check("edit: marked Edited", "Edited" in page.locator("#meta").inner_text())
    check("edit: undo available", not page.locator("#undoBtn").is_disabled())

    # ================= signature placement =================
    page.set_input_files("#sigInput", sig_path)
    page.wait_for_timeout(300)
    check("sign: button enabled after upload", not page.locator("#signBtn").is_disabled())
    page.click("#signBtn")
    box = page.locator(".stage img").first.bounding_box()
    page.mouse.move(box["x"]+60, box["y"]+200)
    page.mouse.down()
    page.mouse.move(box["x"]+220, box["y"]+280, steps=4)
    page.mouse.up()
    page.wait_for_timeout(900)
    check("sign: image embedded on page", len(mod.STATE.doc[0].get_images(full=True)) > 0)
    page.click("#signBtn")   # leave sign mode

    # ================= delete pages (sidebar) =================
    page.locator("#pageDots input").nth(2).check()
    page.click("#delBtn")
    page.wait_for_timeout(700)
    check("delete: page removed (3->2)", mod.STATE.doc.page_count == 2)
    check("delete: viewer updated", page.locator(".stage").count() == 2)

    # ================= compress =================
    page.click("#compressBtn")
    page.wait_for_timeout(1500)
    check("compress: success message", "Compressed" in page.locator("#status").inner_text(),
          page.locator("#status").inner_text()[:80])

    # ================= page -> PNG export =================
    with page.expect_download() as dl:
        page.click("#pngBtn")
    check("png export: file name", dl.value.suggested_filename.endswith("_p1.png"),
          dl.value.suggested_filename)

    # ================= merge =================
    page.set_input_files("#mergeInput", [pdf3, pdf2])
    page.wait_for_selector("#mList li", timeout=5000)
    check("merge: order list shows 2 files", page.locator("#mList li").count() == 2)
    check("merge: include-current offered", page.locator("#mInc").count() == 1)
    page.get_by_role("button", name="Merge 2 file(s)").click()
    page.wait_for_timeout(1000)
    check("merge: combined 3+2=5 pages", mod.STATE.doc.page_count == 5, mod.STATE.doc.page_count)

    # ================= keyboard shortcuts =================
    page.keyboard.press("ArrowRight"); page.wait_for_timeout(400)
    check("keys: arrow next page", page.locator("#pageLabel").inner_text().startswith("2"))
    page.keyboard.press("-"); page.wait_for_timeout(500)
    check("keys: minus zooms out", "75%" in page.locator("#zoomLabel").inner_text())
    page.keyboard.press("Control+s"); page.wait_for_timeout(400)
    check("keys: Ctrl/Cmd+S opens save sheet", page.locator("#saveName").count() == 1)
    page.keyboard.press("Escape"); page.wait_for_timeout(200)
    # v4.0: merge is undoable — Ctrl/Cmd+Z brings the previous document back
    check("merge: undo available after merge", not page.locator("#undoBtn").is_disabled())
    pre_merge_pages = 2
    page.keyboard.press("Control+z")
    page.wait_for_function(f"document.querySelectorAll('.stage').length === {pre_merge_pages}", timeout=6000)
    check("merge: Ctrl/Cmd+Z restores pre-merge document", mod.STATE.doc.page_count == pre_merge_pages)
    # merge again so the following steps keep their 5-page document
    page.set_input_files("#mergeInput", [pdf3, pdf2])
    page.wait_for_selector("#mList li", timeout=5000)
    page.get_by_role("button", name="Merge 2 file(s)").click()
    page.wait_for_function("document.querySelectorAll('.stage').length === 5", timeout=6000)
    # a fresh edit can be undone with the shortcut
    page.locator("#pageDots input").nth(4).check()
    page.click("#delBtn"); page.wait_for_timeout(700)
    check("keys: page deleted (5->4)", mod.STATE.doc.page_count == 4, mod.STATE.doc.page_count)
    page.keyboard.press("Control+z")
    page.wait_for_function("document.querySelectorAll('.stage').length === 5", timeout=6000)
    check("keys: Ctrl/Cmd+Z undoes the delete (back to 5)", mod.STATE.doc.page_count == 5,
          mod.STATE.doc.page_count)

    # ================= double-click zoom =================
    page.keyboard.press("+")   # 75% -> 100%
    page.wait_for_function("document.querySelector('#zoomLabel').textContent==='100%'", timeout=6000)
    vbox = page.locator("#viewer").bounding_box()
    cx, cy = vbox["x"]+vbox["width"]/2, vbox["y"]+vbox["height"]/2
    page.mouse.dblclick(cx, cy)
    page.wait_for_function("document.querySelector('#zoomLabel').textContent==='200%'", timeout=6000)
    check("dblclick: zooms to 200%", True)
    page.mouse.dblclick(cx, cy)
    page.wait_for_function("document.querySelector('#zoomLabel').textContent==='100%'", timeout=6000)
    check("dblclick: toggles back to 100%", True)

    # ================= organize: ✕ delete a page =================
    n_before = mod.STATE.doc.page_count
    page.click("#orgBtn")
    page.wait_for_selector("#thumbs .thumb .delbtn", timeout=5000)
    check("org-delete: ✕ on every thumbnail",
          page.locator("#thumbs .delbtn").count() == n_before)
    page.locator("#thumbs .delbtn").first.click()
    page.wait_for_timeout(300)
    check("org-delete: thumbnail removed from sheet",
          page.locator("#thumbs .thumb").count() == n_before - 1)
    page.get_by_role("button", name="Apply changes").click()
    page.wait_for_function(f"document.querySelectorAll('.stage').length === {n_before-1}", timeout=6000)
    check("org-delete: applied server-side", mod.STATE.doc.page_count == n_before - 1)
    page.keyboard.press("Control+z")
    page.wait_for_function(f"document.querySelectorAll('.stage').length === {n_before}", timeout=6000)
    check("org-delete: undoable", mod.STATE.doc.page_count == n_before)

    # ================= print =================
    with page.expect_popup() as pop:
        page.click("#printBtn")
    pw_page = pop.value
    pw_page.wait_for_function(
        f"document.images.length === {n_before} && [...document.images].every(i=>i.complete)",
        timeout=10000)
    check("print: window contains every page at high res", True)
    pw_page.close()

    # ================= busy overlay exists =================
    check("busy: overlay element present", page.locator("#busy .spin").count() == 1)

    # ================= damaged file via UI =================
    page.locator(".toolbar button", has_text="Open").first.click(); page.wait_for_timeout(250)
    dismiss_guard_continue()
    page.set_input_files("#fileInput", bad_path)
    page.wait_for_timeout(700)
    st = page.locator("#status").inner_text()
    check("damaged: friendly error shown", "damaged" in st, st[:90])

    # ================= encrypted PDF via Open (crash regression) ============
    page.set_input_files("#fileInput", enc_path)
    page.wait_for_timeout(1200)   # dialog handler supplies pw123
    check("encrypted: unlocked and opened via Open", mod.STATE.doc is not None
          and mod.STATE.doc.page_count == 1 and not mod.STATE.locked)
    check("encrypted: content visible", "Top secret" in mod.STATE.doc[0].get_text())
    check("encrypted: marked unsaved (decrypted copy in memory)",
          "Edited" in page.locator("#meta").inner_text())

    # ================= simulated external file drop =================
    with open(pdf2, "rb") as fh:
        import base64 as b64mod
        pdf2_b64 = b64mod.b64encode(fh.read()).decode()
    page.evaluate("""async (b64) => {
        const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
        const f = new File([bytes], "dropped.pdf", { type: "application/pdf" });
        const dt = new DataTransfer(); dt.items.add(f);
        window.dispatchEvent(new DragEvent("drop", { dataTransfer: dt, bubbles: true }));
    }""", pdf2_b64)
    page.wait_for_timeout(500)
    dismiss_guard_continue()
    page.wait_for_timeout(900)
    check("drop: external PDF opened", mod.STATE.filename == "dropped.pdf"
          and mod.STATE.doc.page_count == 2, mod.STATE.filename)

    # ================= two-tab sync =================
    page2 = ctx.new_page()
    page2.goto(BASE); page2.wait_for_timeout(800)
    check("tabs: second tab sees the same document",
          "dropped.pdf" in page2.locator("#meta").inner_text())
    page2.click("#closeBtn"); page2.wait_for_timeout(300)
    if "Unsaved changes" in (page2.locator("#modal").inner_text() or ""):
        page2.get_by_role("button", name="Continue without saving").click()
    page2.wait_for_timeout(2500)   # other tab polls every 1.5s
    check("tabs: close in tab 2 clears tab 1", page.locator(".welcome .big").count() == 2)
    page2.close()

    # ================= Retina sharpness (HiDPI rendering) =================
    ctx2 = browser.new_context(device_scale_factor=2)
    rp = ctx2.new_page()
    rp.goto(BASE); rp.wait_for_timeout(400)
    rp.set_input_files("#fileInput", pdf3)
    rp.wait_for_selector(".stage img[src]", timeout=8000)
    rp.wait_for_function("(()=>{const i=document.querySelector('.stage img');return i&&i.naturalWidth>0;})()",
                         timeout=8000)
    nw = rp.evaluate("document.querySelector('.stage img').naturalWidth")
    cw = rp.evaluate("document.querySelector('.stage img').clientWidth")
    check("retina: page image rendered at ~2x its CSS size", nw >= cw * 1.8, f"natural={nw} css={cw}")
    # spans must still align (positioned in CSS px, not image px)
    rp.wait_for_selector(".stage .span", timeout=8000)
    sp_left = rp.evaluate("parseFloat(document.querySelector('.stage .span').style.left)")
    check("retina: text spans positioned in CSS px (aligned)", 0 < sp_left < cw, sp_left)
    ctx2.close()

    real_errors = [x for x in errors if "favicon" not in x]
    check("no JS errors across all scenarios", not real_errors, real_errors[:2])
    browser.close()

server.shutdown()
print("\n%d scenario checks failed" % len(fails))
sys.exit(1 if fails else 0)
