import importlib, json, threading, urllib.request, urllib.parse, sys, base64
import fitz

mod = importlib.import_module("pdf_editor_app")

from http.server import ThreadingHTTPServer
port = mod.find_free_port(8123)
server = ThreadingHTTPServer(("127.0.0.1", port), mod.Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
B = f"http://127.0.0.1:{port}"

def get(p):
    with urllib.request.urlopen(B+p) as r:
        ct = r.headers.get("Content-Type","")
        body = r.read()
        return (json.loads(body) if "json" in ct else body), r.headers

def post(p, payload=None):
    req = urllib.request.Request(B+p, data=json.dumps(payload or {}).encode(),
        headers={"Content-Type":"application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())

fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ")+name+(" — "+str(extra) if (extra and not cond) else ""))
    if not cond: fails.append(name)

doc = fitz.open()
for i in range(3):
    pg = doc.new_page()
    pg.insert_text((72,72), f"Hello page {i+1}")
b64 = base64.b64encode(doc.tobytes()).decode()

post("/api/open", {"filename":"test.pdf","data_b64":b64})
st,_ = get("/api/state")
check("open: state ok, 3 pages", st["open"] and st["pages"]==3)
check("open: not dirty", st["dirty"]==False)

post("/api/delete_pages", {"pages":[2]})
st,_ = get("/api/state")
check("after delete: dirty", st["dirty"]==True and st["pages"]==2)

body, hdrs = get("/api/save?name="+urllib.parse.quote("../we ird/ name"))
cd = hdrs.get("Content-Disposition","")
st,_ = get("/api/state")
check("save: dirty cleared", st["dirty"]==False)
check("save: name sanitised + .pdf", "we ird name.pdf" in cd or "we irdname.pdf" in cd, cd)
check("save: returns a valid PDF", body[:4]==b"%PDF")

post("/api/reorder", {"order":[1,0]})
st,_ = get("/api/state")
check("after reorder: dirty", st["dirty"]==True)

j = post("/api/close")
check("close: ok", j.get("ok")==True, j)
st,_ = get("/api/state")
check("close: state empty", st["open"]==False and not st.get("locked"))

j = post("/api/open", {"filename":"bad.pdf","data_b64":base64.b64encode(b"not a pdf at all").decode()})
check("damaged file: friendly message", j["ok"]==False and "damaged" in j.get("error",""), j.get("error"))

j = post("/api/undo")
check("undo empty: message kept", "Nothing to undo" in j.get("error",""), j.get("error"))

enc = fitz.open(); enc.new_page().insert_text((72,72),"secret")
encb = enc.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="pw123", owner_pw="pw123")
j = post("/api/unlock", {"filename":"enc.pdf","data_b64":base64.b64encode(encb).decode(),"password":"pw123"})
st,_ = get("/api/state")
check("unlock: opened + dirty (unsaved password-free copy)", j.get("ok")==True and st["dirty"]==True, j)

body, hdrs = get("/api/save")
st,_ = get("/api/state")
check("save default: dirty cleared", st["dirty"]==False)

# edit_text and sign also mark dirty
spans,_ = get("/api/spans?n=0")
if spans["spans"]:
    post("/api/edit_text", {"page":0,"span_index":spans["spans"][0]["index"],"new_text":"changed"})
    st,_ = get("/api/state")
    check("after text edit: dirty", st["dirty"]==True)
    j = post("/api/undo")
    st,_ = get("/api/state")
    check("after undo: still dirty", st["dirty"]==True)

# ---- Phase 2: rotation + copy pages ----
doc3 = fitz.open()
for i in range(4):
    doc3.new_page().insert_text((72,72), f"P{i+1}")
post("/api/open", {"filename":"rot.pdf","data_b64":base64.b64encode(doc3.tobytes()).decode()})

# rotate page 0 by 90, keep order
j = post("/api/reorder", {"order":[0,1,2,3], "rotations":[90,0,0,0]})
st,_ = get("/api/state")
check("rotate: page 0 now 90°", st.get("rotations",[None])[0]==90, st.get("rotations"))
check("rotate: dirty", st["dirty"]==True)

# combined reorder + rotate in one step, then undo restores both
j = post("/api/reorder", {"order":[3,2,1,0], "rotations":[0,180,0,0]})
st,_ = get("/api/state")
check("reorder+rotate: pos1 has 180° added", st["rotations"][1]==180, st["rotations"])
check("reorder+rotate: pos3 kept its 90°", st["rotations"][3]==90, st["rotations"])
j = post("/api/undo")
st,_ = get("/api/state")
check("undo: rotations restored", st["rotations"]==[90,0,0,0], st["rotations"])

# invalid rotation rejected
j = post("/api/reorder", {"order":[0,1,2,3], "rotations":[45,0,0,0]})
check("rotate: 45° rejected", j["ok"]==False and "90" in j.get("error",""), j.get("error"))

# organize-delete: a subset order removes the missing pages, one undoable step
j = post("/api/reorder", {"order":[0,2], "rotations":[0,0]})
st,_ = get("/api/state")
check("organize delete: subset keeps 2 pages", st["pages"]==2, st.get("pages"))
j = post("/api/undo")
st,_ = get("/api/state")
check("organize delete: undo restores all 4", st["pages"]==4, st.get("pages"))
j = post("/api/reorder", {"order":[]})
check("organize delete: empty order rejected", j["ok"]==False)
j = post("/api/reorder", {"order":[0,0,1,2]})
check("organize delete: duplicates rejected", j["ok"]==False)

# merge now snapshots the replaced document (undo restores doc AND name)
m1 = fitz.open(); m1.new_page()
post("/api/merge", {"files":[{"filename":"m.pdf",
    "data_b64": base64.b64encode(m1.tobytes()).decode()}], "include_current": False})
st,_ = get("/api/state")
check("merge: replaced + undoable", st["pages"]==1 and st["can_undo"]==True,
      (st.get("pages"), st.get("can_undo")))
check("merge: renamed to merged.pdf", st["filename"]=="merged.pdf", st.get("filename"))
post("/api/undo")
st,_ = get("/api/state")
check("merge: undo restores doc and name", st["pages"]==4 and st["filename"]=="rot.pdf",
      (st.get("pages"), st.get("filename")))

# copy pages -> new 2-page PDF, original untouched
body, hdrs = get("/api/copy_pages?pages=1,3")
newdoc = fitz.open(stream=body, filetype="pdf")
check("copy: new PDF has 2 pages", newdoc.page_count==2)
check("copy: filename suffix", "rot_pages.pdf" in hdrs.get("Content-Disposition",""),
      hdrs.get("Content-Disposition"))
st,_ = get("/api/state")
check("copy: original untouched (4 pages, not marked dirty by copy)", st["pages"]==4)

# copy with no/invalid selection rejected
try:
    get("/api/copy_pages?pages=")
    check("copy: empty selection rejected", False)
except Exception:
    check("copy: empty selection rejected", True)

# ---- Phase 3: about endpoint + undo memory cap ----
about,_ = get("/api/about")
check("about: version present", about.get("version")==mod.APP_VERSION, about)
check("about: engine named", "PyMuPDF" in about.get("engine",""), about.get("engine"))

# undo cap by total bytes: shrink the cap, take snapshots, oldest get dropped
orig_cap = mod.UNDO_MAX_BYTES
try:
    snap_size = len(mod.STATE.doc.tobytes())
    mod.UNDO_MAX_BYTES = int(snap_size * 2.5)   # room for ~2 snapshots
    mod.STATE.undo = []
    for k in range(6):
        mod.STATE.snapshot("step %d" % k)
    kept = len(mod.STATE.undo)
    total = sum(len(t[1]) for t in mod.STATE.undo)
    check("undo cap: trimmed to fit byte budget", 1 <= kept <= 2 and total <= mod.UNDO_MAX_BYTES,
          f"kept={kept} total={total} cap={mod.UNDO_MAX_BYTES}")
    check("undo cap: newest snapshot kept", mod.STATE.undo[-1][0]=="step 5",
          mod.STATE.undo[-1][0] if mod.STATE.undo else "empty")
finally:
    mod.UNDO_MAX_BYTES = orig_cap
    mod.STATE.undo = []

# ---- v3.2: create-from-images quality ----
# photo-like noise compresses terribly as PNG, so "small" must win clearly
import os as _os
W, H = 2400, 3200
noise = fitz.Pixmap(fitz.csRGB, W, H, bytes(_os.urandom(W * H * 3)), 0)
imgb64 = base64.b64encode(noise.tobytes("png")).decode()

post("/api/create_from_images", {"files":[{"filename":"big.png","data_b64":imgb64}], "quality":"normal"})
stn,_ = get("/api/state"); kb_normal = stn["size_kb"]
post("/api/create_from_images", {"files":[{"filename":"big.png","data_b64":imgb64}], "quality":"small"})
sts,_ = get("/api/state"); kb_small = sts["size_kb"]
check("images small: much lighter than normal", 0 < kb_small < kb_normal * 0.5,
      f"normal={kb_normal}KB small={kb_small}KB")
check("images small: page size matches normal mode", sts["sizes"]==stn["sizes"],
      f"{stn['sizes']} vs {sts['sizes']}")

# graphics that JPEG would bloat: small mode must not make the file bigger
flat = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, W, H), False)
flat.set_rect(flat.irect, (240, 240, 240))
flatb64 = base64.b64encode(flat.tobytes("png")).decode()
post("/api/create_from_images", {"files":[{"filename":"flat.png","data_b64":flatb64}], "quality":"normal"})
kn = get("/api/state")[0]["size_kb"]
post("/api/create_from_images", {"files":[{"filename":"flat.png","data_b64":flatb64}], "quality":"small"})
ks = get("/api/state")[0]["size_kb"]
check("images small: never bloats graphics", ks <= kn * 1.05, f"normal={kn}KB small={ks}KB")

# ---- v3.1: size cache correctness ----
# re-open a multi-page doc so the delete below is valid
doc4 = fitz.open()
for i in range(3):
    doc4.new_page().insert_text((72,72), f"X{i}")
post("/api/open", {"filename":"cache.pdf","data_b64":base64.b64encode(doc4.tobytes()).decode()})
st1,_ = get("/api/state")
st2,_ = get("/api/state")
check("size cache: stable between calls", st1["size_kb"]==st2["size_kb"]>0)
pre = st1["size_kb"]
post("/api/delete_pages", {"pages":[0]})
st3,_ = get("/api/state")
check("size cache: invalidated by edits", st3["size_kb"]>0 and st3["size_kb"]!=pre,
      f"{pre} -> {st3['size_kb']}")
check("size cache: matches direct serialisation",
      abs(st3["size_kb"] - round(len(mod.STATE.to_bytes())/1024,1)) < 0.05)

server.shutdown()
print("\n%d checks failed" % len(fails))
sys.exit(1 if fails else 0)
